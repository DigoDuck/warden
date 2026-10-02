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
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("malformed_reason", sa.Text(), nullable=True),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("verifier IN ('independent')", name="ck_verdicts_verifier"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "verifier", name="uq_verdicts_task_verifier"),
    )
    op.create_index(op.f("ix_verdicts_task_id"), "verdicts", ["task_id"], unique=False)

    # No explicit GRANT: 0003's ALTER DEFAULT PRIVILEGES already covers a table this
    # migration's role creates (same reasoning as 0008).


def downgrade() -> None:
    op.drop_index(op.f("ix_verdicts_task_id"), table_name="verdicts")
    op.drop_table("verdicts")
