"""Asking a task to stop, and the loop's own check for whether one has.

Race-safe against `queue.claim()` by construction: `request_cancel` is one UPDATE, guarded
by `status`, so there is no window between reading a task's status and acting on it for a
worker's claim to land in. `is_requested` is the other half, a plain read the loop calls
from inside a run to notice a request made from a completely different session (another API
request, another worker's own heartbeat task watching for a cancel to act on).
"""

from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from warden.core import events
from warden.models import Task


class CancelOutcome(StrEnum):
    """What a cancel request did. Never an exception: a task that already finished, or an
    id nobody recognises, is a normal answer for the caller to report, not a failure of the
    operation that asked.
    """

    CANCELLED = "cancelled"  # was QUEUED and unclaimed: finished on the spot.
    MARKED = "marked"  # was RUNNING: the loop will see the marker and stop cooperatively.
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"


# One statement, not "read status, then decide, then write": every SET expression here reads
# `status` as the row stood *before* this UPDATE (Postgres evaluates a statement's SET list
# against the pre-image, never against a column the same statement is in the middle of
# changing), and `claim()`'s own UPDATE takes the same row lock this one does, so the two can
# never both act on a status the other has already changed out from under them. Whichever
# commits first is the one whose branch actually ran; the loser's WHERE clause re-reads the
# new status and simply stops matching, same shape as `queue._CLAIM`/`_HEARTBEAT`.
_REQUEST_CANCEL = text("""
    UPDATE tasks
       SET status = CASE WHEN status = 'QUEUED' THEN 'CANCELLED' ELSE status END,
           finished_at = CASE WHEN status = 'QUEUED' THEN now() ELSE finished_at END,
           cancel_requested_at = CASE
               WHEN status = 'RUNNING' THEN COALESCE(cancel_requested_at, now())
               ELSE cancel_requested_at
           END
     WHERE id = :task_id
       AND status IN ('QUEUED', 'RUNNING')
    RETURNING status
""")


async def request_cancel(session: AsyncSession, task_id: UUID) -> CancelOutcome:
    """Ask for a task to stop. Does not commit; same convention as `queue.claim`/`enqueue`,
    the caller decides the transaction boundary.

    A QUEUED task (never claimed, so nothing is running it) is cancelled immediately. A
    RUNNING one only gets the marker: the loop checks it cooperatively (`_check_stoppable`
    in `core/loop.py`), because there is no other safe way to interrupt a model call or a
    tool already in flight from outside the process running it.
    """
    new_status = await session.scalar(_REQUEST_CANCEL, {"task_id": task_id})
    await session.flush()

    if new_status is None:
        # The guarded UPDATE above only tells us it did not match QUEUED or RUNNING; a plain
        # read is what tells apart "already finished" from "no such task", and this branch
        # is cold enough (a request that lost the race, or a bad id) that the extra round
        # trip costs nothing worth avoiding.
        exists = await session.get(Task, task_id)
        return CancelOutcome.NOT_FOUND if exists is None else CancelOutcome.ALREADY_TERMINAL

    # The raw UPDATE bypassed the ORM identity map. Anything the caller already holds for
    # this id would otherwise keep showing the pre-cancel status and marker.
    session.expire_all()

    outcome = CancelOutcome.CANCELLED if new_status == "CANCELLED" else CancelOutcome.MARKED
    await events.append_event(session, task_id, events.CANCEL_REQUESTED, {"outcome": outcome.value})
    if outcome is CancelOutcome.CANCELLED:
        # No worker is running this task, so nothing else will ever emit `task.finished` for
        # it; this is the only chance the event log gets to say the task ended.
        await events.append_event(
            session,
            task_id,
            events.TASK_FINISHED,
            {
                "status": "CANCELLED",
                "iterations": 0,
                "cost_usd": "0",
                "reason": "cancelled before a worker claimed it",
            },
        )
    return outcome


async def is_requested(session: AsyncSession, task_id: UUID) -> bool:
    """True if a cancel has been requested for this task, read fresh from the database.

    A plain, unlocked read: this only decides whether the loop keeps going, so there is
    nothing here for a lock to protect. Postgres gives every statement its own snapshot
    under READ COMMITTED, even inside an already-open transaction, so a marker another
    session committed a moment ago is visible on the very next call here, not only after
    this transaction ends (ADR-019 leans on the same guarantee for `queue.verify_holder`).
    """
    marker = await session.scalar(select(Task.cancel_requested_at).where(Task.id == task_id))
    return marker is not None
