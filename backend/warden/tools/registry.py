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
    # Which argument carries a filesystem path, if any. The loop asks for this so it can
    # normalise that argument once and hand the same value to the policy engine.
    path_arg: str | None = None
    # For a tool whose call can touch more than one path (apply_patch: a diff can rename
    # or edit several files), a coroutine that reports which ones, given the validated
    # arguments. Mutually exclusive with `path_arg` in practice: when both are set, the
    # inspector wins, because it is the more precise answer.
    path_inspector: Callable[[Any], Awaitable[list[str]]] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        execute: Callable[[Any], Awaitable[str]],
        path_arg: str | None = None,
        path_inspector: Callable[[Any], Awaitable[list[str]]] | None = None,
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
            path_arg=path_arg,
            path_inspector=path_inspector,
        )

    def path_arg(self, name: str) -> str | None:
        tool = self._tools.get(name)
        return tool.path_arg if tool else None

    def schemas(self) -> list[ToolSchema]:
        # Sorted so the tool list is byte-identical between runs. A varying tool order
        # silently invalidates the prompt cache and makes replay diffs noisy.
        return [self._tools[name].schema for name in sorted(self._tools)]

    def has(self, name: str) -> bool:
        return name in self._tools

    def _validate(self, name: str, tool: RegisteredTool, arguments: dict[str, Any]) -> BaseModel:
        try:
            return tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            # The message goes back to the model, which can correct itself on the next
            # turn, so it has to say what was wrong rather than just that something was.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise InvalidArgumentsError(f"invalid arguments for {name!r}: {problems}") from exc

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            raise UnknownToolError(f"unknown tool {name!r}; registered tools: {known}")

        validated = self._validate(name, tool, arguments)
        return await tool.execute(validated)

    async def touched_paths(self, name: str, arguments: dict[str, Any]) -> list[str | None]:
        """The raw, un-normalised paths one call would touch, for the policy engine.

        Three shapes, in the order the loop needs them: a tool with a `path_inspector`
        (`apply_patch`) gets whatever it reports, which can be more than one path; a tool
        with a plain `path_arg` gets that single argument, wrapped in a one-item list; a
        tool with neither (`run_command`, `list_files`) gets `[None]`, so the caller still
        evaluates policy exactly once, on no path, instead of skipping the call entirely.

        Unknown tool names are not this method's problem: `execute()` raises
        `UnknownToolError` for those, and the caller (the loop) reaches that only after
        judging a call that will turn out to be for a tool that does not exist, same as
        today. Arguments are validated here first, same as `execute()`, and can raise
        `InvalidArgumentsError`: a call the model got wrong should not lie to the policy
        engine about a path the model never actually gave it.
        """
        tool = self._tools.get(name)
        if tool is None:
            return [None]

        if tool.path_inspector is not None:
            validated = self._validate(name, tool, arguments)
            return list(await tool.path_inspector(validated))

        if tool.path_arg is not None:
            raw = arguments.get(tool.path_arg)
            return [raw if isinstance(raw, str) else None]

        return [None]
