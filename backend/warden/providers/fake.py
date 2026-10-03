"""A provider that replays a scripted conversation instead of calling a model.

This is the most important file in the providers package. It makes the control plane
testable deterministically and for free: the agent loop, resume after crash, policy denial,
approval round trips and the behavioural evals in CI all run against it. What is under test
is the control plane, never the model.

The script format is the one briefing section 19 already defined for behavioural evals, so
week 6 reuses the same YAML files without translation.
"""

import asyncio
import pathlib
from collections.abc import Sequence
from typing import Any

import yaml
from pydantic import BaseModel, Field

from warden.plan.planner import PLAN_TOOL
from warden.providers.base import (
    AssistantMessage,
    Completion,
    Message,
    StopReason,
    ToolCall,
    ToolSchema,
    Usage,
)
from warden.verify.reviewer import VERDICT_TOOL


class ScriptExhausted(RuntimeError):
    """The loop asked for one more turn than the script has.

    Deliberately an error rather than an implicit `end_turn`: a loop with an off-by-one or
    a missed termination check would otherwise finish green and look correct.
    """


class ScriptStep(BaseModel):
    """One model turn. Either it calls tools, or it speaks and stops."""

    tool_calls: list[ToolCall] = Field(default_factory=list)
    text: str = ""
    # Seconds the "model" takes to answer. Zero for every normal script; a test that has to
    # kill a real process in the middle of a model call gives that call a long one, so the
    # kill lands inside the call instead of racing it.
    delay_seconds: float = 0.0

    @property
    def stop_reason(self) -> StopReason:
        return "tool_use" if self.tool_calls else "end_turn"


def _parse_step(raw: Any, index: int) -> ScriptStep:
    if not isinstance(raw, dict):
        raise ValueError(f"step {index}: expected a mapping, got {type(raw).__name__}")

    # `tool_call` (singular) is the shape the briefing wrote; `tool_calls` (plural) takes a
    # list and is how a script exercises the parallel tool call path in the loop.
    calls_raw = raw.get("tool_calls")
    if calls_raw is None and "tool_call" in raw:
        calls_raw = [raw["tool_call"]]
    calls_raw = calls_raw or []

    calls = [
        ToolCall(
            # Deterministic ids: a replayed script must produce byte-identical tool_call
            # rows, otherwise the resume tests cannot assert "no tool ran twice".
            id=f"fake-{index}-{position}",
            name=call["name"],
            arguments=call.get("args") or call.get("arguments") or {},
        )
        for position, call in enumerate(calls_raw)
    ]
    text = raw.get("text", "")
    if not calls and not text:
        raise ValueError(f"step {index}: needs either a tool call or text")
    return ScriptStep(tool_calls=calls, text=text, delay_seconds=float(raw.get("delay_seconds", 0)))


def _calls_verdict(step: ScriptStep) -> bool:
    return any(call.name == VERDICT_TOOL for call in step.tool_calls)


def _calls_plan(step: ScriptStep) -> bool:
    return any(call.name == PLAN_TOOL for call in step.tool_calls)


# What the planner answers when the script has no `submit_plan` step (ADR-031). Only a script
# that wants to assert on the plan's content carries a step; every other script, including all
# the existing ones, gets a valid plan for free and keeps its coder turns where they were.
_DEFAULT_PLAN = ScriptStep(
    tool_calls=[
        ToolCall(
            id="fake-plan-0",
            name=PLAN_TOOL,
            arguments={
                "steps": ["Read the relevant files", "Make the change", "Run the checks"],
                "likely_files": [],
                "risks": [],
                "tests_to_add": [],
            },
        )
    ]
)


class FakeProvider:
    """Replays `script`, one step per `generate()` call. Messages are ignored by design."""

    name = "fake"

    def __init__(
        self,
        script: Sequence[ScriptStep],
        *,
        source: str = "<inline>",
        model: str = "fake-model",
        resume_aware: bool = False,
    ) -> None:
        self._script = list(script)
        self._source = source
        self._model = model
        self._cursor = 0
        self._resume_aware = resume_aware

    @classmethod
    def from_yaml(
        cls, path: str | pathlib.Path, *, model: str = "fake-model", resume_aware: bool = False
    ) -> "FakeProvider":
        path = pathlib.Path(path)
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_steps = document.get("script")
        if not raw_steps:
            raise ValueError(f"{path}: no 'script' key, or it is empty")
        steps = [_parse_step(raw, index) for index, raw in enumerate(raw_steps)]
        return cls(steps, source=str(path), model=model, resume_aware=resume_aware)

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        offered = {tool.name for tool in tools or ()}
        if PLAN_TOOL in offered:
            # The planner (ADR-031) is the first call of a task and is no turn of the coder's
            # dialogue, so it never moves the cursor: the coder's first turn stays step 0 of
            # the script whether or not the script scripts a plan.
            at_cursor = self._cursor < len(self._script) and _calls_plan(self._script[self._cursor])
            planned = next((i for i, step in enumerate(self._script) if _calls_plan(step)), None)
            index = self._cursor if at_cursor else planned if self._resume_aware else None
            if index is None:
                return self._completion(_DEFAULT_PLAN, model)
            return await self._answer(index, model, advance=at_cursor)

        # Resume-aware: the step is the model's position in the dialogue, the number of
        # assistant turns already in it. The Worker builds a new provider for every claim, so a
        # task resumed after an approval or a crash meets a fresh instance; a cursor would
        # restart at step 0 and replay tool call ids the event log already holds. Off by
        # default because tests resume with a script of only the remaining steps.
        #
        # The plan and verdict steps are not turns of that dialogue (neither answer is an
        # assistant message in it), so they are left out of the numbering: counting them would
        # hand the coder `submit_plan` as its first turn, an off-by-one the moment a script
        # scripts a plan.
        if self._resume_aware:
            turns = [
                i
                for i, step in enumerate(self._script)
                if not _calls_plan(step) and not _calls_verdict(step)
            ]
            turn = sum(isinstance(message, AssistantMessage) for message in messages)
            index = turns[turn] if turn < len(turns) else len(self._script)
        else:
            index = self._cursor
        if self._resume_aware and VERDICT_TOOL in offered:
            # The independent reviewer (ADR-010) is a single message with no assistant turn in
            # it, so counting turns would hand it step 0 of the script. Its step is found by
            # content instead: the script's own `submit_verdict` call, wherever it sits.
            index = next(
                (i for i, step in enumerate(self._script) if _calls_verdict(step)),
                len(self._script),
            )
        return await self._answer(index, model)

    async def _answer(self, index: int, model: str | None, *, advance: bool = True) -> Completion:
        if index >= len(self._script):
            raise ScriptExhausted(
                f"script {self._source} has {len(self._script)} step(s) and all were "
                f"consumed; the loop asked for another turn"
            )
        step = self._script[index]
        if advance:
            self._cursor = index + 1
        if step.delay_seconds:
            await asyncio.sleep(step.delay_seconds)
        return self._completion(step, model)

    def _completion(self, step: ScriptStep, model: str | None) -> Completion:
        return Completion(
            provider=self.name,
            model=model or self._model,
            stop_reason=step.stop_reason,
            text=step.text,
            tool_calls=step.tool_calls,
            # Zeroed on purpose: a scripted run costs nothing, and `cost_usd` over a zero
            # usage is Decimal("0") for any priced model.
            usage=Usage(),
            raw_content=step.model_dump(mode="json"),
        )
