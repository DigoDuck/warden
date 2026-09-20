"""Append and verify the hash-chained audit log.

Every row's `hash` covers its own fields plus the previous row's `hash` (`prev_hash`), so
changing anything about a row, including one written long ago, breaks the link the row
after it depends on. That is what "tamper-evident" means here: an alteration becomes
*detectable*, not *impossible*. What actually stops the application role from writing is
the `REVOKE UPDATE/DELETE` in the 0003 migration, not the hash itself. See ADR-007 for the
full list of what a hash chain does and does not protect against.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from warden.models import AuditLog

# The first row in the chain has nothing real to link to. A fixed, obviously-not-a-digest
# value rather than NULL: NULL would need special-casing in every comparison, and sha256
# never produces an all-zero digest in practice, so this can never be confused with a
# genuine hash.
GENESIS_HASH = "0" * 64

# pg_advisory_xact_lock takes one bigint key. Any fixed constant works as long as nothing
# else in this database picks the same one; this is sha256(b"warden.audit_log") truncated
# to 61 bits so it is both fixed and not a suspiciously round number some other feature
# might also reach for.
_LOCK_KEY = 2143467203915984590


@dataclass(frozen=True)
class VerifyResult:
    """What `verify()` found.

    `broken_row_id` and `reason` are set together on failure, and both `None` on success.
    An empty log has nothing to break, so it verifies `ok=True` with `rows_checked=0`.
    """

    ok: bool
    rows_checked: int
    broken_row_id: int | None = None
    reason: str | None = None


def _canonical_json(
    *,
    ts: datetime,
    actor_type: str,
    actor_id: str,
    action: str,
    target_type: str | None,
    target_id: str | None,
    details: dict[str, Any],
) -> str:
    """The exact text that gets hashed, identically on the write side and the verify side.

    Canonical means sorted keys and compact separators, so the same fields always produce
    the same string regardless of dict insertion order, and `ts` turned into a UTC ISO
    string rather than hashed as a driver-specific timestamp object, so a value cannot be
    silently altered by rewriting the raw column and it round-trips through Postgres back
    into the same text `append()` hashed. `astimezone(UTC)` makes that normalisation
    explicit instead of relying on asyncpg already returning UTC for `timestamptz`.
    """
    payload = {
        "ts": ts.astimezone(UTC).isoformat(),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "details": details,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _compute_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def append(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add one row to the chain.

    Concurrency: two sessions appending at the same moment would otherwise both read the
    same last row, both compute a `prev_hash` from it, and both insert a row claiming to
    extend the chain, forking it instead of extending it. `pg_advisory_xact_lock`
    serialises appenders on a fixed key before either one reads the last hash, so the
    second caller always sees the first caller's row.

    Consequence, and it matters: the lock is a *transaction* lock, held until the caller's
    transaction commits or rolls back, not released when this function returns. Call this
    from a short transaction. Calling it from inside a long-running agent-run transaction
    would hold every other appender in the process, including unrelated tasks, blocked for
    as long as that transaction stays open.
    """
    if not actor_type or not actor_id or not action:
        raise ValueError("actor_type, actor_id and action are required")

    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})

    prev_hash = await session.scalar(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
    if prev_hash is None:
        prev_hash = GENESIS_HASH

    ts = datetime.now(UTC)
    resolved_details = details if details is not None else {}
    try:
        canonical = _canonical_json(
            ts=ts,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=resolved_details,
        )
    except TypeError as exc:
        # Trust boundary: `details` can arrive from anywhere up the call chain, including
        # model output. A value json.dumps refuses must fail loudly here, not get past
        # this function and produce a row whose hash nothing can ever re-derive.
        raise ValueError(f"details is not JSON-serialisable: {exc}") from exc

    row = AuditLog(
        ts=ts,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=resolved_details,
        prev_hash=prev_hash,
        hash=_compute_hash(prev_hash, canonical),
    )
    session.add(row)
    await session.flush()
    return row


async def verify(session: AsyncSession) -> VerifyResult:
    """Walk the whole chain in id order and confirm every link and every hash.

    ponytail: loads the full table into memory to walk it. Fine at the size this project
    ever reaches; page by id range before this runs against a log with millions of rows.
    """
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()

    expected_prev = GENESIS_HASH
    for checked, row in enumerate(rows, start=1):
        if row.prev_hash != expected_prev:
            return VerifyResult(
                ok=False,
                rows_checked=checked,
                broken_row_id=row.id,
                reason="prev_hash does not match the previous row's hash",
            )

        canonical = _canonical_json(
            ts=row.ts,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            details=row.details,
        )
        if _compute_hash(row.prev_hash, canonical) != row.hash:
            return VerifyResult(
                ok=False,
                rows_checked=checked,
                broken_row_id=row.id,
                reason="stored hash does not match the recomputed hash",
            )

        expected_prev = row.hash

    return VerifyResult(ok=True, rows_checked=len(rows))
