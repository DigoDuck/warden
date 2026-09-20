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
formatted string would be code, and the value comes from the model. `write_file` goes one
step further: even the file *content* never touches argv or a script literal. It travels as
bytes on a staged upload, and the probe that checks and writes it only ever sees a path.

ponytail: each call starts a CPython interpreter in the container, which measures around
340ms against microseconds for the host read this replaced. Fine while an iteration is
dominated by a model call of seconds. If tool latency ever shows up in the metrics, the
upgrade is a resident helper process in the image reading commands from a pipe, which
removes the interpreter startup without giving up containment.
"""

import shlex
import uuid

from pydantic import BaseModel, Field

from warden.sandbox.docker import MOUNT_ROOT, WORKSPACE, CommandTimeout, ExecResult, Sandbox
from warden.tools.registry import ToolError, ToolRegistry
from warden.tools.workspace import IGNORED_DIRS, normalize_path

MAX_READ_BYTES = 64_000
MAX_LISTED_FILES = 200
# 1 MB: generous for source files, small enough that a runaway write cannot fill the
# workspace volume or blow the event log storing the tool call's arguments.
MAX_WRITE_BYTES = 1_000_000
MAX_COMMAND_OUTPUT = 20_000
RUN_COMMAND_DEFAULT_TIMEOUT = 60.0
# pytest gets more room than an arbitrary command: a real suite can legitimately take longer
# than the one-minute default without being stuck.
RUN_TESTS_TIMEOUT = 120.0
# Outside the workspace, so a staged or refused upload can never be mistaken for a file the
# agent's task actually produced.
STAGING_DIR = ".warden"

# Shared preamble: resolve the argument under the workspace root or refuse. `realpath`
# follows symlinks all the way to their final target, so this alone catches a relative
# escape, an absolute path, a symlinked parent directory pointing outside, and an existing
# target that is itself a symlink pointing outside: every case has the same shape once
# resolved, an absolute path that is not under the workspace root.
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

# Same containment check as every other probe, then the write itself: create whatever parent
# directories are missing and move the staged upload onto the target. One process, so there
# is no window between "is this path allowed" and "write it" for a symlink planted between
# two separate container calls to land in.
_WRITE_FILE = (
    _CONTAIN
    + f"""
