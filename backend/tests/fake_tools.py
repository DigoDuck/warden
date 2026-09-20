"""An in-memory tool surface for tests whose subject is the loop, not the tools.

The real tools execute inside a container, which costs a second or two per test. That price
is worth paying where containment is the thing under test (`test_sandboxed_tools.py`) and is
pure waste where the question is whether the loop records an event or resumes correctly.

The behaviour mirrored here is only what those tests depend on: a read that returns content,
a listing, and a refusal for a path that leaves the workspace.
"""

from typing import Any

from pydantic import BaseModel, Field

from warden.tools.registry import ToolError, ToolRegistry


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path to the file, relative to the workspace root")


class ListFilesArgs(BaseModel):
    pattern: str = Field(default="**/*", description="Glob relative to the workspace root")


class FakeWorkspace:
    """Files in a dictionary, plus a count of what actually executed.

    The execution count matters in the resume tests: the `tool_calls` assertion proves no
    duplicate row was written, and this proves the side effect itself did not happen twice.
    Those are different claims.
    """

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {"src/app.py": "print('hello')\n"})
        self.executions: list[str] = []
        self.crash_after: int | None = None

    async def read_file(self, args: ReadFileArgs) -> str:
        self._record("read_file")
        if ".." in args.path.split("/") or args.path.startswith("/"):
            raise ToolError(f"path {args.path!r} resolves outside the workspace and was refused")
        if args.path not in self.files:
            raise ToolError(f"{args.path!r} is not a file in the workspace")
        return self.files[args.path]

    async def list_files(self, args: ListFilesArgs) -> str:
        self._record("list_files")
        return "\n".join(sorted(self.files))

    def _record(self, name: str) -> None:
        if self.crash_after is not None and len(self.executions) >= self.crash_after:
            # Not a ToolError: this is a process falling over, not a tool refusing.
            raise RuntimeError("worker died mid-iteration")
        self.executions.append(name)

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            args_model=ReadFileArgs,
            execute=self.read_file,
            path_arg="path",
        )
        registry.register(
            name="list_files",
            description="List files in the workspace matching a glob pattern.",
            args_model=ListFilesArgs,
            execute=self.list_files,
        )
        return registry


def fake_registry(files: dict[str, Any] | None = None) -> ToolRegistry:
    return FakeWorkspace(files).registry()
