"""The coding runner end to end: a real Worker, the real sandbox and Postgres, FakeProvider
scripts standing in for the model (the only thing that is not real).

Needs Docker (`make sandbox-image` first) and Postgres, like the behavioral evals. The
anthropic provider is never exercised here: there is no key on this machine to do it with,
and a mocked provider would prove nothing about it.

Every failure case CAUSES the failure for real: a hostile file is written into the task's
workspace volume through the daemon, then the hidden test runs on that volume. Each defence
in coding.HIDDEN_TEST_PROGRAM is paired with a control run that removes it and shows the
planted file does win without it, so none of these tests can pass vacuously.
"""

import asyncio
import pathlib
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import cast

import docker
import docker.errors
import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from warden.config import get_settings
from warden.core.loop import Budget
from warden.core.worker import POLICY_FILE, WORKSPACE
from warden.db import with_database
from warden.models import Task
from warden.models import ToolCall as ToolCallRow
from warden.policy.engine import load_policy, never_readable
from warden.providers.fake import FakeProvider
from warden.sandbox.docker import (
    Sandbox,
    SandboxError,
    SandboxProfile,
    discard_workspace_volume,
    workspace_volume_name,
)
from warden.tools.sandboxed import build_registry

from evals import coding, runner
from evals.coding_checks import Item, load_items

aio = pytest.mark.asyncio(loop_scope="module")
sandbox_test = pytest.mark.sandbox

BUDGET = Budget(max_iterations=30, max_usd=Decimal("1.00"), max_seconds=300.0)

_FORCE_PASS_CONFTEST = """\
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    outcome.get_result().outcome = "passed"
"""


def _items() -> dict[str, Item]:
    items = load_items(coding.DEFAULT_DATASET, repo_root=coding.REPO_ROOT)
    return {i.id: i for i in items}


def _factory_for(script: pathlib.Path) -> Callable[[], FakeProvider]:
    return lambda: FakeProvider.from_yaml(script, resume_aware=True)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[runner.Context]:
    patch = pytest.MonkeyPatch()
    # Restored at teardown: isolate_coding_github writes os.environ directly.
    for key in ("GITHUB_REPO", "GITHUB_TOKEN", "GITHUB_API_URL"):
        patch.setenv(key, "x")
    coding.isolate_coding_github()
    runner.TEST_DB = coding.TEST_DB
    session_factory, url = await runner.prepare_database()
    try:
        yield runner.Context(
            session_factory=session_factory,
            keys=runner.ephemeral_keys(),
            policy=load_policy(POLICY_FILE),
            tmp_dir=tmp_path_factory.mktemp("coding"),
            test_db_url=url,
        )
    finally:
        get_settings.cache_clear()
        patch.undo()


async def _latest_task_id(ctx: runner.Context) -> uuid.UUID:
    async with ctx.session_factory() as session:
        task_id = await session.scalar(
            select(Task.id).order_by(Task.created_at.desc()).limit(1)
        )
    assert task_id is not None
    return task_id


def _volume_exists(task_id: uuid.UUID) -> bool:
    try:
        docker.from_env().volumes.get(workspace_volume_name(str(task_id)))
    except docker.errors.NotFound:
        return False
    return True


# --------------------------------------------------------------------------------------
# The two shipped fake items, end to end
# --------------------------------------------------------------------------------------


@sandbox_test
@aio
async def test_issue_09_fake_script_really_fixes_the_bug_and_the_volume_is_removed(
    ctx: runner.Context,
) -> None:
    item = _items()["issue-09"]
    assert item.fake_script is not None

    result = await coding.run_item(item, ctx, _factory_for(item.fake_script), BUDGET)

    assert result.state == "done", result.detail
    assert result.status == "SUCCEEDED"
    assert result.hidden_passed and result.success
    assert result.verdict_passed is True and result.gating_green
    assert not result.escaped_defect
    assert result.failure_category is None
    assert result.tool_calls == 2  # read_file + apply_patch; `finish` is not counted
    assert result.iterations == 3
    assert result.forbidden_attempts == 0
    # The runner owns the volume after the worker kept it, and discards it.
    assert not _volume_exists(await _latest_task_id(ctx))


