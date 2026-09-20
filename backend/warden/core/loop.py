"""The minimal agent loop: generate, run tools, record events, finish.

This is week 1's version of briefing section 16, cut to what has something to stand on.
Absent on purpose, arriving in week 2: the policy engine deciding every tool call, the
Docker sandbox executing them, the queue claim with a lease, cooperative cancellation,
checkpointing and resume. The shape here is the shape that grows into that, so the seams
are where they will still be.

The model proposes; here, for now, only the tool's own contract disposes. That is the piece
week 2 replaces with a decision the control plane makes.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from warden.core import events
from warden.models import Task
from warden.providers.base import (
    AssistantMessage,
    Message,
    ModelProvider,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    ToolSchema,
    UserMessage,
)
from warden.tools.registry import ToolError, ToolRegistry

FINISH_TOOL = "finish"

SYSTEM_PROMPT = """You are a software agent working on a repository through tools.

Read what you need with the tools available, then call `finish` with a summary of what you
found. Call `finish` exactly once, as your last action. Do not guess at file contents you
have not read."""


@dataclass(frozen=True)
class Budget:
    """What stops a run that will not stop itself."""

    max_iterations: int = 10
    max_usd: Decimal = Decimal("0.25")


@dataclass
class RunResult:
    status: str
    iterations: int
    cost_usd: Decimal
    summary: str | None = None
    reason: str | None = None


def _finish_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What you did and what you found."}
        },
        "required": ["summary"],
    }


async def run_task(
    session: AsyncSession,
    task: Task,
    provider: ModelProvider,
    registry: ToolRegistry,
    *,
    budget: Budget | None = None,
) -> RunResult:
    budget = budget or Budget()
    spent = Decimal("0")

    tool_schemas = list(registry.schemas())
    # `finish` is described to the model but never dispatched to the registry: section 15
    # puts its executor in `core`, because it ends the task instead of producing a result.
    tool_schemas.append(
        ToolSchema(
            name=FINISH_TOOL,
            description="Finish the task and report what you found.",
            input_schema=_finish_schema(),
        )
    )

    messages: list[Message] = [UserMessage(text=task.spec)]
    task.status = "RUNNING"
    task.started_at = datetime.now(UTC)
    await events.append_event(session, task.id, events.TASK_CREATED, {"spec": task.spec})

    for iteration in range(1, budget.max_iterations + 1):
        await events.append_event(session, task.id, events.ITERATION_STARTED, {"n": iteration})

        completion = await provider.generate(messages, tools=tool_schemas, system=SYSTEM_PROMPT)
        cost = await events.record_model_call(session, task.id, completion)
        spent += cost
        await events.append_event(
            session,
            task.id,
            events.MODEL_CALLED,
            {
                "model": completion.model,
                "stop_reason": completion.stop_reason,
                "tokens_in": completion.usage.input_tokens,
                "tokens_out": completion.usage.output_tokens,
                "cost_usd": str(cost),
            },
        )

        # Checked straight after billing, so an expensive turn cannot be followed by
        # another one. Stopping before the next model call is the whole point.
        if spent > budget.max_usd:
            return await _finish(
                session,
                task,
                "BUDGET_EXCEEDED",
                iteration,
                spent,
                reason=f"spent {spent} over the {budget.max_usd} ceiling",
            )

        if completion.stop_reason == "refusal":
            detail = completion.refusal.explanation if completion.refusal else None
            return await _finish(
                session, task, "FAILED", iteration, spent, reason=f"model refused: {detail}"
            )

        if completion.stop_reason not in ("tool_use", "pause_turn", "end_turn"):
            # max_tokens, stop_sequence and model_context_window_exceeded all mean the turn
            # cannot be continued as it is. Failing names which one, rather than looping.
            return await _finish(
                session,
                task,
                "FAILED",
                iteration,
                spent,
                reason=f"unusable stop_reason: {completion.stop_reason}",
            )

        messages.append(AssistantMessage(raw_content=completion.raw_content))

        if completion.stop_reason == "pause_turn":
            # The model paused mid-turn; resending the history continues it.
            continue

        if completion.stop_reason == "end_turn":
            # The model stopped talking without calling `finish`. The summary is whatever
            # it said, and the task is done rather than stuck.
            return await _finish(
                session, task, "SUCCEEDED", iteration, spent, summary=completion.text
            )

        finish_call = next(
            (call for call in completion.tool_calls if call.name == FINISH_TOOL), None
        )
        if finish_call is not None:
            await events.record_tool_call(
                session, task.id, iteration, finish_call, decision="allow"
            )
            summary = str(finish_call.arguments.get("summary", "")) or completion.text
            return await _finish(session, task, "SUCCEEDED", iteration, spent, summary=summary)

        results = await _run_tools(session, task.id, iteration, completion.tool_calls, registry)
        messages.append(ToolResultsMessage(results=results))

    return await _finish(
        session,
        task,
        "TIMED_OUT",
        budget.max_iterations,
        spent,
        reason=f"reached max_iterations ({budget.max_iterations}) without finishing",
    )


async def _run_tools(
    session: AsyncSession,
    task_id: UUID,
    iteration: int,
    calls: Sequence[ToolCall],
    registry: ToolRegistry,
) -> list[ToolResult]:
    """Execute every tool the model asked for, and answer all of them.

    A failed tool still gets a result with `is_error`. Dropping it would leave a `tool_use`
    block unanswered, which the API rejects, and it would hide the failure from the model.
    """
    results: list[ToolResult] = []
    for call in calls:
        await events.append_event(
            session, task_id, events.TOOL_REQUESTED, {"tool": call.name, "id": call.id}
        )
        started = time.monotonic()
        try:
            output = await registry.execute(call.name, call.arguments)
            error = None
        except ToolError as exc:
            # Refusals and bad arguments are normal in an agent loop: the model sees the
            # message and gets to correct itself on the next turn.
            output = str(exc)
            error = str(exc)

        duration_ms = int((time.monotonic() - started) * 1000)
        await events.record_tool_call(
            session,
            task_id,
            iteration,
            call,
            decision="allow",
            result_summary=output[:500],
            error=error,
            duration_ms=duration_ms,
        )
        await events.append_event(
            session,
            task_id,
            events.TOOL_EXECUTED,
            {"tool": call.name, "id": call.id, "ok": error is None, "duration_ms": duration_ms},
        )
        results.append(ToolResult(tool_call_id=call.id, content=output, is_error=error is not None))
    return results


async def _finish(
    session: AsyncSession,
    task: Task,
    status: str,
    iterations: int,
    spent: Decimal,
    *,
    summary: str | None = None,
    reason: str | None = None,
) -> RunResult:
    task.status = status
    task.spent = {"usd": str(spent)}
    task.finished_at = datetime.now(UTC)
    await events.append_event(
        session,
        task.id,
        events.TASK_FINISHED,
        {"status": status, "iterations": iterations, "cost_usd": str(spent), "reason": reason},
    )
    await session.flush()
    return RunResult(
        status=status, iterations=iterations, cost_usd=spent, summary=summary, reason=reason
    )
