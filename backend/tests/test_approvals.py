"""Migration 0006 and the `approvals` table's own guarantees (constraint before code,
same convention as `test_schema.py`), then `core/approvals.py`'s two operations:
`request_approval` (called from the loop, ADR-022) and `decide_approval` (called from the
API). No loop, no Docker, no HTTP here; those live in test_loop.py, test_resume.py and
test_api_approvals.py.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.core import approvals, cancel, queue
from warden.core.events import read_events
from warden.models import Approval, AuditLog, Task, User

_WIPE = text("TRUNCATE audit_log, task_events, approvals, tasks, users RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    """Every test here commits (decide_approval's guarded UPDATE has to be visible to a
    concurrent reader), same reasoning as test_cancel.py and test_resume.py. `audit_log` has
    no foreign key to `tasks` by design (its own docstring), so it needs wiping explicitly
    too, or `decide_approval`'s audit rows outlive this file and break test_audit.py's
    assumption of a log it alone controls (same shape as test_api.py's `clean_committed_rows`).
    """
    await session.execute(_WIPE)
    await session.commit()
    yield
    # A constraint-violation test leaves the transaction unusable for anything but a
    # rollback; harmless to call unconditionally when the test committed cleanly instead.
    await session.rollback()
    await session.execute(_WIPE)
    await session.commit()


async def _a_user(session: AsyncSession) -> User:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(user)
    await session.flush()
    return user


async def _waiting_task(session: AsyncSession) -> Task:
    """A task already paused for approval: RUNNING claimed by a worker, then parked
    WAITING_APPROVAL with the lease released, the exact state `_pause_for_approval`
    (core/loop.py) leaves behind.
    """
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4()))
    await session.commit()
    claimed = await queue.claim(session, "worker-a")
    assert claimed is not None
    await session.commit()
    claimed.status = "WAITING_APPROVAL"
    claimed.claimed_by = None
    claimed.claimed_until = None
    await session.flush()
    await session.commit()
    return claimed


def _approval(task: Task, *, tool_call_id: str = "call-a", status: str = "pending") -> Approval:
    return Approval(
        task_id=task.id,
        tool_call_id=tool_call_id,
        tool="github.open_pr",
        args_safe={"title": "x"},
        matched_rules=["open-pr-needs-human"],
        reason="opening a pull request is visible outside the control plane",
        scopes=["github:pr:open"],
        status=status,
    )


# --- schema: constraint before code (ADR-022) -----------------------------------------------


async def test_status_check_rejects_an_unknown_state(session: AsyncSession) -> None:
    task = await _waiting_task(session)
    session.add(_approval(task, status="not-a-real-status"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_task_may_have_at_most_one_pending_approval(session: AsyncSession) -> None:
    task = await _waiting_task(session)
    session.add(_approval(task, tool_call_id="call-a"))
    await session.flush()

    session.add(_approval(task, tool_call_id="call-b"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_second_pending_approval_is_fine_once_the_first_is_no_longer_pending(
    session: AsyncSession,
) -> None:
    """The partial index only guards `status = 'pending'`: a decided row does not block the
    next question about the same task."""
    task = await _waiting_task(session)
    session.add(_approval(task, tool_call_id="call-a", status="approved"))
    await session.flush()

    session.add(_approval(task, tool_call_id="call-b"))
    await session.flush()  # does not raise


async def test_the_same_tool_call_id_is_never_asked_twice(session: AsyncSession) -> None:
    task = await _waiting_task(session)
    session.add(_approval(task, tool_call_id="call-a", status="rejected"))
    await session.flush()

    # Same call, same task, even though the first is already decided: replay tells pending
    # calls apart by id, so a second row for the same id is a question replay cannot place.
    session.add(_approval(task, tool_call_id="call-a"))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- core/approvals.py: request_approval -----------------------------------------------------


async def test_request_approval_records_the_redacted_args_and_the_policy_reason(
    session: AsyncSession,
) -> None:
    from warden.policy.engine import Decision, Effect
    from warden.providers.base import ToolCall as ProviderToolCall

    task = await _waiting_task(session)
    call = ProviderToolCall(
        id="call-a", name="github.open_pr", arguments={"title": "x", "github_token": "shh"}
    )
    decision = Decision(
        effect=Effect.REQUIRE_APPROVAL,
        matched_rules=["open-pr-needs-human"],
        reason="opening a pull request is visible outside the control plane",
        scopes=["github:pr:open"],
        policy_hash="test",
    )

    row = await approvals.request_approval(session, task, call, decision)
    await session.commit()

    assert row.status == "pending"
    assert row.tool_call_id == "call-a"
    assert row.args_safe["github_token"] == "[redacted]"
    assert row.matched_rules == ["open-pr-needs-human"]
    assert row.reason == decision.reason


# --- core/approvals.py: decide_approval -------------------------------------------------------


async def test_approving_resumes_the_task_and_grants_the_event(session: AsyncSession) -> None:
    task = await _waiting_task(session)
    row = _approval(task)
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()
    user_id, task_id, approval_id = user.id, task.id, row.id  # before expire_all() runs

    decided = await approvals.decide_approval(
        session, approval_id, approve=True, user_id=user_id, note=None
    )
    await session.commit()

    assert decided.status == "approved"
    assert decided.decided_by == user_id
    refreshed = await session.get(Task, task_id)
    assert refreshed is not None and refreshed.status == "QUEUED"

    kinds = [(e.type, e.payload) for e in await read_events(session, task_id)]
    assert kinds == [("approval.granted", {"approval_id": str(approval_id), "id": "call-a"})]


async def test_rejecting_requires_a_non_empty_note(session: AsyncSession) -> None:
    task = await _waiting_task(session)
    row = _approval(task)
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()

    with pytest.raises(ValueError, match="note"):
        await approvals.decide_approval(session, row.id, approve=False, user_id=user.id, note="")

    # Nothing moved: still pending, task still waiting.
    unchanged = await session.get(Approval, row.id)
    assert unchanged is not None and unchanged.status == "pending"


async def test_rejecting_with_a_note_injects_it_into_the_event_and_leaves_the_task_queued(
    session: AsyncSession,
) -> None:
    task = await _waiting_task(session)
    row = _approval(task)
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()
    user_id, task_id = user.id, task.id  # before expire_all() runs

    decided = await approvals.decide_approval(
        session, row.id, approve=False, user_id=user_id, note="too risky right now"
    )
    await session.commit()

    assert decided.status == "rejected"
    refreshed = await session.get(Task, task_id)
    assert refreshed is not None and refreshed.status == "QUEUED"
    event = next(e for e in await read_events(session, task_id) if e.type == "approval.rejected")
    assert event.payload["note"] == "too risky right now"
    assert event.payload["id"] == "call-a"


async def test_an_unknown_approval_id_is_reported_as_not_found(session: AsyncSession) -> None:
    user = await _a_user(session)
    await session.commit()
    with pytest.raises(approvals.ApprovalNotFound):
        await approvals.decide_approval(
            session, uuid.uuid4(), approve=True, user_id=user.id, note=None
        )


async def test_deciding_an_already_decided_approval_is_reported_as_such(
    session: AsyncSession,
) -> None:
    task = await _waiting_task(session)
    row = _approval(task, status="approved")
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()

    with pytest.raises(approvals.ApprovalAlreadyDecided):
        await approvals.decide_approval(session, row.id, approve=True, user_id=user.id, note=None)


async def test_concurrent_approve_and_reject_only_one_wins(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The guarded `UPDATE ... WHERE status = 'pending'` (core/approvals.py) is what makes
    this race have exactly one winner instead of two competing writers each thinking they
    decided it."""
    task = await _waiting_task(session)
    row = _approval(task)
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()
    approval_id = row.id
    task_id = task.id

    async def do_approve() -> str | None:
        async with session_factory() as own:
            try:
                decided = await approvals.decide_approval(
                    own, approval_id, approve=True, user_id=user.id, note=None
                )
                await own.commit()
                return decided.status
            except approvals.ApprovalAlreadyDecided:
                return None

    async def do_reject() -> str | None:
        async with session_factory() as own:
            try:
                decided = await approvals.decide_approval(
                    own, approval_id, approve=False, user_id=user.id, note="no"
                )
                await own.commit()
                return decided.status
            except approvals.ApprovalAlreadyDecided:
                return None

    results = await asyncio.gather(do_approve(), do_reject())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1

    # A fresh session, not `session`: `session` still holds the `Approval`/`Task` Python
    # objects it created before the race, and a plain `get()` on an unexpired identity-map
    # hit never re-queries, so it would report the pre-race state no matter which session
    # actually won.
    async with session_factory() as probe:
        final = await probe.get(Approval, approval_id)
        assert final is not None and final.status == winners[0]
        refreshed = await probe.get(Task, task_id)
        assert refreshed is not None and refreshed.status == "QUEUED"


@pytest.mark.parametrize(
    ("approve", "note", "action"),
    [(True, None, "approval.granted"), (False, "too risky right now", "approval.rejected")],
    ids=["approve", "reject"],
)
async def test_a_decision_writes_an_audit_entry_with_the_user_as_actor(
    session: AsyncSession, approve: bool, note: str | None, action: str
) -> None:
    """Week 3 audit list and ADR-022: the decision is on the tamper-evident log under the
    same name as its task event (`approval.granted`/`approval.rejected`), attributed to the
    human who made it, not to the system."""
    task = await _waiting_task(session)
    row = _approval(task)
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()
    user_id, task_id, approval_id = user.id, task.id, row.id

    await approvals.decide_approval(
        session, approval_id, approve=approve, user_id=user_id, note=note
    )
    await session.commit()

    entry = (
        await session.scalars(select(AuditLog).where(AuditLog.target_id == str(approval_id)))
    ).one()
    assert entry.action == action
    assert (entry.actor_type, entry.actor_id) == ("user", str(user_id))
    assert entry.target_type == "approval"
    assert entry.details["task_id"] == str(task_id)
    assert entry.details["note"] == note


async def _until_lock_waiters(session_factory: async_sessionmaker[AsyncSession], n: int) -> None:
    """Wait until `n` backends in this test database are blocked on a lock, so the test can
    order who queues first instead of hoping the scheduler does."""
    for _ in range(200):
        async with session_factory() as probe:
            waiting = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting is not None and waiting >= n:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never saw {n} backend(s) waiting on a lock")


async def test_a_cancel_racing_a_decision_on_the_same_task_never_deadlocks(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`request_cancel` locks the task row, then the approval row (expiring it). A decision
    has to take the same two locks in the same order, or a cancel and an approve landing on
    the same paused task at the same moment each hold one lock and wait for the other, and
    Postgres kills one of the two requests as a deadlock (a 500 for whoever clicked).

    The interleaving is forced, not raced: a third session holds the task row, the cancel
    queues on it first, then the decision; releasing the row lets them run in that order.
    """
    task = await _waiting_task(session)
    row = _approval(task)
    session.add(row)
    await session.flush()
    await session.commit()
    user = await _a_user(session)
    await session.commit()
    user_id, task_id, approval_id = user.id, task.id, row.id

    async def do_cancel() -> cancel.CancelOutcome:
        async with session_factory() as own:
            outcome = await cancel.request_cancel(own, task_id)
            await own.commit()
            return outcome

    async def do_approve() -> str:
        async with session_factory() as own:
            try:
                await approvals.decide_approval(
                    own, approval_id, approve=True, user_id=user_id, note=None
                )
            except approvals.ApprovalAlreadyDecided:
                return "already decided"
            await own.commit()
            return "approved"

    async with session_factory() as blocker:
        await blocker.execute(
            text("SELECT 1 FROM tasks WHERE id = :id FOR NO KEY UPDATE"), {"id": task_id}
        )
        cancelling = asyncio.create_task(do_cancel())
        await _until_lock_waiters(session_factory, 1)
        approving = asyncio.create_task(do_approve())
        # The decision's first statement may not block at all (that is the bug: it takes
        # the approval row without the task row); give it the time to run either way.
        await asyncio.sleep(0.3)
        await blocker.commit()

    outcome, decided = await asyncio.wait_for(asyncio.gather(cancelling, approving), timeout=15)

    # The cancel queued first, so it wins: the task is cancelled, the question expired, and
    # the decision finds nothing left to decide (a 409 at the API, not a 500).
    assert outcome is cancel.CancelOutcome.CANCELLED
    assert decided == "already decided"
    async with session_factory() as probe:
        final = await probe.get(Approval, approval_id)
        assert final is not None and final.status == "expired"
        refreshed = await probe.get(Task, task_id)
        assert refreshed is not None and refreshed.status == "CANCELLED"
