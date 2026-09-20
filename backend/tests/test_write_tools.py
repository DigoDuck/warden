"""The tools that change the world, against real containers.

Same reasoning as `test_sandboxed_tools.py`: containment and the shell-less exec are the
subject here, so a fake tool surface would only test the fake. The one end-to-end test at
the bottom goes further and drives the real loop with the real default policy, because a
destructive command being refused by the tool is a different claim from it being refused
before the sandbox ever sees it.
"""

import os
import pathlib
import shutil
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.core import events
from warden.core.events import read_events
from warden.core.loop import run_task
from warden.models import Task, ToolCall, User
from warden.policy.engine import load_policy
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import Sandbox, SandboxProfile
from warden.tools.registry import ToolError
from warden.tools.sandboxed import (
    ReadFileArgs,
    RunCommandArgs,
    RunTestsArgs,
    WriteFileArgs,
    build_registry,
    read_file,
    run_command,
    run_tests,
    write_file,
)

pytestmark = pytest.mark.sandbox

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET_REPO = REPO_ROOT / "examples" / "target-repo"
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Skip locally when Docker is not running, but never skip in CI."""
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
async def sandbox(docker_available: None, workspace: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        yield box
    finally:
        await box.destroy()


@pytest.fixture
def target_repo_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A disposable copy: the tools below write into it, and the source tree must not change."""
    root = tmp_path / "target-repo"
    shutil.copytree(TARGET_REPO, root, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    return root


@pytest.fixture
async def target_repo_sandbox(
    docker_available: None, target_repo_workspace: pathlib.Path
) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), target_repo_workspace)
    try:
        yield box
    finally:
        await box.destroy()


async def _staged_files(sandbox: Sandbox) -> str:
    result = await sandbox.exec(["sh", "-c", "ls -A /sandbox/.warden 2>/dev/null"])
    return result.output.strip()


# --- write_file -------------------------------------------------------------------------


async def test_write_file_creates_the_file_and_its_parent_directories(sandbox: Sandbox) -> None:
    await write_file(sandbox, WriteFileArgs(path="src/new/nested.py", content="x = 1\n"))
    assert await read_file(sandbox, ReadFileArgs(path="src/new/nested.py")) == "x = 1\n"


