"""The FakeProvider is what makes the control plane testable for free, so it gets tested."""

import pathlib
import time
from decimal import Decimal

import pytest

from warden.providers.base import AssistantMessage, Message, ToolCall, ToolSchema, UserMessage
from warden.providers.fake import FakeProvider, ScriptExhausted, ScriptStep
from warden.providers.pricing import cost_usd

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "script_minimal.yaml"
ONE_TURN = [UserMessage(text="read the repo")]


async def test_script_replays_in_order() -> None:
    provider = FakeProvider.from_yaml(FIXTURE)

    first = await provider.generate(ONE_TURN)
    assert first.stop_reason == "tool_use"
    assert [c.name for c in first.tool_calls] == ["read_file"]
    assert first.tool_calls[0].arguments == {"path": ".env"}

    second = await provider.generate(ONE_TURN)
    assert [c.name for c in second.tool_calls] == ["read_file", "list_files"]

    third = await provider.generate(ONE_TURN)
    assert third.stop_reason == "end_turn"
    assert third.text == "done"
    assert third.tool_calls == []


async def test_parallel_step_yields_distinct_ids() -> None:
    """Tool call ids key the tool_result blocks; a collision would cross the results over."""
    provider = FakeProvider.from_yaml(FIXTURE)
    await provider.generate(ONE_TURN)
    parallel = await provider.generate(ONE_TURN)

    ids = [c.id for c in parallel.tool_calls]
    assert len(set(ids)) == len(ids)


async def test_ids_are_deterministic_across_runs() -> None:
    """Resume tests assert that no tool ran twice, which needs stable ids on replay."""
    first_run = await FakeProvider.from_yaml(FIXTURE).generate(ONE_TURN)
    second_run = await FakeProvider.from_yaml(FIXTURE).generate(ONE_TURN)
    assert first_run.tool_calls[0].id == second_run.tool_calls[0].id


async def test_exhausted_script_raises_instead_of_ending_quietly() -> None:
    """An implicit end_turn here would let a loop with a broken exit check pass green."""
    provider = FakeProvider.from_yaml(FIXTURE)
    for _ in range(3):
        await provider.generate(ONE_TURN)

    with pytest.raises(ScriptExhausted) as excinfo:
        await provider.generate(ONE_TURN)
    assert "script_minimal.yaml" in str(excinfo.value)


async def test_scripted_run_is_free() -> None:
    completion = await FakeProvider.from_yaml(FIXTURE).generate(ONE_TURN)
    assert completion.usage.input_tokens == 0
    assert cost_usd("claude-opus-5", completion.usage) == Decimal("0")


async def test_raw_content_survives_a_json_round_trip() -> None:
    """raw_content is persisted in task_events.payload (JSONB) and replayed from there."""
    import json

    completion = await FakeProvider.from_yaml(FIXTURE).generate(ONE_TURN)
    assert json.loads(json.dumps(completion.raw_content)) == completion.raw_content


