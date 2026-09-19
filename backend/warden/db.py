from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str) -> AsyncEngine:
    # pool_pre_ping: o Postgres do Compose reinicia sem avisar a aplicacao; sem isso a
    # primeira query depois do restart estoura com conexao morta.
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def with_database(url: str, database: str) -> str:
    """Mesma URL apontando para outro database. Usado pelo banco de teste.

    render_as_string(hide_password=False) e obrigatorio: `str(URL)` do SQLAlchemy troca a
    senha por "***" em silencio (protecao contra vazar credencial em log). Usar str() aqui
    devolve uma URL com a senha literal "***" e o erro que chega e
    "password authentication failed", que aponta para o lugar errado.
    """
    return make_url(url).set(database=database).render_as_string(hide_password=False)
