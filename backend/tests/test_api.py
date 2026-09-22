"""The HTTP surface (briefing §14), exercised through ASGI with a real test database.

`keys` is an ephemeral RSA pair, same as tests/test_identity.py, never `identity.load_keys()`
reading a file. Every helper here uses `session_factory` directly and commits, never the
`session` fixture (which always rolls back): a request through the ASGI app opens its own
session (see `warden.api.deps.get_session`), so a token, user or task has to be genuinely
committed before the app's own session can see it, the same reason test_identity.py's
cross-session tests do this.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import audit, identity
from warden.api.app import create_app
from warden.core import events
from warden.identity.jwt import ALGORITHM, KeyPair, _kid_for
from warden.models import AuditLog, Task, TaskEvent, User


@pytest.fixture(scope="session")
def keys() -> KeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=_kid_for(public_key))


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, keys=keys)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def clean_committed_rows(session: AsyncSession) -> AsyncIterator[None]:
    """Every test here goes through the ASGI app, which commits for real (idempotency and
    the audit trail only mean something across a real commit). Same TRUNCATE pattern as
    test_identity.py and test_queue.py, widened to every table a request can touch.
    """
    wipe = text(
        "TRUNCATE audit_log, issued_tokens, task_events, model_calls, tasks, users "
        "RESTART IDENTITY CASCADE"
    )
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


pytestmark = pytest.mark.usefixtures("clean_committed_rows")


# --- helpers, all committing through session_factory directly -------------------------------


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


async def _agent_token(
    session_factory: async_sessionmaker[AsyncSession], keys: KeyPair, scopes: list[str]
) -> str:
    async with session_factory() as session:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        session.add(user)
        await session.flush()
        task = Task(user_id=user.id, spec="agent task", idempotency_key=str(uuid.uuid4()))
        session.add(task)
        await session.flush()
        token = await identity.issue_agent_token(session, keys, task_id=task.id, scopes=scopes)
        await session.commit()
        return token


async def _expired_token(session_factory: async_sessionmaker[AsyncSession], keys: KeyPair) -> str:
    """A token that is genuine in every way except that it has expired.

    Issued for real, so its `jti` has a live row: otherwise the 401 would come from the
    unknown-jti check and prove nothing about expiry. The row's `expires_at` is moved into
    the past (this connection is not `warden_app`) and the token re-signed with the matching
    `exp`, so `identity.verify()`'s row comparison agrees and only the expiry check is left
    to refuse it.
    """
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    payload = jwt.decode(token, options={"verify_signature": False})
    expired_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=15)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE issued_tokens SET expires_at = :at WHERE jti = :jti"),
            {"at": expired_at, "jti": uuid.UUID(payload["jti"])},
        )
        await session.commit()
    payload["exp"] = int(expired_at.timestamp())
    return jwt.encode(payload, keys.private_key, algorithm=ALGORITHM, headers={"kid": keys.kid})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- authentication: 401 --------------------------------------------------------------------


async def test_no_bearer_token_is_401_with_www_authenticate(client: AsyncClient) -> None:
    response = await client.post("/tasks", json={"spec": "x"}, headers={"Idempotency-Key": "k"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_token_signed_by_a_different_key_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """Same claims as a genuinely issued token, live `jti` included, so the only thing wrong
    is the signature.
    """
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    payload = jwt.decode(token, options={"verify_signature": False})
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(payload, other, algorithm=ALGORITHM, headers={"kid": keys.kid})

    response = await client.post(
        "/tasks", json={"spec": "x"}, headers={**_auth(forged), "Idempotency-Key": "k"}
    )
    assert response.status_code == 401


async def test_an_expired_token_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    response = await client.post(
        "/tasks",
        json={"spec": "x"},
        headers={**_auth(await _expired_token(session_factory, keys)), "Idempotency-Key": "k"},
    )
    assert response.status_code == 401


async def test_a_revoked_token_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    user = await _user(session_factory)
    token = await _user_token(session_factory, keys, user, ["tasks:write"])
    async with session_factory() as session:
        claims = await identity.verify(session, keys, token)
        await identity.revoke(session, claims.jti)
        await session.commit()

    response = await client.post(
        "/tasks", json={"spec": "x"}, headers={**_auth(token), "Idempotency-Key": "k"}
    )
    assert response.status_code == 401


async def test_error_body_never_echoes_the_token(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _expired_token(session_factory, keys)
    response = await client.post(
        "/tasks", json={"spec": "x"}, headers={**_auth(token), "Idempotency-Key": "k"}
    )
    assert token not in response.text


# --- authorization: 403 -------------------------------------------------------------------


async def test_missing_scope_is_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    user = await _user(session_factory)
    token = await _user_token(session_factory, keys, user, ["tasks:read"])  # no tasks:write

    response = await client.post(
        "/tasks", json={"spec": "x"}, headers={**_auth(token), "Idempotency-Key": "k"}
    )
    assert response.status_code == 403


async def test_an_agent_token_is_refused_even_with_a_matching_scope_name(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """Proves the explicit `typ == "user"` guard, not just an incidental scope mismatch: the
    agent token below is minted with the exact scope name the endpoint asks for.
    """
    token = await _agent_token(session_factory, keys, ["tasks:read"])

    response = await client.get("/tasks/00000000-0000-0000-0000-000000000000", headers=_auth(token))
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "granted"),
    [
        ("POST", "/tasks", ["tasks:read", "audit:read"]),
        ("GET", f"/tasks/{uuid.uuid4()}", ["tasks:write", "audit:read"]),
        ("GET", f"/tasks/{uuid.uuid4()}/events", ["tasks:write", "audit:read"]),
        ("GET", "/audit/verify", ["tasks:write", "tasks:read"]),
    ],
)
async def test_each_route_demands_its_own_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    keys: KeyPair,
    method: str,
    path: str,
    granted: list[str],
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), granted)
    response = await client.request(
        method,
        path,
        json={"spec": "x"} if method == "POST" else None,
        headers={**_auth(token), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 403


# --- POST /tasks: validation ----------------------------------------------------------------


async def test_missing_idempotency_key_is_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    response = await client.post("/tasks", json={"spec": "x"}, headers=_auth(token))
    assert response.status_code == 422


async def test_empty_spec_is_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    response = await client.post(
        "/tasks", json={"spec": ""}, headers={**_auth(token), "Idempotency-Key": "k"}
    )
    assert response.status_code == 422


async def test_unknown_field_is_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    response = await client.post(
        "/tasks",
        json={"spec": "x", "not_a_field": 1},
        headers={**_auth(token), "Idempotency-Key": "k"},
    )
    assert response.status_code == 422


async def test_a_non_finite_budget_is_422_not_a_500(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """Python's json parser accepts the bare `Infinity` token, and `gt=0` alone lets it
    through; JSONB cannot store it, so without `allow_inf_nan=False` this is a 500 from the
    driver instead of a validation error.
    """
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    response = await client.post(
        "/tasks",
        content=b'{"spec": "x", "budget": {"max_usd": Infinity}}',
        headers={
            **_auth(token),
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


# --- POST /tasks: idempotency ----------------------------------------------------------------


async def test_the_same_idempotency_key_returns_the_same_task_once_created(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    headers = {**_auth(token), "Idempotency-Key": str(uuid.uuid4())}

    first = await client.post("/tasks", json={"spec": "do the thing"}, headers=headers)
    second = await client.post("/tasks", json={"spec": "do the thing"}, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "task.submitted", AuditLog.target_id == first.json()["id"])
        )
    assert count == 1


async def test_concurrent_submissions_with_one_key_create_one_task_and_one_audit_row(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """ADR-020's claim: the unique index serialises same-key inserts, so the audit-row
    existence check that tells created from replayed has exactly one winner even when the
    requests overlap.
    """
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:write"])
    headers = {**_auth(token), "Idempotency-Key": str(uuid.uuid4())}

    responses = await asyncio.gather(
        *(client.post("/tasks", json={"spec": "race"}, headers=headers) for _ in range(5))
    )

    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "task.submitted")
        )
    assert count == 1


async def test_another_users_idempotency_key_never_returns_their_task(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """The unique index on `idempotency_key` is global, not per user. Without an ownership
    check, replaying someone else's key hands back their task, spec included.
    """
    key = str(uuid.uuid4())
    owner_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write"]
    )
    created = await client.post(
        "/tasks",
        json={"spec": "owner secret spec"},
        headers={**_auth(owner_token), "Idempotency-Key": key},
    )
    assert created.status_code == 201

    stranger_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write"]
    )
    response = await client.post(
        "/tasks", json={"spec": "mine"}, headers={**_auth(stranger_token), "Idempotency-Key": key}
    )

    assert response.status_code == 409
    assert "owner secret spec" not in response.text
    assert created.json()["id"] not in response.text


# --- GET /tasks/{id}: ownership and 404-not-403 ---------------------------------------------


async def test_a_users_own_task_is_visible(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    created = await client.post(
        "/tasks", json={"spec": "x"}, headers={**_auth(token), "Idempotency-Key": str(uuid.uuid4())}
    )
    task_id = created.json()["id"]

    response = await client.get(f"/tasks/{task_id}", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["spec"] == "x"
    assert response.json()["cost_usd"] == "0"
    assert response.json()["iterations"] == 0


async def test_a_missing_task_is_404(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get(f"/tasks/{uuid.uuid4()}", headers=_auth(token))
    assert response.status_code == 404


async def test_another_users_task_is_404_not_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    owner_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write"]
    )
    created = await client.post(
        "/tasks",
        json={"spec": "x"},
        headers={**_auth(owner_token), "Idempotency-Key": str(uuid.uuid4())},
    )
    task_id = created.json()["id"]

    stranger_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:read"]
    )
    response = await client.get(f"/tasks/{task_id}", headers=_auth(stranger_token))
    assert response.status_code == 404
    events_response = await client.get(f"/tasks/{task_id}/events", headers=_auth(stranger_token))
    assert events_response.status_code == 404


async def test_an_admin_scope_sees_another_users_task(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    owner_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write"]
    )
    created = await client.post(
        "/tasks",
        json={"spec": "x"},
        headers={**_auth(owner_token), "Idempotency-Key": str(uuid.uuid4())},
    )
    task_id = created.json()["id"]

    admin_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:read", "admin"]
    )
    response = await client.get(f"/tasks/{task_id}", headers=_auth(admin_token))
    assert response.status_code == 200


# --- GET /tasks/{id}/events: pagination -----------------------------------------------------


async def test_events_are_paginated_by_seq(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    user = await _user(session_factory)
    token = await _user_token(session_factory, keys, user, ["tasks:write", "tasks:read"])
    created = await client.post(
        "/tasks", json={"spec": "x"}, headers={**_auth(token), "Idempotency-Key": str(uuid.uuid4())}
    )
    task_id = uuid.UUID(created.json()["id"])

    async with session_factory() as session:
        for n in range(1, 4):
            session.add(
                TaskEvent(task_id=task_id, seq=n, type=events.ITERATION_STARTED, payload={"n": n})
            )
        await session.commit()

    first_page = await client.get(
        f"/tasks/{task_id}/events", headers=_auth(token), params={"limit": 2}
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert [e["seq"] for e in body["events"]] == [1, 2]
    assert body["next_after"] == 2

    second_page = await client.get(
        f"/tasks/{task_id}/events",
        headers=_auth(token),
        params={"after": body["next_after"], "limit": 2},
    )
    body2 = second_page.json()
    assert [e["seq"] for e in body2["events"]] == [3]
    assert body2["next_after"] is None


# --- GET /audit/verify -----------------------------------------------------------------------


async def test_audit_verify_ok_on_a_clean_chain(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    # Minting the token itself already appends a "token.issued" row (identity/jwt.py), so
    # the chain is never empty by the time a bearer token exists to call this endpoint with.
    token = await _user_token(session_factory, keys, await _user(session_factory), ["audit:read"])
    async with session_factory() as session:
        await audit.append(session, actor_type="system", actor_id="seed", action="seed.0")
        await session.commit()
        expected_rows = await session.scalar(select(func.count()).select_from(AuditLog))

    response = await client.get("/audit/verify", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "rows_checked": expected_rows,
        "broken_row_id": None,
        "reason": None,
    }


async def test_audit_verify_points_at_a_tampered_row(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["audit:read"])
    async with session_factory() as session:
        first = await audit.append(session, actor_type="system", actor_id="seed", action="seed.0")
        await audit.append(session, actor_type="system", actor_id="seed", action="seed.1")
        await session.commit()
        # Superuser-style tamper, same as tests/test_audit.py: this connection is not
        # `warden_app`, so the REVOKE UPDATE on audit_log does not apply to it.
        await session.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": first.id}
        )
        await session.commit()

    response = await client.get("/audit/verify", headers=_auth(token))
    body = response.json()
    assert body["ok"] is False
    assert body["broken_row_id"] == first.id


# --- GET /healthz: no auth ------------------------------------------------------------------


async def test_healthz_needs_no_token(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
