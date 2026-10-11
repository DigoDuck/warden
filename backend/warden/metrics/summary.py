"""The metric definitions (briefing §32), in one place.

Two callers compute the same numbers from the same rows: `GET /metrics/summary` over every
task in a period, and the capability eval runner (`evals/coding.py`) over the tasks of one
run. Both go through `read_runs` (rows -> `RunMetrics`) and `summarize` (`RunMetrics` ->
`MetricsSummary`), so a formula has exactly one definition and "the screen and the runner
agree" is true by construction, then pinned by evals/tests/test_metrics_parity.py.
"""

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.core import events
from warden.core.worker import TERMINAL_STATUSES
from warden.models import TASK_STATUSES, Approval, ModelCall, Task, TaskEvent, ToolCall

# Only terminal tasks count: one still running has no latency, a partial cost and an unknown
# outcome, so averaging it in would make every number a lie. In the state machine's order.
_TERMINAL_ORDER = tuple(s for s in TASK_STATUSES if s in TERMINAL_STATUSES)
# The loop's own exit, not a tool the agent used: it never emits `tool.requested`.
_FINISH_TOOL = "finish"


@dataclass(frozen=True)
class RunMetrics:
    """What one task did, already counted. The raw material of every formula below."""

    status: str
    # Not `status == "SUCCEEDED"` on purpose: the capability evals only call a run a success
    # when the hidden acceptance test also passed, so each caller says what success means.
    success: bool
    cost_usd: Decimal
    tokens_in: int
    tokens_out: int
    latency_s: float | None
    iterations: int
    tool_calls: int
    tool_attempts: int
    tool_failures: int
    interventions: int
    decisions: int
    denies: int


@dataclass(frozen=True)
class MetricsSummary:
    tasks: int
    by_status: dict[str, int]
    successes: int
    success_rate: float | None
    cost_total: Decimal
    cost_per_task: Decimal | None
    cost_per_success: Decimal | None
    tokens_in_per_task: float | None
    tokens_out_per_task: float | None
    latency_p50: float | None
    latency_p95: float | None
    avg_iterations: float | None
    tool_calls_per_task: float | None
    tool_failure_rate: float | None
    interventions_per_task: float | None
    deny_rate: float | None


