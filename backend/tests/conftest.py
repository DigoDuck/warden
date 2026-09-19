import asyncio
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
TEST_DB = "warden_test"


@pytest.fixture(scope="session")
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Cria o banco de teste do zero e aplica as migracoes uma vez por sessao.

    Roda a migracao de verdade em vez de `metadata.create_all`: o que e testado aqui e o
    schema que vai para producao, nao um schema paralelo gerado pelos modelos.
    """
    base_url = get_settings().database_url
    test_url = with_database(base_url, TEST_DB)

    # CREATE/DROP DATABASE nao rodam dentro de transacao, dai o AUTOCOMMIT. FORCE derruba
    # conexao pendurada de uma execucao anterior que tenha morrido no meio.
    admin = create_async_engine(with_database(base_url, "postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    await admin.dispose()

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", test_url)
    # O env.py do alembic chama asyncio.run, que estoura se ja houver loop rodando.
    # to_thread entrega a ele uma thread com loop proprio.
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = make_engine(test_url)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Sessao por teste, sempre desfeita no fim.

    Os testes usam `flush()` e nunca `commit()`: a constraint dispara igual e o rollback
    deixa o banco limpo para o proximo teste, sem TRUNCATE entre eles.
    """
    async with session_factory() as s:
        yield s
        await s.rollback()
