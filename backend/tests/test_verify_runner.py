"""The verifier against real containers (ADR-026).

Every check here runs the real command in the real sandbox image, against a copy of
examples/target-repo, because "ruff passed" proven against a stub proves nothing about the
image. The two durability tests cause their failure for real: one kills the container through
a cancel, the other kills the worker *process* in the middle of verification.
"""

import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import TEST_DB
from warden.config import get_settings
from warden.core import cancel, queue
from warden.core.events import read_events
from warden.core.worker import POLICY_FILE as WORKER_POLICY
from warden.core.worker import WORKSPACE as WORKER_WORKSPACE
from warden.core.worker import Worker
from warden.db import with_database
from warden.identity.jwt import KeyPair
from warden.models import Evidence, ModelCall, User, Verdict
from warden.policy.engine import Effect, Policy, Rule, load_policy
from warden.providers.base import Completion, Message, ToolSchema, Usage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import Sandbox, SandboxProfile, discard_workspace_volume
from warden.tools.sandboxed import WriteFileArgs, write_file
from warden.tools.workspace import is_ignored
from warden.verify.runner import Verifier

pytestmark = pytest.mark.sandbox

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET_REPO = REPO_ROOT / "examples" / "target-repo"
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

SLOW_TEST = "import time\n\n\ndef test_slow() -> None:\n    time.sleep({seconds})\n"


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


@pytest.fixture
def target_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A private copy of the demo target repo, so a test can never edit the real one."""
    copy = tmp_path / "target-repo"
    shutil.copytree(
        TARGET_REPO,
        copy,
        ignore=lambda _dir, names: [n for n in names if is_ignored(pathlib.PurePosixPath(n))],
    )
    return copy


@pytest.fixture
async def sandbox(docker_available: None, target_repo: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), target_repo)
    try:
        yield box
    finally:
        await box.destroy()


@pytest.fixture
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()


def _allow_all() -> Policy:
    return Policy(
        [Rule(id="allow-all", effect=Effect.ALLOW, when={"tool": "*"})],
        default=Effect.DENY,
        policy_hash="test",
    )


class _ReviewerOnly:
    """The provider of a resumed worker: it may be asked for the independent verdict and for
    nothing else. Any other request means the resume bought the agent's turn again."""

    name = "fake"

    def __init__(self) -> None:
        self.reviews = 0

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        names = [tool.name for tool in tools or ()]
        assert names == ["submit_verdict"], f"a resumed worker called the model with {names}"
        self.reviews += 1
        return Completion(
            provider="fake",
            model="fake-model",
            stop_reason="tool_use",
            tool_calls=[
                ProviderToolCall(
                    id="resumed-verdict",
                    name="submit_verdict",
                    arguments={"passed": True, "findings": []},
                )
            ],
            usage=Usage(),
        )


async def _run_all(verifier: Verifier) -> dict[str, dict[str, Any]]:
    return {kind: await verifier.check(kind) for kind in verifier.kinds}


async def test_a_clean_change_to_the_target_repo_passes_every_check(
    sandbox: Sandbox, target_repo: pathlib.Path
) -> None:
    original = (target_repo / "src" / "app.py").read_text(encoding="utf-8")
    await write_file(
        sandbox,
        WriteFileArgs(
            path="src/app.py", content=original + "\n\ndef helper() -> int:\n    return 1\n"
        ),
    )

    evidence = await _run_all(Verifier(sandbox, target_repo))

    assert evidence["diff"]["status"] == "ok"
    [change] = evidence["diff"]["files"]
    assert (change["path"], change["change"], change["additions"]) == ("src/app.py", "modified", 4)
    for kind in ("lint", "types", "tests"):
        assert evidence[kind]["status"] == "passed", evidence[kind]
        assert evidence[kind]["passed"] is True


async def test_a_failing_test_is_recorded_as_failed_with_its_output(
    sandbox: Sandbox, target_repo: pathlib.Path
) -> None:
    await write_file(
        sandbox,
        WriteFileArgs(
            path="tests/test_broken.py", content="def test_broken() -> None:\n    assert 1 == 2\n"
        ),
    )

    tests = await Verifier(sandbox, target_repo).check("tests")

    assert tests["status"] == "failed"
    assert tests["passed"] is False
    [run] = tests["commands"]
    assert run["exit_code"] == 1
    assert "test_broken" in run["output"]


