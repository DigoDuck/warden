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

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from warden.identity import broker
from warden.models import Evidence, ModelCall, PolicyDecision, TaskEvent, ToolCall, Verdict
from warden.policy.engine import Decision
from warden.providers.base import Completion
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.pricing import cost_usd

# The week 1 subset of the event types in briefing section 13. Policy, approval and
# checkpoint events arrive with the modules that emit them.
TASK_CREATED = "task.created"
ITERATION_STARTED = "iteration.started"
MODEL_CALLED = "model.called"
TOOL_REQUESTED = "tool.requested"
POLICY_DECIDED = "policy.decided"
TOOL_EXECUTED = "tool.executed"
TASK_FINISHED = "task.finished"
CANCEL_REQUESTED = "cancel.requested"
# Week 3 (ADR-022): a REQUIRE_APPROVAL decision pauses the task instead of refusing the
# call, and the decision it is waiting on is recorded as its own event so replay can rebuild
# the outcome without querying the `approvals` table (this module stays pure, see
# core/replay.py's own docstring).
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_REJECTED = "approval.rejected"
# ADR-026: the control plane's own checks after the agent finishes. `verify.started` carries
# the agent's summary and iteration count, because a worker that resumes in the middle of
# verification has to finish the task with them and never calls the model again to get them.
# One `verify.recorded` per check, committed with its `evidence` row, so replay knows which
# checks are already done.
VERIFY_STARTED = "verify.started"
VERIFY_RECORDED = "verify.recorded"
# ADR-010: the independent reviewer's verdict, committed together with its `verdicts` and
# `model_calls` rows. Replay reads it to know the (paid) review call must not be made again.
VERIFY_VERDICT = "verify.verdict"
# ADR-028: the control plane's own publication phase after the verdict. `publish.requested` is
# the pull request the control plane (not the model) proposes, committed before it is decided;
# it is its own event type, never `tool.requested`, because replay builds the agent's pending
# calls from that one. `publish.skipped` records why a verified change was not published.
PUBLISH_REQUESTED = "publish.requested"
PUBLISH_SKIPPED = "publish.skipped"
# ADR-031: the planner's advice, recorded before the coder's first iteration. Carries either the
# validated `plan` or a `malformed_reason`, plus the call's `cost_usd`, which replay adds back
# to the task's spend. Committed together with the planner's `model_calls` row.
PLAN_RECORDED = "plan.recorded"

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


def _redact_value(value: Any) -> Any:
    """Walk a JSON-shaped value (what a `task_events.payload`/tool output always is) and run
    every string leaf through `broker.redact()`, whatever shape it is nested in: a bare
    string (`tool.executed`'s `output`), a list of strings (`policy.decided`'s `paths`), or a
    dict (`tool.requested`'s `arguments`, already covered by `redact_args` for the *key*-based
    case, but a value can also just happen to contain a secret verbatim, which is what this
    catches). `redact_args`'s key-name rule and this value-scan are complementary, not
    redundant: a `github_token` argument is masked outright by the first before this ever
    runs, while a tool whose *output* echoes a token back under an innocuous key name
    (`{"body": "used <token>"}`) is only caught by this one.
    """
    if isinstance(value, str):
        return broker.redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


