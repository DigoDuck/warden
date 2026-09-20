"""The agent loop: decide, execute, record, finish.

Every tool call passes the policy engine first. The model proposes, the control plane
decides, and the decision is deterministic, taken without reading model output as
instruction, and recorded with the hash of the policy that produced it.

This is week 2's version of briefing section 16. Still absent, arriving with the rest of the
week: the Docker sandbox executing the calls, the queue claim with a lease, cooperative
cancellation, checkpointing and resume.
"""

import pathlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from warden.core import events
from warden.core.replay import ResumeState
from warden.models import Task, User
from warden.policy.engine import Decision, Effect, Policy, PolicyContext, UserRef
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
from warden.tools.workspace import normalize_path

FINISH_TOOL = "finish"

SYSTEM_PROMPT = """You are a software agent working on a repository through tools.

Read what you need with the tools available, then call `finish` with a summary of what you
found. Call `finish` exactly once, as your last action. Do not guess at file contents you
have not read.

Some actions are refused by policy. A refusal is final: do not retry the same call, work
around it or report that you could not complete that part."""


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


def _refusal_message(decision: Decision) -> str:
    """What the model is told when the control plane says no.

    It names the rules that matched, because a refusal the model cannot understand is a
    refusal it retries verbatim, burning iterations and money.
    """
    rules = ", ".join(decision.matched_rules) or "none"
    if decision.effect is Effect.REQUIRE_APPROVAL:
        return (
            f"This action requires human approval and cannot run yet: {decision.reason}. "
            f"Matched rules: {rules}."
        )
    return (
        f"Refused by policy: {decision.reason}. Matched rules: {rules}. "
        f"Do not retry this call; take a different approach."
    )


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
    policy: Policy,
    *,
    workspace: pathlib.Path,
    budget: Budget | None = None,
    resume: ResumeState | None = None,
) -> RunResult:
    budget = budget or Budget()
    spent = resume.spent if resume else Decimal("0")

    user = await session.get(User, task.user_id)
    user_ref = UserRef(id=task.user_id, role=user.role if user else "worker")

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

    task.status = "RUNNING"

    if resume is None:
        messages: list[Message] = [UserMessage(text=task.spec)]
        first_iteration = 1
        task.started_at = datetime.now(UTC)
        await events.append_event(
            session,
            task.id,
            events.TASK_CREATED,
            {"spec": task.spec, "policy_hash": policy.policy_hash[:12]},
        )
    else:
        messages = list(resume.messages)
        first_iteration = resume.next_iteration
        if resume.is_mid_iteration:
            # The interrupted iteration is finished, not restarted: its assistant turn is
            # already in the messages and its tool_use blocks are still unanswered. Running
            # only the calls that never executed is what keeps "no tool runs twice" true.
            fresh = await _run_tools(
                session,
                task.id,
                first_iteration,
                resume.pending_tool_calls,
                registry,
                policy,
                user_ref,
                workspace,
            )
            # Results already obtained travel with the new ones: the API wants every
            # tool_use from a turn answered in a single message.
            messages.append(ToolResultsMessage(results=[*resume.partial_results, *fresh]))
            first_iteration += 1

    for iteration in range(first_iteration, budget.max_iterations + 1):
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
                # Verbatim, because ADR-016 says the assistant turn goes back to the
                # provider exactly as it came. This is the field that makes replay possible
                # at all, and without it the conversation cannot be rebuilt after a crash.
                "raw_content": completion.raw_content,
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
            # The model stopped talking without calling `finish`. The summary is whatever it
            # said, and the task is done rather than stuck.
            return await _finish(
                session, task, "SUCCEEDED", iteration, spent, summary=completion.text
            )

        finish_call = next(
            (call for call in completion.tool_calls if call.name == FINISH_TOOL), None
        )
        if finish_call is not None:
            # Handled by core rather than dispatched, so it carries no policy decision.
            await events.record_tool_call(
                session, task.id, iteration, finish_call, decision="allow"
            )
            summary = str(finish_call.arguments.get("summary", "")) or completion.text
            return await _finish(session, task, "SUCCEEDED", iteration, spent, summary=summary)

        results = await _run_tools(
            session,
            task.id,
            iteration,
            completion.tool_calls,
            registry,
            policy,
            user_ref,
            workspace,
        )
        messages.append(ToolResultsMessage(results=results))

    return await _finish(
        session,
        task,
        "TIMED_OUT",
        budget.max_iterations,
        spent,
        reason=f"reached max_iterations ({budget.max_iterations}) without finishing",
    )


