"""The queue, exercised against a real Postgres because that is where its guarantees live.

`SKIP LOCKED` and lease expiry are database behaviour. Testing them against a fake would
test the fake.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.core import queue
from warden.models import Task, User


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    """Start and finish each test with an empty queue.

    The rest of the suite relies on the per-test rollback for isolation, but a queue test
    has to commit: `claim` must see rows written by another session, and `created_at`
    defaults to the transaction timestamp, so two tasks in one transaction are the same age.
    Committed rows outlive a rollback, so they are cleared explicitly instead of leaking
    into whichever test claims next.
    """
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


async def test_enqueue_creates_a_queued_task(session: AsyncSession) -> None:
    user = await _a_user(session)
    task = await queue.enqueue(
        session, user_id=user.id, spec="do the thing", idempotency_key=str(uuid.uuid4())
    )
    assert task.status == "QUEUED"
    assert task.claimed_by is None


async def test_the_same_idempotency_key_returns_the_same_task(session: AsyncSession) -> None:
    """Two POST /tasks with one key must not become two agent runs."""
    user = await _a_user(session)
    key = str(uuid.uuid4())

    first = await queue.enqueue(session, user_id=user.id, spec="once", idempotency_key=key)
    second = await queue.enqueue(session, user_id=user.id, spec="once again", idempotency_key=key)

    assert first.id == second.id
    count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.idempotency_key == key)
    )
    assert count == 1


async def test_the_second_call_does_not_overwrite_the_first_spec(session: AsyncSession) -> None:
    """Idempotency means "already done", not "do it again with new arguments"."""
    user = await _a_user(session)
    key = str(uuid.uuid4())
    await queue.enqueue(session, user_id=user.id, spec="original", idempotency_key=key)
    again = await queue.enqueue(session, user_id=user.id, spec="different", idempotency_key=key)
    assert again.spec == "original"


async def test_claim_marks_the_task_and_records_the_holder(session: AsyncSession) -> None:
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4()))

    claimed = await queue.claim(session, "worker-a")

    assert claimed is not None
    assert claimed.status == "RUNNING"
    assert claimed.claimed_by == "worker-a"
    assert claimed.claimed_until is not None
    assert claimed.started_at is not None


async def test_claim_on_an_empty_queue_returns_none(session: AsyncSession) -> None:
    """None rather than blocking: the worker decides how long to wait, not the queue."""
    assert await queue.claim(session, "worker-a") is None


async def test_a_claimed_task_is_not_claimed_again(session: AsyncSession) -> None:
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4()))

    assert await queue.claim(session, "worker-a") is not None
    assert await queue.claim(session, "worker-b") is None


async def test_tasks_are_claimed_oldest_first(session: AsyncSession) -> None:
    user = await _a_user(session)
    first = await queue.enqueue(
        session, user_id=user.id, spec="first", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    second = await queue.enqueue(
        session, user_id=user.id, spec="second", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()

    assert (await queue.claim(session, "w")).id == first.id  # type: ignore[union-attr]
    assert (await queue.claim(session, "w")).id == second.id  # type: ignore[union-attr]


async def test_an_expired_lease_makes_the_task_claimable_again(session: AsyncSession) -> None:
    """This is crash recovery: a dead worker stops extending, and the task comes back."""
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4()))

    dead = await queue.claim(session, "worker-dead")
    assert dead is not None
    assert await queue.claim(session, "worker-live") is None

    await queue.expire_lease_now(session, dead.id)

    reclaimed = await queue.claim(session, "worker-live")
    assert reclaimed is not None
    assert reclaimed.id == dead.id
    assert reclaimed.claimed_by == "worker-live"


async def test_heartbeat_extends_the_lease(session: AsyncSession) -> None:
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4()))
    task = await queue.claim(session, "worker-a", lease_seconds=5)
    assert task is not None

    await queue.expire_lease_now(session, task.id)
    assert await queue.heartbeat(session, task.id, "worker-a", lease_seconds=60) is True

    # Extended, so nobody else can take it.
    assert await queue.claim(session, "worker-b") is None


async def test_a_worker_cannot_heartbeat_a_task_it_lost(session: AsyncSession) -> None:
    """A worker declared dead must not be able to quietly take its task back."""
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4()))
    task = await queue.claim(session, "worker-dead")
    assert task is not None
    await queue.expire_lease_now(session, task.id)
    await queue.claim(session, "worker-live")

    assert await queue.heartbeat(session, task.id, "worker-dead") is False


async def test_release_puts_the_task_back_immediately(session: AsyncSession) -> None:
    """A planned exit should not make the next worker wait out a crash-detection timeout."""
    user = await _a_user(session)
    await queue.enqueue(session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4()))
    task = await queue.claim(session, "worker-a")
    assert task is not None

    assert await queue.release(session, task.id, "worker-a") is True

    back = await queue.claim(session, "worker-b")
    assert back is not None and back.id == task.id


async def test_concurrent_workers_take_different_tasks(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The point of SKIP LOCKED: two workers make progress instead of one waiting.

    Two sessions, genuinely concurrent, against two queued tasks. Without SKIP LOCKED one
    claim would block on the other's row lock and this would be a serial queue.
    """
    user = await _a_user(session)
    for _ in range(2):
        await queue.enqueue(
            session, user_id=user.id, spec="work", idempotency_key=str(uuid.uuid4())
        )
    await session.commit()

    async def claim_with(worker: str) -> uuid.UUID | None:
        async with session_factory() as own_session:
            task = await queue.claim(own_session, worker)
            await own_session.commit()
            return task.id if task else None

    first, second = await asyncio.gather(claim_with("worker-a"), claim_with("worker-b"))

    assert first is not None and second is not None
    assert first != second
