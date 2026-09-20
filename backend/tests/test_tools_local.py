"""Containment is the only thing between model output and the filesystem in week 1.

The policy engine takes that job in week 2, but these tests stay: the tool has to honour
its own contract regardless of what decides above it.
"""

import pathlib

import pytest

from warden.tools.local import (
    ListFilesArgs,
    ReadFileArgs,
    build_registry,
    list_files,
    read_file,
)
from warden.tools.registry import InvalidArgumentsError, ToolError, UnknownToolError


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    # newline="\n" matters: on Windows write_text would otherwise translate to CRLF, and
    # read_file returns the bytes as they are on disk, which is the behaviour we want.
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    (root / "README.md").write_text("# demo\n", encoding="utf-8", newline="\n")
    # A secret one directory above the workspace, which is what an escape would reach.
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-should-never-be-read\n", encoding="utf-8")
    return root


async def test_reads_a_file_inside_the_workspace(workspace: pathlib.Path) -> None:
    assert await read_file(workspace, ReadFileArgs(path="src/app.py")) == "print('hello')\n"


async def test_relative_escape_is_refused(workspace: pathlib.Path) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(workspace, ReadFileArgs(path="../.env"))


async def test_deep_relative_escape_is_refused(workspace: pathlib.Path) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(workspace, ReadFileArgs(path="src/../../.env"))


async def test_absolute_path_is_refused(workspace: pathlib.Path, tmp_path: pathlib.Path) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(workspace, ReadFileArgs(path=str(tmp_path / ".env")))


async def test_symlink_pointing_out_is_refused(
    workspace: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """resolve() follows symlinks, so a link planted inside the workspace does not help."""
    link = workspace / "sneaky.env"
    try:
        link.symlink_to(tmp_path / ".env")
    except OSError:  # pragma: no cover - Windows without developer mode
        pytest.skip("creating symlinks requires elevated rights on this machine")

    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(workspace, ReadFileArgs(path="sneaky.env"))


async def test_missing_file_is_a_tool_error_not_a_crash(workspace: pathlib.Path) -> None:
    with pytest.raises(ToolError, match="not a file"):
        await read_file(workspace, ReadFileArgs(path="src/nope.py"))


async def test_large_file_is_truncated(workspace: pathlib.Path) -> None:
    """An unbounded read would blow up the context window, and the bill, in one call."""
    (workspace / "big.txt").write_text("x" * 200_000, encoding="utf-8")
    output = await read_file(workspace, ReadFileArgs(path="big.txt"))
    assert "truncated" in output
    assert len(output) < 200_000


async def test_list_files_honours_the_glob(workspace: pathlib.Path) -> None:
    assert await list_files(workspace, ListFilesArgs(pattern="**/*.py")) == "src/app.py"


async def test_list_files_emits_posix_separators(workspace: pathlib.Path) -> None:
    """Week 2 policy globs are written `src/**`; a backslash here would never match them."""
    listing = await list_files(workspace, ListFilesArgs(pattern="**/*"))
    assert "\\" not in listing


async def test_list_files_finds_only_workspace_files(workspace: pathlib.Path) -> None:
    listing = await list_files(workspace, ListFilesArgs(pattern="**/*"))
    assert "app.py" in listing
    assert ".env" not in listing


async def test_list_files_reports_no_match_instead_of_empty(workspace: pathlib.Path) -> None:
    assert "no files match" in await list_files(workspace, ListFilesArgs(pattern="**/*.rs"))


async def test_registry_rejects_arguments_that_do_not_match_the_schema(
    workspace: pathlib.Path,
) -> None:
    """Model output is untrusted: it never reaches a Python call unvalidated."""
    registry = build_registry(workspace)
    with pytest.raises(InvalidArgumentsError, match="path"):
        await registry.execute("read_file", {"wrong_key": 1})


async def test_registry_rejects_an_unregistered_tool(workspace: pathlib.Path) -> None:
    registry = build_registry(workspace)
    with pytest.raises(UnknownToolError, match="delete_repo"):
        await registry.execute("delete_repo", {})


def test_registry_exposes_a_stable_tool_order(workspace: pathlib.Path) -> None:
    """A varying tool list silently invalidates the prompt cache."""
    names = [schema.name for schema in build_registry(workspace).schemas()]
    assert names == sorted(names)


async def test_listing_skips_vendored_dependencies(workspace: pathlib.Path) -> None:
    """A target repo with a .venv would otherwise fill the listing with site-packages.

    Not merely noisy: the cap is 200 entries, so the agent would spend its whole listing on
    someone else's code and never see src/ at all.
    """
    vendored = workspace / ".venv" / "Lib" / "site-packages" / "pytest"
    vendored.mkdir(parents=True)
    (vendored / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")

    listing = await list_files(workspace, ListFilesArgs(pattern="**/*"))

    assert "src/app.py" in listing
    assert ".venv" not in listing
    assert "__pycache__" not in listing
