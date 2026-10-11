"""Response body for routes_metrics.py.

Kept out of api/schemas.py on purpose (same reason as approval_schemas.py): that module
belongs to another track this wave. Money is `Decimal`, so it serialises as a string and
never loses a digit to a float; rates are fractions in [0, 1], not percentages.
"""

from decimal import Decimal

from pydantic import BaseModel


class MetricsSummaryOut(BaseModel):
    # Terminal tasks in the period; a task still running is in none of these numbers.
    tasks: int
    by_status: dict[str, int]
    successes: int
    # Every figure below is null, not zero, when its denominator is zero: "no data" and
    # "none succeeded" are different statements, and a screen must be able to tell them apart.
    success_rate: float | None
    cost_total: Decimal
    cost_per_task: Decimal | None
    cost_per_success: Decimal | None
    tokens_in_per_task: float | None
    tokens_out_per_task: float | None
    latency_p50_s: float | None
    latency_p95_s: float | None
    avg_iterations: float | None
    tool_calls_per_task: float | None
    tool_failure_rate: float | None
    interventions_per_task: float | None
    deny_rate: float | None
