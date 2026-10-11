"""The metric formulas (briefing §32): pure table cases, then the same numbers read back from
real rows. The API and the eval runner are pinned to these definitions elsewhere
(test_api_metrics.py, evals/tests/test_metrics_parity.py)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.core import events
from warden.metrics.summary import RunMetrics, percentile, read_runs, summarize
from warden.models import Approval, ModelCall, Task, ToolCall, User


def run(**overrides: Any) -> RunMetrics:
    base: dict[str, Any] = {
        "status": "SUCCEEDED",
        "success": True,
        "cost_usd": Decimal("0.10"),
        "tokens_in": 100,
        "tokens_out": 10,
        "latency_s": 10.0,
        "iterations": 2,
        "tool_calls": 3,
        "tool_attempts": 3,
        "tool_failures": 0,
        "interventions": 0,
        "decisions": 3,
        "denies": 0,
    }
    base.update(overrides)
    return RunMetrics(**base)


def failed(**overrides: Any) -> RunMetrics:
    return run(status="FAILED", success=False, **overrides)


# --- percentile ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([], 50, None),
        ([7.0], 95, 7.0),
        ([float(n) for n in range(1, 11)], 50, 5.0),
        ([float(n) for n in range(1, 11)], 95, 10.0),
        ([3.0, 1.0, 2.0], 50, 2.0),  # unsorted input
    ],
)
def test_percentile_is_nearest_rank(values: list[float], p: int, expected: float | None) -> None:
    assert percentile(values, p) == expected


# --- summarize -------------------------------------------------------------------------------


def test_empty_period_has_no_rates_only_zero_counts() -> None:
    s = summarize([])
    assert s.tasks == 0 and s.successes == 0
    assert s.by_status == {
        "SUCCEEDED": 0,
        "FAILED": 0,
        "CANCELLED": 0,
        "TIMED_OUT": 0,
        "BUDGET_EXCEEDED": 0,
    }
    assert s.cost_total == Decimal(0)
    for none in (
        s.success_rate,
        s.cost_per_task,
        s.cost_per_success,
        s.tokens_in_per_task,
        s.tokens_out_per_task,
        s.latency_p50,
        s.latency_p95,
        s.avg_iterations,
        s.tool_calls_per_task,
        s.tool_failure_rate,
        s.interventions_per_task,
        s.deny_rate,
    ):
        assert none is None


def test_zero_successes_has_a_cost_per_task_but_no_cost_per_success() -> None:
    s = summarize([failed(cost_usd=Decimal("0.30")), failed(cost_usd=Decimal("0.10"))])
    assert s.success_rate == 0.0
    assert s.cost_per_task == Decimal("0.20")
    assert s.cost_per_success is None


def test_a_failed_attempt_is_part_of_the_price_of_a_success() -> None:
    s = summarize([run(cost_usd=Decimal("0.10")), failed(cost_usd=Decimal("0.30"))])
    assert s.success_rate == 0.5
    assert s.cost_total == Decimal("0.40")
    assert s.cost_per_success == Decimal("0.40")  # total over successes, not over tasks
    assert s.cost_per_task == Decimal("0.20")


def test_non_terminal_runs_are_left_out_of_every_number() -> None:
    s = summarize([run(), run(status="RUNNING", success=False, cost_usd=Decimal("99"))])
    assert s.tasks == 1
    assert s.cost_total == Decimal("0.10")
    assert s.by_status["SUCCEEDED"] == 1


def test_counts_by_status() -> None:
    s = summarize([run(), failed(), failed(), run(status="CANCELLED", success=False)])
    assert s.by_status == {
        "SUCCEEDED": 1,
        "FAILED": 2,
        "CANCELLED": 1,
        "TIMED_OUT": 0,
        "BUDGET_EXCEEDED": 0,
    }


def test_averages_per_task() -> None:
    s = summarize(
        [
            run(tokens_in=100, tokens_out=10, iterations=2, tool_calls=4, interventions=1),
            run(tokens_in=300, tokens_out=30, iterations=4, tool_calls=2, interventions=0),
        ]
    )
    assert (s.tokens_in_per_task, s.tokens_out_per_task) == (200.0, 20.0)
    assert s.avg_iterations == 3.0
    assert s.tool_calls_per_task == 3.0
    assert s.interventions_per_task == 0.5


def test_latency_percentiles_skip_tasks_without_one() -> None:
    s = summarize([run(latency_s=float(n)) for n in range(1, 11)] + [run(latency_s=None)])
    assert (s.latency_p50, s.latency_p95) == (5.0, 10.0)


def test_failure_and_deny_rates_are_over_their_own_denominators() -> None:
    s = summarize(
        [
            run(tool_attempts=3, tool_failures=1, decisions=4, denies=1),
            run(tool_attempts=1, tool_failures=0, decisions=0, denies=0),
        ]
    )
    assert s.tool_failure_rate == 0.25
    assert s.deny_rate == 0.25


def test_rates_are_none_when_nothing_was_attempted_or_decided() -> None:
    s = summarize([run(tool_attempts=0, tool_failures=0, decisions=0, denies=0)])
    assert s.tool_failure_rate is None and s.deny_rate is None


# --- read_runs: the same definitions, from real rows -------------------------------------------


@pytest.fixture(autouse=True)
async def clean(session: AsyncSession) -> AsyncIterator[None]:
    wipe = text("TRUNCATE task_events, model_calls, tasks, users RESTART IDENTITY CASCADE")
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


async def _task(sf: async_sessionmaker[AsyncSession], status: str) -> uuid.UUID:
    async with sf() as s:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        s.add(user)
        await s.flush()
        now = datetime.now(UTC)
        task = Task(
            idempotency_key=str(uuid.uuid4()),
            user_id=user.id,
            spec="x",
            status=status,
            started_at=now - timedelta(seconds=30),
            finished_at=now,
        )
        s.add(task)
        await s.commit()
        return task.id


async def test_read_runs_counts_what_each_formula_needs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(session_factory, "SUCCEEDED")
    async with session_factory() as s:
        # Every purpose counts toward cost: the reviewer and the planner are spend too.
        for purpose, cost, tin, tout in (("coder", "0.10", 100, 10), ("reviewer", "0.05", 50, 5)):
            s.add(
                ModelCall(
                    task_id=task_id,
                    provider="fake",
                    model="m",
                    purpose=purpose,
                    cost_usd=Decimal(cost),
                    tokens_in=tin,
                    tokens_out=tout,
                )
            )
        for n in (1, 2):
            await events.append_event(s, task_id, events.ITERATION_STARTED, {"n": n})
        # Two requested calls (finish never emits `tool.requested`), three decisions, one deny.
        for call_id, effect in (("a", "allow"), ("b", "deny"), ("c", "allow")):
            if call_id != "c":
                await events.append_event(
                    s, task_id, events.TOOL_REQUESTED, {"id": call_id, "tool": "read_file"}
                )
            await events.append_event(
                s, task_id, events.POLICY_DECIDED, {"id": call_id, "effect": effect}
            )
        rows: tuple[tuple[str, str, str | None], ...] = (
            ("read_file", "allow", None),
            ("run_command", "allow", "exit 1"),  # a failure
            ("read_file", "deny", "denied"),  # a deny is the policy working, not a tool failing
            ("finish", "allow", None),  # the loop's own tool: never counted
            ("finish", "allow", "boom"),
        )
        for name, decision, error in rows:
            s.add(
                ToolCall(
                    task_id=task_id,
                    iteration=1,
                    tool_name=name,
                    args_hash="h",
                    decision=decision,
                    error=error,
                )
            )
        s.add(
            Approval(
                task_id=task_id,
                tool_call_id="x",
                tool="github.open_pr",
                reason="r",
                status="approved",
            )
        )
        await s.commit()

    async with session_factory() as s:
        (got,) = await read_runs(s, Task.id == task_id)

    assert got.status == "SUCCEEDED" and got.success
    assert got.cost_usd == Decimal("0.15")
    assert (got.tokens_in, got.tokens_out) == (150, 15)
    assert got.latency_s is not None and 29 < got.latency_s < 31
    assert got.iterations == 2
    assert got.tool_calls == 2
    assert (got.tool_attempts, got.tool_failures) == (2, 1)
    assert got.interventions == 1
    assert (got.decisions, got.denies) == (3, 1)


async def test_read_runs_gives_a_bare_task_zeros_and_no_latency_when_unfinished(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(session_factory, "FAILED")
    async with session_factory() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.finished_at = None
        await s.commit()
        (got,) = await read_runs(s, Task.id == task_id)
    assert got.cost_usd == Decimal(0) and got.latency_s is None
    assert (got.iterations, got.tool_calls, got.interventions, got.decisions) == (0, 0, 0, 0)
    assert not got.success


async def test_read_runs_only_returns_terminal_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _task(session_factory, "SUCCEEDED")
    await _task(session_factory, "RUNNING")
    await _task(session_factory, "WAITING_APPROVAL")
    async with session_factory() as s:
        runs = await read_runs(s)
    assert [r.status for r in runs] == ["SUCCEEDED"]
