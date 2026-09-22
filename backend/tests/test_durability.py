"""ADR-019: a run survives the worker *process* dying, not just an exception inside it.

Everything in this file is scoped by pytestmark to `sandbox`, because the checklist item
it proves end to end needs a real container to kill. Most of the individual tests do not
touch Docker at all, though, and run in every environment: only the ones that request the
`docker_available` fixture skip locally without a daemon (and fail outright in CI).
"""

import asyncio
import contextlib
import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import TEST_DB
from tests.fake_tools import FakeWorkspace
from warden.config import get_settings
from warden.core import cancel, queue
from warden.core.events import read_events
from warden.core.loop import Budget, run_task
from warden.core.loop import _finish as _loop_finish
from warden.core.worker import WORKSPACE as WORKER_WORKSPACE
from warden.core.worker import Worker, run_claimed_task
from warden.db import with_database
from warden.models import ModelCall, Task, ToolCall, User
from warden.policy.engine import Effect, Policy, Rule, load_policy
from warden.providers.base import Completion
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import Sandbox, SandboxProfile, discard_workspace_volume
from warden.tools.registry import ToolRegistry

pytestmark = pytest.mark.sandbox

BUDGET = Budget(max_iterations=6)
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Same pattern as test_sandbox.py: skip locally without Docker, never skip in CI."""
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    return root


def _allow_all() -> Policy:
    return Policy(
        [Rule(id="allow-all", effect=Effect.ALLOW, when={"tool": "*"})],
        default=Effect.DENY,
        policy_hash="test",
    )


def _finish(summary: str = "done") -> ScriptStep:
    call = ProviderToolCall(id="call-finish", name="finish", arguments={"summary": summary})
    return ScriptStep(tool_calls=[call])


async def _user_and_task(session: AsyncSession, *, lease_seconds: int) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    await queue.enqueue(session, user_id=user.id, spec="x", idempotency_key=str(uuid.uuid4()))
    await session.commit()
    claimed = await queue.claim(session, "worker-a", lease_seconds=lease_seconds)
    assert claimed is not None
    await session.commit()
    return claimed


class _PausingProvider:
    """Wraps a FakeProvider so the test can catch the run right after its first checkpoint
    commits, before it asks for a model turn, hold it there, and release it later.

    Standing in for what a slow model call or a long tool would otherwise do to create the
    same window; this way the pause point is exact and the test has no sleep in it.
    """

    def __init__(self, inner: FakeProvider, paused: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._paused = paused
        self._release = release
        self.name = inner.name

    async def generate(self, *args: object, **kwargs: object) -> Completion:
        self._paused.set()
        await self._release.wait()
        return await self._inner.generate(*args, **kwargs)  # type: ignore[arg-type]


async def test_a_run_in_progress_does_not_starve_the_heartbeat(
    session_factory: async_sessionmaker[AsyncSession], workspace: pathlib.Path
) -> None:
    """Regression test for a real, confirmed defect, not a hypothesis.

    Before ADR-019 the whole run was one transaction: `task.started_at` was flushed (an
    UPDATE, which takes a row lock in Postgres) at the very start and nothing committed
    until the run finished. `_beat`'s heartbeat runs the same UPDATE against the same row on
    a second connection, so it blocked for the entire run instead of extending the lease
    every `HEARTBEAT_FRACTION * lease_seconds`. Confirmed red against the pre-ADR-019 loop
    before this fix existed: the heartbeat below timed out instead of succeeding. Green here
    because checkpoint (a) commits, and releases the row lock, before the loop ever asks the
    provider for a turn.
    """
    async with session_factory() as claim_session:
        task = await _user_and_task(claim_session, lease_seconds=3)
        task_id = task.id

    paused = asyncio.Event()
    release = asyncio.Event()

    async with session_factory() as run_session:
        claimed_row = await run_session.get(Task, task_id)
        assert claimed_row is not None
        provider = _PausingProvider(FakeProvider([_finish()]), paused, release)
        runner = asyncio.create_task(
            run_claimed_task(
                run_session,
                claimed_row,
                provider,
                _allow_all(),
                workspace,
                FakeWorkspace().registry(),
                budget=BUDGET,
                holder="worker-a",
            )
        )
        try:
            await asyncio.wait_for(paused.wait(), timeout=5)

            async with session_factory() as beat_session:
                # The defect made this hang until `runner` finished. The fix makes it
                # return almost immediately, well inside the 3 second lease.
                extended = await asyncio.wait_for(
                    queue.heartbeat(beat_session, task_id, "worker-a", lease_seconds=60),
                    timeout=2,
                )
                await beat_session.commit()
            assert extended is True
        finally:
            release.set()
            await runner


async def test_a_worker_that_lost_its_lease_writes_nothing_after_and_cannot_finish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fencing at the level of one checkpoint (ADR-019), without Docker.

    Per-step commits make an existing problem worse: a worker that lost its lease keeps
    running, and without a fence it would interleave events with the new owner and corrupt
    replay. B reclaims A's task; A then reaches a checkpoint, here `_finish` called directly,
    and must get `LeaseLost` instead of a commit, with nothing it flushed surviving.

    This is the fast, narrow version. The same thing with a live worker, a real container
    and a real volume is `test_a_live_worker_that_loses_its_lease_...` below.
    """
    async with session_factory() as setup:
        task = await _user_and_task(setup, lease_seconds=2)
        task_id = task.id

    async with session_factory() as reclaim_session:
        await queue.expire_lease_now(reclaim_session, task_id)
        await reclaim_session.commit()
        b_claim = await queue.claim(reclaim_session, "worker-b", lease_seconds=60)
        assert b_claim is not None and b_claim.id == task_id
        await reclaim_session.commit()

    async with session_factory() as a_session:
        stray = await a_session.get(Task, task_id)
        assert stray is not None
        with pytest.raises(queue.LeaseLost):
            await _loop_finish(
                a_session, stray, "SUCCEEDED", 1, Decimal("0"), "worker-a", summary="from A"
            )

    async with session_factory() as probe:
        current = await probe.get(Task, task_id)
        kinds = [e.type for e in await read_events(probe, task_id)]

    # B's claim stands; A never got to mark the task finished, and its task.finished event
    # never landed (the rollback inside `_checkpoint` discarded it).
    assert current is not None
    assert current.claimed_by == "worker-b"
    assert current.status == "RUNNING"
    assert "task.finished" not in kinds


