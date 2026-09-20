"""Read-only tools, running directly on the host.

Temporary by design. From week 2 these run inside a hardened container with no network,
and the workspace is a copy rather than the directory itself. Until then the guard below is
all that stands between a model's output and the filesystem.

**This containment is a floor, not the guarantee.** The project's argument is that the
guarantee comes from the policy engine, which is deterministic and never reads model output
as instruction. The policy engine does not exist yet in week 1, so the tool is the only
thing here. When week 2 lands, "the tool already validates" is not a reason to let a path
rule out of `policies/`: the tool protects its own contract, the policy decides authority.
"""

import asyncio
import pathlib

from pydantic import BaseModel, Field

from warden.tools.registry import ToolError, ToolRegistry

# Files bigger than this are truncated rather than blowing up the context window (and the
# bill) on one read. The model is told the content was cut.
MAX_READ_BYTES = 64_000
MAX_LISTED_FILES = 200

# Directories that are in the workspace but are not the project: installed dependencies,
# build caches, version control internals. Listing them is actively harmful, not merely
# noisy. A target repo with a .venv has thousands of vendored files, so the listing hits its
# cap on site-packages and the agent never sees src/ at all, having spent the context window
# on someone else's code.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
    }
)


def is_ignored(relative: pathlib.PurePosixPath) -> bool:
    return any(part in IGNORED_DIRS for part in relative.parts)


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path to the file, relative to the workspace root")


class ListFilesArgs(BaseModel):
    pattern: str = Field(
        default="**/*",
        description="Glob relative to the workspace root, for example 'src/**/*.py'",
    )


def resolve_in_workspace(workspace: pathlib.Path, candidate: str) -> pathlib.Path:
    """Resolve `candidate` under `workspace`, or refuse.

    `resolve()` collapses `..` and follows symlinks, so comparing the resolved paths also
    catches a symlink inside the workspace that points outside it. An absolute path is
    refused for the same reason it would be by a chroot: it is not addressing the workspace.
    """
    root = workspace.resolve()
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"path {candidate!r} resolves outside the workspace and was refused")
    return target


def normalize_path(workspace: pathlib.Path, candidate: str) -> str:
    """Workspace-relative POSIX form of `candidate`, for the policy engine to judge.

    Built on resolve_in_workspace so that normalisation and containment cannot drift apart.
    POSIX separators because the policy globs are written `src/**`.
    """
    resolved = resolve_in_workspace(workspace, candidate)
    return resolved.relative_to(workspace.resolve()).as_posix()


def _read_file_sync(workspace: pathlib.Path, path: str) -> str:
    target = resolve_in_workspace(workspace, path)
    if not target.is_file():
        raise ToolError(f"{path!r} is not a file in the workspace")

    data = target.read_bytes()
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_READ_BYTES:
        text += f"\n\n[truncated: {len(data)} bytes total, first {MAX_READ_BYTES} shown]"
    return text


def _list_files_sync(workspace: pathlib.Path, pattern: str) -> str:
    root = workspace.resolve()
    matches = sorted(
        # as_posix(), not str(): on Windows str() yields backslashes, and the policy globs
        # of week 2 are written as `src/**`. Emitting one separator everywhere keeps what
        # the model sees, what it sends back, and what the policy matches all the same.
        path.relative_to(root).as_posix()
        for path in root.glob(pattern)
        # A glob can walk out through a symlink, so every hit is re-checked against the
        # same rule read_file uses instead of being trusted because glob produced it.
        if path.is_file()
        and root in path.resolve().parents
        and not is_ignored(pathlib.PurePosixPath(path.relative_to(root).as_posix()))
    )
    if not matches:
        return f"no files match {pattern!r}"
    listing = matches[:MAX_LISTED_FILES]
    text = "\n".join(listing)
    if len(matches) > MAX_LISTED_FILES:
        text += f"\n[truncated: {len(matches)} matches, first {MAX_LISTED_FILES} shown]"
    return text


# Filesystem work runs in a worker thread. Irrelevant while one task runs in one process,
# but week 2 puts several tasks on one event loop, and a blocking read there stalls every
# other task's cancellation check.
async def read_file(workspace: pathlib.Path, args: ReadFileArgs) -> str:
    return await asyncio.to_thread(_read_file_sync, workspace, args.path)


async def list_files(workspace: pathlib.Path, args: ListFilesArgs) -> str:
    return await asyncio.to_thread(_list_files_sync, workspace, args.pattern)


def build_registry(workspace: pathlib.Path) -> ToolRegistry:
    """The week 1 tool surface: read the repository, then say you are done.

    `finish` is not here on purpose. Briefing section 15 puts its executor in `core`: it
    ends the task rather than producing a tool result, so the loop handles it directly.
    """
    registry = ToolRegistry()
    registry.register(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        args_model=ReadFileArgs,
        execute=lambda args: read_file(workspace, args),
        path_arg="path",
    )
    registry.register(
        name="list_files",
        description="List files in the workspace matching a glob pattern.",
        args_model=ListFilesArgs,
        # No path_arg: the argument is a glob, not a path. What it can reach is already
        # bounded by workspace containment, and the policy rules it by tool name.
        execute=lambda args: list_files(workspace, args),
    )
    return registry
