"""add evidence

Revision ID: 0008_evidence
Revises: 0007_audit_log_target_index
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_evidence"
down_revision: str | None = "0007_audit_log_target_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('diff', 'lint', 'types', 'tests')", name="ck_evidence_kind"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "kind", name="uq_evidence_task_kind"),
    )
    op.create_index(op.f("ix_evidence_task_id"), "evidence", ["task_id"], unique=False)

    # 0003's ALTER DEFAULT PRIVILEGES already covers a table this migration's role creates
    # (see 0004's comment for the same reasoning); no explicit GRANT needed here either.


def downgrade() -> None:
    op.drop_index(op.f("ix_evidence_task_id"), table_name="evidence")
    op.drop_table("evidence")
