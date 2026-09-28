"""GET /tasks/{id}/stream: Server-Sent Events of a task's event log.

Same fixtures as test_api_tasks_list.py (duplicated, not imported: matches the existing
split between test_api.py and test_api_approvals.py). `_STREAM_POLL_SECONDS` is monkeypatched
down in every test that needs the poll loop to actually run more than once, so the suite
does not spend real wall-clock time waiting on the endpoint's 1s default.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import identity
from warden.api import routes_tasks
from warden.api.app import create_app
from warden.core import events as core_events
from warden.identity.jwt import KeyPair
from warden.models import TaskEvent, User


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


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default 1s poll would make every test here slow for no reason; tests that care
    # about timing (the no-held-transaction one) still control their own sleeps around this.
    monkeypatch.setattr(routes_tasks, "_STREAM_POLL_SECONDS", 0.02)
    monkeypatch.setattr(routes_tasks, "_STREAM_HEARTBEAT_SECONDS", 60.0)


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


async def _read_sse_events(response: Response) -> list[dict[str, str]]:
    """Group raw SSE lines into `{id, event, data}` dicts, one per blank-line-terminated block.

    A leading `:` line (a heartbeat/comment) carries no field and is skipped.
    """
    out: list[dict[str, str]] = []
    current: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line == "":
            if current:
                out.append(current)
                current = {}
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        current[field] = value
    if current:
        out.append(current)
    return out


# --- auth/visibility: same rules as every other /tasks/{id} route -----------------------


async def test_stream_without_bearer_token_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)
    response = await client.get(f"/tasks/{task_id}/stream")
    assert response.status_code == 401


async def test_stream_of_a_missing_task_is_404(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get(f"/tasks/{uuid.uuid4()}/stream", headers=_auth(token))
    assert response.status_code == 404


async def test_stream_of_another_users_task_is_404(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    owner_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write"]
    )
    task_id = await _submit(client, owner_token)

    stranger_token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:read"]
    )
    response = await client.get(f"/tasks/{task_id}/stream", headers=_auth(stranger_token))
    assert response.status_code == 404


# --- content and resume ------------------------------------------------------------------


async def test_stream_ends_after_the_terminal_task_finished_event(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)
    async with session_factory() as session:
        session.add_all(
            [
                TaskEvent(
                    task_id=uuid.UUID(task_id),
                    seq=1,
                    type="policy.decided",
                    payload={"effect": "deny", "matched_rules": ["no-network"]},
                ),
                TaskEvent(
                    task_id=uuid.UUID(task_id),
                    seq=2,
                    type=core_events.TASK_FINISHED,
                    payload={"status": "SUCCEEDED"},
                ),
            ]
        )
        await session.commit()

    async def run() -> list[dict[str, str]]:
        async with client.stream(
            "GET", f"/tasks/{task_id}/stream", headers=_auth(token)
        ) as response:
            assert response.status_code == 200
            return await _read_sse_events(response)

    received = await asyncio.wait_for(run(), timeout=5)

    assert [e["id"] for e in received] == ["1", "2"]
    assert received[0]["event"] == "policy.decided"
    assert received[1]["event"] == core_events.TASK_FINISHED


async def test_stream_resumes_after_last_event_id(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)
    async with session_factory() as session:
        session.add_all(
            [
                TaskEvent(task_id=uuid.UUID(task_id), seq=1, type="iteration.started", payload={}),
                TaskEvent(task_id=uuid.UUID(task_id), seq=2, type="policy.decided", payload={}),
                TaskEvent(
                    task_id=uuid.UUID(task_id),
                    seq=3,
                    type=core_events.TASK_FINISHED,
                    payload={"status": "SUCCEEDED"},
                ),
            ]
        )
        await session.commit()

    async def run() -> list[dict[str, str]]:
        async with client.stream(
            "GET",
            f"/tasks/{task_id}/stream",
            headers={**_auth(token), "Last-Event-ID": "1"},
        ) as response:
            assert response.status_code == 200
            return await _read_sse_events(response)

    received = await asyncio.wait_for(run(), timeout=5)

    # Never seq 1 again: that is the whole point of Last-Event-ID.
    assert [e["id"] for e in received] == ["2", "3"]


async def test_stream_does_not_hold_a_transaction_open_between_polls(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """ADR-019's rule applied to the stream: each poll must use its own short session, never
    one held for the connection's whole life. Proved by causing, not simulating, the failure
    this would produce: if the stream held a transaction touching the task row (or a lock on
    it), a concurrent UPDATE of that same row would hang until the stream disconnected.
    """
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)

    async def consume() -> None:
        async with client.stream(
            "GET", f"/tasks/{task_id}/stream", headers=_auth(token)
        ) as response:
            async for _ in response.aiter_lines():
                pass  # never emits (no task.finished): drained until cancelled below

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.2)  # let the stream get past its first poll, into the wait

        async with session_factory() as session:
            await asyncio.wait_for(
                session.execute(
                    text("UPDATE tasks SET spec = 'nudged' WHERE id = :id"), {"id": task_id}
                ),
                timeout=2,
            )
            await session.commit()
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


async def test_stream_ends_promptly_when_resuming_at_or_after_task_finished(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """A client that reconnects with Last-Event-ID already at (or past) the seq of
    task.finished has nothing left to receive, and the task is already terminal: the stream
    must end on its very next empty poll instead of polling forever, since it will never see
    a fresh task.finished event to trigger the old "only stop on that event" exit.
    """
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)
    async with session_factory() as session:
        session.add(
            TaskEvent(
                task_id=uuid.UUID(task_id),
                seq=1,
                type=core_events.TASK_FINISHED,
                payload={"status": "SUCCEEDED"},
            )
        )
        await session.execute(
            text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": task_id}
        )
        await session.commit()

    async def run() -> list[dict[str, str]]:
        async with client.stream(
            "GET",
            f"/tasks/{task_id}/stream",
            headers={**_auth(token), "Last-Event-ID": "1"},
        ) as response:
            assert response.status_code == 200
            return await _read_sse_events(response)

    received = await asyncio.wait_for(run(), timeout=2)
    assert received == []


async def test_stream_of_a_terminal_task_without_a_task_finished_event_ends(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """A task can be terminal without task.finished ever having been written (data fixed up
    by hand is the realistic case, but the stream must not rely on that event existing at
    all): the stream must still end instead of polling this task forever.
    """
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE tasks SET status = 'FAILED' WHERE id = :id"), {"id": task_id}
        )
        await session.commit()

    async def run() -> list[dict[str, str]]:
        async with client.stream(
            "GET", f"/tasks/{task_id}/stream", headers=_auth(token)
        ) as response:
            assert response.status_code == 200
            return await _read_sse_events(response)

    received = await asyncio.wait_for(run(), timeout=2)
    assert received == []


async def test_stream_releases_the_request_session_before_streaming(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    """The auth + visibility checks run on the request's SessionDep, and FastAPI (>= 0.118)
    only exits a yield dependency after the response has been fully sent. For a stream that
    is the life of the browser tab: left alone, that session sits "idle in transaction"
    holding ACCESS SHARE on `tasks` (from the visibility SELECT) and a pool connection.

    Caused, not simulated: an ACCESS EXCLUSIVE lock on `tasks` (what an ALTER TABLE in a
    migration takes) must be grantable while a stream is open. A plain UPDATE, as in the test
    above, does not conflict with ACCESS SHARE, so it cannot catch this.
    """
    token = await _user_token(
        session_factory, keys, await _user(session_factory), ["tasks:write", "tasks:read"]
    )
    task_id = await _submit(client, token)

    async def consume() -> None:
        async with client.stream(
            "GET", f"/tasks/{task_id}/stream", headers=_auth(token)
        ) as response:
            async for _ in response.aiter_lines():
                pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.2)
        async with session_factory() as session:
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            await session.execute(text("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE"))
            await session.rollback()
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
