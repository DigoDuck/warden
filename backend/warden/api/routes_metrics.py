"""GET /metrics/summary (briefing §14, §32).

No formula lives here (briefing §10): the route picks the tasks the caller may see and hands
them to `warden.metrics.summary`, the same module the eval runner uses.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from warden.api.deps import SessionDep, require_scope
from warden.api.metrics_schemas import MetricsSummaryOut
from warden.identity import Claims

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/summary", response_model=MetricsSummaryOut)
async def metrics_summary(
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("tasks:read"))],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    strategy: Annotated[str | None, Query(max_length=64)] = None,
) -> MetricsSummaryOut:
    raise NotImplementedError
