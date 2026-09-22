"""`request_cancel`'s four outcomes, and that it is race-safe against `queue.claim`.

No Docker and no loop here: what this module owns is the database side of asking for a
cancel, independent of whether anything is actually running the task. The loop's own
cooperative check (`core/loop.py::_check_stoppable`) is covered in `test_loop.py`, and the
real, end-to-end "kill the sandbox" path is the one sandbox test in `test_durability.py`.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.core import cancel, events, queue, worker
from warden.core.events import read_events
from warden.models import Task, User


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    """Same reasoning as test_queue.py: a cancel test commits, so the per-test rollback
    does not clean it up on its own."""
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()


async def _a_user(session: AsyncSession) -> User:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(user)
    await session.flush()
    return user


async def test_a_queued_unclaimed_task_is_cancelled_immediately(session: AsyncSession) -> None:
    user = await _a_user(session)
    task = await queue.enqueue(
        session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id  # captured before request_cancel expires the identity map's copy

    outcome = await cancel.request_cancel(session, task_id)
    await session.commit()

    assert outcome is cancel.CancelOutcome.CANCELLED
    row = await session.get(Task, task_id)
    assert row is not None
    assert row.status == "CANCELLED"
    assert row.finished_at is not None
    # A QUEUED task was never running, so the marker itself is never the right thing to set
    # here: nothing would ever check it. `ck_tasks_cancel_requested_only_after_claim` pins
    # this in the database too.
    assert row.cancel_requested_at is None

    kinds = [event.type for event in await read_events(session, task_id)]
    assert kinds == ["cancel.requested", "task.finished"]


async def test_a_running_task_only_gets_the_marker(session: AsyncSession) -> None:
    user = await _a_user(session)
    task = await queue.enqueue(
        session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    claimed = await queue.claim(session, "worker-a")
    assert claimed is not None
    await session.commit()
    task_id = task.id

    outcome = await cancel.request_cancel(session, task_id)
    await session.commit()

    assert outcome is cancel.CancelOutcome.MARKED
    row = await session.get(Task, task_id)
    assert row is not None
    assert row.status == "RUNNING"  # unchanged: the loop, not this call, ends the task
    assert row.cancel_requested_at is not None
    assert await cancel.is_requested(session, task_id) is True

    kinds = [event.type for event in await read_events(session, task_id)]
    assert kinds == ["cancel.requested"]


async def test_a_second_request_on_a_running_task_keeps_the_first_timestamp(
    session: AsyncSession,
) -> None:
    """Idempotent: a retried cancel request must not keep pushing the marker forward."""
    user = await _a_user(session)
    task = await queue.enqueue(
        session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    await queue.claim(session, "worker-a")
    await session.commit()
    task_id = task.id

    await cancel.request_cancel(session, task_id)
    await session.commit()
    first_row = await session.get(Task, task_id)
    assert first_row is not None and first_row.cancel_requested_at is not None
    first = first_row.cancel_requested_at

    outcome = await cancel.request_cancel(session, task_id)
    await session.commit()

    assert outcome is cancel.CancelOutcome.MARKED
    second_row = await session.get(Task, task_id)
    assert second_row is not None
    assert second_row.cancel_requested_at == first


@pytest.mark.parametrize("terminal_status", ["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"])
async def test_a_terminal_task_is_a_no_op(session: AsyncSession, terminal_status: str) -> None:
    user = await _a_user(session)
    task = Task(
        idempotency_key=str(uuid.uuid4()), user_id=user.id, spec="x", status=terminal_status
    )
    session.add(task)
    await session.flush()
    await session.commit()
    task_id = task.id

    outcome = await cancel.request_cancel(session, task_id)
    await session.commit()

    assert outcome is cancel.CancelOutcome.ALREADY_TERMINAL
    row = await session.get(Task, task_id)
    assert row is not None
    assert row.status == terminal_status
    assert row.cancel_requested_at is None
    assert await read_events(session, task_id) == []


async def test_an_unknown_task_id_is_reported_not_found(session: AsyncSession) -> None:
    outcome = await cancel.request_cancel(session, uuid.uuid4())
    await session.commit()
    assert outcome is cancel.CancelOutcome.NOT_FOUND


async def test_cancel_racing_claim_never_lets_the_worker_win_a_task_that_was_cancelled(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The guarded UPDATE is one statement precisely so this cannot happen: a task ends up
    either cancelled with no claimant, or claimed with the marker set, never claimed with
    no marker and no cancellation on record.
    """
    user = await _a_user(session)
    task = await queue.enqueue(
        session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id

    async def do_cancel() -> cancel.CancelOutcome:
        async with session_factory() as own:
            outcome = await cancel.request_cancel(own, task_id)
            await own.commit()
            return outcome

    async def do_claim() -> bool:
        async with session_factory() as own:
            claimed = await queue.claim(own, "worker-a")
            await own.commit()
            return claimed is not None

    cancel_outcome, claimed = await asyncio.gather(do_cancel(), do_claim())

    async with session_factory() as probe:
        row = await probe.get(Task, task_id)
        assert row is not None
        if cancel_outcome is cancel.CancelOutcome.CANCELLED:
            # Cancel won the race: claim() found nothing runnable.
            assert claimed is False
            assert row.status == "CANCELLED"
            assert row.cancel_requested_at is None
        else:
            # claim() won: the task is RUNNING, and cancel only had the marker left to set.
            assert cancel_outcome is cancel.CancelOutcome.MARKED
            assert claimed is True
            assert row.status == "RUNNING"
            assert row.cancel_requested_at is not None


async def test_a_cancel_landing_while_the_worker_holds_an_uncommitted_event_does_not_collide(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`request_cancel` is the first writer to a RUNNING task's event log that is not the
    worker running it. Replays the exact shape of a checkpoint (ADR-019): the worker has an
    event flushed but not committed, then fences with `verify_holder` before committing,
    and the cancel arrives in between. With `seq` allocated as an unlocked max+1 the two
    pick the same `seq`: the cancel waits on the unique index for the worker's transaction,
    the worker's `FOR UPDATE` waits on the cancel's row lock, and Postgres kills one of them
    as a deadlock. Either loser is a bug: a crashed worker or a failed cancel request.
    """
    user = await _a_user(session)
    task = await queue.enqueue(
        session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    await queue.claim(session, "worker-a")
    await session.commit()
    task_id = task.id

    async def do_cancel() -> cancel.CancelOutcome:
        async with session_factory() as own:
            outcome = await cancel.request_cancel(own, task_id)
            await own.commit()
            return outcome

    async with session_factory() as worker_session:
        await events.append_event(worker_session, task_id, events.ITERATION_STARTED, {"n": 1})
        cancelling = asyncio.create_task(do_cancel())
        # Long enough for the cancel to reach the database and block on whatever it blocks
        # on; the worker's fence then has to get through regardless.
        await asyncio.sleep(0.5)
        assert await queue.verify_holder(worker_session, task_id, "worker-a")
        await worker_session.commit()

    outcome = await asyncio.wait_for(cancelling, timeout=10)
    assert outcome is cancel.CancelOutcome.MARKED
    rows = await read_events(session, task_id)
    assert [(e.seq, e.type) for e in rows] == [(1, "iteration.started"), (2, "cancel.requested")]


class _RecordingSandbox:
    def __init__(self) -> None:
        self.killed = asyncio.Event()

    async def kill_for_cancel(self) -> None:
        self.killed.set()


@pytest.mark.parametrize(
    "transient",
    [
        OSError("connection reset by peer"),
        OperationalError("SELECT", {}, Exception("server closed the connection")),
    ],
    ids=["os-error", "sqlalchemy-operational-error"],
)
async def test_the_cancel_watcher_survives_a_transient_database_error(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    transient: Exception,
) -> None:
    """A watcher that died on one failed poll would stop killing the sandbox for the rest of
    the run, and `run_once`'s `finally` would re-raise the error, skip `sandbox.destroy()`
    and take `run_forever` down with it. One bad poll must cost one poll, nothing more."""
    answers: list[Exception | bool] = [transient, True]

    async def flaky_is_requested(_session: AsyncSession, _task_id: uuid.UUID) -> bool:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(worker, "CANCEL_POLL_SECONDS", 0)
    monkeypatch.setattr(cancel, "is_requested", flaky_is_requested)
    sandbox = _RecordingSandbox()

    await asyncio.wait_for(
        worker._watch_cancel(session_factory, uuid.uuid4(), sandbox),  # type: ignore[arg-type]
        timeout=5,
    )

    assert sandbox.killed.is_set()
