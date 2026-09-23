"""POST /tasks, GET /tasks, GET /tasks/{id}, GET /tasks/{id}/events, GET /tasks/{id}/stream.

No agent logic here (briefing §10): this module validates input, calls
`core.queue.enqueue`, and reads rows back. It never touches a provider, a sandbox or the
policy engine.
"""

import asyncio
import base64
import binascii
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import audit
from warden.api.deps import SessionDep, require_scope
from warden.api.schemas import (
    TaskCreate,
    TaskEventOut,
    TaskEventPage,
    TaskListItemOut,
    TaskListOut,
    TaskOut,
)
from warden.core import cancel, queue
from warden.core import events as core_events
from warden.core.events import ITERATION_STARTED
from warden.identity import Claims
from warden.models import TASK_STATUSES, AuditLog, ModelCall, Task, TaskEvent

router = APIRouter(prefix="/tasks", tags=["tasks"])

# A page any bigger risks turning "read the events" into "read the whole table" for a long
# task; 500 is generous for a UI timeline and still cheap to serialise.
_MAX_EVENTS_PAGE = 500

# Same reasoning for the task list: a page bounded well below "the whole table".
_MAX_TASKS_PAGE = 100


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


def _encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{task_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_raw, id_raw = raw.rsplit("|", 1)
        return datetime.fromisoformat(created_at_raw), uuid.UUID(id_raw)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(422, "invalid cursor") from exc


@router.get("", response_model=TaskListOut)
async def list_tasks(
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:read"))],
    status: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=_MAX_TASKS_PAGE)] = 50,
) -> TaskListOut:
    if status is not None and status not in TASK_STATUSES:
        raise HTTPException(422, f"status must be one of {TASK_STATUSES}")

    conditions = []
    if "admin" not in claims.scopes:
        conditions.append(Task.user_id == _user_id_of(claims))
    if status is not None:
        conditions.append(Task.status == status)
    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        # Row-value comparison: matches the (created_at DESC, id DESC) order below, so
        # "strictly after the cursor" means exactly the rows the previous page did not
        # already return, whatever ties `created_at` alone would have left ambiguous.
        conditions.append(tuple_(Task.created_at, Task.id) < (cursor_created_at, cursor_id))

    rows = (
        await session.scalars(
            select(Task)
            .where(*conditions)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(limit)
        )
    ).all()

    next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if len(rows) == limit else None
    return TaskListOut(
        tasks=[
            TaskListItemOut(
                id=row.id,
                status=row.status,
                spec=row.spec,
                target_repo=row.target_repo,
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
            )
            for row in rows
        ],
        next_cursor=next_cursor,
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

    # The unique index on `idempotency_key` is global, not per user, so enqueue() hands back
    # whoever's task already holds the key. Returning it would leak another user's task to
    # anyone who guesses or reuses their key; 409 says "pick another key" and nothing else.
    if task.user_id != user_id:
        raise HTTPException(409, "idempotency key already used")

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


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:write"))],
) -> TaskOut:
    """Ask a task to stop. Maps `core.cancel.request_cancel`'s outcome enum only, never a
    task status directly: which statuses lead to which outcome is `request_cancel`'s call
    (a task waiting for approval, for one, has no worker to notice a marker), and this route
    must keep working unchanged when that mapping grows.
    """
    user_id = _user_id_of(claims)
    task = await _task_or_404(session, task_id, claims, user_id)
    # Peeked before the race-safe UPDATE below, purely to decide whether *this* request is
    # the one that actually changed something. `request_cancel`'s own COALESCE makes the
    # marker itself idempotent (a second request never moves it), but it always returns
    # MARKED again for a still-running task, so without this peek every poll of an
    # already-marked task would append another audit row. A genuine concurrent double
    # request can still both read the marker as unset and both audit once each; accepted,
    # the same kind of narrow race ADR-021 already tolerates elsewhere, and cheap to
    # tighten later by having `request_cancel` report whether it just transitioned.
    already_marked = task.cancel_requested_at is not None

    outcome = await cancel.request_cancel(session, task_id)

    if outcome is cancel.CancelOutcome.NOT_FOUND:
        # _task_or_404 above already proved this row exists and is visible; reaching this
        # means it vanished between the two statements, which nothing in this codebase
        # does today. Mapped anyway because the outcome enum, not our own prior read, is
        # what this route promises to map.
        raise HTTPException(404, "task not found")
    if outcome is cancel.CancelOutcome.ALREADY_TERMINAL:
        raise HTTPException(409, "task already finished")

    if outcome is cancel.CancelOutcome.CANCELLED or not already_marked:
        await audit.append(
            session,
            actor_type="user",
            actor_id=str(user_id),
            action="task.cancel_requested",
            target_type="task",
            target_id=str(task_id),
        )

    await session.commit()
    response.status_code = 200 if outcome is cancel.CancelOutcome.CANCELLED else 202
    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    return await _to_task_out(session, refreshed)


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


