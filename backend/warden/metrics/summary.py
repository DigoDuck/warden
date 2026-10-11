"""The metric definitions (briefing §32), in one place.

Two callers compute the same numbers from the same rows: `GET /metrics/summary` over every
task in a period, and the capability eval runner (`evals/coding.py`) over the tasks of one
run. Both go through `read_runs` (rows -> `RunMetrics`) and `summarize` (`RunMetrics` ->
`MetricsSummary`), so a formula has exactly one definition and "the screen and the runner
agree" is true by construction, then pinned by evals/tests/test_metrics_parity.py.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

# Statuses a task never leaves. Only these count: a task still running has no latency, a
# partial cost and an unknown outcome, so averaging it in would make every number a lie.
TERMINAL_STATUSES: tuple[str, ...] = (
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "BUDGET_EXCEEDED",
)


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
    raise NotImplementedError


def summarize(runs: Sequence[RunMetrics]) -> MetricsSummary:
    raise NotImplementedError


async def read_runs(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[RunMetrics]:
    raise NotImplementedError


