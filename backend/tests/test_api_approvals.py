"""The HTTP surface for approvals (ADR-022): GET /approvals, POST .../approve, .../reject.

Same ASGI-through-httpx shape as test_api.py: a request through the app opens its own
session (`warden.api.deps.get_session`), so a user, task or approval has to be genuinely
committed through `session_factory` before the app's own session can see it. No loop, no
Docker here: `core/approvals.py`'s own guarantees (the guarded UPDATE, the note requirement)
are covered in test_approvals.py, and the loop's side of pausing in test_loop.py. This file
is only the route: scopes, status mapping, and the shape of what comes back.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import identity
from warden.api.app import create_app
from warden.core import queue
from warden.identity.jwt import KeyPair, _kid_for
from warden.models import Approval, Task, User


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


@pytest.fixture(autouse=True)
async def clean_committed_rows(session: AsyncSession) -> AsyncIterator[None]:
    wipe = text(
        "TRUNCATE audit_log, issued_tokens, task_events, approvals, model_calls, tasks, users "
        "RESTART IDENTITY CASCADE"
    )
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def _waiting_task_with_pending_approval(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """A task parked WAITING_APPROVAL with one pending approval, committed for real."""
    async with session_factory() as session:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        session.add(user)
        await session.flush()
        await queue.enqueue(
            session, user_id=user.id, spec="open a pull request", idempotency_key=str(uuid.uuid4())
        )
        await session.commit()
        claimed = await queue.claim(session, "worker-a")
        assert claimed is not None
        claimed.status = "WAITING_APPROVAL"
        claimed.claimed_by = None
        claimed.claimed_until = None
        approval = Approval(
            task_id=claimed.id,
            tool_call_id="call-a",
            tool="github.open_pr",
            args_safe={"title": "x", "github_token": "[redacted]"},
            matched_rules=["needs-human"],
            reason="opening a pull request is visible outside the control plane",
            scopes=["github:pr:open"],
            status="pending",
        )
        session.add(approval)
        await session.flush()
        await session.commit()
        return claimed.id, approval.id


# --- scopes -----------------------------------------------------------------------------


async def test_listing_with_no_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/approvals")
    assert response.status_code == 401


async def test_listing_without_the_read_scope_is_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get("/approvals", headers=_auth(token))
    assert response.status_code == 403


async def test_deciding_without_the_decide_scope_is_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    _, approval_id = await _waiting_task_with_pending_approval(session_factory)
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:read"]
    )
    response = await client.post(
        f"/approvals/{approval_id}/approve", json={"note": None}, headers=_auth(token)
    )
    assert response.status_code == 403


# --- GET /approvals?status=pending -------------------------------------------------------


async def test_listing_returns_the_pending_approval_with_its_fields(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    task_id, approval_id = await _waiting_task_with_pending_approval(session_factory)
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:read"]
    )

    response = await client.get("/approvals", headers=_auth(token))

    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == [str(approval_id)]
    row = rows[0]
    assert row["task_id"] == str(task_id)
    assert row["tool"] == "github.open_pr"
    assert row["args_safe"]["github_token"] == "[redacted]"
    assert row["matched_rules"] == ["needs-human"]
    assert row["reason"] == "opening a pull request is visible outside the control plane"
    assert "requested_at" in row


async def test_listing_never_includes_an_already_decided_approval(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    _, approval_id = await _waiting_task_with_pending_approval(session_factory)
    async with session_factory() as session:
        row = await session.get(Approval, approval_id)
        assert row is not None
        row.status = "approved"
        await session.commit()

    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:read"]
    )
    response = await client.get("/approvals", headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == []


# --- POST /approvals/{id}/approve and /reject ---------------------------------------------


async def test_approving_resumes_the_task_to_queued(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    task_id, approval_id = await _waiting_task_with_pending_approval(session_factory)
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:decide"]
    )

    response = await client.post(
        f"/approvals/{approval_id}/approve", json={"note": None}, headers=_auth(token)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None and task.status == "QUEUED"


async def test_rejecting_without_a_note_is_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    _, approval_id = await _waiting_task_with_pending_approval(session_factory)
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:decide"]
    )

    response = await client.post(
        f"/approvals/{approval_id}/reject", json={"note": None}, headers=_auth(token)
    )

    assert response.status_code == 422


async def test_rejecting_with_a_note_is_accepted(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    task_id, approval_id = await _waiting_task_with_pending_approval(session_factory)
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:decide"]
    )

    response = await client.post(
        f"/approvals/{approval_id}/reject",
        json={"note": "too risky right now"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None and task.status == "QUEUED"


async def test_deciding_an_unknown_approval_is_404(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:decide"]
    )
    response = await client.post(
        f"/approvals/{uuid.uuid4()}/approve", json={"note": None}, headers=_auth(token)
    )
    assert response.status_code == 404


async def test_deciding_an_already_decided_approval_is_409(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    _, approval_id = await _waiting_task_with_pending_approval(session_factory)
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["approvals:decide"]
    )
    first = await client.post(
        f"/approvals/{approval_id}/approve", json={"note": None}, headers=_auth(token)
    )
    assert first.status_code == 200

    second = await client.post(
        f"/approvals/{approval_id}/approve", json={"note": None}, headers=_auth(token)
    )
    assert second.status_code == 409
