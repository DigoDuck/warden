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

`apply_patch` contains itself differently: a diff can touch several files, so there is no
single argument for a Python probe to resolve. It leans on git instead, which already
refuses a path outside the workspace and a target beyond a symlink (verified, not assumed:
ADR-017), and on the policy engine judging every path a diff touches before `apply_patch`
is allowed to run at all (`core/loop.py`, `policy/engine.py::combine`).
"""

import re
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
# Same ceiling, same reasoning, named separately because a diff and a file's content are
# different things that happen to share a limit today.
MAX_PATCH_BYTES = 1_000_000
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


class ApplyPatchArgs(BaseModel):
    diff: str = Field(
        description=(
            "A unified diff in `git diff` format, one or more files, including renames "
            "and deletions. Paths are relative to the workspace root."
        )
    )


# `git apply --numstat -z` (ADR-017 has the verified byte layout): one NUL-terminated
# record per touched file, "<added>\t<deleted>\t<path>". Counts are never parsed, so a
# binary file's "-" placeholder is harmless; the path is everything after the second tab,
# taken verbatim rather than by further splitting, because a path is untrusted input and a
# literal tab inside one must not be mistaken for the field separator that preceded it.
def _parse_numstat_z(output: str) -> list[str]:
    return [field.split("\t", 2)[2] for field in output.split("\0") if field]


# git's C-style quoting of an extended-header path (quote.c: quote_c_style / unquote_c_style).
# A path holding a quote, a backslash, or (core.quotepath, on by default) a non-ASCII byte is
# wrapped in double quotes, with `\\`, `\"`, the usual single-letter C escapes, and every other
# non-printable byte written as a 3-digit octal `\NNN`. `git apply` accepts a quoted header path
# even when nothing about it required quoting (verified: `rename from ".env"` behaves exactly
# like `rename from .env`), so a bare regex capture would keep the quote marks around a path
# that has none on disk, and the quoted string would then never match a deny rule written
# against the real name.
_C_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r"}
_OCTAL_ESCAPE = re.compile(r"[0-7]{1,3}")


def _unquote_c_style(raw: str) -> str:
    """Undo git's C-style quoting, or return `raw` unchanged if it was never quoted."""
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.extend(char.encode("utf-8", "replace"))
            i += 1
            continue
        escape = body[i + 1 : i + 2]
        if escape in ("\\", '"'):
            out.extend(escape.encode("ascii"))
            i += 2
        elif escape in _C_ESCAPES:
            out.extend(_C_ESCAPES[escape].encode("ascii"))
            i += 2
        else:
            octal = _OCTAL_ESCAPE.match(body, i + 1)
            if octal:
                out.append(int(octal.group(), 8) & 0xFF)
                i = octal.end()
            else:
                # Not an escape git would ever emit; keep the backslash literally rather
                # than guess. This only ever changes what an already-strange path decodes
                # to, never whether it ends up in the judged set below.
                out.extend(char.encode("ascii"))
                i += 1
    return out.decode("utf-8", "replace")


# ADR-017: verified empirically that `git apply --numstat -z` reports only the
# *destination* of a rename, never the source, in every git version this project has
# tested. The source is recoverable, safely, from the patch's own extended header: a
# `rename from` line is always immediately followed by `rename to`, and a throwaway patch
# with a decoy `diff --git a/X b/Y` line proved git apply itself ignores that line for a
# rename and acts on these two instead, so reading them is reading exactly what git reads,
# not guessing at it. `copy from`/`copy to` get the same treatment, same reasoning.
#
# This used to keep a (source, destination) pair only when the destination also byte-matched
# one of numstat's, to avoid trusting a decoy header the patch text merely contains rather
# than one git actually reads. That cross-check was itself the bug: a C-quoted or
# CRLF-terminated destination never byte-matches numstat's unquoted, LF-terminated form, so
# the pair, and the real source with it, silently disappeared; and because the match was a
# plain dict keyed by destination, a *later* decoy pair for the same destination overwrote
# the real source outright. There is no pairing left to spoof: every path named in any
# `rename`/`copy` header line is judged, in addition to numstat's destinations, so a decoy
# or a mangled header can only ever add an extra path to the judged set, never remove the
# real one.
_HEADER_PATH_LINE = re.compile(r"^(?:rename|copy) (?:from|to) (.+)$", re.MULTILINE)


