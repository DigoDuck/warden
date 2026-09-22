"""Rebuild a task's conversation from its event log.

Briefing section 12: the task *is* the event log. `tasks` holds the snapshot, the events
hold what happened. Resume after a crash means replaying these rows rather than keeping
state in a worker that just died.

This module is pure. It reads events and produces the state the loop needs, touching no
database and no provider, which is why it is tested without either.

It also never opens `raw_content`. ADR-016 makes that value opaque to everything outside
the provider that produced it, so the pending tool calls are derived from `tool.requested`
minus `tool.executed`, which are the control plane's own events.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from warden.core import events as ev
from warden.models import TaskEvent
from warden.providers.base import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    UserMessage,
)


@dataclass
class ResumeState:
    """Everything the loop needs to carry on as if it had never stopped."""

    messages: list[Message] = field(default_factory=list)
    # The iteration to run next. On a clean boundary this is the one after the last
    # completed; mid-iteration it is the interrupted one, which is finished rather than
    # restarted.
    next_iteration: int = 1
    spent: Decimal = Decimal("0")
    # Tools that were requested and never executed, in the order the model asked for them.
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    # Results already obtained in the interrupted iteration. They must travel in the same
    # message as the pending ones, because the API wants every tool_use answered at once.
    partial_results: list[ToolResult] = field(default_factory=list)
    finished: bool = False

    @property
    def is_mid_iteration(self) -> bool:
        return bool(self.pending_tool_calls)


def _tool_result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(
        tool_call_id=str(payload["id"]),
        content=str(payload.get("output", "")),
        is_error=bool(payload.get("is_error", False)),
    )


def rebuild(task_events: Sequence[TaskEvent]) -> ResumeState:
    """Turn an ordered event log back into conversation state."""
    state = ResumeState()

    # Per-iteration bookkeeping, flushed when the iteration's results are complete.
    requested: list[dict[str, Any]] = []
    executed: dict[str, dict[str, Any]] = {}
    iteration = 0
    # Whether the iteration being read got as far as its model call. `iteration.started` is
    # committed before the provider is asked, so a crash inside the call leaves it alone in
    # the log, and that iteration has to run again rather than be counted as done.
    model_called = False

    def flush_results() -> None:
        """Emit the tool results message for the iteration that just ended."""
        if not requested:
            return
        done = [_tool_result(executed[item["id"]]) for item in requested if item["id"] in executed]
        if done:
            state.messages.append(ToolResultsMessage(results=done))

    for event in sorted(task_events, key=lambda e: e.seq):
        payload: dict[str, Any] = dict(event.payload or {})

        if event.type == ev.TASK_CREATED:
            state.messages.append(UserMessage(text=str(payload.get("spec", ""))))

        elif event.type == ev.ITERATION_STARTED:
            # A new iteration means the previous one's results are settled.
            flush_results()
            requested, executed = [], {}
            iteration = int(payload.get("n", iteration + 1))
            model_called = False

        elif event.type == ev.MODEL_CALLED:
            model_called = True
            state.spent += Decimal(str(payload.get("cost_usd", "0")))
            if "raw_content" in payload:
                state.messages.append(AssistantMessage(raw_content=payload["raw_content"]))

        elif event.type == ev.TOOL_REQUESTED:
            # First request wins. The loop records each call once, but everything below
            # (pending, partial results, the results message) is built from this list, so a
            # repeated id would become a tool that runs twice and a tool_use answered twice.
            # Every reader routes through here, which makes it the place to be strict.
            if all(item["id"] != payload["id"] for item in requested):
                requested.append(payload)

        elif event.type == ev.TOOL_EXECUTED:
            executed[str(payload["id"])] = payload

        elif event.type == ev.TASK_FINISHED:
            state.finished = True

    # Whatever is left belongs to the iteration that was cut short.
    pending = [item for item in requested if item["id"] not in executed]
    if pending:
        state.pending_tool_calls = [
            ToolCall(
                id=str(item["id"]),
                name=str(item["tool"]),
                arguments=dict(item.get("arguments") or {}),
            )
            for item in pending
        ]
        state.partial_results = [
            _tool_result(executed[item["id"]]) for item in requested if item["id"] in executed
        ]
        # Finish this iteration rather than starting the next one: its assistant turn is
        # already in the messages, and its tool_use blocks are still unanswered.
        state.next_iteration = iteration
    else:
        flush_results()
        state.next_iteration = iteration + 1 if model_called or iteration == 0 else iteration

    return state
