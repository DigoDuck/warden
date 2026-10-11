"""Parity: for the same tasks, the Metrics endpoint and the eval runner publish the same numbers.

This replaces the week 7 criterion "the screen reflects docs/metrics.md", which cannot be
checked (the evals run in throwaway databases and their success depends on a hidden test).
What can be checked is that both sides compute from the same rows with the same formulas:
tasks are written by hand into the scratch database, the numbers are read once through
`GET /metrics/summary` and once the way the runner does (`coding.read_back` ->
`ItemResult` -> `summarize_items`), and compared. The rows are written here, not by a worker,
so the expected figures do not depend on a model or on Docker.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from warden import identity
from warden.api.app import create_app
from warden.core import events
from warden.core.worker import POLICY_FILE
from warden.identity.jwt import KeyPair
from warden.models import Approval, ModelCall, Task, ToolCall, User
from warden.policy.engine import load_policy

from evals import coding, runner
from evals.coding_checks import ItemResult, summarize_items

aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[runner.Context]:
    session_factory, url = await runner.prepare_database()
    yield runner.Context(
        session_factory=session_factory,
        keys=runner.ephemeral_keys(),
        policy=load_policy(POLICY_FILE),
        tmp_dir=tmp_path_factory.mktemp("parity"),
        test_db_url=url,
    )


async def _seed_task(
    sf: async_sessionmaker[AsyncSession],
    user: User,
    status: str,
    *,
    seconds: int,
    calls: tuple[tuple[str, str, int, int], ...],
    iterations: int,
    requested: int,
    decided: tuple[str, ...],
    tools: tuple[tuple[str, str, str | None], ...],
    approvals: int = 0,
) -> uuid.UUID:
    finished = datetime.now(UTC)
    async with sf() as s:
        task = Task(
            idempotency_key=str(uuid.uuid4()),
            user_id=user.id,
            spec="x",
            status=status,
            started_at=finished - timedelta(seconds=seconds),
            finished_at=finished,
        )
        s.add(task)
        await s.flush()
        for purpose, cost, tin, tout in calls:
            s.add(
                ModelCall(
                    task_id=task.id,
                    provider="fake",
                    model="m",
                    purpose=purpose,
                    cost_usd=Decimal(cost),
                    tokens_in=tin,
                    tokens_out=tout,
                )
            )
        for n in range(iterations):
            await events.append_event(s, task.id, events.ITERATION_STARTED, {"n": n + 1})
        for n in range(requested):
            await events.append_event(
                s, task.id, events.TOOL_REQUESTED, {"id": f"c{n}", "tool": "read_file"}
            )
        for n, effect in enumerate(decided):
            await events.append_event(
                s, task.id, events.POLICY_DECIDED, {"id": f"c{n}", "effect": effect}
            )
        for name, decision, error in tools:
            s.add(
                ToolCall(
                    task_id=task.id,
                    iteration=1,
                    tool_name=name,
                    args_hash="h",
                    decision=decision,
                    error=error,
                )
            )
        for n in range(approvals):
            s.add(
                Approval(
                    task_id=task.id,
                    tool_call_id=f"a{n}",
                    tool="github.open_pr",
                    reason="r",
                    status="approved",
                )
            )
        await s.commit()
        return task.id


@aio
async def test_the_endpoint_and_the_runner_publish_identical_numbers(ctx: runner.Context) -> None:
    sf = ctx.session_factory
    async with sf() as s:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        s.add(user)
        await s.commit()

    terminal = [
        await _seed_task(
            sf,
            user,
            "SUCCEEDED",
            seconds=12,
            calls=(("coder", "0.1234", 1000, 100), ("reviewer", "0.0200", 300, 30)),
            iterations=3,
            requested=4,
            decided=("allow", "allow", "allow", "deny"),
            tools=(("read_file", "allow", None), ("finish", "allow", None)),
            approvals=1,
        ),
        await _seed_task(
            sf,
            user,
            "FAILED",
            seconds=47,
            calls=(("coder", "0.3000", 2000, 250),),
            iterations=5,
            requested=2,
            decided=("allow", "allow"),
            tools=(("run_command", "allow", "exit 1"),),
        ),
        await _seed_task(
            sf,
            user,
            "TIMED_OUT",
            seconds=300,
            calls=(),
            iterations=1,
            requested=0,
            decided=(),
            tools=(),
        ),
    ]
    # Not terminal: neither side may count it.
    await _seed_task(
        sf, user, "RUNNING", seconds=5, calls=(("coder", "9.0", 1, 1),),
        iterations=1, requested=1, decided=("allow",), tools=(),
    )  # fmt: skip

    # The runner's way: read each task back, publish it as an ItemResult, summarize.
    results = []
    for task_id in terminal:
        back = await coding.read_back(ctx, task_id)
        results.append(
            ItemResult(
                id=str(task_id),
                state="done",
                status=back.facts.status,
                success=back.facts.status == "SUCCEEDED",
                cost_usd=back.cost_usd,
                tokens_in=back.tokens_in,
                tokens_out=back.tokens_out,
                latency_s=back.latency_s,
                iterations=back.iterations,
                tool_calls=back.tool_calls,
            )
        )
    published = summarize_items(results)

    # The screen's way: an admin token (the seeded user is nobody's) over the whole database.
    async with sf() as s:
        admin = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        s.add(admin)
        await s.flush()
        token = await identity.issue_user_token(
            s, ctx.keys, admin, scopes=["tasks:read", "admin"]
        )
        await s.commit()
    app = create_app(session_factory=sf, keys=ctx.keys)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/metrics/summary", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    screen = response.json()

    assert screen["tasks"] == published.executed == 3
    assert screen["successes"] == published.successes == 1
    assert screen["success_rate"] == published.success_rate
    assert Decimal(screen["cost_total"]) == published.cost_total == Decimal("0.4434")
    assert Decimal(screen["cost_per_task"]) == published.cost_per_task
    assert Decimal(screen["cost_per_success"]) == published.cost_per_success
    assert screen["latency_p50_s"] == published.latency_p50 == 47.0
    assert screen["latency_p95_s"] == published.latency_p95 == 300.0
    # Per-task averages the runner does not print in its table but does measure per item.
    n = len(results)
    assert screen["tokens_in_per_task"] == sum(r.tokens_in for r in results) / n
    assert screen["tokens_out_per_task"] == sum(r.tokens_out for r in results) / n
    assert screen["avg_iterations"] == sum(r.iterations for r in results) / n
    assert screen["tool_calls_per_task"] == sum(r.tool_calls for r in results) / n
    # And the two figures only the screen has, from the same rows, checked by hand.
    assert screen["tool_failure_rate"] == 0.5  # read_file ok, run_command failed; finish ignored
    assert screen["deny_rate"] == 1 / 6  # 1 deny among 6 policy.decided events
