"""Tools that execute inside the hardened container.

This replaces the host-executing version that `local.py` carried while there was no
sandbox. There is deliberately no fallback to running on the host: two execution paths is
how you end up with one that nobody tests and that quietly becomes the one in use.

Containment now happens where the file is actually opened. Resolving a path against a copy
of the workspace on the host was a good enough approximation while the two were identical,
but it stops being true the moment the agent can create a symlink inside the container. The
probes below call `realpath` in the container and refuse anything that lands outside the
workspace root.

The path arrives as `argv`, never interpolated into the script. An argument is data; a
formatted string would be code, and the value comes from the model.

ponytail: each call starts a CPython interpreter in the container, which measures around
340ms against microseconds for the host read this replaced. Fine while an iteration is
dominated by a model call of seconds. If tool latency ever shows up in the metrics, the
upgrade is a resident helper process in the image reading commands from a pipe, which
removes the interpreter startup without giving up containment.
"""

import pathlib

from pydantic import BaseModel, Field

from warden.sandbox.docker import WORKSPACE, Sandbox
from warden.tools.registry import ToolError, ToolRegistry
from warden.tools.workspace import IGNORED_DIRS

MAX_READ_BYTES = 64_000
MAX_LISTED_FILES = 200

# Shared preamble: resolve the argument under the workspace root or refuse. `realpath`
# follows symlinks, so a link planted inside the workspace pointing out is caught here.
_CONTAIN = f"""
import os, sys
root = os.path.realpath({WORKSPACE!r})
target = os.path.realpath(os.path.join(root, sys.argv[1]))
if target != root and not target.startswith(root + os.sep):
    print("__REFUSED__", file=sys.stderr)
    sys.exit(3)
"""

_READ_FILE = (
    _CONTAIN
    + f"""
if not os.path.isfile(target):
    print("__NOT_A_FILE__", file=sys.stderr)
    sys.exit(4)
with open(target, "rb") as handle:
    data = handle.read()
sys.stdout.write(data[:{MAX_READ_BYTES}].decode("utf-8", "replace"))
if len(data) > {MAX_READ_BYTES}:
    sys.stdout.write(
        "\\n\\n[truncated: %d bytes total, first {MAX_READ_BYTES} shown]" % len(data)
    )
"""
)

_LIST_FILES = f"""
import os, sys, fnmatch, pathlib
root = os.path.realpath({WORKSPACE!r})
pattern = sys.argv[1]
ignored = set({sorted(IGNORED_DIRS)!r})
matches = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in ignored]
    for name in filenames:
        full = os.path.join(dirpath, name)
        # Every hit is re-checked rather than trusted because the walk produced it: a
        # symlinked directory can lead the walk outside the root.
        if os.path.realpath(full) != full and not os.path.realpath(full).startswith(root + os.sep):
            continue
        relative = pathlib.PurePath(os.path.relpath(full, root)).as_posix()
        if pathlib.PurePosixPath(relative).full_match(pattern):
            matches.append(relative)
matches.sort()
if not matches:
    print("no files match %r" % pattern)
else:
    sys.stdout.write("\\n".join(matches[:{MAX_LISTED_FILES}]))
    if len(matches) > {MAX_LISTED_FILES}:
        sys.stdout.write(
            "\\n[truncated: %d matches, first {MAX_LISTED_FILES} shown]" % len(matches)
        )
"""


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path to the file, relative to the workspace root")


class ListFilesArgs(BaseModel):
    pattern: str = Field(
        default="**/*",
        description="Glob relative to the workspace root, for example 'src/**/*.py'",
    )


async def _run_probe(sandbox: Sandbox, script: str, argument: str, *, what: str) -> str:
    result = await sandbox.exec(["python", "-c", script, argument], kill_after=30)
    if result.exit_code == 3:
        raise ToolError(f"path {argument!r} resolves outside the workspace and was refused")
    if result.exit_code == 4:
        raise ToolError(f"{argument!r} is not a file in the workspace")
    if result.exit_code != 0:
        raise ToolError(f"{what} failed: {result.output.strip()[:300]}")
    return result.output


async def read_file(sandbox: Sandbox, args: ReadFileArgs) -> str:
    return await _run_probe(sandbox, _READ_FILE, args.path, what="read_file")


async def list_files(sandbox: Sandbox, args: ListFilesArgs) -> str:
    return await _run_probe(sandbox, _LIST_FILES, args.pattern, what="list_files")


def build_registry(sandbox: Sandbox) -> ToolRegistry:
    """The tool surface for a task, bound to that task's container.

    `finish` is absent on purpose: briefing section 15 puts its executor in `core`, because
    it ends the task rather than producing a tool result.
    """
    registry = ToolRegistry()
    registry.register(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        args_model=ReadFileArgs,
        execute=lambda args: read_file(sandbox, args),
        path_arg="path",
    )
    registry.register(
        name="list_files",
        description="List files in the workspace matching a glob pattern.",
        args_model=ListFilesArgs,
        # No path_arg: the argument is a glob, not a path. What it can reach is bounded by
        # the container, and the policy rules it by tool name.
        execute=lambda args: list_files(sandbox, args),
    )
    return registry


def workspace_source(repo_root: pathlib.Path) -> pathlib.Path:
    """Where the workspace is copied from on the first sandbox for a task."""
    return repo_root / "examples" / "target-repo"
