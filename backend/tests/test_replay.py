"""Rebuilding a conversation from events, with no database and no provider.

`rebuild` is pure, so these tests construct event rows directly. That keeps the hard part
(what the event log has to contain for a replay to be exact) separate from the plumbing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from warden.core import events as ev
from warden.core.replay import rebuild
from warden.models import TaskEvent
from warden.providers.base import AssistantMessage, ToolResultsMessage, UserMessage

TASK_ID = uuid.uuid4()


def _events(*pairs: tuple[str, dict[str, Any]]) -> list[TaskEvent]:
    return [
        TaskEvent(task_id=TASK_ID, seq=index, type=kind, payload=payload)
        for index, (kind, payload) in enumerate(pairs, start=1)
    ]


def _model_called(raw: Any, cost: str = "0.01") -> tuple[str, dict[str, Any]]:
    return (ev.MODEL_CALLED, {"cost_usd": cost, "stop_reason": "tool_use", "raw_content": raw})


def _requested(call_id: str, tool: str, **args: Any) -> tuple[str, dict[str, Any]]:
    return (ev.TOOL_REQUESTED, {"id": call_id, "tool": tool, "arguments": args})


def _executed(call_id: str, output: str, is_error: bool = False) -> tuple[str, dict[str, Any]]:
    return (ev.TOOL_EXECUTED, {"id": call_id, "output": output, "is_error": is_error})


def test_an_empty_log_replays_to_nothing() -> None:
    state = rebuild([])
    assert state.messages == []
    assert state.next_iteration == 1
    assert not state.is_mid_iteration


def test_a_completed_iteration_rebuilds_the_conversation_in_order() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "summarise the repo"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use", "id": "t1"}]),
            _requested("t1", "read_file", path="src/app.py"),
            _executed("t1", "print('hello')"),
            (ev.ITERATION_STARTED, {"n": 2}),
        )
    )

    assert [type(m) for m in state.messages] == [
        UserMessage,
        AssistantMessage,
        ToolResultsMessage,
    ]
    assert state.messages[0].text == "summarise the repo"  # type: ignore[union-attr]
    assert state.messages[1].raw_content == [{"type": "tool_use", "id": "t1"}]  # type: ignore[union-attr]
    assert state.messages[2].results[0].content == "print('hello')"  # type: ignore[union-attr]
    assert not state.is_mid_iteration


def test_parallel_results_land_in_a_single_message() -> None:
    """Splitting them across messages teaches the model to stop making parallel calls."""
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "look"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use"}]),
            _requested("t1", "read_file", path="a.py"),
            _requested("t2", "read_file", path="b.py"),
            _executed("t1", "A"),
            _executed("t2", "B"),
            (ev.ITERATION_STARTED, {"n": 2}),
        )
    )

    results_messages = [m for m in state.messages if isinstance(m, ToolResultsMessage)]
    assert len(results_messages) == 1
    assert [r.content for r in results_messages[0].results] == ["A", "B"]


def test_a_refused_tool_replays_as_an_error_result() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "look"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use"}]),
            _requested("t1", "read_file", path=".env"),
            _executed("t1", "Refused by policy: secrets are never readable", is_error=True),
            (ev.ITERATION_STARTED, {"n": 2}),
        )
    )
    result = [m for m in state.messages if isinstance(m, ToolResultsMessage)][0].results[0]
    assert result.is_error is True
    assert "Refused by policy" in result.content


def test_spending_is_accumulated_from_the_log() -> None:
    """Resuming must not reset the budget: a crash is not a fresh allowance."""
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([], cost="0.030000"),
            (ev.ITERATION_STARTED, {"n": 2}),
            _model_called([], cost="0.020000"),
        )
    )
    assert state.spent == Decimal("0.050000")


def test_an_interrupted_iteration_reports_only_the_unexecuted_calls() -> None:
    """The heart of it: the first tool ran, the process died, the second never ran."""
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 3}),
            _model_called([{"type": "tool_use"}]),
            _requested("t1", "read_file", path="a.py"),
            _requested("t2", "read_file", path="b.py"),
            _executed("t1", "A"),
        )
    )

    assert state.is_mid_iteration
    assert [c.id for c in state.pending_tool_calls] == ["t2"]
    assert state.pending_tool_calls[0].arguments == {"path": "b.py"}
    # The one that did run comes back as a result, not as something to run again.
    assert [r.content for r in state.partial_results] == ["A"]
    # The interrupted iteration is finished, not restarted.
    assert state.next_iteration == 3


def test_the_assistant_turn_of_an_interrupted_iteration_is_kept() -> None:
    """Without it the pending tool_use blocks would have no turn to answer."""
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use", "id": "t1"}]),
            _requested("t1", "read_file", path="a.py"),
        )
    )
    assert isinstance(state.messages[-1], AssistantMessage)
    assert state.pending_tool_calls[0].id == "t1"


def test_a_clean_boundary_advances_to_the_next_iteration() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 4}),
            _model_called([]),
            _requested("t1", "read_file", path="a.py"),
            _executed("t1", "A"),
        )
    )
    assert not state.is_mid_iteration
    assert state.next_iteration == 5


def test_a_finished_task_is_reported_as_finished() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([]),
            (ev.TASK_FINISHED, {"status": "SUCCEEDED"}),
        )
    )
    assert state.finished is True


def test_events_out_of_order_are_sorted_before_replay() -> None:
    """The reader must not depend on the database handing rows back in order."""
    ordered = _events(
        (ev.TASK_CREATED, {"spec": "x"}),
        (ev.ITERATION_STARTED, {"n": 1}),
        _model_called([{"type": "text"}]),
    )
    shuffled = [ordered[2], ordered[0], ordered[1]]
    assert rebuild(shuffled).messages[0].text == "x"  # type: ignore[union-attr]


# --- approval decisions (ADR-022): read off the event log, never off `approvals` ------------


def test_a_pending_call_with_no_decision_yet_has_none_recorded() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use"}]),
            _requested("t1", "github.open_pr", title="x"),
            (ev.APPROVAL_REQUESTED, {"approval_id": "a1", "tool": "github.open_pr", "id": "t1"}),
        )
    )
    assert state.is_mid_iteration
    assert [c.id for c in state.pending_tool_calls] == ["t1"]
    assert state.approval_decisions == {}


def test_a_granted_approval_is_recorded_by_call_id() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use"}]),
            _requested("t1", "github.open_pr", title="x"),
            (ev.APPROVAL_REQUESTED, {"approval_id": "a1", "tool": "github.open_pr", "id": "t1"}),
            (ev.APPROVAL_GRANTED, {"approval_id": "a1", "id": "t1"}),
        )
    )
    outcome = state.approval_decisions["t1"]
    assert outcome.status == "approved"
    assert outcome.approval_id == "a1"
    assert outcome.note is None


def test_a_rejected_approval_carries_the_reviewers_note() -> None:
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called([{"type": "tool_use"}]),
            _requested("t1", "github.open_pr", title="x"),
            (ev.APPROVAL_REQUESTED, {"approval_id": "a1", "tool": "github.open_pr", "id": "t1"}),
            (ev.APPROVAL_REJECTED, {"approval_id": "a1", "id": "t1", "note": "too risky"}),
        )
    )
    outcome = state.approval_decisions["t1"]
    assert outcome.status == "rejected"
    assert outcome.note == "too risky"


def test_time_spent_waiting_for_a_decision_is_summed_from_the_event_timestamps() -> None:
    """ADR-022: a human's thinking time is not the agent's. Two pauses, one hour and ten
    minutes, add up; a request still waiting for its decision adds nothing yet."""
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    events = _events(
        (ev.TASK_CREATED, {"spec": "x"}),
        (ev.APPROVAL_REQUESTED, {"approval_id": "a1", "tool": "github.open_pr", "id": "t1"}),
        (ev.APPROVAL_GRANTED, {"approval_id": "a1", "id": "t1"}),
        (ev.APPROVAL_REQUESTED, {"approval_id": "a2", "tool": "github.open_pr", "id": "t2"}),
        (ev.APPROVAL_REJECTED, {"approval_id": "a2", "id": "t2", "note": "no"}),
        (ev.APPROVAL_REQUESTED, {"approval_id": "a3", "tool": "github.open_pr", "id": "t3"}),
    )
    stamps = [
        t0,
        t0,
        t0 + timedelta(hours=1),
        t0 + timedelta(hours=2),
        t0 + timedelta(hours=2, minutes=10),
        t0 + timedelta(hours=3),
    ]
    for event, stamp in zip(events, stamps, strict=True):
        event.created_at = stamp

    assert rebuild(events).paused_seconds == 3600 + 600


def test_raw_content_is_never_inspected() -> None:
    """ADR-016: opaque to everything outside the provider that produced it.

    Replay carries it through by identity, so a shape the control plane does not understand
    survives a resume untouched.
    """
    opaque = [{"type": "thinking", "thinking": "...", "signature": "abc"}]
    state = rebuild(
        _events(
            (ev.TASK_CREATED, {"spec": "x"}),
            (ev.ITERATION_STARTED, {"n": 1}),
            _model_called(opaque),
        )
    )
    assert state.messages[-1].raw_content is opaque  # type: ignore[union-attr]
