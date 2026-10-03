"""A worker killed while it publishes (ADR-028), against a real process and a real container.

The publication phase is a commit-then-act sequence like every other in the loop: the proposal
(`publish.requested`) and the human's answer are on record, then GitHub is called, then the
result is recorded. The crash that matters is the one between the last two: GitHub may or may
not have heard of the request, and the database does not know. The resumed worker must neither
ask the human a second time nor open a second pull request. The first is the replay of the
recorded request and decision; the second is the branch name being a function of the task
(ADR-025), so repeating the call converges on the same PR.
"""

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from cryptography.hazmat.primitives import serialization
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import TEST_DB
from tests.fake_github import FakeGitHub, FakeGitHubServer
from warden.config import get_settings
from warden.core import approvals, queue
from warden.core.events import read_events
from warden.core.worker import POLICY_FILE as WORKER_POLICY
from warden.core.worker import WORKSPACE as WORKER_WORKSPACE
from warden.core.worker import Worker
from warden.db import with_database
from warden.identity.jwt import KeyPair
from warden.models import Approval, ModelCall, User
from warden.policy.engine import load_policy
from warden.providers.base import Completion, Message, ToolSchema
from warden.sandbox.docker import discard_workspace_volume

pytestmark = pytest.mark.sandbox

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
FAKE_TOKEN = "not-a-real-secret-abcdefghijklmnop"
NOTES = '"""Notes the agent added."""\n\nNOTE = "hello"\n'


@pytest.fixture(scope="session")
def docker_available() -> None:
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()


class _NoModel:
    """The provider of a resumed worker: the agent's conversation and the verdict are both on
    record, so any call to the model means the resume bought something again."""

    name = "fake"

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        raise AssertionError("a resumed worker called the model")


async def test_a_worker_killed_while_github_is_called_neither_asks_twice_nor_opens_two_prs(
    docker_available: None,
    empty_queue: None,
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import docker as docker_sdk

    fake = FakeGitHub()
    github = FakeGitHubServer(fake, block_first=True)
    github.start()

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = await queue.enqueue(
        session, user_id=user.id, spec="Add a notes module", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    task_id, user_id = task.id, user.id

    script = tmp_path / "script.yaml"
    script.write_text(
        f"""
script:
  - tool_call:
      name: write_file
      args:
        path: src/notes.py
        content: {json.dumps(NOTES)}
  - tool_call:
      name: finish
      args: {{ summary: "added a notes module" }}
  - tool_call:
      name: submit_verdict
      args: {{ passed: true, findings: [] }}
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
    github_env = {
        "GITHUB_REPO": "acme/widgets",
        "GITHUB_TOKEN": FAKE_TOKEN,
        "GITHUB_API_URL": github.url,
    }
    env = {
        **os.environ,
        **github_env,
        "DATABASE_URL": with_database(get_settings().database_url, TEST_DB),
        "JWT_PRIVATE_KEY_PATH": str(key_path),
    }
    # The second worker runs in this process, so it reads the same configuration the first
    # one got through its environment.
    for name, value in github_env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    client = docker_sdk.from_env()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(  # noqa: ASYNC220 - needs a real process to kill, as test_durability.py
            [
                sys.executable,
                "-m",
                "warden.core.worker",
                "--script",
                str(script),
                "--policy",
                str(WORKER_POLICY),
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        loop_time = asyncio.get_event_loop().time

        async def until(condition: str, seconds: float) -> None:
            deadline = loop_time() + seconds
            while loop_time() < deadline:
                assert proc is not None and proc.poll() is None, (
                    f"the worker died before {condition}"
                )
                if await check():
                    return
                await asyncio.sleep(0.2)
            raise AssertionError(f"never reached: {condition}")

        # 1. The worker runs the agent, verifies, reviews, proposes the pull request, and parks
        #    the task for a human.
        async def asked() -> bool:
            rows = await read_events(session, task_id)
            return any(e.type == "approval.requested" for e in rows)

        check = asked
        await until("the pull request was proposed", 240)
        events = await read_events(session, task_id)
        assert [e.type for e in events if e.type.startswith("publish.")] == ["publish.requested"]

        # 2. A human approves. The (still running) worker claims the task again and calls
        #    GitHub, where the fake parks the request: the worker is now inside the call.
        approval = (
            await session.scalars(select(Approval).where(Approval.task_id == task_id))
        ).one()
        await approvals.decide_approval(
            session, approval.id, approve=True, user_id=user_id, note=None
        )
        await session.commit()

        async def inside_github_call() -> bool:
            return github.first_request.is_set()

        check = inside_github_call
        await until("the worker reached GitHub", 120)

        # 3. The kill lands inside the call: the proposal and the approval are on record,
        #    `tool.executed` is not, and GitHub never got an answer back to the worker.
        proc.kill()  # TerminateProcess on Windows, SIGKILL on Linux: no cleanup runs.
        proc.wait(timeout=15)
        proc = None
        github.release.set()

        events = await read_events(session, task_id)
        assert not [
            e
            for e in events
            if e.type == "tool.executed" and e.payload["id"].startswith("publish-")
        ]
        assert fake.pulls == []

        await queue.expire_lease_now(session, task_id)
        await session.commit()

        # 4. A second worker resumes: no model call, no second question, one pull request.
        worker2 = Worker(
            session_factory,
            lambda: _NoModel(),
            load_policy(WORKER_POLICY),
            WORKER_WORKSPACE,
            keys,
        )
        result = await worker2.run_once()

        assert result is not None
        assert result.status == "SUCCEEDED", result.reason
        assert len(fake.pulls) == 1, "a resumed publication must not open a second pull request"

        events = await read_events(session, task_id)
        types = [e.type for e in events]
        assert types.count("publish.requested") == 1
        assert types.count("approval.requested") == 1, "the human was asked a second time"
        executed = [
            e
            for e in events
            if e.type == "tool.executed" and e.payload["id"].startswith("publish-")
        ]
        assert len(executed) == 1 and executed[0].payload["output"].startswith("opened PR #1")
        assert types[-1] == "task.finished"

        approvals_rows = (
            await session.scalars(select(Approval).where(Approval.task_id == task_id))
        ).all()
        assert [(a.tool_call_id, a.status) for a in approvals_rows] == [
            (f"publish-{task_id}", "approved")
        ]
        # The planner's, the agent's two turns and the reviewer's, all bought by the first worker.
        calls = await session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task_id)
        )
        assert calls == 4
        assert client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}) == []
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        github.stop()
        get_settings.cache_clear()
        for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
            stray.remove(force=True)
        await asyncio.to_thread(discard_workspace_volume, str(task_id))