async def test_the_diff_is_taken_before_any_agent_code_runs(
    sandbox: Sandbox, target_repo: pathlib.Path
) -> None:
    """A `conftest.py` executes the moment pytest starts. Whatever it does to the workspace
    must not be what the diff reports, which is why `diff` is first in `KINDS`."""
    await write_file(
        sandbox,
        WriteFileArgs(
            path="conftest.py",
            content='import pathlib\n\npathlib.Path("planted.txt").write_text("late\\n")\n',
        ),
    )
    verifier = Verifier(sandbox, target_repo)

    evidence = await _run_all(verifier)

    assert [f["path"] for f in evidence["diff"]["files"]] == ["conftest.py"]
    # The file really was planted by the time the tests ran: taken again now, it shows up.
    later = await verifier.check("diff")
    assert "planted.txt" in [f["path"] for f in later["files"]]


async def test_a_cancel_during_the_tests_kills_the_container_and_records_nothing_more(
    docker_available: None,
    empty_queue: None,
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: pathlib.Path,
) -> None:
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
                            id="w1",
                            name="write_file",
                            arguments={
                                "path": "tests/test_slow.py",
                                "content": SLOW_TEST.format(seconds=60),
                            },
                        )
                    ]
                ),
                ScriptStep(
                    tool_calls=[
                        ProviderToolCall(id="f1", name="finish", arguments={"summary": "slow"})
                    ]
                ),
            ]
        )

    worker = Worker(session_factory, provider_factory, _allow_all(), target_repo, keys)
    client = docker_sdk.from_env()
    running = asyncio.create_task(worker.run_once())
    try:
        loop_time = asyncio.get_event_loop().time
        deadline = loop_time() + 90
        while loop_time() < deadline:
            rows = await read_events(session, task_id)
            if any(e.type == "verify.recorded" and e.payload["kind"] == "types" for e in rows):
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("verification never reached the tests check")

        await cancel.request_cancel(session, task_id)
        await session.commit()
        cancel_requested_at = loop_time()

        result = await asyncio.wait_for(running, timeout=30)
        assert result is not None
        assert result.status == "CANCELLED"
        # The 60s test was killed, not waited out.
        assert loop_time() - cancel_requested_at < 20

        kinds = await session.scalars(
            select(Evidence.kind).where(Evidence.task_id == task_id).order_by(Evidence.created_at)
        )
        assert list(kinds) == ["diff", "lint", "types"]
        # Counted, not trusted to the `finally`: the container is really gone.
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []
    finally:
        if not running.done():
            running.cancel()
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))


