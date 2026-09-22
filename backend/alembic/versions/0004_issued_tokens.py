"""add issued_tokens

Revision ID: 0004_issued_tokens
Revises: 0003_audit_log
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_issued_tokens"
down_revision: str | None = "0003_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issued_tokens",
        sa.Column("jti", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(op.f("ix_issued_tokens_task_id"), "issued_tokens", ["task_id"], unique=False)

    # 0003's ALTER DEFAULT PRIVILEGES applies to every table the *connected owner role* creates
    # from here on, and every migration in this project runs as that same role, so in theory
    # warden_app already got SELECT/INSERT/UPDATE/DELETE on this table for free. Proven, not
    # assumed: tests/test_identity.py::test_warden_app_can_insert_and_revoke inserts and updates
    # under SET LOCAL ROLE warden_app against exactly this table, and it passes without any
    # GRANT here. No explicit GRANT needed in this migration.


def downgrade() -> None:
    op.drop_index(op.f("ix_issued_tokens_task_id"), table_name="issued_tokens")
    op.drop_table("issued_tokens")
