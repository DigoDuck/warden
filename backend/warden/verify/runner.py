"""Deterministic evidence about a finished task, collected by the control plane (ADR-026).

The agent says it is done; this module finds out what "done" actually looks like. Nothing
here reads the agent's summary or takes an argument from the model: the checks are fixed,
run by the control plane after `finish`, and recorded as `evidence` rows. Whether that
evidence is good enough is a separate question, answered by the verdict (ADR-010, next PR).
This module only collects.

Two decisions carry the design:

- **The diff baseline lives on the host, not in the sandbox.** The "before" is the directory
  that seeded the container, filtered exactly as `sandbox/docker.py::_workspace_tar` filtered
  it. A git baseline inside the workspace volume would sit where code the agent wrote can
  reach it: pytest runs that code, and a `conftest.py` could rewrite the baseline to hide a
  change. What the coder cannot touch, the coder cannot falsify.
- **Order matters: diff, lint, types, tests.** Only the last one executes the agent's code.
  The diff is read before anything runs at all, and ruff/mypy only parse files, so none of
  the first three can be influenced by what a test does to the workspace while it runs.
"""

import difflib
import io
import pathlib
import tarfile
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

import docker.errors
from pydantic import BaseModel

from warden.sandbox.docker import CommandTimeout, Sandbox, SandboxError
from warden.tools.sandboxed import RUN_TESTS_TIMEOUT, truncate_output
from warden.tools.workspace import is_ignored, normalize_path

# The order the checks run in, and the only kinds `evidence.kind` accepts (models.py's
# EVIDENCE_KINDS mirrors this; the CHECK constraint is the one that actually enforces it).
KINDS = ("diff", "lint", "types", "tests")

# A target repo for this project is small (examples/target-repo is a few KB). The cap is
# about an agent that writes a huge file on purpose or by accident: past it the diff is
# recorded as an error instead of the control plane buffering whatever is in there.
MAX_WORKSPACE_ARCHIVE_BYTES = 50_000_000
# The unified diff kept in the evidence row. Counts and the file list are always complete;
# only the patch text is cut, and `patch_truncated` says so.
MAX_PATCH_CHARS = 100_000
# ruff and mypy only parse files, so they get the same fixed deadline as a normal command.
STATIC_CHECK_TIMEOUT = 120.0

