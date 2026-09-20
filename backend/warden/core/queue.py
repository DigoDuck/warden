"""The task queue, which is a table.

PostgreSQL rather than Redis or a broker (ADR-002). The volume this project will ever see
fits comfortably, and one store means the queue and the event log commit in the same
transaction: a task cannot be marked running without its events, and vice versa. That
property is worth more here than throughput nobody needs.

Two mechanisms carry the whole design:

- `FOR UPDATE SKIP LOCKED` makes two workers take *different* rows instead of one waiting
  on the other. Without it the queue serialises and a second worker buys nothing.
- A lease with a heartbeat is what tells a dead worker apart from a slow one. A worker that
  dies holds its task only until the lease expires; a worker that is alive keeps extending.
"""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from warden.models import Task

DEFAULT_LEASE_SECONDS = 60

# Claims the oldest runnable task and marks it in one statement, so there is no window
# between choosing a row and owning it.
_CLAIM = text("""
    UPDATE tasks
       SET status = 'RUNNING',
           claimed_by = :worker_id,
           claimed_until = now() + make_interval(secs => :lease_seconds),
           started_at = COALESCE(started_at, now())
     WHERE id = (
           SELECT id FROM tasks
            WHERE status = 'QUEUED'
               OR (status = 'RUNNING' AND claimed_until < now())
            ORDER BY created_at
              FOR UPDATE SKIP LOCKED
            LIMIT 1
     )
    RETURNING id
""")

# Refuses to extend a lease the caller does not hold: a worker that was declared dead and
# whose task was reclaimed must not be able to quietly take it back.
_HEARTBEAT = text("""
    UPDATE tasks
       SET claimed_until = now() + make_interval(secs => :lease_seconds)
     WHERE id = :task_id
       AND claimed_by = :worker_id
       AND status = 'RUNNING'
    RETURNING id
""")


async def enqueue(
    session: AsyncSession,
    *,
    user_id: UUID,
    spec: str,
    idempotency_key: str,
    target_repo: str | None = None,
    budget: dict[str, object] | None = None,
) -> Task:
    """Add a task, or return the one that already exists for this key.

    Idempotency is enforced by the UNIQUE constraint, not by checking first and inserting
    after: between the check and the insert, a concurrent caller could slip in. Here the
    database decides, and `DO NOTHING` turns the loser of that race into a read.
    """
    statement = (
        insert(Task)
        .values(
            user_id=user_id,
            spec=spec,
            idempotency_key=idempotency_key,
            target_repo=target_repo,
            budget=budget or {},
            status="QUEUED",
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Task.id)
    )
    inserted = await session.scalar(statement)
    await session.flush()

    if inserted is None:
        existing = await session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is None:  # pragma: no cover - only if the row vanished mid-flight
            raise RuntimeError(f"idempotency key {idempotency_key!r} conflicted but has no row")
        return existing

    task = await session.get(Task, inserted)
    assert task is not None
    return task


async def claim(
    session: AsyncSession, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> Task | None:
    """Take ownership of one runnable task, or return None if there is nothing to take."""
    claimed_id = await session.scalar(
        _CLAIM, {"worker_id": worker_id, "lease_seconds": lease_seconds}
    )
    await session.flush()
    if claimed_id is None:
        return None
    # expire_all so the ORM does not hand back a stale copy of a row the raw UPDATE changed.
    session.expire_all()
    return await session.get(Task, claimed_id)


async def heartbeat(
    session: AsyncSession,
    task_id: UUID,
    worker_id: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend the lease. False means the task is no longer this worker's to extend."""
    extended = await session.scalar(
        _HEARTBEAT,
        {"task_id": task_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
    )
    await session.flush()
    return extended is not None


async def release(session: AsyncSession, task_id: UUID, worker_id: str) -> bool:
    """Hand a task back to the queue on a graceful shutdown.

    Better than letting the lease run out: the task becomes claimable immediately instead of
    waiting for a timeout that exists to detect crashes, not planned exits.
    """
    result = await session.execute(
        text("""
            UPDATE tasks
               SET status = 'QUEUED', claimed_by = NULL, claimed_until = NULL
             WHERE id = :task_id AND claimed_by = :worker_id AND status = 'RUNNING'
            RETURNING id
        """),
        {"task_id": task_id, "worker_id": worker_id},
    )
    await session.flush()
    return result.scalar() is not None


async def expire_lease_now(session: AsyncSession, task_id: UUID) -> None:
    """Force a lease to look expired. For tests that simulate a worker dying."""
    await session.execute(
        text("UPDATE tasks SET claimed_until = now() - make_interval(secs => 1) WHERE id = :id"),
        {"id": task_id},
    )
    await session.flush()