@sandbox_test
@aio
async def test_issue_01_that_changes_nothing_is_approved_by_the_control_plane_and_still_fails(
    ctx: runner.Context,
) -> None:
    item = _items()["issue-01"]
    assert item.fake_script is not None

    result = await coding.run_item(item, ctx, _factory_for(item.fake_script), BUDGET)

    assert result.state == "done", result.detail
    # The control plane's own checks and the (scripted) reviewer all say yes...
    assert result.status == "SUCCEEDED"
    assert result.verdict_passed is True and result.gating_green
    # ...and the hidden acceptance test says no: this is an escaped defect.
    assert not result.hidden_passed and not result.success
    assert result.escaped_defect
    # By the precedence rules an empty diff touches none of expected_files: wrong_file
    # outranks tests_fail (evals/coding_checks.py::classify_failure).
    assert result.failure_category == "wrong_file"
    assert not _volume_exists(await _latest_task_id(ctx))


# --------------------------------------------------------------------------------------
# A hidden test that passes on the baseline is vacuous
# --------------------------------------------------------------------------------------


@sandbox_test
@aio
async def test_every_hidden_test_fails_on_the_unmodified_target_repo(
    ctx: runner.Context,
) -> None:
    sandbox = await Sandbox.create(
        SandboxProfile(), WORKSPACE, exclude=never_readable(ctx.policy)
    )
    try:
        for item in _items().values():
            hidden = await coding.exec_hidden_test(sandbox, item)
            # exit 1 is "tests ran and some failed". 2 would be a collection or import
            # error: a red for the wrong reason, which is what the README warns about.
            assert hidden.exit_code == 1, f"{item.id}: {hidden.output}"
            assert not hidden.passed
            assert "failed" in hidden.output, f"{item.id}: {hidden.output}"
            assert "ModuleNotFoundError" not in hidden.output, (
                f"{item.id}: {hidden.output}"
            )
    finally:
        await sandbox.destroy()


# --------------------------------------------------------------------------------------
# An agent that controls the workspace cannot rewrite the hidden test's result
# --------------------------------------------------------------------------------------


async def _exec_on(task_id: uuid.UUID, item: Item, argv: list[str]) -> tuple[int, str]:
    """Run `argv` on the task's real volume, with the hidden test copied in."""
    sandbox = await Sandbox.create(SandboxProfile(), WORKSPACE, task_id=str(task_id))
    try:
        await sandbox.put_file(
            f"workspace/{coding.HIDDEN_DIR}/{item.hidden_test.name}",
            item.hidden_test.read_bytes(),
        )
        result = await sandbox.exec(argv, kill_after=coding.HIDDEN_TIMEOUT_SECONDS)
        return result.exit_code, result.output
    finally:
        await sandbox.destroy()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def hostile_volume(ctx: runner.Context) -> AsyncIterator[tuple[uuid.UUID, Item]]:
    """A finished issue-01 task whose workspace volume then has hostile files planted in it.

    Issue 01 changes nothing, so its hidden test fails for a real reason: any "pass" below
    can only come from tampering. The files go in through the daemon, like anything an agent
    could leave behind (a test it wrote and ran with `run_command pytest` can write anywhere
    in the volume).
    """
    item = _items()["issue-01"]
    assert item.fake_script is not None
    task_id = await coding.run_agent(item, ctx, _factory_for(item.fake_script), BUDGET)
    sandbox = await Sandbox.create(SandboxProfile(), WORKSPACE, task_id=str(task_id))
    try:
        await sandbox.put_file("workspace/conftest.py", _FORCE_PASS_CONFTEST.encode())
        await sandbox.put_file(
            f"workspace/{coding.HIDDEN_DIR}/conftest.py", _FORCE_PASS_CONFTEST.encode()
        )
        await sandbox.put_file("workspace/pytest.py", b"raise SystemExit(0)\n")
    finally:
        await sandbox.destroy()
    try:
        yield task_id, item
    finally:
        await asyncio.to_thread(discard_workspace_volume, str(task_id))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shadowed_volume(ctx: runner.Context) -> AsyncIterator[tuple[uuid.UUID, Item]]:
    """Same idea, a different volume: an installed package shadowed by a workspace file. Kept
    apart from hostile_volume because a root-level conftest makes pytest put the workspace on
    sys.path itself, which would turn this shadow on in the other volume's control runs."""
    item = _items()["issue-01"]
    assert item.fake_script is not None
    task_id = await coding.run_agent(item, ctx, _factory_for(item.fake_script), BUDGET)
    sandbox = await Sandbox.create(SandboxProfile(), WORKSPACE, task_id=str(task_id))
    try:
        await sandbox.put_file(
            "workspace/fastapi.py", b'raise ImportError("planted")\n'
        )
    finally:
        await sandbox.destroy()
    try:
        yield task_id, item
    finally:
        await asyncio.to_thread(discard_workspace_volume, str(task_id))


