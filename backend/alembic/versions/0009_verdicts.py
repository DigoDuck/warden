"""add verdicts

Revision ID: 0009_verdicts
Revises: 0008_evidence
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_verdicts"
down_revision: str | None = "0008_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verdicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("verifier", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("gate", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=64), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("verifier IN ('independent')", name="ck_verdicts_verifier"),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_verdicts_status"),
        # An errored review can never be a pass: fail closed, enforced where it cannot be
        # bypassed by a code path that forgets.
        sa.CheckConstraint("status = 'ok' OR NOT passed", name="ck_verdicts_error_never_passes"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "verifier", name="uq_verdicts_task_verifier"),
    )
    op.create_index(op.f("ix_verdicts_task_id"), "verdicts", ["task_id"], unique=False)

    # 0003's ALTER DEFAULT PRIVILEGES already covers a table this migration's role creates
    # (see 0004's comment for the same reasoning); no explicit GRANT needed here either.


def downgrade() -> None:
    op.drop_index(op.f("ix_verdicts_task_id"), table_name="verdicts")
    op.drop_table("verdicts")
