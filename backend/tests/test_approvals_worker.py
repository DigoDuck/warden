"""ADR-022 through a real `Worker`, not just `run_task`: the one place claim/release and a
real sandbox actually interact with a pause. `test_loop.py` already covers the decision
logic (approve/reject/deny-override) against `FakeWorkspace`; what only a real `Worker` can
prove is that `_pause_for_approval` truly frees the row for another worker to claim, that the
workspace volume survives because WAITING_APPROVAL is not in `worker.py::TERMINAL_STATUSES`,
and that a *second* `Worker.run_once`, on a fresh container, resumes and finishes it.
"""

import pathlib
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.core import approvals, queue
from warden.core.worker import Worker
from warden.models import Approval, Task, ToolCall, User
from warden.policy.engine import Effect, Policy, Rule
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import workspace_volume_name

pytestmark = pytest.mark.sandbox


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Same pattern as test_sandbox.py/test_durability.py: skip locally without Docker,
    never skip in CI."""
    import os

    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    # `audit_log` needs wiping explicitly, not just `tasks ... CASCADE`: it has no foreign
    # key to `tasks` by design (warden/audit/log.py's own docstring), so a task.finished or
    # approval.requested entry this test's real run_task writes would otherwise outlive it
    # and break test_audit.py's own tests, which assume the table starts empty (same
    # reasoning as test_api.py's and test_approvals.py's own wipe fixtures).
    wipe = text("TRUNCATE audit_log, tasks, users RESTART IDENTITY CASCADE")
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    return root


def _require_approval_policy() -> Policy:
    return Policy(
        [
            Rule(
                id="shell-needs-human",
                effect=Effect.REQUIRE_APPROVAL,
                reason="a shell command needs a human's sign-off",
                scopes=["shell:run"],
                when={"tool": "run_command"},
            )
        ],
        default=Effect.DENY,
        policy_hash="test",
    )


def _run_echo() -> ScriptStep:
    call = ProviderToolCall(id="call-run", name="run_command", arguments={"cmd": "echo warden-ok"})
    return ScriptStep(tool_calls=[call])


def _finish() -> ScriptStep:
    call = ProviderToolCall(id="call-finish", name="finish", arguments={"summary": "done"})
    return ScriptStep(tool_calls=[call])


async def test_a_real_worker_pauses_releases_and_a_second_one_resumes_after_approval(
    docker_available: None,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """Every read below goes through a fresh session (`session_factory()`), never a session
    this test itself wrote through earlier: `Worker.run_once` does all of its own work on
    its own sessions, and a session that still holds the pre-run `Task` in its identity map
    would hand back stale, cached attributes on a plain `.get()` instead of seeing what
    another connection committed (same reasoning as test_cancel.py's and test_durability.py's
    own "probe" sessions).
    """
    import docker as docker_sdk

    async with session_factory() as setup:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
        setup.add(user)
        await setup.flush()
        task = await queue.enqueue(
            setup, user_id=user.id, spec="run a command", idempotency_key=str(uuid.uuid4())
        )
        await setup.commit()
        user_id, task_id = user.id, task.id

    worker_a = Worker(
        session_factory, lambda: FakeProvider([_run_echo()]), _require_approval_policy(), workspace
    )
    paused = await worker_a.run_once()

    assert paused is not None
    assert paused.status == "WAITING_APPROVAL"

    async with session_factory() as probe:
        row = await probe.get(Task, task_id)
        assert row is not None
        # The claim/release path, end to end: run_once really claimed it (RUNNING at some
        # point), and _pause_for_approval really released the lease rather than leaving it
        # to expire.
        assert row.status == "WAITING_APPROVAL"
        assert row.claimed_by is None
        assert row.claimed_until is None
        approval = (await probe.scalars(select(Approval).where(Approval.task_id == task_id))).one()
        approval_id = approval.id

    # WAITING_APPROVAL is not in worker.py::TERMINAL_STATUSES: the workspace volume this
    # task's sandbox created must still be there for the resume that is coming.
    client = docker_sdk.from_env()
    client.volumes.get(workspace_volume_name(str(task_id)))  # raises NotFound if discarded

    async with session_factory() as decide:
        await approvals.decide_approval(
            decide, approval_id, approve=True, user_id=user_id, note=None
        )
        await decide.commit()

    worker_b = Worker(
        session_factory, lambda: FakeProvider([_finish()]), _require_approval_policy(), workspace
    )
    finished = await worker_b.run_once()

    assert finished is not None
    assert finished.status == "SUCCEEDED"

    async with session_factory() as probe:
        run_rows = list(
            await probe.scalars(
                select(ToolCall).where(
                    ToolCall.task_id == task_id, ToolCall.tool_name == "run_command"
                )
            )
        )
        assert len(run_rows) == 1  # executed exactly once, by worker_b, never by worker_a
        assert run_rows[0].decision == "allow"
        assert "warden-ok" in (run_rows[0].result_summary or "")

    # Terminal now: the volume this test created is cleaned up like any other finished task.
    with pytest.raises(docker_sdk.errors.NotFound):
        client.volumes.get(workspace_volume_name(str(task_id)))
