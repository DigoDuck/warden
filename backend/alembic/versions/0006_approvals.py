"""add approvals

Revision ID: 0006_approvals
Revises: 0005_task_cancellation
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_approvals"
down_revision: str | None = "0005_task_cancellation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("args_safe", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("matched_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_approvals_status",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "tool_call_id", name="uq_approvals_task_tool_call"),
    )
    op.create_index(op.f("ix_approvals_task_id"), "approvals", ["task_id"], unique=False)
    # Partial: only `status = 'pending'` rows compete for the slot, so a task can accumulate
    # any number of decided approvals over its life without ever blocking the next question.
    op.create_index(
        "uq_approvals_one_pending_per_task",
        "approvals",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # 0003's ALTER DEFAULT PRIVILEGES already covers a table this migration's role creates
    # (see 0004's comment for the same reasoning); no explicit GRANT needed here either.


def downgrade() -> None:
    op.drop_index("uq_approvals_one_pending_per_task", table_name="approvals")
    op.drop_index(op.f("ix_approvals_task_id"), table_name="approvals")
    op.drop_table("approvals")