async def test_a_live_worker_that_loses_its_lease_stops_without_touching_what_is_no_longer_its(
    docker_available: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """The zombie, for real: worker A is alive and parked inside a model call that will not
    return, its lease runs out, B takes the task, and then A wakes up.

    Two claims are under test, and the first one used to be false. While a run held a
    transaction open across the model call, its uncommitted `iteration.started` row kept a
    key-share lock on the task through the foreign key, `claim()` skipped the locked row, and
    a hung worker kept its task for as long as it stayed hung, lease or no lease. B's claim
    succeeding below is the proof that nothing is held open any more.

    The second is what `Worker.run_once` does with `LeaseLost`. It is the one branch in this
    change whose failure destroys data: discarding the workspace volume here would leave B
    resuming against a fresh copy of the repository while its event log tells the model that
    the first run's edits are on disk.
    """
    import docker as docker_sdk

    from warden.sandbox.docker import workspace_volume_name

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = await queue.enqueue(
        session, user_id=user.id, spec="zombie probe", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id

    paused = asyncio.Event()
    release = asyncio.Event()
    read = ProviderToolCall(id="c1", name="read_file", arguments={"path": "src/app.py"})

    def provider_factory() -> _PausingProvider:
        script = FakeProvider([ScriptStep(tool_calls=[read]), _finish()])
        return _PausingProvider(script, paused, release)

    worker_a = Worker(session_factory, provider_factory, _allow_all(), workspace)
    client = docker_sdk.from_env()
    running = asyncio.create_task(worker_a.run_once())
    try:
        # Generous: creating the sandbox comes first and takes seconds on Docker Desktop.
        await asyncio.wait_for(paused.wait(), timeout=90)

        await queue.expire_lease_now(session, task_id)
        await session.commit()
        b_claim = await queue.claim(session, "worker-b", lease_seconds=60)
        assert b_claim is not None and b_claim.id == task_id, (
            "a worker parked in a model call still blocks its task from being reclaimed"
        )
        await session.commit()

        release.set()
        result = await asyncio.wait_for(running, timeout=90)

        # Stopped quietly: no exception, no result to report.
        assert result is None

        # The workspace B now depends on is still there, and A took its own container along.
        client.volumes.get(workspace_volume_name(str(task_id)))
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []

        async with session_factory() as probe:
            current = await probe.get(Task, task_id)
            kinds = [e.type for e in await read_events(probe, task_id)]
        assert current is not None
        assert current.claimed_by == "worker-b"
        assert current.status == "RUNNING"
        # Everything A did after waking up, the model call it paid for included, stayed out
        # of the log: its first checkpoint after the lease was gone refused to commit.
        assert kinds == ["task.created", "iteration.started"]
    finally:
        release.set()
        if not running.done():
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))