async def test_verification_survives_the_worker_process_being_killed(
    docker_available: None,
    empty_queue: None,
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """`kill` on the worker while `tests` runs. The next worker resumes in VERIFYING, never
    calls the agent's model, and records only what is missing: the database refuses a
    duplicate. The only model call it makes is the reviewer's."""
    import docker as docker_sdk

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = await queue.enqueue(
        session, user_id=user.id, spec="verify probe", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id

    script = tmp_path / "script.yaml"
    script.write_text(
        f"""
script:
  - tool_call:
      name: write_file
      args:
        path: tests/test_slow.py
        content: {json.dumps(SLOW_TEST.format(seconds=8))}
  - tool_call:
      name: finish
      args: {{ summary: "wrote a slow test" }}
  - tool_call:
      name: submit_verdict
      args: {{ passed: true, findings: [] }}
""",
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        """
version: 1
default: deny
rules:
  - id: allow-test-writes
    effect: allow
    when:
      tool: write_file
      path: ["tests/**"]
""",
        encoding="utf-8",
    )
    key_path = tmp_path / "jwt-private.pem"
    key_path.write_bytes(
        keys.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    env = {
        **os.environ,
        "DATABASE_URL": with_database(get_settings().database_url, TEST_DB),
        "JWT_PRIVATE_KEY_PATH": str(key_path),
    }

    client = docker_sdk.from_env()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(  # noqa: ASYNC220 - same reasoning as test_durability.py
            [
                sys.executable,
                "-m",
                "warden.core.worker",
                "--script",
                str(script),
                "--policy",
                str(policy_path),
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        loop_time = asyncio.get_event_loop().time
        deadline = loop_time() + 90
        while loop_time() < deadline:
            assert proc.poll() is None, "the worker exited before reaching the tests check"
            rows = await read_events(session, task_id)
            if any(e.type == "verify.recorded" and e.payload["kind"] == "types" for e in rows):
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("verification never reached the tests check")

        proc.kill()  # TerminateProcess on Windows, SIGKILL on Linux: no cleanup runs.
        proc.wait(timeout=15)
        proc = None

        before = await session.scalars(select(Evidence.kind).where(Evidence.task_id == task_id))
        assert sorted(before) == ["diff", "lint", "types"]

        await queue.expire_lease_now(session, task_id)
        await session.commit()

        resumed_provider = _ReviewerOnly()
        worker2 = Worker(
            session_factory,
            lambda: resumed_provider,
            load_policy(policy_path),
            WORKER_WORKSPACE,
            keys,
        )
        result = await worker2.run_once()

        assert result is not None
        assert result.status == "SUCCEEDED"
        assert result.summary == "wrote a slow test"
        assert resumed_provider.reviews == 1

        recorded = (
            await session.scalars(
                select(Evidence).where(Evidence.task_id == task_id).order_by(Evidence.created_at)
            )
        ).all()
        assert [row.kind for row in recorded] == ["diff", "lint", "types", "tests"]
        by_kind = {row.kind: row.payload for row in recorded}
        # The resumed worker's tests ran against the workspace the dead one left behind.
        assert by_kind["tests"]["status"] == "passed"
        assert [f["path"] for f in by_kind["diff"]["files"]] == ["tests/test_slow.py"]

        model_calls = await session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task_id)
        )
        # The agent's two turns (replayed from the log, never bought again) plus the
        # independent reviewer's single call, made by the second worker.
        assert model_calls == 3
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))


async def test_the_review_survives_the_worker_process_being_killed_inside_the_call(
    docker_available: None,
    empty_queue: None,
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """`kill` on a real worker while it waits on the reviewer. All four checks are on record
    and no verdict is. The next worker must not re-run a check or buy the agent's turn again;
    it calls the reviewer once more (the lost call left no trace to reuse) and the task ends
    with exactly one verdict row and a decided status.

    The reviewer's answer is scripted to take 60 s, so the kill lands inside the call instead
    of racing a call that would otherwise finish in a millisecond."""
    import docker as docker_sdk

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = await queue.enqueue(
        session, user_id=user.id, spec="review probe", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id = task.id

    script = tmp_path / "script.yaml"
    script.write_text(
        """
script:
  - tool_call:
      name: finish
      args: { summary: "changed nothing" }
  - tool_call:
      name: submit_verdict
      args: { passed: true, findings: [] }
    delay_seconds: 60
""",
        encoding="utf-8",
    )
    key_path = tmp_path / "jwt-private.pem"
    key_path.write_bytes(
        keys.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    env = {
        **os.environ,
        "DATABASE_URL": with_database(get_settings().database_url, TEST_DB),
        "JWT_PRIVATE_KEY_PATH": str(key_path),
    }

    client = docker_sdk.from_env()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(  # noqa: ASYNC220 - same reasoning as test_durability.py
            [sys.executable, "-m", "warden.core.worker", "--script", str(script)],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        loop_time = asyncio.get_event_loop().time
        deadline = loop_time() + 180
        while loop_time() < deadline:
            assert proc.poll() is None, "the worker exited before reaching the review"
            rows = await read_events(session, task_id)
            if any(e.type == "verify.recorded" and e.payload["kind"] == "tests" for e in rows):
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("verification never recorded the tests check")

        # A moment for the worker to pass its checkpoint and enter the (60 s) reviewer call.
        await asyncio.sleep(2)
        proc.kill()  # TerminateProcess on Windows, SIGKILL on Linux: no cleanup runs.
        proc.wait(timeout=15)
        proc = None

        assert sorted(
            await session.scalars(select(Evidence.kind).where(Evidence.task_id == task_id))
        ) == [
            "diff",
            "lint",
            "tests",
            "types",
        ]
        assert (
            await session.scalars(select(Verdict).where(Verdict.task_id == task_id))
        ).all() == []

        await queue.expire_lease_now(session, task_id)
        await session.commit()

        resumed_provider = _ReviewerOnly()
        worker2 = Worker(
            session_factory,
            lambda: resumed_provider,
            load_policy(WORKER_POLICY),
            WORKER_WORKSPACE,
            keys,
        )
        result = await worker2.run_once()

        assert result is not None
        # The target repo at baseline passes every check, and the reviewer approved.
        assert result.status == "SUCCEEDED", result.reason
        assert resumed_provider.reviews == 1

        verdicts = (await session.scalars(select(Verdict).where(Verdict.task_id == task_id))).all()
        assert [(v.verifier, v.passed) for v in verdicts] == [("independent", True)]
        # One agent call (the `finish` turn) and the reviewer's, made by the second worker.
        purposes = await session.scalars(
            select(ModelCall.purpose).where(ModelCall.task_id == task_id)
        )
        assert sorted(purposes) == ["agent", "reviewer"]
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))
