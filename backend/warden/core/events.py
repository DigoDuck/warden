"""Everything the loop writes down about a task.

Briefing section 12: the task is an event log. `tasks` holds the current snapshot,
`task_events` holds what happened, in order, append-only. Week 2 rebuilds the conversation
from these rows to resume after a crash, which is why the ordering guarantee lives in the
database rather than in this module.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.models import ModelCall, TaskEvent, ToolCall
from warden.providers.base import Completion
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.pricing import cost_usd

# The week 1 subset of the event types in briefing section 13. Policy, approval and
# checkpoint events arrive with the modules that emit them.
TASK_CREATED = "task.created"
ITERATION_STARTED = "iteration.started"
MODEL_CALLED = "model.called"
TOOL_REQUESTED = "tool.requested"
TOOL_EXECUTED = "tool.executed"
TASK_FINISHED = "task.finished"

_SENSITIVE_KEY_PARTS = ("token", "key", "secret", "password", "credential", "authorization")
_MAX_ARG_CHARS = 2_000


def redact_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Make tool arguments safe to store and to ship to telemetry.

    The column is called `args_safe`, so writing raw arguments into it would be a quiet lie.
    In week 1 the arguments are paths and nothing here triggers, but the function exists
    from the same commit as the column rather than arriving after the secret broker does.
    """
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
            safe[key] = "[redacted]"
        elif isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
            safe[key] = value[:_MAX_ARG_CHARS] + f"[truncated: {len(value)} chars]"
        else:
            safe[key] = value
    return safe


async def append_event(
    session: AsyncSession, task_id: UUID, event_type: str, payload: dict[str, Any] | None = None
) -> TaskEvent:
    """Append one event, allocating the next `seq` for this task.

    ponytail: seq comes from max(seq)+1, which is only safe while one process writes a given
    task. That holds in week 1 (the demo runs the loop in-process) and the
    UNIQUE(task_id, seq) constraint turns a violation into an error rather than a silent
    overwrite. Week 2 replaces this with allocation under the queue claim.
    """
    highest = await session.scalar(
        select(func.max(TaskEvent.seq)).where(TaskEvent.task_id == task_id)
    )
    event = TaskEvent(
        task_id=task_id,
        seq=(highest or 0) + 1,
        type=event_type,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


async def read_events(session: AsyncSession, task_id: UUID) -> list[TaskEvent]:
    result = await session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
    )
    return list(result)


async def record_model_call(
    session: AsyncSession, task_id: UUID, completion: Completion, *, purpose: str = "agent"
) -> Decimal:
    """Persist one model call and return what it cost.

    Cost is computed here, next to the tokens that produced it, so the number in the
    database and the number the loop bills against the budget are the same number.
    """
    cost = cost_usd(completion.model, completion.usage)
    session.add(
        ModelCall(
            task_id=task_id,
            provider=completion.provider,
            model=completion.model,
            purpose=purpose,
            tokens_in=completion.usage.input_tokens,
            tokens_out=completion.usage.output_tokens,
            cost_usd=cost,
            error=completion.refusal.explanation if completion.refusal else None,
        )
    )
    await session.flush()
    return cost


async def record_tool_call(
    session: AsyncSession,
    task_id: UUID,
    iteration: int,
    call: ProviderToolCall,
    *,
    decision: str,
    result_summary: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> ToolCall:
    safe = redact_args(call.arguments)
    session.add(
        row := ToolCall(
            task_id=task_id,
            iteration=iteration,
            tool_name=call.name,
            args_safe=safe,
            # Hash of the redacted arguments: enough to tell two calls apart for the
            # "no tool ran twice" assertion in the week 2 resume tests, without keeping a
            # second copy of the arguments around.
            args_hash=hashlib.sha256(
                json.dumps(safe, sort_keys=True, default=str).encode()
            ).hexdigest(),
            decision=decision,
            result_summary=result_summary,
            error=error,
            duration_ms=duration_ms,
        )
    )
    await session.flush()
    return row
