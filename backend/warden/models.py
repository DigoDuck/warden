"""Warden database schema.

Briefing principle (section 12): a task is an event log. `tasks` holds the current
snapshot, `task_events` holds everything that happened, in order, append-only.
Resume means rebuilding the messages from those events.

ponytail: every table in a single file. Ceiling: around 20 tables by week 11. One file
keeps Alembic's `target_metadata` trivial and avoids circular imports between the
relationships; split into per-package modules when the file starts to get in the way.
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

# The state machine from section 16 of the briefing. It lives as a CHECK in the database
# rather than as a rule in application code: this repository lets the database guarantee
# whatever the database can guarantee, so week 2's core/ cannot invent a new state
# silently.
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
    # UNIQUE is what makes enqueueing idempotent. Without it, two POST /tasks carrying
    # the same Idempotency-Key become two tasks and the agent runs twice.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    # No foreign key yet: `agent_versions` is born with the registry. The FK lands in that
    # week's migration, next to the table it references.
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
    # claimed_by plus claimed_until are the lease behind the SKIP LOCKED claim (week 2):
    # a dead worker holds the task only until the lease expires, then another can claim.
    claimed_by: Mapped[str | None] = mapped_column(String(128), default=None)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TaskEvent(Base):
    __tablename__ = "task_events"
    # UNIQUE(task_id, seq): two workers never write the same step. Concurrency is settled
    # in the database, not with a lock in application code. This is what makes resume safe.
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
    # args_safe: arguments with secrets redacted and values truncated. Raw arguments never
    # reach telemetry or the database (briefing, section 13).
    args_safe: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    args_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    result_summary: Mapped[str | None] = mapped_column(Text, default=None)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class PolicyDecision(Base):
    """Why one tool call was allowed, refused or escalated.

    One row per decided tool call. `matched_rules` keeps every rule that matched, not just
    the deciding one, and `policy_hash` pins which version of the policy produced it: an
    audit months later has to answer "under which rules was this allowed".
    """

    __tablename__ = "policy_decisions"

    id: Mapped[uuid.UUID] = _pk()
    tool_call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), index=True
    )
    effect: Mapped[str] = mapped_column(String(32))
    matched_rules: Mapped[list[str]] = mapped_column(JSONB, default=list)
    reason: Mapped[str] = mapped_column(Text)
    policy_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # Numeric, not float: cost is money and gets summed per task, per experiment and
    # published in docs/metrics.md. Accumulated floating point error there would be a lie.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    fallback_from: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
