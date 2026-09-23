"""A provider that replays a scripted conversation instead of calling a model.

This is the most important file in the providers package. It makes the control plane
testable deterministically and for free: the agent loop, resume after crash, policy denial,
approval round trips and the behavioural evals in CI all run against it. What is under test
is the control plane, never the model.

The script format is the one briefing section 19 already defined for behavioural evals, so
week 6 reuses the same YAML files without translation.
"""

import pathlib
from collections.abc import Sequence
from typing import Any

import yaml
from pydantic import BaseModel, Field

from warden.providers.base import (
    Completion,
    Message,
    StopReason,
    ToolCall,
    ToolSchema,
    Usage,
)


class ScriptExhausted(RuntimeError):
    """The loop asked for one more turn than the script has.

    Deliberately an error rather than an implicit `end_turn`: a loop with an off-by-one or
    a missed termination check would otherwise finish green and look correct.
    """


class ScriptStep(BaseModel):
    """One model turn. Either it calls tools, or it speaks and stops."""

    tool_calls: list[ToolCall] = Field(default_factory=list)
    text: str = ""

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
    return ScriptStep(tool_calls=calls, text=text)


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
        if self._cursor >= len(self._script):
            raise ScriptExhausted(
                f"script {self._source} has {len(self._script)} step(s) and all were "
                f"consumed; the loop asked for another turn"
            )
        step = self._script[self._cursor]
        self._cursor += 1

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