def _build_context(
    call: ToolCall, registry: ToolRegistry, user: UserRef, task_id: UUID
) -> PolicyContext:
    """Normalise the path once, here, so policy and tool judge the same string.

    The two layers still do different work, and that is intentional. The policy rules on the
    normalised string, deterministically and without touching a disk, so the decision is
    reproducible from the event log. The tool resolves the path again inside the container
    when it opens the file, which is the only place a symlink the agent created is visible.
    Neither layer alone covers both cases.
    """
    path_arg = registry.path_arg(call.name)
    normalised: str | None = None
    if path_arg is not None:
        raw = call.arguments.get(path_arg)
        if isinstance(raw, str):
            # None when the path escapes: no allow rule can match an unset path, so the
            # default deny applies before the container is ever asked.
            normalised = normalize_path(raw)
    return PolicyContext(
        tool=call.name, args=call.arguments, path=normalised, user=user, task_id=task_id
    )


async def _run_tools(
    session: AsyncSession,
    task_id: UUID,
    iteration: int,
    calls: Sequence[ToolCall],
    registry: ToolRegistry,
    policy: Policy,
    user: UserRef,
    workspace: pathlib.Path,
) -> list[ToolResult]:
    """Decide on every call, execute the allowed ones, and answer all of them.

    A refused tool still gets a result with `is_error`. Dropping it would leave a `tool_use`
    block unanswered, which the API rejects, and it would hide the refusal from the model.
    """
    results: list[ToolResult] = []
    for call in calls:
        await events.append_event(
            session,
            task_id,
            events.TOOL_REQUESTED,
            # Full arguments, not the redacted ones that go to `tool_calls.args_safe`.
            # Replay needs to re-issue a pending call, and "[redacted]" is not a path. Safe
            # because the model never sees credentials: the broker injects them at
            # execution, so an argument the model produced contains no secret by design.
            {"tool": call.name, "id": call.id, "arguments": call.arguments},
        )

        decision = policy.evaluate(_build_context(call, registry, user, task_id))
        await events.append_event(
            session,
            task_id,
            events.POLICY_DECIDED,
            {
                "tool": call.name,
                "id": call.id,
                "effect": decision.effect.value,
                "matched_rules": decision.matched_rules,
                "policy_hash": decision.policy_hash[:12],
            },
        )

        started = time.monotonic()
        if decision.effect is Effect.ALLOW:
            try:
                output = await registry.execute(call.name, call.arguments)
                error = None
            except ToolError as exc:
                # Bad arguments and tool-level refusals are normal in an agent loop: the
                # model sees the message and corrects itself on the next turn.
                output = str(exc)
                error = str(exc)
        else:
            # The tool never runs. require_approval degrades to a refusal until week 3
            # builds the approval machinery, which errs on the restrictive side.
            output = _refusal_message(decision)
            error = output

        duration_ms = int((time.monotonic() - started) * 1000)
        row = await events.record_tool_call(
            session,
            task_id,
            iteration,
            call,
            decision=decision.effect.value,
            result_summary=output[:500],
            error=error,
            duration_ms=duration_ms,
        )
        await events.record_policy_decision(session, row.id, decision)
        await events.append_event(
            session,
            task_id,
            events.TOOL_EXECUTED,
            {
                "tool": call.name,
                "id": call.id,
                "ok": error is None,
                "effect": decision.effect.value,
                "duration_ms": duration_ms,
                # The full output, not the 500-character summary in `tool_calls`. That one
                # is for a human reading the timeline; this one has to rebuild the exact
                # tool_result the model already saw.
                "output": output,
                "is_error": error is not None,
            },
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
