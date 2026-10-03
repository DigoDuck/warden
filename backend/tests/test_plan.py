"""plan/planner.py: one advisory model call, a structured plan, and what the coder is shown
of it (ADR-031).

No database and no container. A recording provider stands in for the model because the
FakeProvider ignores the messages it is sent, and this file's point is to look at them.
"""

from collections.abc import Sequence
from typing import Any

from warden.plan.planner import (
    PLAN_TOOL,
    PLANNER_SYSTEM_PROMPT,
    ProviderPlanner,
    plan_message,
)
from warden.providers.base import Completion, Message, ToolSchema, Usage, UserMessage
from warden.providers.base import ToolCall as ProviderToolCall

SPEC = "make average() ignore None values"
GOOD = {
    "steps": ["read src/stats.py", "skip None in average()"],
    "likely_files": ["src/stats.py"],
    "risks": ["an empty list now divides by zero"],
    "tests_to_add": ["average([1, None, 3]) == 2"],
}


class _Recording:
    name = "fake"

    def __init__(self, completion: Completion) -> None:
        self._completion = completion
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        self.calls.append(
            {"messages": list(messages), "tools": list(tools or []), "system": system}
        )
        return self._completion


def _answer(arguments: dict[str, Any], name: str = PLAN_TOOL) -> Completion:
    return Completion(
        provider="fake",
        model="fake-model",
        stop_reason="tool_use",
        tool_calls=[ProviderToolCall(id="p1", name=name, arguments=arguments)],
        usage=Usage(),
    )


async def test_a_well_formed_plan_is_parsed() -> None:
    result = await ProviderPlanner(_Recording(_answer(GOOD))).plan(SPEC)

    assert result.malformed_reason is None
    assert result.plan is not None
    assert result.plan.steps == GOOD["steps"]
    assert result.plan.likely_files == GOOD["likely_files"]
    assert result.plan.risks == GOOD["risks"]
    assert result.plan.tests_to_add == GOOD["tests_to_add"]


async def test_the_planner_is_shown_the_spec_and_only_the_plan_tool() -> None:
    provider = _Recording(_answer(GOOD))
    await ProviderPlanner(provider).plan(SPEC)

    (call,) = provider.calls
    assert call["messages"] == [UserMessage(text=SPEC)]
    assert [tool.name for tool in call["tools"]] == [PLAN_TOOL]
    # Its own prompt, not the coder's: a different job asked of a different context.
    assert call["system"] == PLANNER_SYSTEM_PROMPT
    assert PLANNER_SYSTEM_PROMPT.strip()


async def test_no_plan_tool_call_is_malformed_not_an_exception() -> None:
    text_only = Completion(
        provider="fake", model="fake-model", stop_reason="end_turn", text="sure", usage=Usage()
    )
    result = await ProviderPlanner(_Recording(text_only)).plan(SPEC)

    assert result.plan is None
    assert result.malformed_reason is not None
    assert PLAN_TOOL in result.malformed_reason


async def test_a_plan_with_no_steps_is_malformed() -> None:
    """A plan that says nothing to do is not a plan; `steps` must hold at least one."""
    result = await ProviderPlanner(_Recording(_answer({**GOOD, "steps": []}))).plan(SPEC)

    assert result.plan is None
    assert result.malformed_reason is not None
    assert "steps" in result.malformed_reason


async def test_a_wrongly_typed_field_is_malformed_and_names_it() -> None:
    result = await ProviderPlanner(_Recording(_answer({**GOOD, "risks": "none"}))).plan(SPEC)

    assert result.plan is None
    assert result.malformed_reason is not None
    assert "risks" in result.malformed_reason


async def test_the_plan_the_coder_reads_is_labelled_as_unverified_model_output() -> None:
    text = plan_message(GOOD)

    assert "not verified" in text
    assert "planner" in text
    for line in (*GOOD["steps"], *GOOD["likely_files"], *GOOD["risks"], *GOOD["tests_to_add"]):
        assert line in text