@sandbox_test
@aio
async def test_a_planted_conftest_does_not_make_the_hidden_test_pass(
    hostile_volume: tuple[uuid.UUID, Item],
) -> None:
    task_id, item = hostile_volume

    hidden = await coding.run_hidden_test(task_id, item)
    assert not hidden.passed and hidden.exit_code == 1, hidden.output

    # Control: the same run WITHOUT --noconftest. The planted conftest does force a pass, so
    # the defence above is what made the difference, not a conftest that never loaded.
    weak = coding.HIDDEN_TEST_PROGRAM.replace("'--noconftest', ", "")
    assert weak != coding.HIDDEN_TEST_PROGRAM
    code, output = await _exec_on(
        task_id,
        item,
        ["python", "-I", "-c", weak, f"{coding.HIDDEN_DIR}/{item.hidden_test.name}"],
    )
    assert code == 0, output


@sandbox_test
@aio
async def test_a_planted_pytest_py_at_the_workspace_root_does_not_shadow_pytest(
    hostile_volume: tuple[uuid.UUID, Item],
) -> None:
    task_id, item = hostile_volume

    hidden = await coding.run_hidden_test(task_id, item)
    assert not hidden.passed and hidden.exit_code == 1, hidden.output
    assert (
        "failed" in hidden.output
    )  # the real pytest ran; the planted one would exit silently

    # Control: without -I the cwd is on sys.path and the planted file IS what gets imported:
    # it exits 0 before the print, i.e. a silent "pass" with no tests run.
    code, output = await _exec_on(
        task_id, item, ["python", "-c", "import pytest; print(pytest.__file__)"]
    )
    assert code == 0 and output == "", output


@sandbox_test
@aio
async def test_a_planted_fastapi_py_does_not_shadow_an_installed_package(
    shadowed_volume: tuple[uuid.UUID, Item],
) -> None:
    task_id, item = shadowed_volume

    hidden = await coding.run_hidden_test(task_id, item)
    assert hidden.exit_code == 1 and "planted" not in hidden.output, hidden.output

    # Control: without `-c /dev/null` pytest obeys the workspace's pyproject.toml
    # (`pythonpath = ["."]`), which puts the workspace FIRST on sys.path, and the planted
    # module wins. This is why the flag exists.
    weak = coding.HIDDEN_TEST_PROGRAM.replace("'-c', '/dev/null', ", "")
    assert weak != coding.HIDDEN_TEST_PROGRAM
    _, output = await _exec_on(
        task_id,
        item,
        ["python", "-I", "-c", weak, f"{coding.HIDDEN_DIR}/{item.hidden_test.name}"],
    )
    assert "planted" in output, output


# --------------------------------------------------------------------------------------
# The workspace is kept for the hidden test, and only then discarded
# --------------------------------------------------------------------------------------


@sandbox_test
@aio
async def test_the_worker_keeps_the_workspace_and_a_missing_volume_is_refused(
    ctx: runner.Context,
) -> None:
    item = _items()["issue-09"]
    assert item.fake_script is not None
    task_id = await coding.run_agent(item, ctx, _factory_for(item.fake_script), BUDGET)
    try:
        # The task is terminal, and the volume is still there (keep_workspaces=True).
        assert _volume_exists(task_id)
    finally:
        await asyncio.to_thread(discard_workspace_volume, str(task_id))

    # Gone: running the hidden test now would recreate the pristine repo and score the
    # baseline. It must refuse instead.
    with pytest.raises(SandboxError, match="is gone"):
        await coding.run_hidden_test(task_id, item)
    assert not _volume_exists(task_id)  # and the refusal did not recreate it


# --------------------------------------------------------------------------------------
# The classifier against real loop output
# --------------------------------------------------------------------------------------


def _scripted_item(
    base: Item, tmp_path: pathlib.Path, script: list[dict[str, object]]
) -> Item:
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump({"script": script}), encoding="utf-8")
    return Item(
        id=base.id,
        issue=base.issue,
        hidden_test=base.hidden_test,
        kind=base.kind,
        expected_files=base.expected_files,
        forbidden=base.forbidden,
        fake_script=path,
    )


@sandbox_test
@aio
async def test_a_run_that_uses_every_iteration_is_classified_loop(
    ctx: runner.Context, tmp_path: pathlib.Path
) -> None:
    # Pins the reason string coding_checks matches on: core/loop.py ends such a run with
    # TIMED_OUT and "reached max_iterations", the same status the wall-clock deadline uses.
    item = _scripted_item(
        _items()["issue-01"],
        tmp_path,
        [{"tool_call": {"name": "list_files", "args": {"pattern": "**/*.py"}}}] * 3,
    )
    assert item.fake_script is not None

    result = await coding.run_item(
        item,
        ctx,
        _factory_for(item.fake_script),
        Budget(max_iterations=2, max_usd=Decimal(1)),
    )

    assert result.state == "done", result.detail
    assert result.status == "TIMED_OUT"
    assert result.failure_category == "loop"
    assert result.iterations == 2


