"""add audit_log, and the warden_app role that owns the application's own privileges

Revision ID: 0003_audit_log
Revises: 0002_policy_decisions
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_audit_log"
down_revision: str | None = "0002_policy_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A role is CLUSTER-global in Postgres, not per-database, but this migration runs once per
# database on the cluster (dev, plus one test database per checkout, sometimes concurrently
# during CI). `IF NOT EXISTS` alone still races: two databases can both see "not there yet"
# and both issue CREATE ROLE, and the loser fails with "role already exists" instead of
# quietly doing nothing. The DO block therefore checks pg_roles *and* catches the
# duplicate_object error the losing CREATE ROLE raises, so either path ends with the role
# existing and the migration succeeding.
_CREATE_APP_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warden_app') THEN
        CREATE ROLE warden_app NOLOGIN;
    END IF;
EXCEPTION
    WHEN duplicate_object THEN
        NULL;
END
$$;
"""


def upgrade() -> None:
    op.execute(_CREATE_APP_ROLE)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Generated in Python (see warden/audit/log.py), never server_default=now(): the
        # value has to be known before the insert so it can be hashed.
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("hash", sa.CHAR(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # The application connects as `warden_app` from here on (well, will: today it still
    # connects as the database owner, see ADR-007). Give it what it needs on everything
    # that exists already, and on everything future migrations create, in one place instead
    # of re-granting per table each time a table is added.
    op.execute("GRANT USAGE ON SCHEMA public TO warden_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO warden_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO warden_app")
    # ALTER DEFAULT PRIVILEGES only covers objects created *by the role that runs this
    # statement* (there is no FOR ROLE clause here). Every migration in this project runs
    # as the same connected owner role, so every table a future migration creates is
    # covered; a table created by some other role later would not be, and would need its
    # own GRANT.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO warden_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO warden_app"
    )

    # Narrow what the blanket grant above just gave on these two tables specifically.
    # audit_log: append-only from the application's point of view. SELECT and INSERT
    # survive the blanket grant; UPDATE/DELETE are explicitly revoked here, which is the
    # immutability guarantee ADR-007 documents. TRUNCATE was never in the blanket grant in
    # the first place (it only covers SELECT/INSERT/UPDATE/DELETE), so revoking it here is
    # belt-and-suspenders: it survives someone widening that blanket grant later without
    # remembering this table needs the exception.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM warden_app")
    # alembic_version: the application has no business changing which migration a database
    # is on. Read is harmless and occasionally useful for a health check; write is not.
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM warden_app")


def downgrade() -> None:
    # Undo the default-privilege and blanket grants, then the table. The `warden_app` role
    # itself is NOT dropped: it is cluster-global, and other databases on this cluster
    # (dev, another checkout's test database) may still depend on it existing. Dropping a
    # role out from under a sibling database is a much worse failure than leaving an unused,
    # privilege-less role behind.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM warden_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM warden_app"
    )
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM warden_app")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM warden_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM warden_app")

    op.drop_table("audit_log")