staged = os.path.join({MOUNT_ROOT!r}, sys.argv[2])
os.makedirs(os.path.dirname(target), exist_ok=True)
os.replace(staged, target)
"""
)


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path to the file, relative to the workspace root")


class ListFilesArgs(BaseModel):
    pattern: str = Field(
        default="**/*",
        description="Glob relative to the workspace root, for example 'src/**/*.py'",
    )


class WriteFileArgs(BaseModel):
    path: str = Field(description="Path to write, relative to the workspace root")
    content: str = Field(description="UTF-8 text content for the file")


class RunCommandArgs(BaseModel):
    cmd: str = Field(
        description=(
            "The command as one string, for example 'pytest -q tests/'. There is no shell: "
            "it is split with shlex and the argv executed directly, so pipes, redirection, "
            "substitution and chaining with && are not available."
        )
    )
    timeout_seconds: float = Field(
        default=RUN_COMMAND_DEFAULT_TIMEOUT,
        ge=1,
        le=300,
        description="Kill the command if it runs longer than this many seconds.",
    )


class RunTestsArgs(BaseModel):
    path: str | None = Field(
        default=None,
        description="Workspace-relative file or directory to test; omit to run the whole suite",
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


def _truncate(output: str) -> str:
    """Keep both ends of long output rather than just the head.

    A test failure worth seeing is as likely to be the last line (the assertion) as the
    first (the command that started), so a plain head truncation would routinely cut the
    one line that explains what went wrong.
    """
    if len(output) <= MAX_COMMAND_OUTPUT:
        return output
    half = MAX_COMMAND_OUTPUT // 2
    return (
        f"{output[:half]}\n\n"
        f"[truncated: {len(output)} chars total, showing the first and last {half}]\n\n"
        f"{output[-half:]}"
    )


async def _exec_or_timeout_error(
    sandbox: Sandbox, argv: list[str], *, kill_after: float
) -> ExecResult:
    """Run argv, turning a killed deadline into a ToolError.

    `CommandTimeout` is a `SandboxError`, not a `ToolError`: left alone it would propagate
    past the registry and take the whole task down instead of becoming a normal, recoverable
    tool result the model can see and react to.
    """
    try:
        return await sandbox.exec(argv, kill_after=kill_after)
    except CommandTimeout as exc:
        raise ToolError(str(exc)) from exc


async def read_file(sandbox: Sandbox, args: ReadFileArgs) -> str:
    return await _run_probe(sandbox, _READ_FILE, args.path, what="read_file")


async def list_files(sandbox: Sandbox, args: ListFilesArgs) -> str:
    return await _run_probe(sandbox, _LIST_FILES, args.pattern, what="list_files")


async def write_file(sandbox: Sandbox, args: WriteFileArgs) -> str:
    content = args.content.encode("utf-8")
    if len(content) > MAX_WRITE_BYTES:
        raise ToolError(f"content is {len(content)} bytes, over the {MAX_WRITE_BYTES}-byte limit")

    staged = f"{STAGING_DIR}/{uuid.uuid4().hex}"
    await sandbox.put_file(staged, content)
    try:
        result = await sandbox.exec(["python", "-c", _WRITE_FILE, args.path, staged], kill_after=30)
    finally:
        # Whatever happened above, nothing should be left under .warden/: a refusal leaves
        # the staged file in place exactly as much as a success would, since success moves
        # it out with os.replace and a refusal never reaches that line.
        await sandbox.exec(["rm", "-f", f"{MOUNT_ROOT}/{staged}"], kill_after=30)

    if result.exit_code == 3:
        raise ToolError(f"path {args.path!r} resolves outside the workspace and was refused")
    if result.exit_code != 0:
        raise ToolError(f"write_file failed: {result.output.strip()[:300]}")
    return f"wrote {args.path}"


async def run_command(sandbox: Sandbox, args: RunCommandArgs) -> str:
    try:
        argv = shlex.split(args.cmd)
    except ValueError as exc:
        raise ToolError(f"could not parse command {args.cmd!r}: {exc}") from exc
    if not argv:
        raise ToolError("empty command")

    result = await _exec_or_timeout_error(sandbox, argv, kill_after=args.timeout_seconds)
    # A non-zero exit is not a refusal: the model has to see the failing output to react to
    # it, same as a human running the command would.
    return f"exit code: {result.exit_code}\n{_truncate(result.output)}"


async def run_tests(sandbox: Sandbox, args: RunTestsArgs) -> str:
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if args.path is not None:
        normalised = normalize_path(args.path)
        if normalised is None:
            raise ToolError(f"path {args.path!r} resolves outside the workspace and was refused")
        argv.append(normalised)

    result = await _exec_or_timeout_error(sandbox, argv, kill_after=RUN_TESTS_TIMEOUT)
    # A failing test is a normal result, not a refusal: pytest's own exit code already says
    # so, and the model needs the failure to fix it.
    return f"exit code: {result.exit_code}\n{_truncate(result.output)}"


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
    registry.register(
        name="write_file",
        description="Write a UTF-8 text file in the workspace, creating parent directories.",
        args_model=WriteFileArgs,
        execute=lambda args: write_file(sandbox, args),
        path_arg="path",
    )
    registry.register(
        name="run_command",
        description="Run a command in the workspace. No shell, so no pipes or redirection.",
        args_model=RunCommandArgs,
        execute=lambda args: run_command(sandbox, args),
        # No path_arg: policy rules run_command by args.cmd, not by a path.
    )
    registry.register(
        name="run_tests",
        description="Run the project's pytest suite, optionally scoped to one path.",
        args_model=RunTestsArgs,
        execute=lambda args: run_tests(sandbox, args),
    )
    return registry