async def test_a_turns_tool_requests_all_commit_before_the_first_one_executes(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """The hazard checkpoint (b) closes: with one `tool.requested` per call instead of one
    batch, a crash between the first tool finishing and the second's request being written
    would leave replay unable to tell "never asked for" apart from "not run yet". Proven by
    reading from a wholly separate connection, inside the first tool's own execution: it can
    only see what another session has *committed*, so finding both requests there proves
    they landed before either tool ran, not just before this one did.
    """
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec="x")
    session.add(task)
    await session.flush()

    seen_at_first_execution: list[int] = []

    class _Args(BaseModel):
        path: str

    async def _spy_read(args: _Args) -> str:
        async with session_factory() as spy:
            rows = await read_events(spy, task.id)
        seen_at_first_execution.append(sum(1 for e in rows if e.type == "tool.requested"))
        return f"read {args.path}"

    registry = ToolRegistry()
    registry.register(
        name="read_file",
        description="spy",
        args_model=_Args,
        execute=_spy_read,
        path_arg="path",
    )

    provider = FakeProvider(
        [
            ScriptStep(
                tool_calls=[
                    ProviderToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),
                    ProviderToolCall(id="c2", name="read_file", arguments={"path": "b.py"}),
                ]
            ),
            _finish(),
        ]
    )

    result = await run_task(session, task, provider, registry, _allow_all(), workspace=workspace)

    assert result.status == "SUCCEEDED"
    assert len(seen_at_first_execution) == 2
    # Both tool.requested events were already visible from a separate connection at the
    # moment the FIRST call executed, not just one.
    assert seen_at_first_execution[0] == 2
    assert seen_at_first_execution[1] == 2


async def test_creating_a_sandbox_removes_an_orphaned_container_for_the_same_task(
    docker_available: None, workspace: pathlib.Path
) -> None:
    """A worker killed outright never runs `Sandbox.destroy()`. Its container has to be
    someone's problem, and per ADR-019 it is the next owner's: whoever creates a sandbox for
    a task force-removes whatever already carries that task's label first.
    """
    import docker as docker_sdk

    client = docker_sdk.from_env()
    task_id = f"orphan-{uuid.uuid4().hex}"

    orphan = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
    orphan_container_id = orphan.id
    # No orphan.destroy() here on purpose: this stands in for a worker whose process was
    # killed and never reached its `finally` block.

    try:
        assert len(client.containers.list(filters={"label": f"warden.task={task_id}"})) == 1

        replacement = await Sandbox.create(SandboxProfile(), workspace, task_id=task_id)
        try:
            with pytest.raises(docker_sdk.errors.NotFound):
                client.containers.get(orphan_container_id)
            assert replacement.id != orphan_container_id
            # The replacement still works and still has the task's workspace, since the
            # volume (unlike the container) is never touched by orphan removal.
            result = await replacement.exec(["cat", "src/app.py"])
            assert result.output.strip() == "print('hello')"
        finally:
            await replacement.destroy()
    finally:
        await asyncio.to_thread(discard_workspace_volume, task_id)


def _write_yaml(path: pathlib.Path, text_content: str) -> pathlib.Path:
    path.write_text(text_content, encoding="utf-8")
    return path


