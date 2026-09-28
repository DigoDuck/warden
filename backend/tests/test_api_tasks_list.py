"""GET /tasks: listing, filtering, ownership and keyset pagination.

Same fixtures and conventions as tests/test_api.py and tests/test_api_approvals.py: every
request goes through the ASGI app (a real commit), so helpers use `session_factory` directly
and `clean_committed_rows` truncates between tests. Fixtures duplicated rather than imported,
matching the existing split between test_api.py and test_api_approvals.py.
"""

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import identity
from warden.api.app import create_app
from warden.identity.jwt import KeyPair
from warden.models import User


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, keys=keys)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def clean_committed_rows(session: AsyncSession) -> AsyncIterator[None]:
    wipe = text(
        "TRUNCATE audit_log, issued_tokens, task_events, model_calls, tasks, users "
        "RESTART IDENTITY CASCADE"
    )
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


async def _user(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session:
        row = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        session.add(row)
        await session.flush()
        await session.commit()
        return row


async def _user_token(
    session_factory: async_sessionmaker[AsyncSession],
    keys: KeyPair,
    user: User,
    scopes: list[str],
) -> str:
    async with session_factory() as session:
        token = await identity.issue_user_token(session, keys, user, scopes=scopes)
        await session.commit()
        return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _submit(client: AsyncClient, token: str, spec: str = "x") -> str:
    headers = {**_auth(token), "Idempotency-Key": str(uuid.uuid4())}
    created = await client.post("/tasks", json={"spec": spec}, headers=headers)
    assert created.status_code == 201
    return created.json()["id"]


async def test_list_is_empty_for_a_user_with_no_tasks(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get("/tasks", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"tasks": [], "next_cursor": None}


async def test_list_returns_newest_first(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    first = await _submit(client, token, "first")
    second = await _submit(client, token, "second")

    response = await client.get("/tasks", headers=_auth(token))
    ids = [t["id"] for t in response.json()["tasks"]]
    assert ids == [second, first]


async def test_list_does_not_include_another_users_tasks(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    owner_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    await _submit(client, owner_token)

    stranger_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:read"]
    )
    response = await client.get("/tasks", headers=_auth(stranger_token))
    assert response.json()["tasks"] == []


async def test_an_admin_scope_sees_every_users_tasks(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    owner_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write"]
    )
    task_id = await _submit(client, owner_token)

    admin_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:read", "admin"]
    )
    response = await client.get("/tasks", headers=_auth(admin_token))
    assert task_id in [t["id"] for t in response.json()["tasks"]]


async def test_list_omits_cost_and_iterations_to_avoid_n_plus_one(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    await _submit(client, token)
    response = await client.get("/tasks", headers=_auth(token))
    row = response.json()["tasks"][0]
    assert "cost_usd" not in row
    assert "iterations" not in row


async def test_status_filter_returns_only_matching_tasks(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    queued_id = await _submit(client, token, "queued one")
    other_id = await _submit(client, token, "will be marked done")
    async with session_factory() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": other_id}
        )
        await session.commit()

    response = await client.get("/tasks", headers=_auth(token), params={"status": "QUEUED"})
    ids = [t["id"] for t in response.json()["tasks"]]
    assert ids == [queued_id]


async def test_an_invalid_status_filter_is_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get("/tasks", headers=_auth(token), params={"status": "NOT_A_STATUS"})
    assert response.status_code == 422


async def test_keyset_pagination_walks_every_task_once(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    created = [await _submit(client, token, f"task {n}") for n in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # bounded loop: a real bug here must not hang the test suite
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await client.get("/tasks", headers=_auth(token), params=params)
        assert page.status_code == 200
        body = page.json()
        seen.extend(t["id"] for t in body["tasks"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "pagination never terminated"
    assert seen == list(reversed(created))  # newest first, no dup, no gap


def _forge_cursor(raw: str) -> str:
    """Build a cursor the same way `_encode_cursor` would, but from a raw string this test
    controls, so it can forge one carrying whatever `created_at` text it wants."""
    return base64.urlsafe_b64encode(raw.encode()).decode()


async def test_a_malformed_cursor_is_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get("/tasks", headers=_auth(token), params={"cursor": "not-base64!!"})
    assert response.status_code == 422


async def test_a_cursor_with_a_timezone_naive_datetime_is_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """`created_at` is `DateTime(timezone=True)`: comparing it to a naive datetime in the
    keyset `WHERE` clause is a database-level type error, not a client mistake to 500 on. A
    forged cursor is the only way to get a naive value in here — `_encode_cursor` always
    serialises an aware `created_at` read back from that same column."""
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    cursor = _forge_cursor(f"2024-01-01T00:00:00|{uuid.uuid4()}")
    response = await client.get("/tasks", headers=_auth(token), params={"cursor": cursor})
    assert response.status_code == 422


async def test_missing_scope_on_list_is_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    response = await client.get("/tasks", headers=_auth(token))
    assert response.status_code == 403


async def test_keyset_pagination_breaks_created_at_ties_by_id(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """Tasks sharing one `created_at` (same-microsecond submits, a bulk insert) must still
    be walked exactly once: a cursor over `created_at` alone would skip the rest of a tie."""
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    created = [await _submit(client, token, f"tie {n}") for n in range(3)]
    async with session_factory() as session:
        await session.execute(text("UPDATE tasks SET created_at = '2026-01-01T00:00:00Z'"))
        await session.commit()

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get("/tasks", headers=_auth(token), params=params)).json()
        seen.extend(t["id"] for t in body["tasks"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == sorted(created)
    assert len(seen) == len(created)
