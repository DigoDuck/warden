from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str) -> AsyncEngine:
    # pool_pre_ping: the Compose Postgres restarts without telling the application.
    # Without it, the first query after a restart fails on a dead connection.
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def with_database(url: str, database: str) -> str:
    """Return the same URL pointing at another database. Used by the test database.

    render_as_string(hide_password=False) is mandatory here: SQLAlchemy's `str(URL)`
    silently replaces the password with "***" to keep credentials out of logs. Using
    str() would yield a URL whose password is literally "***", and the error that
    surfaces is "password authentication failed", which points at the wrong place.
    """
    return make_url(url).set(database=database).render_as_string(hide_password=False)
