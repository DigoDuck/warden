"""POST /tasks, GET /tasks/{id}, GET /tasks/{id}/events.

No agent logic here (briefing §10): this module validates input, calls
`core.queue.enqueue`, and reads rows back. It never touches a provider, a sandbox or the
policy engine.
"""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit
from warden.api.deps import SessionDep, require_scope
from warden.api.schemas import TaskCreate, TaskEventOut, TaskEventPage, TaskOut
from warden.core import queue
from warden.core.events import ITERATION_STARTED
from warden.identity import Claims
from warden.models import AuditLog, ModelCall, Task, TaskEvent

router = APIRouter(prefix="/tasks", tags=["tasks"])

# A page any bigger risks turning "read the events" into "read the whole table" for a long
# task; 500 is generous for a UI timeline and still cheap to serialise.
_MAX_EVENTS_PAGE = 500


def _user_id_of(claims: Claims) -> uuid.UUID:
    """`identity.issue_user_token` mints subjects as exactly `user:<uuid>`. A subject that
    does not parse means a bug upstream, not a client mistake, so this is a 401 (the token
    is not usable here) rather than a 500 escaping to the caller.
    """
    try:
        return uuid.UUID(claims.sub.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise HTTPException(401, "token subject is not a user id") from exc


def _visible_to(task: Task, claims: Claims, user_id: uuid.UUID) -> bool:
    return task.user_id == user_id or "admin" in claims.scopes


async def _task_or_404(
    session: AsyncSession, task_id: uuid.UUID, claims: Claims, user_id: uuid.UUID
) -> Task:
    task = await session.get(Task, task_id)
    # Someone else's task looks exactly like no task at all: existence is information too,
    # and 403 would hand it out for free (briefing/spec: 404, not 403, unless admin).
    if task is None or not _visible_to(task, claims, user_id):
        raise HTTPException(404, "task not found")
    return task


async def _to_task_out(session: AsyncSession, task: Task) -> TaskOut:
    cost = await session.scalar(
        select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(ModelCall.task_id == task.id)
    )
    iterations = await session.scalar(
        select(func.count())
        .select_from(TaskEvent)
        .where(TaskEvent.task_id == task.id, TaskEvent.type == ITERATION_STARTED)
    )
    return TaskOut(
        id=task.id,
        status=task.status,
        spec=task.spec,
        target_repo=task.target_repo,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        cost_usd=Decimal(cost or 0),
        iterations=iterations or 0,
    )


@router.post("", response_model=TaskOut, status_code=201)
async def submit_task(
    body: TaskCreate,
    response: Response,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:write"))],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> TaskOut:
    user_id = _user_id_of(claims)
    budget = body.budget.model_dump(exclude_none=True) if body.budget else None

    task = await queue.enqueue(
        session,
        user_id=user_id,
        spec=body.spec,
        idempotency_key=idempotency_key,
        target_repo=body.target_repo,
        budget=budget,
    )

    # core.queue.enqueue only ever returns a Task, created or pre-existing, it does not say
    # which. Telling them apart without touching that module: Postgres serialises concurrent
    # inserts on `idempotency_key`'s unique index, so a second request with the same key
    # blocks inside enqueue() above until the first request's whole transaction (this same
    # commit, audit row included) has landed or rolled back. By the time we get here, an
    # audit row for a genuine first submission already exists for every caller except the
    # one that actually created it, so a plain existence check doubles as the idempotency
    # test. See ADR-020.
    already_submitted = await session.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.target_type == "task",
            AuditLog.target_id == str(task.id),
            AuditLog.action == "task.submitted",
        )
        .limit(1)
    )
    if already_submitted is None:
        await audit.append(
            session,
            actor_type="user",
            actor_id=str(user_id),
            action="task.submitted",
            target_type="task",
            target_id=str(task.id),
            details={"idempotency_key": idempotency_key},
        )
    else:
        response.status_code = 200

    await session.commit()
    return await _to_task_out(session, task)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:read"))],
) -> TaskOut:
    user_id = _user_id_of(claims)
    task = await _task_or_404(session, task_id, claims, user_id)
    return await _to_task_out(session, task)


@router.get("/{task_id}/events", response_model=TaskEventPage)
async def get_task_events(
    task_id: uuid.UUID,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:read"))],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=_MAX_EVENTS_PAGE)] = 100,
) -> TaskEventPage:
    user_id = _user_id_of(claims)
    await _task_or_404(session, task_id, claims, user_id)

    rows = (
        await session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.seq > after)
            .order_by(TaskEvent.seq)
            .limit(limit)
        )
    ).all()
    events = [
        TaskEventOut(seq=row.seq, type=row.type, payload=row.payload, created_at=row.created_at)
        for row in rows
    ]
    # Payloads are returned exactly as core/loop.py wrote them. `tool.requested` carries the
    # full, unredacted tool arguments (core/loop.py::_request_tools), unlike `tool_calls
    # .args_safe`. Nothing in this control plane's design puts a secret in a tool argument
    # today (the secret broker never hands one to the model, see briefing §10), so this is
    # not a live leak, but `tasks:read` is the boundary that would matter if that ever
    # changed. See ADR-020.
    next_after = events[-1].seq if len(events) == limit else None
    return TaskEventPage(events=events, next_after=next_after)
