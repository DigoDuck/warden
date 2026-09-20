"""The audit log, exercised against a real Postgres because its guarantees are Postgres's:
role privileges and the advisory lock are database behaviour, not something a fake proves.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import audit
from warden.audit.log import _LOCK_KEY, GENESIS_HASH, VerifyResult, _canonical_json, _compute_hash
from warden.models import AuditLog


@pytest.fixture
async def clean_committed_audit_log(session: AsyncSession) -> AsyncIterator[None]:
    """TRUNCATE audit_log before and after a test that commits.

    Every other test in this file only flushes and leans on the `session` fixture's
    rollback for isolation, same as the rest of the suite. The advisory-lock test is the
    one exception: it needs two *separate* sessions committing so each can see the other's
    row, and committed rows outlive a rollback. TRUNCATE ... RESTART IDENTITY as superuser
    is how test_queue.py isolates its own committing tests, for the same reason.
    """
    await session.execute(text("TRUNCATE audit_log RESTART IDENTITY"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE audit_log RESTART IDENTITY"))
    await session.commit()


async def _seed(session: AsyncSession, n: int = 1) -> list[AuditLog]:
    return [
        await audit.append(session, actor_type="system", actor_id="seed", action=f"seed.{i}")
        for i in range(n)
    ]


# --- append() / verify() on a clean chain -----------------------------------------------


async def test_empty_log_verifies_ok(session: AsyncSession) -> None:
    assert await audit.verify(session) == VerifyResult(ok=True, rows_checked=0)


async def test_the_first_row_chains_from_genesis(session: AsyncSession) -> None:
    row = (await _seed(session))[0]
    assert row.prev_hash == GENESIS_HASH


async def test_a_chain_of_several_appends_verifies_ok(session: AsyncSession) -> None:
    await _seed(session, n=5)
    result = await audit.verify(session)
    assert result == VerifyResult(ok=True, rows_checked=5)


async def test_each_row_chains_from_the_previous_rows_hash(session: AsyncSession) -> None:
    first, second = await _seed(session, n=2)
    assert second.prev_hash == first.hash
    assert second.hash != first.hash


async def test_append_rejects_a_missing_actor_or_action(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="required"):
        await audit.append(session, actor_type="", actor_id="x", action="x")


async def test_append_rejects_details_that_json_cannot_serialise(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="JSON-serialisable"):
        await audit.append(
            session,
            actor_type="system",
            actor_id="x",
            action="x",
            details={"bad": object()},
        )


async def test_append_rejects_a_float_postgres_cannot_round_trip(session: AsyncSession) -> None:
    """Postgres renders JSONB numbers via `numeric`, without an exponent: `1e+16` comes
    back as the literal integer `10000000000000000`, which would hash to a different
    canonical string than the one `append()` computed. Nested, to prove the walk recurses.
    """
    with pytest.raises(ValueError, match="round-trip"):
        await audit.append(
            session,
            actor_type="system",
            actor_id="x",
            action="x",
            details={"nested": {"n": 1e16}},
        )


async def test_append_rejects_a_non_finite_float(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        await audit.append(
            session,
            actor_type="system",
            actor_id="x",
            action="x",
            details={"n": float("inf")},
        )


async def test_details_with_a_nested_payload_round_trips_and_verifies_ok(
    session: AsyncSession,
) -> None:
    """`_seed()` always appends with the default `{}`, so nothing else in this file
    exercises a non-empty `details` through an actual Postgres round trip. Nest a dict, a
    list and an ordinary (non-scientific-notation) float, force a real SELECT, and confirm
    verify() still finds the row untampered.
    """
    details = {"count": 3, "ok": True, "ratio": 0.5, "tags": ["a", "b"], "meta": {"k": "v"}}
    row = await audit.append(
        session, actor_type="system", actor_id="x", action="x", details=details
    )
    await session.refresh(row)  # force a real SELECT, not the Python object just built

    result = await audit.verify(session)

    assert result == VerifyResult(ok=True, rows_checked=1)
    assert row.details == details


# --- tamper detection ---------------------------------------------------------------------


async def test_tampering_a_row_makes_verify_point_at_that_row(session: AsyncSession) -> None:
    _first, second, _third = await _seed(session, n=3)
    await session.execute(
        text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": second.id}
    )

    result = await audit.verify(session)

    assert result.ok is False
    assert result.broken_row_id == second.id
    assert result.reason is not None and "hash" in result.reason
    # The row before the tampered one is unaffected: verify() checked it fine on the way.
    assert result.rows_checked == 2


async def test_tampering_prev_hash_is_caught(session: AsyncSession) -> None:
    _first, second = await _seed(session, n=2)
    await session.execute(
        text("UPDATE audit_log SET prev_hash = :bad WHERE id = :id"),
        {"bad": "f" * 64, "id": second.id},
    )

    result = await audit.verify(session)

    assert result.ok is False
    assert result.broken_row_id == second.id
    assert result.reason is not None and "prev_hash" in result.reason


async def test_verify_is_not_fooled_by_its_own_sessions_stale_identity_map(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clean_committed_audit_log: None,
) -> None:
    """verify() has to be authoritative about what is in Postgres, not what this session
    already has cached. `session` here both appended and, without `populate_existing=True`
    in verify()'s query, would already hold these rows warm in its identity map: SQLAlchemy
    would hand back those Python objects instead of re-reading the tampered column, and
    verify() would report ok=True against a database that is not ok. The tampering has to
    come from a genuinely separate, committed session/connection, same as a real attacker.
    """
    _first, second, _third = await _seed(session, n=3)
    await session.commit()

    async with session_factory() as attacker:
        await attacker.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": second.id}
        )
        await attacker.commit()

    result = await audit.verify(session)

    assert result.ok is False
    assert result.broken_row_id == second.id


async def test_deleting_a_middle_row_is_caught_at_the_following_row(
    session: AsyncSession,
) -> None:
    _first, second, third = await _seed(session, n=3)
    await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": second.id})

    result = await audit.verify(session)

    assert result.ok is False
    assert result.broken_row_id == third.id


async def test_deleting_the_last_row_is_not_detected(session: AsyncSession) -> None:
    """Documented in ADR-007: tail truncation has nothing after it to break a link with."""
    _first, second = await _seed(session, n=2)
    await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": second.id})

    result = await audit.verify(session)

    assert result == VerifyResult(ok=True, rows_checked=1)


# --- the ts round trip (append() hashes a value verify() must reproduce exactly) ----------


async def test_ts_serialises_byte_identically_after_a_postgres_round_trip(
    session: AsyncSession,
) -> None:
    row = (await _seed(session))[0]
    before = _canonical_json(
        ts=row.ts,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        details=row.details,
    )

    await session.refresh(row)  # force a real SELECT, not the Python object we just built

    after = _canonical_json(
        ts=row.ts,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        details=row.details,
    )

    assert before == after
    assert _compute_hash(row.prev_hash, after) == row.hash


# --- database-enforced immutability (warden_app cannot UPDATE/DELETE/TRUNCATE) ------------


async def test_insert_and_select_work_under_the_app_role(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL ROLE warden_app"))
    row = await audit.append(session, actor_type="system", actor_id="app", action="app.insert")
    fetched = await session.get(AuditLog, row.id)
    assert fetched is not None
    assert fetched.action == "app.insert"


async def test_update_fails_under_the_app_role(session: AsyncSession) -> None:
    row = (await _seed(session))[0]
    await session.execute(text("SET LOCAL ROLE warden_app"))
    with pytest.raises(DBAPIError, match="permission denied"):
        await session.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": row.id}
        )


async def test_delete_fails_under_the_app_role(session: AsyncSession) -> None:
    row = (await _seed(session))[0]
    await session.execute(text("SET LOCAL ROLE warden_app"))
    with pytest.raises(DBAPIError, match="permission denied"):
        await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row.id})


async def test_truncate_fails_under_the_app_role(session: AsyncSession) -> None:
    await _seed(session)
    await session.execute(text("SET LOCAL ROLE warden_app"))
    with pytest.raises(DBAPIError, match="permission denied"):
        await session.execute(text("TRUNCATE audit_log"))


# --- the advisory lock: concurrent appenders must not fork the chain ----------------------


async def test_a_held_lock_blocks_a_second_appender_until_the_holder_commits(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    clean_committed_audit_log: None,
) -> None:
    """Exercises `pg_advisory_xact_lock` directly instead of racing two coroutines and
    hoping they overlap in time. An earlier version of this test ran `append()` from two
    sessions under `asyncio.gather` and asserted the chain came out unforked; with the lock
    call deleted from `append()`, that version still passed 4 times out of 5 locally,
    because forcing two coroutines to race in real wall-clock time against a real
    database is exactly the kind of thing that doesn't reproduce reliably. Holding the
    lock open in one session and asserting the second session's `append()` cannot
    proceed within a timeout is deterministic: it goes red on every run with the lock
    call removed, not most of them.
    """
    holder = session_factory()
    waiter = session_factory()
    try:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                audit.append(waiter, actor_type="system", actor_id="waiter", action="race"),
                timeout=0.5,
            )

        await holder.commit()  # releases the transaction-scoped advisory lock
    finally:
        await holder.close()
        # `waiter`'s query was cancelled mid-flight; close it rather than trust its
        # connection is still in a reusable state, and open a fresh session below.
        await waiter.close()

    async with session_factory() as fresh:
        row = await audit.append(fresh, actor_type="system", actor_id="waiter", action="race")
        await fresh.commit()

    # The holder only ever held the lock, it never called append(): the row above is the
    # only one in the log, and it chains from genesis, not from some phantom predecessor.
    assert row.prev_hash == GENESIS_HASH

    result = await audit.verify(session)
    assert result == VerifyResult(ok=True, rows_checked=1)
