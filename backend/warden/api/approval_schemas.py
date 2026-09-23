"""Request/response bodies for routes_approvals.py.

Kept out of api/schemas.py on purpose: that module belongs to routes_tasks.py's track this
wave. Same conventions as api/schemas.py: Pydantic v2, `extra="forbid"` on every request body
(briefing §10, validation at the API's trust boundary).
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApprovalOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    tool: str
    # Already redacted (core/events.py::redact_args, same as tool_calls.args_safe): a
    # reviewer's dashboard never has to be a trust boundary of its own.
    args_safe: dict[str, Any]
    matched_rules: list[str]
    reason: str
    status: str
    requested_at: datetime


class ApprovalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required for a reject (core/approvals.py::decide_approval enforces the non-empty
    # check), optional for an approve. Bounded for the same reason TaskCreate.spec is: every
    # audit entry and event this produces carries it verbatim, and on a reject so does the
    # tool_result the model reads next. 2,000 matches events._MAX_ARG_CHARS.
    note: str | None = Field(default=None, max_length=2_000)
