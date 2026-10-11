"""GET /metrics/summary (briefing §14, §32).

No formula lives here (briefing §10): the route picks the tasks the caller may see and hands
them to `warden.metrics.summary`, the same module the eval runner uses.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from warden.api.deps import SessionDep, require_scope
from warden.api.metrics_schemas import MetricsSummaryOut
from warden.identity import Claims
from warden.metrics.summary import read_runs, summarize
from warden.models import Task

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _user_id_of(claims: Claims) -> uuid.UUID:
    # Same rule as routes_tasks.py: `identity.issue_user_token` mints `user:<uuid>`; a subject
    # that does not parse is a bug upstream, so 401, not a 500.
    try:
        return uuid.UUID(claims.sub.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise HTTPException(401, "token subject is not a user id") from exc


@router.get("/summary", response_model=MetricsSummaryOut)
async def metrics_summary(
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:read"))],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    strategy: Annotated[str | None, Query(max_length=64)] = None,
) -> MetricsSummaryOut:
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(422, "from must not be after to")

    conditions = []
    # Same visibility as GET /tasks: your own tasks, unless you carry the admin scope.
    if "admin" not in claims.scopes:
        conditions.append(Task.user_id == _user_id_of(claims))
    # The period is when a task finished: only terminal tasks count, and a task belongs to
    # the day it ended, not the day it was queued.
    if from_ is not None:
        conditions.append(Task.finished_at >= from_)
    if to is not None:
        conditions.append(Task.finished_at <= to)
    if strategy is not None:
        conditions.append(Task.routing_strategy == strategy)

    s = summarize(await read_runs(session, *conditions))
    return MetricsSummaryOut(
        tasks=s.tasks,
        by_status=s.by_status,
        successes=s.successes,
        success_rate=s.success_rate,
        cost_total=s.cost_total,
        cost_per_task=s.cost_per_task,
        cost_per_success=s.cost_per_success,
        tokens_in_per_task=s.tokens_in_per_task,
        tokens_out_per_task=s.tokens_out_per_task,
        latency_p50_s=s.latency_p50,
        latency_p95_s=s.latency_p95,
        avg_iterations=s.avg_iterations,
        tool_calls_per_task=s.tool_calls_per_task,
        tool_failure_rate=s.tool_failure_rate,
        interventions_per_task=s.interventions_per_task,
        deny_rate=s.deny_rate,
    )
