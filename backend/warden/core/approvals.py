"""Requesting and deciding a human approval (ADR-022).

Two operations, two callers: `request_approval` runs from inside the loop
(`core/loop.py`), the instant a call's policy decision is `REQUIRE_APPROVAL`. `decide_approval`
runs from the API (`api/routes_approvals.py`), whenever a reviewer approves or rejects. Neither
touches a provider or a sandbox; this module only ever talks to the database.
"""

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit
from warden.core import events
from warden.core.events import redact_args
from warden.models import Approval, Task
from warden.policy.engine import Decision
from warden.providers.base import ToolCall


class ApprovalNotFound(LookupError):
    """No approval exists with this id."""


class ApprovalAlreadyDecided(RuntimeError):
    """The guarded UPDATE matched no pending row: someone else decided it first (or it
    expired under it, see `core/cancel.py`)."""


async def request_approval(
    session: AsyncSession, task: Task, call: ToolCall, decision: Decision
) -> Approval:
    """Record the question a human now has to answer.

    Called once, by `core/loop.py::_run_tools`, for the one call in a turn whose decision is
    `REQUIRE_APPROVAL`; pausing the task itself (`WAITING_APPROVAL`, lease released) is the
    loop's job, not this function's, so the caller still owns that checkpoint.
    """
    approval = Approval(
        task_id=task.id,
        tool_call_id=call.id,
        tool=call.name,
        args_safe=redact_args(call.arguments),
        matched_rules=decision.matched_rules,
        reason=decision.reason,
        scopes=decision.scopes,
        status="pending",
    )
    session.add(approval)
    await session.flush()
    return approval


# Guarded the same way `queue._CLAIM`/`_HEARTBEAT` are: one statement, `status = 'pending'`
# in the WHERE, so two concurrent decisions on the same row can never both win. Whichever
# commits first is `RETURNING`ed; the loser's UPDATE matches zero rows.
_DECIDE = text("""
    UPDATE approvals
       SET status = :new_status, decided_at = now(), decided_by = :user_id, note = :note
     WHERE id = :approval_id AND status = 'pending'
    RETURNING task_id, tool_call_id, tool
""")

# The other half of the same atomic step: a decided approval always resumes its task. Guarded
# by status too, not because a race is expected here (the approval's own guard already means
# only one caller ever reaches this line for a given approval), but because "a pending
# approval implies a WAITING_APPROVAL task" is an invariant this function is the only writer
# of, and a guarded UPDATE is how that invariant gets checked instead of assumed.
_RESUME_TASK = text("""
    UPDATE tasks SET status = 'QUEUED' WHERE id = :task_id AND status = 'WAITING_APPROVAL'
    RETURNING id
""")


async def decide_approval(
    session: AsyncSession,
    approval_id: uuid.UUID,
    *,
    approve: bool,
    user_id: uuid.UUID,
    note: str | None,
) -> Approval:
    """Resolve one pending approval and hand its task back to the queue.

    Raises `ApprovalNotFound` for an unknown id, `ApprovalAlreadyDecided` for one no longer
    pending (decided already, or expired by a cancel, see `core/cancel.py`), and `ValueError`
    for a reject with no note. `api/routes_approvals.py` maps each to a status code; this
    function only ever decides, it never answers HTTP.
    """
    if not approve and not (note and note.strip()):
        raise ValueError("rejecting an approval requires a non-empty note")

    new_status = "approved" if approve else "rejected"
    params = {
        "approval_id": approval_id,
        "new_status": new_status,
        "user_id": user_id,
        "note": note,
    }
    row = (await session.execute(_DECIDE, params)).one_or_none()
    if row is None:
        exists = await session.get(Approval, approval_id)
        if exists is None:
            raise ApprovalNotFound(str(approval_id))
        raise ApprovalAlreadyDecided(f"approval {approval_id} is already {exists.status}")

    task_id, tool_call_id, tool = row

    resumed = await session.scalar(_RESUME_TASK, {"task_id": task_id})
    if resumed is None:
        # A pending approval that just got resolved always pairs with a WAITING_APPROVAL
        # task (request_approval and the partial unique index both guarantee at most one
        # open question per task). Not reachable without something else in this codebase
        # breaking that pairing, so this surfaces as the bug it would be rather than as a
        # normal outcome a caller is expected to handle.
        raise RuntimeError(f"task {task_id} was not WAITING_APPROVAL when its approval decided")

    # The raw UPDATEs above bypassed the ORM identity map; anything already held for this
    # task would otherwise keep showing the pre-decision state.
    session.expire_all()

    event_type = events.APPROVAL_GRANTED if approve else events.APPROVAL_REJECTED
    payload: dict[str, Any] = {"approval_id": str(approval_id), "id": tool_call_id}
    if not approve:
        payload["note"] = note
    await events.append_event(session, task_id, event_type, payload)
    await audit.append(
        session,
        actor_type="user",
        actor_id=str(user_id),
        # Same name as the task event, so the audit log and the event log agree on what
        # happened (`approval.granted`, not `approval.approved`).
        action=event_type,
        target_type="approval",
        target_id=str(approval_id),
        details={"task_id": str(task_id), "tool": tool, "note": note},
    )

    # `populate_existing=True`, not a plain `session.get()`: the row already in the identity
    # map (the caller may hold the same `Approval` instance from before this call) is not
    # merely expired, it was mutated by a raw UPDATE this session's ORM layer never saw, so
    # a `get()` that trusted "not expired" would hand back stale, pre-decision attributes
    # instead of re-querying (same reasoning as `audit/log.py::verify`'s own comment on why
    # it needs this option).
    decided = await session.scalar(
        select(Approval).where(Approval.id == approval_id).execution_options(populate_existing=True)
    )
    assert decided is not None
    return decided
