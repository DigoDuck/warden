"""Schema do Warden.

Principio do briefing (secao 12): a tarefa e um event log. `tasks` guarda o snapshot
atual, `task_events` guarda tudo que aconteceu em ordem, append-only. Resume e
reconstruir as mensagens a partir dos eventos.

ponytail: todas as tabelas num arquivo so. Teto: ~20 tabelas ate a semana 11. Arquivo
unico mantem o `target_metadata` do Alembic trivial e evita import circular entre os
relacionamentos; separar por subpacote quando o arquivo comecar a atrapalhar.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Maquina de estados da secao 16 do briefing. Fica como CHECK no banco, nao como regra
# em codigo: a convencao do projeto e deixar o banco garantir o que ele consegue
# garantir, para que o core/ da semana 2 nao consiga inventar um estado novo em silencio.
TASK_STATUSES = (
    "QUEUED",
    "RUNNING",
    "VERIFYING",
    "WAITING_APPROVAL",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "BUDGET_EXCEEDED",
)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in TASK_STATUSES) + ")",
            name="ck_tasks_status",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # UNIQUE e o que torna o enqueue idempotente. Sem ele, dois POST /tasks com o mesmo
    # Idempotency-Key viram duas tarefas e o agente roda duas vezes.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    # Sem FK ainda: `agent_versions` nasce com o registry. A FK entra na migracao daquela
    # semana, junto da tabela que ela referencia.
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    spec: Mapped[str] = mapped_column(Text)
    target_repo: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    experiment_id: Mapped[str | None] = mapped_column(String(64), default=None)
    routing_strategy: Mapped[str | None] = mapped_column(String(64), default=None)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    spent: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # claimed_by + claimed_until sao o lease do claim com SKIP LOCKED (semana 2): worker
    # morto deixa a tarefa presa ate o lease vencer, e ai outro worker pode reivindicar.
    claimed_by: Mapped[str | None] = mapped_column(String(128), default=None)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TaskEvent(Base):
    __tablename__ = "task_events"
    # UNIQUE(task_id, seq): dois workers nunca gravam o mesmo passo. A concorrencia se
    # resolve no banco, nao com lock na aplicacao. E o que torna o resume seguro.
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_task_events_task_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    iteration: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(128))
    # args_safe: argumentos ja com segredo redigido e truncados. Argumento cru nunca vai
    # para telemetria nem para o banco (briefing, secao 13).
    args_safe: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    args_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    result_summary: Mapped[str | None] = mapped_column(Text, default=None)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64), default=None)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    # Numeric, nao float: custo e dinheiro e vai ser somado por tarefa, por experimento e
    # publicado em docs/metrics.md. Erro de ponto flutuante acumulado ali seria mentira.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    fallback_from: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
