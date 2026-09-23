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
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
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
        # A QUEUED task is unclaimed by definition (nothing is running it to cooperate with
        # a marker), so `core/cancel.py::request_cancel` cancels it outright instead of
        # setting this column. The constraint keeps that invariant true in the database
        # itself, not only in whichever code path happens to be the only writer today.
        CheckConstraint(
            "status != 'QUEUED' OR cancel_requested_at IS NULL",
            name="ck_tasks_cancel_requested_only_after_claim",
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
    # Set by `core/cancel.py::request_cancel` on a RUNNING task; the loop polls it
    # cooperatively (`core/loop.py::_check_stoppable`). NULL means no cancel is pending.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
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


class Approval(Base):
    """A `REQUIRE_APPROVAL` decision the loop paused on, and how a human resolved it.

    See `core/approvals.py` and ADR-022. `tool_call_id` is the provider's own id for the
    call (`providers.base.ToolCall.id`, e.g. `"fake-0-0"`), not a foreign key into
    `tool_calls`: at request time no `tool_calls` row exists yet for a call that never ran,
    and after a reject it never will. Replay (`core/replay.py`) matches a resumed call back
    to its decision by this id, read straight off the event log, so this table itself is
    never queried mid-resume (it is pure, briefing §12).
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_approvals_status",
        ),
        # At most one open question per task. `core/loop.py::_pause_for_approval` only ever
        # creates one of these after parking the task WAITING_APPROVAL, and
        # `core/approvals.decide_approval` resolves it before the task can run again, so two
        # pending rows for the same task would mean two decisions in flight for a task only
        # one worker ever held at a time.
        Index(
            "uq_approvals_one_pending_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # A given call is asked about at most once, ever, whatever the outcome: replay
        # builds "which pending call was this" from the event log by id (ADR-022), so a
        # second approval row for the same call would be a second question replay cannot
        # tell apart from the first.
        UniqueConstraint("task_id", "tool_call_id", name="uq_approvals_task_tool_call"),
    )

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(128))
    tool: Mapped[str] = mapped_column(String(128))
    # Redacted the same way as `tool_calls.args_safe` (`core/events.py::redact_args`): raw
    # arguments never reach a table a reviewer's dashboard reads from.
    args_safe: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    matched_rules: Mapped[list[str]] = mapped_column(JSONB, default=list)
    reason: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    note: Mapped[str | None] = mapped_column(Text, default=None)


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


class IssuedToken(Base):
    """One row per JWT this control plane has signed. See warden/identity/jwt.py and ADR-005.

    Keyed by `jti` itself rather than a separate surrogate id: the token's own identifier is
    already the row's natural key, so a second uuid column would only duplicate it. Unlike
    `audit_log`, this table has a real foreign key to `tasks`: it is bookkeeping for a token's
    own lifecycle (verify's fail-closed lookup, revocation), not an independent trail that has
    to keep making sense after the task it names is gone.
    """

    __tablename__ = "issued_tokens"

    jti: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(128))
    # NULL for a user token: only an agent token is scoped to one run of one task.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, default=None
    )
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """One append-only, hash-chained entry. See warden/audit/log.py and ADR-007.

    Independent of the other tables on purpose (briefing, section on the data model): it
    references a task or actor by id in `target_id`/`actor_id`, not by foreign key, so
    deleting or rewriting unrelated data can never cascade into the audit trail, and the
    trail keeps making sense even for actors (an approver, a revoked token) that never had
    a row of their own here.

    `prev_hash`/`hash` are fixed-width `CHAR(64)`, not `String`, because every row always
    holds exactly one lowercase hex sha256 digest: the column type says so instead of
    relying on application code to keep them that length.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Generated in Python, not server_default=func.now(): the value has to be known
    # *before* the insert so it can be part of what gets hashed (see audit/log.py).
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32), default=None)
    target_id: Mapped[str | None] = mapped_column(String(128), default=None)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(CHAR(64))
    hash: Mapped[str] = mapped_column(CHAR(64))
