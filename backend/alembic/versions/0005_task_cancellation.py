"""add cancel_requested_at to tasks

Revision ID: 0005_task_cancellation
Revises: 0004_issued_tokens
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_task_cancellation"
down_revision: str | None = "0004_issued_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    # A QUEUED task is unclaimed by definition, so `core/cancel.py::request_cancel` cancels
    # it outright instead of setting this column. The constraint says so in the database,
    # not only in the one code path that happens to be the only writer today.
    op.create_check_constraint(
        "ck_tasks_cancel_requested_only_after_claim",
        "tasks",
        "status != 'QUEUED' OR cancel_requested_at IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_cancel_requested_only_after_claim", "tasks", type_="check")
    op.drop_column("tasks", "cancel_requested_at")
