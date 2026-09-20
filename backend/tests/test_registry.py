"""`ToolRegistry.touched_paths`: the three shapes a call's paths can come in.

Fast and container-free on purpose: this is about the registry choosing the right source
of paths, not about anything a real tool does. `test_write_tools.py` covers `apply_patch`'s
own inspector against a real container; this file is where a fake inspector proves the
registry wires it up correctly.
"""

from typing import Any

import pytest
from pydantic import BaseModel

from warden.tools.registry import InvalidArgumentsError, ToolRegistry


class _PathArgs(BaseModel):
    path: str


class _DiffArgs(BaseModel):
    diff: str


class _NoPathArgs(BaseModel):
    pattern: str = "**/*"


async def _inspector(args: _DiffArgs) -> list[str]:
    # A stand-in for apply_patch's real one: pretend every diff touches two paths.
    return [f"{args.diff}.a", f"{args.diff}.b"]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="write_file",
        description="",
        args_model=_PathArgs,
        execute=lambda args: _unused(args),
        path_arg="path",
    )
    registry.register(
        name="apply_patch",
        description="",
        args_model=_DiffArgs,
        execute=lambda args: _unused(args),
        path_inspector=_inspector,
    )
    registry.register(
        name="list_files",
        description="",
        args_model=_NoPathArgs,
        execute=lambda args: _unused(args),
    )
    return registry


async def _unused(args: Any) -> str:
    raise AssertionError("execute() should not run in these tests")


async def test_a_tool_with_only_path_arg_reports_that_one_path() -> None:
    paths = await _registry().touched_paths("write_file", {"path": "src/a.py"})
    assert paths == ["src/a.py"]


async def test_a_tool_with_an_inspector_reports_whatever_it_returns() -> None:
    paths = await _registry().touched_paths("apply_patch", {"diff": "d"})
    assert paths == ["d.a", "d.b"]


async def test_a_tool_with_neither_reports_a_single_none() -> None:
    paths = await _registry().touched_paths("list_files", {"pattern": "**/*.py"})
    assert paths == [None]


async def test_an_unknown_tool_reports_a_single_none_rather_than_raising() -> None:
    """`execute()` is where an unknown tool becomes an error; judging a call that will
    turn out to be for a tool that does not exist still needs a context to judge."""
    paths = await _registry().touched_paths("delete_everything", {})
    assert paths == [None]


async def test_a_non_string_path_arg_value_reports_none() -> None:
    paths = await _registry().touched_paths("write_file", {"path": 123})
    assert paths == [None]


async def test_the_inspector_only_sees_validated_arguments() -> None:
    """Deliverable 2's requirement made concrete: arguments are checked against the args
    model before the inspector runs, same as before execute() runs."""
    with pytest.raises(InvalidArgumentsError, match="diff"):
        await _registry().touched_paths("apply_patch", {"not_diff": "x"})