async def test_a_relative_escape_is_refused_and_nothing_is_written(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await write_file(sandbox, WriteFileArgs(path="../escaped.txt", content="nope"))

    result = await sandbox.exec(["sh", "-c", "test -f /sandbox/escaped.txt && echo YES || echo NO"])
    assert result.output.strip() == "NO"


async def test_an_absolute_path_is_refused_and_nothing_is_written(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await write_file(sandbox, WriteFileArgs(path="/etc/passwd", content="nope"))

    result = await sandbox.exec(["cat", "/etc/passwd"])
    assert "nope" not in result.output


async def test_symlink_out_is_refused_as_a_parent_and_as_the_target(sandbox: Sandbox) -> None:
    """Both shapes realpath has to catch: a symlinked parent directory, and the target
    itself being a symlink. Both resolve to the same thing, an absolute path outside the
    workspace root, which is why one `_CONTAIN` check covers both without special-casing.
    """
    parent_link = await sandbox.exec(["ln", "-s", "/etc", "sneaky"])
    assert parent_link.exit_code == 0, parent_link.output
    with pytest.raises(ToolError, match="outside the workspace"):
        await write_file(sandbox, WriteFileArgs(path="sneaky/passwd", content="nope"))

    target_link = await sandbox.exec(["ln", "-s", "/etc/passwd", "linked"])
    assert target_link.exit_code == 0, target_link.output
    with pytest.raises(ToolError, match="outside the workspace"):
        await write_file(sandbox, WriteFileArgs(path="linked", content="nope"))

    result = await sandbox.exec(["cat", "/etc/passwd"])
    assert "nope" not in result.output


async def test_reading_and_writing_through_an_in_tree_symlink_is_refused(sandbox: Sandbox) -> None:
    """POLICY BYPASS this closes: the resolved target staying under the workspace root was
    never proof that it was the *same* file the caller named. `src/alias -> ../top.txt`
    resolves inside the workspace, so the old `_CONTAIN` check waved it through; policy then
    judges the allowed-looking name `src/alias`, never the real, possibly-denied `top.txt`.
    """
    await write_file(sandbox, WriteFileArgs(path="top.txt", content="SECRET=nope\n"))
    link = await sandbox.exec(["ln", "-s", "../top.txt", "src/alias"])
    assert link.exit_code == 0, link.output

    with pytest.raises(ToolError, match="outside the workspace"):
        await read_file(sandbox, ReadFileArgs(path="src/alias"))
    with pytest.raises(ToolError, match="outside the workspace"):
        await write_file(sandbox, WriteFileArgs(path="src/alias", content="pwned"))

    assert await read_file(sandbox, ReadFileArgs(path="top.txt")) == "SECRET=nope\n"


async def test_content_over_the_limit_is_a_tool_error(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="limit"):
        await write_file(sandbox, WriteFileArgs(path="too_big.txt", content="x" * 1_000_001))


async def test_staged_files_do_not_accumulate_after_success_or_refusal(sandbox: Sandbox) -> None:
    await write_file(sandbox, WriteFileArgs(path="ok.txt", content="fine"))
    with pytest.raises(ToolError):
        await write_file(sandbox, WriteFileArgs(path="../nope.txt", content="denied"))

    assert await _staged_files(sandbox) == ""


async def test_a_write_survives_across_sandboxes_for_the_same_task(
    docker_available: None, workspace: pathlib.Path
) -> None:
    """The promise the read-only tools already kept: a task's workspace is not the
    container's, so a second sandbox for the same task sees what the first one wrote.
    """
    task_id = str(uuid.uuid4())
    first = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    try:
        await write_file(first, WriteFileArgs(path="made.txt", content="it survived\n"))
    finally:
        await first.destroy()

    second = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    try:
        assert await read_file(second, ReadFileArgs(path="made.txt")) == "it survived\n"
    finally:
        await second.destroy()
        await second.discard_workspace()


# --- run_command --------------------------------------------------------------------------


async def test_run_command_runs_the_target_repos_suite_with_no_network(
    target_repo_sandbox: Sandbox,
) -> None:
    result = await run_command(target_repo_sandbox, RunCommandArgs(cmd="python -m pytest -q"))
    assert "exit code: 0" in result
    assert "5 passed" in result


async def test_there_is_no_shell(sandbox: Sandbox) -> None:
    chained = await run_command(sandbox, RunCommandArgs(cmd="echo a && echo b"))
    # No shell means "&&" is just another argv token handed to `echo`, not a second command.
    assert "exit code: 0" in chained
    assert "a && echo b" in chained

    literal = await run_command(sandbox, RunCommandArgs(cmd="echo $(whoami) `id`"))
    assert "$(whoami)" in literal
    assert "`id`" in literal


async def test_a_non_zero_exit_is_not_a_tool_error(sandbox: Sandbox) -> None:
    result = await run_command(sandbox, RunCommandArgs(cmd="python -c 'raise SystemExit(7)'"))
    assert "exit code: 7" in result


async def test_a_shlex_error_is_a_tool_error(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError):
        await run_command(sandbox, RunCommandArgs(cmd="echo 'unterminated"))


async def test_an_empty_command_is_a_tool_error(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="empty"):
        await run_command(sandbox, RunCommandArgs(cmd="   "))


async def test_a_command_past_its_deadline_is_a_tool_error_and_recovers(sandbox: Sandbox) -> None:
    await write_file(sandbox, WriteFileArgs(path="before.txt", content="still here\n"))

    with pytest.raises(ToolError):
        await run_command(sandbox, RunCommandArgs(cmd="sleep 30", timeout_seconds=2))

    after = await run_command(sandbox, RunCommandArgs(cmd="echo back"))
    assert "exit code: 0" in after
    assert await read_file(sandbox, ReadFileArgs(path="before.txt")) == "still here\n"


async def test_long_output_is_truncated(sandbox: Sandbox) -> None:
    result = await run_command(sandbox, RunCommandArgs(cmd="python -c \"print('x' * 100000)\""))
    assert "truncated" in result
    assert len(result) < 100_000


# --- run_tests ------------------------------------------------------------------------------


async def test_run_tests_reports_a_passing_and_a_failing_run_without_raising(
    sandbox: Sandbox,
) -> None:
    await write_file(
        sandbox, WriteFileArgs(path="tests/test_ok.py", content="def test_ok():\n    assert True\n")
    )
    passing = await run_tests(sandbox, RunTestsArgs(path="tests/test_ok.py"))
    assert "exit code: 0" in passing
    assert "1 passed" in passing

    await write_file(
        sandbox,
        WriteFileArgs(path="tests/test_bad.py", content="def test_bad():\n    assert False\n"),
    )
    failing = await run_tests(sandbox, RunTestsArgs(path="tests/test_bad.py"))
    assert "exit code: 1" in failing
    assert "1 failed" in failing


async def test_run_tests_refuses_a_path_that_escapes_the_workspace(sandbox: Sandbox) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await run_tests(sandbox, RunTestsArgs(path="../elsewhere"))


# --- through the real loop, with the real default policy ----------------------------------


def _step(tool: str, **args: object) -> ScriptStep:
    return ScriptStep(
        tool_calls=[ProviderToolCall(id=f"call-{uuid.uuid4()}", name=tool, arguments=args)]
    )


async def _a_task(session: AsyncSession, spec: str) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec=spec)
    session.add(task)
    await session.flush()
    return task


async def test_loop_denies_rm_rf_and_really_runs_the_allowed_test_command(
    session: AsyncSession, target_repo_workspace: pathlib.Path
) -> None:
    """The claim is stronger than "the tool refuses it": policy has to deny `rm -rf /`
    before the sandbox is ever asked to run it, while a command the same rules allow, and
    that the tool's own no-shell parsing does not mangle, really executes.
    """
    task = await _a_task(session, "run the project's checks")
    provider = FakeProvider(
        [
            _step("run_command", cmd="rm -rf /"),
            _step("run_command", cmd="pytest -q"),
            _step("finish", summary="done"),
        ]
    )

    sandbox = await Sandbox.create(SandboxProfile(), target_repo_workspace)
    try:
        result = await run_task(
            session,
            task,
            provider,
            build_registry(sandbox),
            load_policy(DEFAULT_POLICY),
            workspace=target_repo_workspace,
        )
    finally:
        await sandbox.destroy()

    assert result.status == "SUCCEEDED"

    rows = (
        await session.scalars(
            select(ToolCall)
            .where(ToolCall.task_id == task.id, ToolCall.tool_name == "run_command")
            .order_by(ToolCall.iteration)
        )
    ).all()
    assert len(rows) == 2

    denied, allowed = rows
    assert denied.decision == "deny"
    assert allowed.decision == "allow"

    # `result_summary` is truncated to 500 characters for the audit trail; "5 passed" is
    # pytest's own last line, so the full output from the event log is what proves it ran
    # rather than merely being permitted to.
    outputs = [
        event.payload["output"]
        for event in await read_events(session, task.id)
        if event.type == events.TOOL_EXECUTED and event.payload["tool"] == "run_command"
    ]
    assert len(outputs) == 2
    assert outputs[0].startswith("Refused by policy")
    assert "5 passed" in outputs[1]
