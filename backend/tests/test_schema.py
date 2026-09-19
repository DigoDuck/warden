"""A convencao do projeto e constraint no banco antes de regra em codigo.

Constraint sem teste e so uma intencao: estes testes existem para que ela falhe de forma
visivel se alguem mexer na migracao.
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
        password_hash="nao-e-um-hash-de-verdade",
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
    """Dois POST /tasks com o mesmo Idempotency-Key nao podem virar duas tarefas."""
    user = await _a_user(session)
    session.add(Task(idempotency_key="mesma-chave", user_id=user.id, spec="ler o repo"))
    await session.flush()

    session.add(Task(idempotency_key="mesma-chave", user_id=user.id, spec="ler de novo"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_task_event_seq_is_unique_per_task(session: AsyncSession) -> None:
    """Dois workers nunca gravam o mesmo passo. E o que torna o resume seguro."""
    user = await _a_user(session)
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec="qualquer")
    session.add(task)
    await session.flush()

    session.add(TaskEvent(task_id=task.id, seq=1, type="task.created"))
    await session.flush()

    session.add(TaskEvent(task_id=task.id, seq=1, type="task.created"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_status_check_rejects_unknown_state(session: AsyncSession) -> None:
    """O core/ da semana 2 nao vai conseguir inventar um estado fora da maquina."""
    user = await _a_user(session)
    session.add(
        Task(
            idempotency_key=str(uuid.uuid4()),
            user_id=user.id,
            spec="qualquer",
            status="QUASE_PRONTO",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
