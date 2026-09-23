"""The FakeProvider is what makes the control plane testable for free, so it gets tested."""

import pathlib
from decimal import Decimal

import pytest

from warden.providers.base import AssistantMessage, UserMessage
from warden.providers.fake import FakeProvider, ScriptExhausted
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
    history = [
        UserMessage(text="read the repo"),
        AssistantMessage(raw_content={"step": "zero"}),
    ]
    fresh = FakeProvider.from_yaml(FIXTURE, resume_aware=True)

    completion = await fresh.generate(history)

    assert [call.id for call in completion.tool_calls] == ["fake-1-0", "fake-1-1"]
    assert [call.name for call in completion.tool_calls] == ["read_file", "list_files"]