# How often the stream polls the database for new events, and how often it sends a
# comment-line heartbeat when nothing new shows up. Module-level so tests can monkeypatch
# them down instead of waiting on real wall-clock seconds.
_STREAM_POLL_SECONDS = 1.0
_STREAM_HEARTBEAT_SECONDS = 15.0


@router.get("/{task_id}/stream")
async def stream_task_events(
    task_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:read"))],
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Server-Sent Events of `task_events` for one task, newest-appended-first is not a
    thing here: it is always in `seq` order, the same order the event log is written in.

    Visibility and the `after` param are resolved with the request's `SessionDep`, which
    is then closed by hand below. FastAPI (>= 0.118) only exits a yield dependency after the
    response has been fully sent, which for a stream is the life of the browser tab: left
    open, that session would sit "idle in transaction" holding a pool connection and an
    ACCESS SHARE lock on `tasks`. See ADR-019: no transaction may sit open for the life of a
    connection that waits on something external, and a browser holding this one open is
    exactly that.
    """
    user_id = _user_id_of(claims)
    await _task_or_404(session, task_id, claims, user_id)

    # EventSource cannot set a custom Authorization header, which is why the frontend uses
    # fetch + ReadableStream instead and can set Last-Event-ID by hand on a reconnect; ?after
    # covers the very first connection, before there is any last event id to send.
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            start_after = int(last_event_id)
        except ValueError as exc:
            raise HTTPException(422, "Last-Event-ID must be an integer") from exc
    else:
        start_after = after

    # Ends the checks' transaction and returns the connection to the pool now, not when the
    # stream ends. The dependency's own `async with` closes it again later: a no-op.
    await session.close()

    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory

    async def event_source() -> AsyncIterator[str]:
        last = start_after
        last_activity = time.monotonic()
        while True:
            if await request.is_disconnected():
                return

            # A fresh, short-lived session per poll (never the request's own SessionDep):
            # it is opened, used for one SELECT and closed before the sleep below, so no
            # transaction or connection sits idle for the ~1s between polls. See ADR-019.
            async with session_factory() as poll_session:
                rows = (
                    await poll_session.scalars(
                        select(TaskEvent)
                        .where(TaskEvent.task_id == task_id, TaskEvent.seq > last)
                        .order_by(TaskEvent.seq)
                        .limit(_MAX_EVENTS_PAGE)
                    )
                ).all()

            if rows:
                for row in rows:
                    out = TaskEventOut(
                        seq=row.seq, type=row.type, payload=row.payload, created_at=row.created_at
                    )
                    yield f"id: {out.seq}\nevent: {out.type}\ndata: {out.model_dump_json()}\n\n"
                    last = out.seq
                    if row.type == core_events.TASK_FINISHED:
                        # The terminal marker: every path to a terminal status writes this
                        # event (core/loop.py::_finish, core/cancel.py::request_cancel), so
                        # seeing it is exactly "the task reached a terminal status and that
                        # was the last thing it had to say".
                        return
                last_activity = time.monotonic()
                continue

            now = time.monotonic()
            if now - last_activity >= _STREAM_HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_activity = now
            await asyncio.sleep(_STREAM_POLL_SECONDS)

    return StreamingResponse(event_source(), media_type="text/event-stream")