def percentile(values: Sequence[float], p: int) -> float | None:
    """Nearest-rank percentile: the smallest value with at least p% of the sample at or below
    it. No interpolation, so the number is always one that was actually measured."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(runs: Sequence[RunMetrics]) -> MetricsSummary:
    done = [r for r in runs if r.status in TERMINAL_STATUSES]
    n = len(done)
    successes = sum(1 for r in done if r.success)
    total = sum((r.cost_usd for r in done), Decimal(0))
    latencies = [r.latency_s for r in done if r.latency_s is not None]

    def per_task(values: Sequence[int]) -> float | None:
        return sum(values) / n if n else None

    return MetricsSummary(
        tasks=n,
        by_status={s: sum(1 for r in done if r.status == s) for s in _TERMINAL_ORDER},
        successes=successes,
        success_rate=_ratio(successes, n),
        cost_total=total,
        cost_per_task=total / n if n else None,
        # Everything spent over what worked: a failed attempt is part of the price of a success.
        cost_per_success=total / successes if successes else None,
        tokens_in_per_task=per_task([r.tokens_in for r in done]),
        tokens_out_per_task=per_task([r.tokens_out for r in done]),
        latency_p50=percentile(latencies, 50),
        latency_p95=percentile(latencies, 95),
        avg_iterations=per_task([r.iterations for r in done]),
        tool_calls_per_task=per_task([r.tool_calls for r in done]),
        tool_failure_rate=_ratio(
            sum(r.tool_failures for r in done), sum(r.tool_attempts for r in done)
        ),
        interventions_per_task=per_task([r.interventions for r in done]),
        deny_rate=_ratio(sum(r.denies for r in done), sum(r.decisions for r in done)),
    )


async def read_runs(
    session: AsyncSession, *conditions: ColumnElement[bool], terminal_only: bool = True
) -> list[RunMetrics]:
    """One `RunMetrics` per terminal task matching `conditions` (filters on `Task`).

    One grouped query per source table, not one per task: a period holds hundreds of tasks.
    `model_calls` and `tool_calls` carry no timestamp, so a period can only be expressed on
    the task (the caller's `conditions`), which is why every aggregate is keyed by task id.

    `terminal_only=False` is for a caller that names one task and wants to see it whatever
    state it is in (the eval runner reports a task parked mid-run instead of dropping it);
    `summarize` still ignores any non-terminal run.
    """
    status_filter = [Task.status.in_(TERMINAL_STATUSES)] if terminal_only else []
    tasks = (
        await session.execute(
            select(Task.id, Task.status, Task.started_at, Task.finished_at).where(
                *status_filter, *conditions
            )
        )
    ).all()
    ids = [t.id for t in tasks]
    if not ids:
        return []

    spend = {
        r.task_id: r
        for r in await session.execute(
            select(
                ModelCall.task_id,
                func.sum(ModelCall.cost_usd).label("cost"),
                func.sum(ModelCall.tokens_in).label("tokens_in"),
                func.sum(ModelCall.tokens_out).label("tokens_out"),
            )
            .where(ModelCall.task_id.in_(ids))
            .group_by(ModelCall.task_id)
        )
    }

    # Event counts by task and type. `policy.decided` is counted from the log, not from
    # `policy_decisions`: a call parked for approval has no row there until a human answers.
    event_counts: dict[Any, dict[str, tuple[int, int]]] = defaultdict(dict)
    is_deny = TaskEvent.payload["effect"].astext == "deny"
    for r in await session.execute(
        select(
            TaskEvent.task_id,
            TaskEvent.type,
            func.count().label("n"),
            func.count().filter(is_deny).label("denies"),
        )
        .where(
            TaskEvent.task_id.in_(ids),
            TaskEvent.type.in_(
                (events.ITERATION_STARTED, events.TOOL_REQUESTED, events.POLICY_DECIDED)
            ),
        )
        .group_by(TaskEvent.task_id, TaskEvent.type)
    ):
        event_counts[r.task_id][r.type] = (r.n, r.denies)

    # A tool "attempt" is a call the policy let through; a failure is one that errored. A deny
    # has an `error` too (the refusal text), but that is the policy working, not a tool failing.
    attempted = and_(ToolCall.decision == "allow", ToolCall.tool_name != _FINISH_TOOL)
    tool_rows = {
        r.task_id: r
        for r in await session.execute(
            select(
                ToolCall.task_id,
                func.count().filter(attempted).label("attempts"),
                func.count().filter(and_(attempted, ToolCall.error.is_not(None))).label("failures"),
            )
            .where(ToolCall.task_id.in_(ids))
            .group_by(ToolCall.task_id)
        )
    }

    interventions = {
        r.task_id: r.n
        for r in await session.execute(
            select(Approval.task_id, func.count().label("n"))
            .where(Approval.task_id.in_(ids))
            .group_by(Approval.task_id)
        )
    }

    runs = []
    for t in tasks:
        counts = event_counts.get(t.id, {})
        decided, denied = counts.get(events.POLICY_DECIDED, (0, 0))
        spent, tools = spend.get(t.id), tool_rows.get(t.id)
        runs.append(
            RunMetrics(
                status=t.status,
                success=t.status == "SUCCEEDED",
                cost_usd=Decimal(spent.cost) if spent else Decimal(0),
                tokens_in=int(spent.tokens_in) if spent else 0,
                tokens_out=int(spent.tokens_out) if spent else 0,
                latency_s=(t.finished_at - t.started_at).total_seconds()
                if t.finished_at and t.started_at
                else None,
                iterations=counts.get(events.ITERATION_STARTED, (0, 0))[0],
                tool_calls=counts.get(events.TOOL_REQUESTED, (0, 0))[0],
                tool_attempts=tools.attempts if tools else 0,
                tool_failures=tools.failures if tools else 0,
                interventions=interventions.get(t.id, 0),
                decisions=decided,
                denies=denied,
            )
        )
    return runs
