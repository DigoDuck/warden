"""What counts as a path inside a workspace.

This replaces the module that held tools reading the host filesystem directly, which was
always marked temporary. Those are gone: tools now execute inside the container
(`sandboxed.py`), and containment happens where the file is actually opened.

What is left is the vocabulary both sides need. The policy engine judges a path *string*, so
normalising one must not require touching a disk, and must not depend on which side of the
container boundary the caller sits on.
"""

import pathlib

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


def normalize_path(candidate: str) -> str | None:
    """Workspace-relative POSIX form of `candidate`, or None if it does not address one.

    Purely lexical: no `resolve()`, no filesystem. Two reasons.

    The policy engine judges the string, and a decision that depended on the state of a disk
    would not be reproducible from the event log afterwards. And the real containment now
    happens inside the container against `realpath` there, which is the only place that can
    see a symlink the agent itself created.

    None means the path escapes the workspace or is absolute. The caller then leaves
    `PolicyContext.path` unset, no allow rule can match, and the default deny applies.
    """
    pure = pathlib.PurePosixPath(candidate.replace("\\", "/"))
    if pure.is_absolute():
        return None

    parts: list[str] = []
    for part in pure.parts:
        if part == "..":
            if not parts:
                # Escapes above the workspace root. Collapsing it silently would turn
                # `../../.env` into `.env`, which is a path that matches rules.
                return None
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)

    return "/".join(parts) if parts else "."
