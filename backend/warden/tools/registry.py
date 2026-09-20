"""Maps a tool name to the function that runs it, and to the schema the model sees.

The registry does no authorisation. Deciding whether a call may run is the policy engine's
job (week 2); this module only knows how to describe a tool, validate its arguments and
invoke it.

One Pydantic model per tool is the single source of truth for both halves: the JSON schema
the model is shown, and the validation the arguments must pass before any Python function
sees them. Model output is untrusted input, so `execute(**arguments)` straight off the wire
is not an option.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from warden.providers.base import ToolSchema


class ToolError(Exception):
    """A tool refused or failed in a way the model should see and can react to.

    Raised rather than returned so a tool cannot forget to signal failure. The loop turns it
    into a `tool_result` with `is_error` and carries on: a rejected tool call is a normal
    event in an agent loop, not a crash.
    """


class UnknownToolError(ToolError):
    """The model asked for a tool that is not registered."""


class InvalidArgumentsError(ToolError):
    """The model sent arguments that do not match the tool's schema."""


@dataclass(frozen=True)
class RegisteredTool:
    schema: ToolSchema
    args_model: type[BaseModel]
    execute: Callable[[Any], Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        execute: Callable[[Any], Awaitable[str]],
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = RegisteredTool(
            schema=ToolSchema(
                name=name,
                description=description,
                input_schema=args_model.model_json_schema(),
            ),
            args_model=args_model,
            execute=execute,
        )

    def schemas(self) -> list[ToolSchema]:
        # Sorted so the tool list is byte-identical between runs. A varying tool order
        # silently invalidates the prompt cache and makes replay diffs noisy.
        return [self._tools[name].schema for name in sorted(self._tools)]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            raise UnknownToolError(f"unknown tool {name!r}; registered tools: {known}")

        try:
            validated = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            # The message goes back to the model, which can correct itself on the next
            # turn, so it has to say what was wrong rather than just that something was.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise InvalidArgumentsError(f"invalid arguments for {name!r}: {problems}") from exc

        return await tool.execute(validated)
