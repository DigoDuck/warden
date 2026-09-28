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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from warden.providers.base import ToolSchema

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from warden.identity.jwt import Claims


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
class ToolContext:
    """Identity handed to a tool registered with `needs_identity=True` (ADR-025).

    `claims` is what `tools/gateway.py` already verified before this call reached the
    registry: a live, task-bound token carrying whatever scope the tool required. `session`
    is the loop's own transaction, so a tool that needs to spend a credential
    (`identity.broker.get_credential`, which itself audits) writes its audit row in the same
    short transaction as everything else the call does, rather than opening a second one.
    """

    claims: "Claims"
    session: "AsyncSession"


@dataclass(frozen=True)
class RegisteredTool:
    schema: ToolSchema
    args_model: type[BaseModel]
    # Two call shapes share this one slot: `execute(args)` for an ordinary tool,
    # `execute(args, context)` when `needs_identity` is set. `Callable[..., Awaitable[str]]`
    # rather than a stricter alias because the registry itself picks which shape to call
    # (`ToolRegistry.execute`, below) based on `needs_identity`, not the type checker.
    execute: Callable[..., Awaitable[str]]
    # Which argument carries a filesystem path, if any. The loop asks for this so it can
    # normalise that argument once and hand the same value to the policy engine.
    path_arg: str | None = None
    # For a tool whose call can touch more than one path (apply_patch: a diff can rename
    # or edit several files), a coroutine that reports which ones, given the validated
    # arguments. Mutually exclusive with `path_arg` in practice: when both are set, the
    # inspector wins, because it is the more precise answer.
    path_inspector: Callable[[Any], Awaitable[list[str]]] | None = None
    # The scope `tools/gateway.py` requires the call's token to carry before this tool ever
    # runs. None means any live, task-bound token is enough, which is every tool before
    # github.open_pr: the policy engine already decided the call may run, and there is no
    # narrower credential underneath it to gate a second time.
    required_scope: str | None = None
    # True for a tool whose executor needs `ToolContext` (github.open_pr, so it can reach
    # the secret broker). Kept a flag rather than inspecting `execute`'s arity: an explicit
    # opt-in at registration is one line to read, a `inspect.signature` probe is not.
    needs_identity: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        execute: Callable[..., Awaitable[str]],
        path_arg: str | None = None,
        path_inspector: Callable[[Any], Awaitable[list[str]]] | None = None,
        required_scope: str | None = None,
        needs_identity: bool = False,
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
            required_scope=required_scope,
            needs_identity=needs_identity,
        )

    def path_arg(self, name: str) -> str | None:
        tool = self._tools.get(name)
        return tool.path_arg if tool else None

    def required_scope(self, name: str) -> str | None:
        """The scope `tools/gateway.py` must find on a call's token before running `name`,
        or None for a tool no narrower credential gates (every tool but github.open_pr)."""
        tool = self._tools.get(name)
        return tool.required_scope if tool else None

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

    async def execute(
        self, name: str, arguments: dict[str, Any], *, context: ToolContext | None = None
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            raise UnknownToolError(f"unknown tool {name!r}; registered tools: {known}")

        validated = self._validate(name, tool, arguments)
        if tool.needs_identity:
            if context is None:
                # A caller bug, not a model-facing refusal: `tools/gateway.py` is the only
                # caller that ever has a `ToolContext` to give, so reaching here with none
                # means something called `execute()` directly for a tool that requires the
                # gateway's verification step first.
                raise ToolError(f"tool {name!r} requires identity context but none was given")
            return await tool.execute(validated, context)
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
