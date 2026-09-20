"""This repository puts guarantees in the database before rules in application code.

A constraint without a test is only an intention: these tests exist so that one fails
visibly if someone changes the migration.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from warden.models import Task, TaskEvent, User

EXPECTED_TABLES = {
    "users",
    "tasks",
    "task_events",
    "tool_calls",
    "model_calls",
    "alembic_version",
}


async def _a_user(session: AsyncSession) -> User:
    user = User(
        email=f"{uuid.uuid4()}@warden.test",
        password_hash="not-a-real-hash",
        role="submitter",
    )
    session.add(user)
    await session.flush()
    return user


async def test_migration_creates_expected_tables(session: AsyncSession) -> None:
    rows = await session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    assert {row[0] for row in rows} >= EXPECTED_TABLES


async def test_idempotency_key_rejects_duplicate(session: AsyncSession) -> None:
    """Two POST /tasks with the same Idempotency-Key must not become two runs."""
    user = await _a_user(session)
    session.add(Task(idempotency_key="same-key", user_id=user.id, spec="read the repo"))
    await session.flush()

    session.add(Task(idempotency_key="same-key", user_id=user.id, spec="read it again"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_task_event_seq_is_unique_per_task(session: AsyncSession) -> None:
    """Two workers never write the same step. This is what makes resume safe."""
    user = await _a_user(session)
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec="anything")
    session.add(task)
    await session.flush()

    session.add(TaskEvent(task_id=task.id, seq=1, type="task.created"))
    await session.flush()

    session.add(TaskEvent(task_id=task.id, seq=1, type="task.created"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_status_check_rejects_unknown_state(session: AsyncSession) -> None:
    """Week 2's core/ will not be able to invent a state outside the machine."""
    user = await _a_user(session)
    session.add(
        Task(
            idempotency_key=str(uuid.uuid4()),
            user_id=user.id,
            spec="anything",
            status="ALMOST_DONE",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
