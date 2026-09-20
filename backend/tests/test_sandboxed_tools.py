"""The tools, running where they will actually run.

These cost a container each, which is why the loop and resume tests use an in-memory
registry instead. Here containment *is* the subject, so a fake would test the fake.
"""

import os
import pathlib
import time
import uuid
from collections.abc import AsyncIterator

import pytest

from warden.sandbox.docker import Sandbox, SandboxProfile, discard_workspace_volume
from warden.tools.registry import InvalidArgumentsError, ToolError
from warden.tools.sandboxed import (
    ListFilesArgs,
    ReadFileArgs,
    build_registry,
    list_files,
    read_file,
)

pytestmark = pytest.mark.sandbox

SECRET = "sk-ant-this-must-never-be-read"


@pytest.fixture(scope="session")
def docker_available() -> None:
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    (root / "README.md").write_text("# demo\n", encoding="utf-8", newline="\n")
    # A secret the agent must never reach, one level above the workspace on the host. It is
    # not copied into the container at all, which is itself part of the containment.
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET}\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
async def sandbox(docker_available: None, workspace: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        yield box
    finally:
        await box.destroy()
        await box.discard_workspace()


async def test_reads_a_file_from_the_copied_workspace(sandbox: Sandbox) -> None:
    assert await read_file(sandbox, ReadFileArgs(path="src/app.py")) == "print('hello')\n"


async def test_a_relative_escape_is_refused_inside_the_container(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(sandbox, ReadFileArgs(path="../../.env"))


async def test_an_absolute_path_is_refused(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(sandbox, ReadFileArgs(path="/etc/passwd"))


async def test_the_host_secret_was_never_copied_in(sandbox: Sandbox) -> None:
    """Belt and braces: the path is refused, and there is nothing there to refuse to.

    The workspace is copied rather than mounted, so the host directory holding the secret
    does not exist in the container at all. Escaping the path would not be enough; the agent
    would have to escape the container.

    Scoped to the mount instead of the whole filesystem: a copy bug could only put the
    secret there, and grepping / takes longer than the command deadline allows.
    """
    result = await sandbox.exec(
        ["sh", "-c", f"grep -r {SECRET!r} /sandbox 2>/dev/null | head -1"], kill_after=30
    )
    assert SECRET not in result.output

    # And the host layout above the workspace simply is not there.
    parent = await sandbox.exec(["ls", "-a", "/sandbox"])
    assert ".env" not in parent.output


async def test_a_symlink_planted_in_the_workspace_is_refused(sandbox: Sandbox) -> None:
    """The reason containment moved inside the container.

    Resolving against a copy of the workspace on the host cannot see a link the agent
    created in the container, because the host copy does not have it.
    """
    made = await sandbox.exec(["ln", "-s", "/etc/passwd", "sneaky"])
    assert made.exit_code == 0, made.output

    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(sandbox, ReadFileArgs(path="sneaky"))


async def test_a_missing_file_is_a_tool_error_not_a_crash(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="not a file"):
        await read_file(sandbox, ReadFileArgs(path="src/nope.py"))


async def test_a_large_file_is_truncated(sandbox: Sandbox) -> None:
    """An unbounded read would spend the context window, and the bill, on one call."""
    written = await sandbox.exec(
        ["python", "-c", "open('big.txt','w').write('x' * 200000)"], kill_after=30
    )
    assert written.exit_code == 0

    output = await read_file(sandbox, ReadFileArgs(path="big.txt"))
    assert "truncated" in output
    assert len(output) < 200_000


async def test_listing_finds_the_workspace_files(sandbox: Sandbox) -> None:
    listing = await list_files(sandbox, ListFilesArgs(pattern="**/*"))
    assert "src/app.py" in listing
    assert "README.md" in listing


async def test_listing_honours_the_glob(sandbox: Sandbox) -> None:
    assert await list_files(sandbox, ListFilesArgs(pattern="**/*.py")) == "src/app.py"


async def test_listing_reports_no_match_rather_than_nothing(sandbox: Sandbox) -> None:
    assert "no files match" in await list_files(sandbox, ListFilesArgs(pattern="**/*.rs"))


async def test_listing_skips_vendored_dependencies(sandbox: Sandbox) -> None:
    """With a 200-entry cap, site-packages would crowd out the project entirely."""
    made = await sandbox.exec(
        ["python", "-c", "import os; os.makedirs('.venv/lib'); open('.venv/lib/x.py','w').close()"]
    )
    assert made.exit_code == 0

    listing = await list_files(sandbox, ListFilesArgs(pattern="**/*.py"))
    assert "src/app.py" in listing
    assert ".venv" not in listing


async def test_arguments_are_validated_before_any_container_call(sandbox: Sandbox) -> None:
    """Model output is untrusted input: it never reaches an exec unvalidated."""
    registry = build_registry(sandbox)
    with pytest.raises(InvalidArgumentsError, match="path"):
        await registry.execute("read_file", {"wrong_key": 1})


async def test_a_tool_call_stays_under_a_second(sandbox: Sandbox) -> None:
    """The price of containment, measured rather than assumed.

    Measured at roughly 340ms median on the development machine, against microseconds for
    the host read this replaced. Most of it is a CPython interpreter starting inside the
    container on every call, not the Docker round trip.

    Acceptable for now because an iteration is dominated by a model call of seconds, and a
    task making thirty tool calls pays about ten seconds of overhead. The bound here is a
    second, generous on purpose: its job is to catch the number becoming seconds, not to
    pin the current value on a machine whose load varies.
    """
    started = time.monotonic()
    for _ in range(5):
        await read_file(sandbox, ReadFileArgs(path="src/app.py"))
    per_call_ms = (time.monotonic() - started) * 1000 / 5

    assert per_call_ms < 1000, f"a tool call took {per_call_ms:.0f}ms"


# --- the workspace belongs to the task, not to the container ---------------------------


async def test_two_sandboxes_for_one_task_share_the_workspace(
    docker_available: None, workspace: pathlib.Path
) -> None:
    """What makes resume honest once tools can write.

    Without this, a task that changed a file, crashed and resumed would get a fresh copy of
    the original workspace while its own event log told the model the change was there.
    """
    task_id = str(uuid.uuid4())
    first = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    try:
        written = await first.exec(["python", "-c", "open('made.txt','w').write('survived')"])
        assert written.exit_code == 0
    finally:
        await first.destroy()

    second = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    try:
        assert await read_file(second, ReadFileArgs(path="made.txt")) == "survived"
    finally:
        await second.destroy()
        await second.discard_workspace()


async def test_destroy_keeps_the_workspace_and_discard_removes_it(
    docker_available: None, workspace: pathlib.Path
) -> None:
    import docker as docker_sdk

    from warden.sandbox.docker import workspace_volume_name

    task_id = str(uuid.uuid4())
    box = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    client = docker_sdk.from_env()

    await box.destroy()
    # Still there: the task may simply be between workers.
    assert client.volumes.get(workspace_volume_name(task_id)) is not None

    await box.discard_workspace()
    with pytest.raises(docker_sdk.errors.NotFound):
        client.volumes.get(workspace_volume_name(task_id))


async def test_tasks_do_not_see_each_others_workspaces(
    docker_available: None, workspace: pathlib.Path
) -> None:
    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    first = await Sandbox.create(SandboxProfile(), workspace, task_id=one)
    second = await Sandbox.create(SandboxProfile(), workspace, task_id=two)
    try:
        await first.exec(["python", "-c", "open('private.txt','w').write('mine')"])
        with pytest.raises(ToolError, match="not a file"):
            await read_file(second, ReadFileArgs(path="private.txt"))
    finally:
        for box in (first, second):
            await box.destroy()
            await box.discard_workspace()


async def test_discarding_a_workspace_without_a_sandbox_works(
    docker_available: None, workspace: pathlib.Path
) -> None:
    """The worker discards after the run, when the sandbox object may already be gone."""
    import asyncio

    import docker as docker_sdk

    from warden.sandbox.docker import workspace_volume_name

    task_id = str(uuid.uuid4())
    box = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    await box.destroy()

    await asyncio.to_thread(discard_workspace_volume, task_id)

    with pytest.raises(docker_sdk.errors.NotFound):
        docker_sdk.from_env().volumes.get(workspace_volume_name(task_id))
