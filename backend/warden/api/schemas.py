"""Request/response bodies. Pydantic v2, `extra="forbid"` on every request body: this is
the trust boundary the briefing calls out for `api` (§10, "validação de entrada").
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int | None = Field(default=None, gt=0, le=1000)
    # allow_inf_nan=False: Python's json parser accepts a bare `Infinity`, `gt=0` passes it,
    # and JSONB then refuses it at insert time as a 500 instead of a 422 here.
    max_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Same `allow_inf_nan=False` reasoning as `max_usd`. Bound at a day (86 400s), the same
    # ceiling ADR-020 already uses for a user token's TTL (`identity.USER_TTL_CAP_SECONDS`):
    # a caller can only ever tighten `core.worker`'s own deadline (see
    # `core/worker.py::merge_budget`), so this is a sanity bound on the request body, not the
    # thing that actually protects a run.
    max_seconds: float | None = Field(default=None, gt=0, le=86_400, allow_inf_nan=False)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A spec is a task description, not a place to paste a repository: bounded so one
    # request cannot make the row, and every event and audit entry that names it, unbounded.
    spec: str = Field(min_length=1, max_length=20_000)
    target_repo: str | None = Field(default=None, max_length=512)
    budget: BudgetIn | None = None


class TaskOut(BaseModel):
    id: uuid.UUID
    status: str
    spec: str
    target_repo: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cost_usd: Decimal
    iterations: int


class TaskEventOut(BaseModel):
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


class TaskEventPage(BaseModel):
    events: list[TaskEventOut]
    # The `after` value a follow-up request needs to get the next page; None once the last
    # page returned fewer rows than `limit`, so there is nothing left to ask for.
    next_after: int | None


class AuditVerifyOut(BaseModel):
    ok: bool
    rows_checked: int
    broken_row_id: int | None
    reason: str | None