async def test_a_task_survives_the_worker_process_being_killed(
    docker_available: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """The checklist item, end to end: `kill -9` on the worker, not a caught exception.

    THE FIX's five checkpoints only matter if a step really does survive a process that
    disappears mid-run with no chance to run any cleanup code. This kills a real OS process
    holding a real container, and proves durability from a connection that was never the
    crashed worker's own.

    The default policy has nothing that allows a long-running command (`run-project-commands`
    only allowlists pytest/ruff/mypy/npm); weakening it to add one would leave every other
    task subject to a looser rule it should never have. A test-only policy file, loaded via
    a small `--policy` CLI flag on the worker entry point, keeps `policies/default.yaml`
    untouched and still lets the script call `run_command "sleep 5"` exactly as specified.
    """
    import docker as docker_sdk

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = await queue.enqueue(
        session, user_id=user.id, spec="durability probe", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id

    script_path = _write_yaml(
        tmp_path / "crash-script.yaml",
        """
script:
  - tool_calls:
      - name: read_file
        args: { path: "src/app.py" }
      - name: run_command
        args: { cmd: "sleep 5" }
  - tool_call:
      name: finish
      args: { summary: "should never get here" }
""",
    )
    # Allows exactly what this probe needs and nothing the default policy does not already
    # allow for reads; the point is a long command, not a looser policy for anything else.
    policy_path = _write_yaml(
        tmp_path / "sleep-allowed-policy.yaml",
        """
version: 1
default: deny
rules:
  - id: allow-read
    effect: allow
    when:
      tool: [read_file]
      path: ["src/**"]
  - id: allow-sleep
    effect: allow
    when:
      tool: run_command
      args.cmd: "^sleep "
""",
    )
    resume_script = _write_yaml(
        tmp_path / "resume-script.yaml",
        """
script:
  - tool_call:
      name: finish
      args: { summary: "resumed after the crash" }
""",
    )

    test_db_url = with_database(get_settings().database_url, TEST_DB)
    env = {**os.environ, "DATABASE_URL": test_db_url}

    client = docker_sdk.from_env()
    proc: subprocess.Popen[bytes] | None = None
    try:
        # subprocess.Popen, not asyncio's subprocess API: what this test needs is
        # `Popen.kill()` specifically (TerminateProcess on Windows, SIGKILL on Linux, no
        # cleanup handlers run either way), and the polling loop below already yields to the
        # event loop between checks, so a plain blocking start is not worth the extra layer.
        proc = subprocess.Popen(  # noqa: ASYNC220
            [
                sys.executable,
                "-m",
                "warden.core.worker",
                "--script",
                str(script_path),
                "--policy",
                str(policy_path),
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # From the test's OWN connection, which never shares a transaction with the
        # subprocess's: under Postgres's default READ COMMITTED isolation every new
        # statement re-snapshots committed data, so seeing policy.decided for the sleeping
        # call show up here, while the subprocess is still alive and blocked in `sleep 5`,
        # IS the proof that checkpoints commit mid-run rather than at the very end.
        deadline = asyncio.get_event_loop().time() + 30
        sleeping_decided = False
        while asyncio.get_event_loop().time() < deadline:
            assert proc.poll() is None, "the worker exited before reaching the sleeping call"
            rows = await read_events(session, task_id)
            if any(
                e.type == "policy.decided" and e.payload.get("tool") == "run_command" for e in rows
            ):
                sleeping_decided = True
                break
            await asyncio.sleep(0.3)
        assert sleeping_decided, "policy.decided for the sleeping run_command never showed up"

        # The orphan really exists before we clean it up below, or the later assertion that
        # it is gone would be vacuous.
        assert len(client.containers.list(filters={"label": f"warden.task={task_id}"})) == 1

        proc.kill()  # TerminateProcess on Windows, SIGKILL on Linux: no cleanup handlers run.
        proc.wait(timeout=15)
        proc = None

        before_resume = await read_events(session, task_id)
        kinds = [e.type for e in before_resume]
        assert "task.created" in kinds
        assert "model.called" in kinds
        model_called = next(e for e in before_resume if e.type == "model.called")
        assert model_called.payload.get("raw_content") is not None
        assert "policy.decided" in kinds
        # read_file, the first call, had already executed and committed before the process
        # died on the second call.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(ToolCall.task_id == task_id, ToolCall.tool_name == "read_file")
            )
        ) == 1

        await queue.expire_lease_now(session, task_id)
        await session.commit()

        def resume_provider_factory() -> FakeProvider:
            return FakeProvider.from_yaml(resume_script)

        worker2 = Worker(
            session_factory,
            resume_provider_factory,
            load_policy(policy_path),
            WORKER_WORKSPACE,
        )
        result = await worker2.run_once()

        assert result is not None
        assert result.status == "SUCCEEDED"

        model_call_count = await session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task_id)
        )
        # One for the interrupted iteration (replayed from the log, not bought again) and
        # one for the iteration that produced `finish`.
        assert model_call_count == 2

        read_file_calls = await session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(ToolCall.task_id == task_id, ToolCall.tool_name == "read_file")
        )
        assert read_file_calls == 1

        # The dead worker's container is gone: force-removed when worker2's Sandbox.create
        # ran, and worker2's own container was destroyed when the task reached SUCCEEDED.
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))


