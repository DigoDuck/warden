"""add index on audit_log(target_type, target_id)

Revision ID: 0007_audit_log_target_index
Revises: 0006_approvals
Create Date: 2026-09-23

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_audit_log_target_index"
down_revision: str | None = "0006_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"])

    # No GRANT needed: an index carries no privilege of its own. `warden_app`'s existing
    # SELECT on audit_log (0003) is all a query needs to make the planner consider it, and
    # the REVOKE UPDATE/DELETE/TRUNCATE from that same migration is untouched by adding one
    # (tests/test_audit.py's app-role tests keep proving that).


def downgrade() -> None:
    op.drop_index("ix_audit_log_target", table_name="audit_log")