async def append_event(
    session: AsyncSession, task_id: UUID, event_type: str, payload: dict[str, Any] | None = None
) -> TaskEvent:
    """Append one event, allocating the next `seq` for this task.

    `seq` is max(seq)+1, allocated under a lock on the task's row. The lock is what makes
    that safe with more than one writer per task, which ADR-021 introduced: a cancel request
    appends `cancel.requested` from its own session while the worker is mid-run. Unlocked,
    both read the same max from their snapshots and pick the same `seq`; the second then
    waits on the unique index for the first's transaction, and when the first is a worker
    about to fence its checkpoint (`queue.verify_holder`'s `FOR UPDATE`, which waits on the
    cancel's row lock), Postgres breaks the cycle by killing one of them as a deadlock.
    Taking the row lock *first* orders every writer on the same lock before anyone touches
    the index, and the max below is read by a fresh statement after the wait, so it sees
    whatever the previous holder committed. `NO KEY UPDATE` rather than `UPDATE`: enough to
    serialise writers, while still letting other tables' foreign-key checks through.
    """
    await session.execute(
        text("SELECT 1 FROM tasks WHERE id = :task_id FOR NO KEY UPDATE"), {"task_id": task_id}
    )
    highest = await session.scalar(
        select(func.max(TaskEvent.seq)).where(TaskEvent.task_id == task_id)
    )
    event = TaskEvent(
        task_id=task_id,
        seq=(highest or 0) + 1,
        type=event_type,
        # The event log's one choke point for a secret in tool output (ADR-025, deliverable
        # 4): whatever wrote this payload, nothing reaches the database before this.
        payload=_redact_value(payload or {}),
    )
    session.add(event)
    await session.flush()
    return event


async def read_events(session: AsyncSession, task_id: UUID) -> list[TaskEvent]:
    result = await session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
    )
    return list(result)


async def add_model_call(
    session: AsyncSession, task_id: UUID, completion: Completion, *, purpose: str = "agent"
) -> ModelCall:
    """Persist one model call. Cost is computed here, next to the tokens that produced it,
    so the number in the database and the number the loop bills are the same number."""
    row = ModelCall(
        task_id=task_id,
        provider=completion.provider,
        model=completion.model,
        purpose=purpose,
        tokens_in=completion.usage.input_tokens,
        tokens_out=completion.usage.output_tokens,
        cost_usd=cost_usd(completion.model, completion.usage),
        error=completion.refusal.explanation if completion.refusal else None,
    )
    session.add(row)
    await session.flush()
    return row


async def record_model_call(
    session: AsyncSession, task_id: UUID, completion: Completion, *, purpose: str = "agent"
) -> Decimal:
    """`add_model_call`, returning only what it cost (what the agent loop bills against)."""
    return (await add_model_call(session, task_id, completion, purpose=purpose)).cost_usd


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
            # Same choke point as `append_event`'s payload: the tool's own text, not just
            # its arguments, can echo a secret back.
            result_summary=_redact_value(result_summary),
            error=_redact_value(error),
            duration_ms=duration_ms,
        )
    )
    await session.flush()
    return row


async def record_policy_decision(
    session: AsyncSession, tool_call_id: UUID, decision: Decision
) -> PolicyDecision:
    """Persist why a tool call was allowed, refused or escalated.

    Kept next to the tool call rather than only in the event log, because this is the row an
    auditor filters on months later: which rules matched, and under which policy hash.
    """
    session.add(
        row := PolicyDecision(
            tool_call_id=tool_call_id,
            effect=decision.effect.value,
            matched_rules=decision.matched_rules,
            reason=decision.reason,
            policy_hash=decision.policy_hash,
        )
    )
    await session.flush()
    return row


async def record_evidence(
    session: AsyncSession, task_id: UUID, kind: str, payload: dict[str, Any]
) -> Evidence:
    """Persist one check's result (ADR-026).

    The payload holds output of code the agent wrote (a test that prints, a diff of files it
    edited), so it goes through the same redaction as every event payload before it reaches
    the database.
    """
    session.add(row := Evidence(task_id=task_id, kind=kind, payload=_redact_value(payload)))
    await session.flush()
    return row


async def record_verdict(
    session: AsyncSession,
    task_id: UUID,
    *,
    passed: bool,
    findings: list[str],
    malformed_reason: str | None,
    model_call_id: UUID,
) -> Verdict:
    """Persist the independent reviewer's verdict (ADR-010).

    The findings are model text written after reading evidence that came out of the agent's
    own code, so they pass through the same redaction as every other payload that reaches the
    database.
    """
    session.add(
        row := Verdict(
            task_id=task_id,
            verifier="independent",
            passed=passed,
            findings=_redact_value(findings),
            malformed_reason=_redact_value(malformed_reason),
            model_call_id=model_call_id,
        )
    )
    await session.flush()
    return row
