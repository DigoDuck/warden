"""GET /metrics/summary through ASGI with a real test database: scope, visibility, period and
strategy filters, and the numbers themselves on rows written by hand (so the expected value
is arithmetic done here, not a call to the code under test)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import identity
from warden.api.app import create_app
from warden.core import events
from warden.identity.jwt import KeyPair
from warden.models import ModelCall, Task, ToolCall, User

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, keys=keys)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def clean(session: AsyncSession) -> AsyncIterator[None]:
    wipe = text(
        "TRUNCATE audit_log, issued_tokens, task_events, model_calls, tasks, users "
        "RESTART IDENTITY CASCADE"
    )
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


async def _user(sf: async_sessionmaker[AsyncSession]) -> User:
    async with sf() as s:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        s.add(user)
        await s.commit()
        return user


async def _headers(
    sf: async_sessionmaker[AsyncSession], keys: KeyPair, user: User, scopes: list[str]
) -> dict[str, str]:
    async with sf() as s:
        token = await identity.issue_user_token(s, keys, user, scopes=scopes)
        await s.commit()
    return {"Authorization": f"Bearer {token}"}


async def _task(
    sf: async_sessionmaker[AsyncSession],
    user: User,
    status: str,
    *,
    cost: str | None = None,
    seconds: int = 10,
    finished_at: datetime = NOW,
    strategy: str | None = None,
) -> uuid.UUID:
    async with sf() as s:
        task = Task(
            idempotency_key=str(uuid.uuid4()),
            user_id=user.id,
            spec="x",
            status=status,
            routing_strategy=strategy,
            started_at=finished_at - timedelta(seconds=seconds),
            finished_at=finished_at,
        )
        s.add(task)
        await s.flush()
        if cost is not None:
            s.add(
                ModelCall(
                    task_id=task.id,
                    provider="fake",
                    model="m",
                    purpose="coder",
                    cost_usd=Decimal(cost),
                    tokens_in=100,
                    tokens_out=10,
                )
            )
        await s.commit()
        return task.id


async def test_a_request_without_a_token_is_401(client: AsyncClient) -> None:
    assert (await client.get("/metrics/summary")).status_code == 401


async def test_a_token_without_tasks_read_is_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    headers = await _headers(
        session_factory, keys, await _user(session_factory), ["approvals:read"]
    )
    assert (await client.get("/metrics/summary", headers=headers)).status_code == 403


async def test_a_period_with_no_tasks_is_an_empty_summary_not_an_error(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    headers = await _headers(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get("/metrics/summary", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tasks"] == 0
    assert body["success_rate"] is None and body["cost_per_success"] is None
    assert Decimal(body["cost_total"]) == 0
    assert body["by_status"]["SUCCEEDED"] == 0


async def test_the_numbers_on_a_hand_built_set(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    user = await _user(session_factory)
    ok = await _task(session_factory, user, "SUCCEEDED", cost="0.10", seconds=10)
    await _task(session_factory, user, "FAILED", cost="0.30", seconds=30)
    await _task(session_factory, user, "RUNNING", cost="9.00")  # not terminal: ignored
    async with session_factory() as s:
        await events.append_event(s, ok, events.ITERATION_STARTED, {"n": 1})
        await events.append_event(s, ok, events.TOOL_REQUESTED, {"id": "a", "tool": "read_file"})
        await events.append_event(s, ok, events.POLICY_DECIDED, {"id": "a", "effect": "allow"})
        await events.append_event(s, ok, events.POLICY_DECIDED, {"id": "b", "effect": "deny"})
        for name, error in (("read_file", None), ("finish", "ignored"), ("run_command", "boom")):
            s.add(
                ToolCall(
                    task_id=ok,
                    iteration=1,
                    tool_name=name,
                    args_hash="h",
                    decision="allow",
                    error=error,
                )
            )
        await s.commit()

    headers = await _headers(session_factory, keys, user, ["tasks:read"])
    body = (await client.get("/metrics/summary", headers=headers)).json()

    assert body["tasks"] == 2
    assert body["by_status"] == {
        "SUCCEEDED": 1,
        "FAILED": 1,
        "CANCELLED": 0,
        "TIMED_OUT": 0,
        "BUDGET_EXCEEDED": 0,
    }
    assert body["success_rate"] == 0.5
    assert Decimal(body["cost_total"]) == Decimal("0.40")
    assert Decimal(body["cost_per_task"]) == Decimal("0.20")
    assert Decimal(body["cost_per_success"]) == Decimal("0.40")
    assert (body["tokens_in_per_task"], body["tokens_out_per_task"]) == (100.0, 10.0)
    assert (body["latency_p50_s"], body["latency_p95_s"]) == (10.0, 30.0)
    assert body["avg_iterations"] == 0.5
    assert body["tool_calls_per_task"] == 0.5
    assert body["tool_failure_rate"] == 0.5  # run_command failed of read_file + run_command
    assert body["deny_rate"] == 0.5


async def test_a_user_sees_only_their_own_tasks_and_admin_sees_all(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    alice, bob = await _user(session_factory), await _user(session_factory)
    await _task(session_factory, alice, "SUCCEEDED", cost="0.10")
    await _task(session_factory, bob, "FAILED", cost="0.50")
    await _task(session_factory, bob, "FAILED", cost="0.50")

    as_alice = await _headers(session_factory, keys, alice, ["tasks:read"])
    mine = (await client.get("/metrics/summary", headers=as_alice)).json()
    assert (mine["tasks"], Decimal(mine["cost_total"])) == (1, Decimal("0.10"))

    admin = await _headers(
        session_factory, keys, await _user(session_factory), ["tasks:read", "admin"]
    )
    everyone = (await client.get("/metrics/summary", headers=admin)).json()
    assert (everyone["tasks"], Decimal(everyone["cost_total"])) == (3, Decimal("1.10"))


async def test_the_period_is_filtered_on_when_the_task_finished(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    user = await _user(session_factory)
    for days_ago in (1, 5, 10):
        await _task(session_factory, user, "SUCCEEDED", finished_at=NOW - timedelta(days=days_ago))
    headers = await _headers(session_factory, keys, user, ["tasks:read"])

    async def count(**params: str) -> int:
        response = await client.get("/metrics/summary", headers=headers, params=params)
        assert response.status_code == 200
        return int(response.json()["tasks"])

    assert await count() == 3
    assert await count(**{"from": (NOW - timedelta(days=7)).isoformat()}) == 2
    assert await count(to=(NOW - timedelta(days=7)).isoformat()) == 1
    window = {
        "from": (NOW - timedelta(days=6)).isoformat(),
        "to": (NOW - timedelta(days=2)).isoformat(),
    }
    assert await count(**window) == 1


async def test_a_backwards_period_is_a_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    headers = await _headers(session_factory, keys, await _user(session_factory), ["tasks:read"])
    response = await client.get(
        "/metrics/summary",
        headers=headers,
        params={"from": NOW.isoformat(), "to": (NOW - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 422


async def test_strategy_filters_by_the_tasks_routing_strategy(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    user = await _user(session_factory)
    await _task(session_factory, user, "SUCCEEDED", strategy="frontier_always")
    await _task(session_factory, user, "SUCCEEDED", strategy="cheap_first")
    await _task(session_factory, user, "FAILED", strategy="cheap_first")
    headers = await _headers(session_factory, keys, user, ["tasks:read"])

    response = await client.get(
        "/metrics/summary", headers=headers, params={"strategy": "cheap_first"}
    )
    assert response.json()["tasks"] == 2