# Fixed argv, never built from model output. `--no-cache`/`--cache-dir=/dev/null` keep the
# checks from writing cache directories into the workspace (they would be ignored by the
# diff anyway, see IGNORED_DIRS, but a check has no business leaving files behind).
_LINT_COMMANDS = (
    ["ruff", "check", "--no-cache", "."],
    ["ruff", "format", "--check", "--no-cache", "."],
)
# `--explicit-package-bases`: without it, a repo laid out like examples/target-repo (no
# `__init__.py`, tests importing `src.app`) stops mypy with "source file found twice under
# different module names" before it checks a single type. Verified against that repo, not
# assumed.
_TYPES_COMMANDS = (["mypy", "--cache-dir=/dev/null", "--explicit-package-bases", "."],)
# Same argv as the `run_tests` tool, so "the agent saw green" and "the control plane saw
# green" are the same command. What differs is who ran it and when.
_TESTS_COMMANDS = (["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],)

CheckStatus = Literal["passed", "failed", "timeout", "error"]


class CommandRun(BaseModel):
    argv: list[str]
    exit_code: int | None
    output: str
    duration_ms: int


class CommandEvidence(BaseModel):
    """Evidence of kind `lint`, `types` or `tests`."""

    kind: Literal["lint", "types", "tests"]
    status: CheckStatus
    passed: bool
    commands: list[CommandRun]
    error: str | None = None


class FileChange(BaseModel):
    path: str
    change: Literal["added", "removed", "modified"]
    additions: int
    deletions: int
    binary: bool


class DiffEvidence(BaseModel):
    """Evidence of kind `diff`. No `passed`: a diff is a fact, not a verdict."""

    kind: Literal["diff"] = "diff"
    status: Literal["ok", "error"]
    files: list[FileChange] = []
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    patch_truncated: bool = False
    error: str | None = None


class EvidenceCollector(Protocol):
    """What `core/loop.py` needs from a verifier: the kinds, in order, and one check at a
    time. The loop owns durability (one checkpoint per recorded check) and cancellation;
    the collector only runs checks. Loop-level tests implement this without Docker."""

    kinds: tuple[str, ...]

    async def check(self, kind: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------------------
# The diff: pure functions, testable without a container.
# ---------------------------------------------------------------------------------------


def read_seed(root: pathlib.Path, exclude: Callable[[str], bool] | None = None) -> dict[str, bytes]:
    """The workspace as it was handed to the sandbox, keyed by workspace-relative path.

    Same two filters as `_workspace_tar`, applied in the same way. They have to match: a
    file excluded by policy (ADR-018) never entered the container, and without the same
    filter here it would show up in every diff as "removed".
    """
    files: dict[str, bytes] = {}
    base = root.resolve()
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base).as_posix()
        if is_ignored(pathlib.PurePosixPath(relative)):
            continue
        if exclude is not None and exclude(relative):
            continue
        if path.is_file() and not path.is_symlink():
            files[relative] = path.read_bytes()
    return files


def read_archive(archive: bytes) -> dict[str, bytes]:
    """The workspace as the container holds it now, from `Sandbox.export_workspace`.

    Every name in this tar was chosen by the agent, so it is read in memory and never
    extracted: extraction is where a `../` name or a symlink turns into a write outside the
    target directory. A name that does not normalise under `workspace/` is dropped. A link
    is recorded as a line naming its target rather than followed, so a symlink the agent
    planted shows up in the diff as what it is.
    """
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            name = member.name
            if not name.startswith("workspace/"):
                continue
            relative = normalize_path(name.removeprefix("workspace/"))
            if relative is None or relative == ".":
                continue
            if is_ignored(pathlib.PurePosixPath(relative)):
                continue
            if member.isfile():
                extracted = tar.extractfile(member)
                files[relative] = extracted.read() if extracted is not None else b""
            elif member.issym() or member.islnk():
                files[relative] = f"<link to {member.linkname}>\n".encode()
            # Directories, devices and FIFOs carry no content worth diffing.
    return files


def _as_text(data: bytes) -> str | None:
    # A NUL byte is git's own heuristic for "binary"; anything that is not UTF-8 is too.
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _unified(path: str, before: str | None, after: str | None) -> list[str]:
    old = before.splitlines(keepends=True) if before is not None else []
    new = after.splitlines(keepends=True) if after is not None else []
    lines = list(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{path}" if before is not None else "/dev/null",
            tofile=f"b/{path}" if after is not None else "/dev/null",
        )
    )
    # difflib leaves the last line without "\n" when the file has none; joining the patch
    # would glue it to the next file's header.
    return [line if line.endswith("\n") else line + "\n" for line in lines]


def compute_diff(before: dict[str, bytes], after: dict[str, bytes]) -> DiffEvidence:
    """Compare the seed with the exported workspace. Pure: bytes in, evidence out."""
    changes: list[FileChange] = []
    patch_parts: list[str] = []

    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        change: Literal["added", "removed", "modified"] = (
            "added" if old is None else "removed" if new is None else "modified"
        )
        old_text = _as_text(old) if old is not None else ""
        new_text = _as_text(new) if new is not None else ""
        if old_text is None or new_text is None:
            changes.append(
                FileChange(path=path, change=change, additions=0, deletions=0, binary=True)
            )
            patch_parts.append(f"Binary files a/{path} and b/{path} differ\n")
            continue

        lines = _unified(
            path,
            old_text if old is not None else None,
            new_text if new is not None else None,
        )
        # Header lines ("--- a/x", "+++ b/x") start with the same characters as content
        # lines; only the body is counted.
        body = [line for line in lines if not line.startswith(("---", "+++"))]
        changes.append(
            FileChange(
                path=path,
                change=change,
                additions=sum(1 for line in body if line.startswith("+")),
                deletions=sum(1 for line in body if line.startswith("-")),
                binary=False,
            )
        )
        patch_parts.append("".join(lines))

    patch = "".join(patch_parts)
    return DiffEvidence(
        status="ok",
        files=changes,
        files_changed=len(changes),
        additions=sum(c.additions for c in changes),
        deletions=sum(c.deletions for c in changes),
        patch=patch[:MAX_PATCH_CHARS],
        patch_truncated=len(patch) > MAX_PATCH_CHARS,
    )


# ---------------------------------------------------------------------------------------
# The collector bound to one task's sandbox.
# ---------------------------------------------------------------------------------------


class Verifier:
    """Runs the fixed checks against one task's sandbox.

    `check()` never raises for a sandbox that failed underneath it: a dead container, a
    daemon error or a timeout becomes evidence with `status: "error"` or `"timeout"`, since
    "the checks could not run" is itself something a reviewer has to see. A genuine bug in
    this module still raises, like everywhere else in the loop. If a cancel is what killed the
    container, `core/loop.py` notices after the check and stops before recording anything.
    """

    kinds: tuple[str, ...] = KINDS

    def __init__(
        self,
        sandbox: Sandbox,
        seed: pathlib.Path,
        exclude: Callable[[str], bool] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._seed = seed
        self._exclude = exclude

    async def check(self, kind: str) -> dict[str, Any]:
        if kind == "diff":
            return (await self._diff()).model_dump(mode="json")
        if kind == "lint":
            evidence = await self._commands("lint", _LINT_COMMANDS, STATIC_CHECK_TIMEOUT)
        elif kind == "types":
            evidence = await self._commands("types", _TYPES_COMMANDS, STATIC_CHECK_TIMEOUT)
        elif kind == "tests":
            evidence = await self._commands("tests", _TESTS_COMMANDS, RUN_TESTS_TIMEOUT)
        else:
            raise ValueError(f"unknown evidence kind {kind!r}")
        return evidence.model_dump(mode="json")

    async def _diff(self) -> DiffEvidence:
        try:
            archive = await self._sandbox.export_workspace(max_bytes=MAX_WORKSPACE_ARCHIVE_BYTES)
        except (SandboxError, docker.errors.DockerException) as exc:
            return DiffEvidence(status="error", error=str(exc)[:500])
        return compute_diff(read_seed(self._seed, self._exclude), read_archive(archive))

    async def _commands(
        self,
        kind: Literal["lint", "types", "tests"],
        commands: tuple[list[str], ...],
        kill_after: float,
    ) -> CommandEvidence:
        runs: list[CommandRun] = []
        for argv in commands:
            started = time.monotonic()
            try:
                result = await self._sandbox.exec(argv, kill_after=kill_after)
            except CommandTimeout as exc:
                # The container is dead now (that is how a deadline is enforced), so the
                # remaining commands of this check could not run either.
                runs.append(
                    CommandRun(argv=argv, exit_code=None, output=str(exc), duration_ms=_ms(started))
                )
                return CommandEvidence(
                    kind=kind, status="timeout", passed=False, commands=runs, error=str(exc)
                )
            except (SandboxError, docker.errors.DockerException) as exc:
                runs.append(
                    CommandRun(argv=argv, exit_code=None, output="", duration_ms=_ms(started))
                )
                return CommandEvidence(
                    kind=kind, status="error", passed=False, commands=runs, error=str(exc)[:500]
                )
            runs.append(
                CommandRun(
                    argv=argv,
                    exit_code=result.exit_code,
                    output=truncate_output(result.output),
                    duration_ms=_ms(started),
                )
            )

        passed = all(run.exit_code == 0 for run in runs)
        return CommandEvidence(
            kind=kind, status="passed" if passed else "failed", passed=passed, commands=runs
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