def _touched_paths(diff: str, numstat_output: str) -> list[str]:
    destinations = _parse_numstat_z(numstat_output)
    header_paths = [_unquote_c_style(raw.rstrip("\r")) for raw in _HEADER_PATH_LINE.findall(diff)]
    # dict.fromkeys dedupes while keeping numstat's order first, which is what the existing
    # rename test pins; the values are never read.
    return list(dict.fromkeys([*destinations, *header_paths]))


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


async def _stage_patch(sandbox: Sandbox, diff: str) -> str:
    """Upload a diff under `.warden/` and return its staged path, over the size cap.

    Shared by the inspector and the executor: each call stages, uses and removes its own
    copy, so there is never a staged patch left behind for either to trip over the other.
    """
    content = diff.encode("utf-8")
    if len(content) > MAX_PATCH_BYTES:
        raise ToolError(f"diff is {len(content)} bytes, over the {MAX_PATCH_BYTES}-byte limit")
    staged = f"{STAGING_DIR}/{uuid.uuid4().hex}"
    await sandbox.put_file(staged, content)
    return staged


async def apply_patch_paths(sandbox: Sandbox, args: ApplyPatchArgs) -> list[str]:
    """The inspector: ask git what this diff would touch, without changing anything.

    ADR-017's design: the control plane cannot parse a diff itself without risking a
    parser differential against what git apply actually does, so git is asked instead.
    `--numstat -z` neither applies the patch nor validates it against real file content (a
    context mismatch still reports a path here; that only fails later, at `--check` inside
    `apply_patch`), so a malformed patch is the only thing that makes this raise.
    """
    staged = await _stage_patch(sandbox, args.diff)
    try:
        result = await sandbox.exec(
            ["git", "-C", WORKSPACE, "apply", "--numstat", "-z", f"{MOUNT_ROOT}/{staged}"],
            kill_after=30,
        )
    finally:
        await sandbox.exec(["rm", "-f", f"{MOUNT_ROOT}/{staged}"], kill_after=30)

    if result.exit_code != 0:
        raise ToolError(f"could not read the patch: {result.output.strip()[:300]}")
    return _touched_paths(args.diff, result.output)


async def apply_patch(sandbox: Sandbox, args: ApplyPatchArgs) -> str:
    """The executor: validate for real, then apply for real, both inside the workspace.

    `--check` first and only then the real apply, the same two-step shape `write_file`'s
    containment probe uses, because a change this one-way deserves a dry run first. git's
    own refusal of a `../` path and of any target beyond a symlink (ADR-017, verified
    empirically rather than assumed) is the actual containment here: this tool adds no
    realpath probe of its own on top of it, because there is nothing left for one to catch
    that git does not already refuse first.
    """
    staged = await _stage_patch(sandbox, args.diff)
    try:
        check = await sandbox.exec(
            ["git", "-C", WORKSPACE, "apply", "--check", f"{MOUNT_ROOT}/{staged}"], kill_after=30
        )
        if check.exit_code != 0:
            raise ToolError(f"patch does not apply: {check.output.strip()[:300]}")

        result = await sandbox.exec(
            ["git", "-C", WORKSPACE, "apply", f"{MOUNT_ROOT}/{staged}"], kill_after=30
        )
        if result.exit_code != 0:
            raise ToolError(f"apply_patch failed: {result.output.strip()[:300]}")
    finally:
        await sandbox.exec(["rm", "-f", f"{MOUNT_ROOT}/{staged}"], kill_after=30)

    return f"applied patch ({len(args.diff.encode('utf-8'))} bytes)"


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
        name="apply_patch",
        description="Apply a unified diff to the workspace: edits, creates, deletes and renames.",
        args_model=ApplyPatchArgs,
        execute=lambda args: apply_patch(sandbox, args),
        # No path_arg: a diff can touch several files, so the paths it would touch come
        # from path_inspector instead, one call judging all of them together.
        path_inspector=lambda args: apply_patch_paths(sandbox, args),
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