def test_script_without_steps_is_rejected(tmp_path: pathlib.Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("script: []", encoding="utf-8")
    with pytest.raises(ValueError):
        FakeProvider.from_yaml(empty)


def test_step_with_neither_tool_call_nor_text_is_rejected(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("script:\n  - {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        FakeProvider.from_yaml(bad)


async def test_a_resume_aware_provider_picks_the_step_from_the_conversation() -> None:
    """The Worker builds a new provider for every claim, so a task resumed after an approval
    or a crash meets a fresh FakeProvider. A cursor would restart the script at step 0 and
    replay tool call ids the log already holds. Resume-aware, the provider counts the
    assistant turns already in the conversation instead, which is what a real model's
    position in the dialogue is."""
    history: list[Message] = [
        UserMessage(text="read the repo"),
        AssistantMessage(raw_content={"step": "zero"}),
    ]
    fresh = FakeProvider.from_yaml(FIXTURE, resume_aware=True)

    completion = await fresh.generate(history)

    assert [call.id for call in completion.tool_calls] == ["fake-1-0", "fake-1-1"]
    assert [call.name for call in completion.tool_calls] == ["read_file", "list_files"]


VERDICT_SCRIPT = """
script:
  - tool_call: { name: finish, args: { summary: "done" } }
  - tool_call: { name: submit_verdict, args: { passed: true, findings: [] } }
"""


async def test_a_resume_aware_provider_finds_the_reviewers_step_by_content(
    tmp_path: pathlib.Path,
) -> None:
    """The reviewer's request has no assistant turn in it, so counting turns would hand it
    step 0 (`finish`). Its step is the script's own `submit_verdict`, whatever its position."""
    script = tmp_path / "script.yaml"
    script.write_text(VERDICT_SCRIPT, encoding="utf-8")
    fresh = FakeProvider.from_yaml(script, resume_aware=True)
    verdict_tool = ToolSchema(name="submit_verdict", description="", input_schema={})

    completion = await fresh.generate([UserMessage(text="spec + evidence")], tools=[verdict_tool])

    assert [call.name for call in completion.tool_calls] == ["submit_verdict"]


async def test_a_step_can_take_time_to_answer(tmp_path: pathlib.Path) -> None:
    """A scripted delay is what lets a test kill a real process inside a model call."""
    script = tmp_path / "script.yaml"
    script.write_text("script:\n  - text: slow\n    delay_seconds: 0.2\n", encoding="utf-8")
    started = time.monotonic()

    await FakeProvider.from_yaml(script).generate(ONE_TURN)

    assert time.monotonic() - started >= 0.15


# ADR-031: the planner's `submit_plan` is not an assistant turn of the coder's dialogue, so
# the scripted provider must not count it (or the reviewer's verdict) as one.
PLAN_TOOL_SCHEMA = ToolSchema(name="submit_plan", description="plan", input_schema={})


def _plan_step() -> ScriptStep:
    call = ToolCall(
        id="x",
        name="submit_plan",
        arguments={"steps": ["scripted"], "likely_files": [], "risks": [], "tests_to_add": []},
    )
    return ScriptStep(tool_calls=[call])


def _tool_step(name: str) -> ScriptStep:
    return ScriptStep(tool_calls=[ToolCall(id="x", name=name, arguments={})])


async def test_a_plan_is_answered_by_default_without_consuming_a_script_step() -> None:
    """A script with no `submit_plan` step still gets a plan, and the coder's first turn is
    still step 0: only a script that wants to test plan content has to carry a step."""
    provider = FakeProvider([_tool_step("list_files"), _tool_step("finish")])

    plan = await provider.generate(ONE_TURN, tools=[PLAN_TOOL_SCHEMA])
    assert [c.name for c in plan.tool_calls] == ["submit_plan"]
    assert plan.tool_calls[0].arguments["steps"]

    first = await provider.generate(ONE_TURN)
    assert [c.name for c in first.tool_calls] == ["list_files"]


async def test_a_scripted_plan_step_is_used_in_cursor_mode() -> None:
    provider = FakeProvider([_plan_step(), _tool_step("finish")])

    plan = await provider.generate(ONE_TURN, tools=[PLAN_TOOL_SCHEMA])
    assert plan.tool_calls[0].arguments["steps"] == ["scripted"]
    coder = await provider.generate(ONE_TURN)
    assert [c.name for c in coder.tool_calls] == ["finish"]


async def test_resume_aware_turn_index_skips_plan_and_verdict_steps() -> None:
    """The off-by-one this fixes: `submit_plan` leads the script, but the coder's first turn
    (no assistant message yet) must be the first step that is not the planner's or the
    reviewer's."""
    script = [
        _plan_step(),
        _tool_step("list_files"),
        _tool_step("finish"),
        _tool_step("submit_verdict"),
    ]
    provider = FakeProvider(script, resume_aware=True)
    assistant = AssistantMessage(raw_content={})

    first = await provider.generate(ONE_TURN)
    assert [c.name for c in first.tool_calls] == ["list_files"]
    second = await provider.generate([*ONE_TURN, assistant])
    assert [c.name for c in second.tool_calls] == ["finish"]

    plan = await provider.generate(ONE_TURN, tools=[PLAN_TOOL_SCHEMA])
    assert plan.tool_calls[0].arguments["steps"] == ["scripted"]