async def test_a_cancel_request_kills_a_long_running_tool_and_the_task_ends_cancelled(
    docker_available: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """The real thing, end to end: a `Worker` is genuinely blocked inside a sandboxed
    `run_command "sleep 30"`, `request_cancel` arrives on a completely different session
    (standing in for an API request or another process), and the container has to die well
    before the command's own 30 second deadline, not because of it.

    A killed-for-cancel container and a killed-for-timeout one look identical once they are
    both dead, so speed is what tells them apart here: the assertion is "gone within a
    handful of seconds", not "gone eventually".
    """
    import docker as docker_sdk

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = await queue.enqueue(
        session, user_id=user.id, spec="cancel probe", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id

    def provider_factory() -> FakeProvider:
        return FakeProvider(
            [
                ScriptStep(
                    tool_calls=[
                        ProviderToolCall(
                            id="c1",
                            name="run_command",
                            arguments={"cmd": "sleep 30", "timeout_seconds": 30},
                        )
                    ]
                ),
                _finish(),
            ]
        )

    worker = Worker(session_factory, provider_factory, _allow_all(), workspace)
    client = docker_sdk.from_env()
    running = asyncio.create_task(worker.run_once())
    try:
        # Wait for `policy.decided` on `run_command`, not merely for the container to exist:
        # the container is created before the model is ever asked for a turn, so requesting
        # a cancel as soon as it appears can race ahead of the model call that produces
        # `run_command` entirely, and the check at the top of iteration 1 would then stop
        # the run before it ever generates. `policy.decided` only commits once that model
        # call is durable and the tool is about to execute (same technique as
        # `test_a_task_survives_the_worker_process_being_killed` above).
        loop_time = asyncio.get_event_loop().time
        deadline = loop_time() + 30
        run_command_decided = False
        while loop_time() < deadline:
            rows = await read_events(session, task_id)
            if any(
                e.type == "policy.decided" and e.payload.get("tool") == "run_command" for e in rows
            ):
                run_command_decided = True
                break
            await asyncio.sleep(0.2)
        assert run_command_decided, "policy.decided for run_command never showed up"

        found = client.containers.list(filters={"label": f"warden.task={task_id}"})
        assert found, "the sandbox should exist once run_command has been decided"
        container = found[0]

        await cancel.request_cancel(session, task_id)
        await session.commit()

        cancel_requested_at = loop_time()
        killed_after: float | None = None
        while loop_time() < cancel_requested_at + 10:
            container.reload()
            if container.status != "running":
                killed_after = loop_time() - cancel_requested_at
                break
            await asyncio.sleep(0.2)

        assert killed_after is not None, "the container was still running 10s after cancel"
        assert killed_after < 10, "a cancel-kill should not take anywhere near the 30s deadline"

        result = await asyncio.wait_for(running, timeout=30)
        assert result is not None
        assert result.status == "CANCELLED"

        # sandbox.destroy() in the worker's own `finally` force-removes whatever the cancel
        # watcher's kill left behind.
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []

        model_calls = await session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task_id)
        )
        # Exactly the one turn that asked for `run_command`; the cancel pre-empted the next.
        assert model_calls == 1
    finally:
        if not running.done():
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))
