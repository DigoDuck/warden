import asyncio
import os
import pathlib
from collections.abc import AsyncIterator

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from warden.config import get_settings
from warden.db import make_engine, make_session_factory, with_database

BACKEND = pathlib.Path(__file__).resolve().parents[1]
# Overridable so two checkouts can run the suite against one Postgres at the same time.
# The fixture below drops and recreates this database, so with a shared name one run
# would destroy the other's. CI and a single checkout keep the default.
TEST_DB = os.environ.get("WARDEN_TEST_DB", "warden_test")


@pytest.fixture(scope="session")
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create the test database from scratch and migrate it once per session.

    This runs the real migration instead of `metadata.create_all`, so what the tests
    exercise is the schema that ships, not a parallel one generated from the models.
    """
    base_url = get_settings().database_url
    test_url = with_database(base_url, TEST_DB)

    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT. FORCE
    # kills connections left behind by an earlier run that died halfway through.
    admin = create_async_engine(with_database(base_url, "postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    await admin.dispose()

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", test_url)
    # Alembic's env.py calls asyncio.run, which raises inside an already running loop.
    # to_thread hands it a thread with a loop of its own.
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = make_engine(test_url)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session per test, always rolled back.

    Tests use `flush()` and never `commit()`: the constraint fires just the same, and
    the rollback leaves a clean database for the next test without TRUNCATE in between.
    """
    async with session_factory() as s:
        yield s
        await s.rollback()