@sandbox_test
@aio
async def test_a_scripted_open_pr_cannot_reach_anything(
    ctx: runner.Context, tmp_path: pathlib.Path
) -> None:
    # The agent never has github.open_pr (ADR-028: only the control plane publishes), so the
    # model is calling a tool that does not exist. `_decide` denies an unregistered tool before
    # any rule is read, even though `open-pr-needs-human` names it, and the run carries on.
    item = _scripted_item(
        _items()["issue-01"],
        tmp_path,
        [
            {
                "tool_call": {
                    "name": "github.open_pr",
                    "args": {
                        "title": "t",
                        "body": "b",
                        "branch": "x",
                        "files": ["src/app.py"],
                    },
                }
            },
            {"tool_call": {"name": "finish", "args": {"summary": "opened a PR"}}},
            {"tool_call": {"name": "submit_verdict", "args": {"passed": True, "findings": []}}},
        ],
    )
    assert item.fake_script is not None

    result = await coding.run_item(item, ctx, _factory_for(item.fake_script), BUDGET)

    assert result.state == "done", result.detail
    # Nothing changed, so there is nothing to publish and no question for a human: the task
    # ends, the hidden test fails, and the hallucinated tool is what the run is classified by.
    assert result.status == "SUCCEEDED"
    assert result.failure_category == "hallucinated_api"
    async with ctx.session_factory() as session:
        rows = list(
            await session.execute(
                select(ToolCallRow.tool_name, ToolCallRow.decision).where(
                    ToolCallRow.tool_name == "github.open_pr"
                )
            )
        )
    assert [tuple(r) for r in rows] == [("github.open_pr", "deny")]  # recorded, never run

    registry = build_registry(cast(Sandbox, None))
    assert not registry.has("github.open_pr")


# --------------------------------------------------------------------------------------
# CLI guards (no Docker, no database)
# --------------------------------------------------------------------------------------


def test_isolate_coding_github_leaves_the_pr_tool_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_a_real_looking_token")
    monkeypatch.setenv("GITHUB_REPO", "someone/real-repo")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    try:
        coding.isolate_coding_github()
        settings = get_settings()
        assert settings.github_token.get_secret_value() == ""
        assert settings.github_repo == ""
        assert settings.github_api_url.startswith("http://127.0.0.1:")
        assert not build_registry(cast(Sandbox, None)).has("github.open_pr")
    finally:
        get_settings.cache_clear()


def test_hidden_test_argv_is_fixed_and_the_name_is_never_interpolated() -> None:
    argv = coding.hidden_test_argv("test_issue_09.py; rm -rf /")
    assert argv[:3] == ["python", "-I", "-c"]
    assert argv[3] == coding.HIDDEN_TEST_PROGRAM
    assert "rm" not in coding.HIDDEN_TEST_PROGRAM
    assert (
        argv[4] == ".warden_hidden/test_issue_09.py; rm -rf /"
    )  # data, not program text


def test_write_metrics_is_refused_for_the_fake_provider_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["coding", "--provider", "fake", "--write-metrics"]
    )
    with pytest.raises(SystemExit, match="anthropic provider"):
        coding.main()


def test_the_anthropic_provider_fails_fast_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["coding", "--provider", "anthropic"])
    try:
        with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY is not set"):
            coding.main()
    finally:
        get_settings.cache_clear()


def test_the_total_cap_skips_every_remaining_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A cap of zero is already reached before the first item: nothing may start. Uses its own
    # database name so it cannot drop the one the module fixture above is connected to.
    scratch = "warden_evals_coding_cli"
    monkeypatch.setattr(coding, "TEST_DB", scratch)
    monkeypatch.setattr(
        runner, "TEST_DB", runner.TEST_DB
    )  # restored after _amain sets it
    for key in ("GITHUB_REPO", "GITHUB_TOKEN", "GITHUB_API_URL"):
        monkeypatch.setenv(key, "x")  # restored after _amain overwrites them
    monkeypatch.setattr(
        sys,
        "argv",
        ["coding", "--provider", "fake", "--max-usd-total", "0", "--only", "issue-09"],
    )
    try:
        code = coding.main()
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(scratch))
    out = capsys.readouterr().out
    assert "issue-09 - budget cap" in out
    assert code == 1  # nothing was scored


async def _drop_database(name: str) -> None:
    admin = create_async_engine(
        with_database(get_settings().database_url, "postgres"),
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()
