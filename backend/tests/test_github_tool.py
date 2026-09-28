"""tools/github.py against a fake GitHub (httpx.MockTransport): never the real network.

`sandbox` is real (Docker): "reuse the existing sandboxed read helper, no new container
code" means `open_pr` reads through the same container-hardened path every other tool does,
so faking that half away would test a different tool than the one that ships. The GitHub side
is a `MockTransport` that plays a small in-memory git/pulls server, records every request, and
never touches the network, so the assertions are about what warden sent, not about GitHub.
"""

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from warden import identity
from warden.config import Settings
from warden.identity.jwt import KeyPair
from warden.models import Task, User
from warden.policy.engine import Effect, PolicyContext, UserRef, combine, load_policy
from warden.sandbox.docker import Sandbox, SandboxProfile
from warden.tools.github import OpenPrArgs, open_pr, open_pr_paths
from warden.tools.registry import ToolContext, ToolError

pytestmark = pytest.mark.sandbox

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"
# Deliberately not shaped like a real GitHub PAT: a secret scanner flagging a fixture string
# defeats the point of a fixture. Same convention as test_broker.py/test_events.py.
FAKE_TOKEN = "not-a-real-secret-abcdefghijklmnop"


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
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    (root / ".env").write_text("SECRET=nope\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
async def sandbox(docker_available: None, workspace: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        yield box
    finally:
        await box.destroy()


def _settings() -> Settings:
    return Settings(github_token=SecretStr(FAKE_TOKEN), github_repo="acme/widgets")


async def _claims(session: AsyncSession, keys: KeyPair) -> identity.Claims:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = Task(user_id=user.id, spec="open a pr", idempotency_key=str(uuid.uuid4()))
    session.add(task)
    await session.flush()
    token = await identity.issue_agent_token(
        session, keys, task_id=task.id, scopes=["github:pr:open"]
    )
    return await identity.verify(session, keys, token)


class FakeGitHub:
    """A tiny, stateful stand-in for the Git Data + Pulls API, keyed by ref/branch name.

    Every request is recorded (`self.requests`) so a test can inspect what warden actually
    sent, in particular the `Authorization` header, without ever making a real HTTP call:
    `httpx.MockTransport` calls `handler` in place of opening a socket.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refs: dict[str, str] = {"heads/main": "base-sha"}
        self.pulls: list[dict[str, object]] = []
        self._blob_seq = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path

        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": self.refs["heads/main"]}})
        if method == "GET" and "/git/commits/" in path:
            return httpx.Response(200, json={"sha": "base-sha", "tree": {"sha": "base-tree"}})
        if method == "POST" and path.endswith("/git/blobs"):
            self._blob_seq += 1
            return httpx.Response(201, json={"sha": f"blob-{self._blob_seq}"})
        if method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "new-tree"})
        if method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "new-commit"})
        if method == "GET" and "/git/ref/heads/warden/" in path:
            branch = path.split("/git/ref/", 1)[1]
            if branch in self.refs:
                return httpx.Response(200, json={"object": {"sha": self.refs[branch]}})
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "POST" and path.endswith("/git/refs"):
            body = json.loads(request.content)
            self.refs[body["ref"].removeprefix("refs/")] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if method == "PATCH" and "/git/refs/heads/warden/" in path:
            body = json.loads(request.content)
            branch = path.split("/git/refs/", 1)[1]
            self.refs[branch] = body["sha"]
            return httpx.Response(
                200, json={"ref": f"refs/{branch}", "object": {"sha": body["sha"]}}
            )
        if method == "GET" and path.endswith("/pulls"):
            head = request.url.params.get("head")
            branch = head.split(":", 1)[1] if head else None
            return httpx.Response(200, json=[p for p in self.pulls if p["_branch"] == branch])
        if method == "POST" and path.endswith("/pulls"):
            body = json.loads(request.content)
            number = len(self.pulls) + 1
            pr = {
                "number": number,
                "html_url": f"https://github.com/acme/widgets/pull/{number}",
                "_branch": body["head"],
            }
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        raise AssertionError(f"unexpected request: {method} {request.url}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# --- the inspector: judged like apply_patch --------------------------------------------------


async def test_open_pr_paths_reports_every_path_verbatim() -> None:
    args = OpenPrArgs(title="t", body="b", branch_slug="fix", paths=["src/a.py", "src/b.py"])
    assert await open_pr_paths(args) == ["src/a.py", "src/b.py"]


def test_a_denied_path_blocks_the_whole_pr() -> None:
    """The real default policy's `never-read-secrets` (tool: "*") must deny a call that
    would publish `.env`, exactly as it would for apply_patch: `combine()` takes the most
    restrictive decision across every path this call touches."""
    policy = load_policy(DEFAULT_POLICY)
    user = UserRef(role="worker")
    decisions = [
        policy.evaluate(PolicyContext(tool="github.open_pr", path=path, user=user))
        for path in ("src/app.py", ".env")
    ]
    assert combine(decisions).effect == Effect.DENY


# --- the real flow, against the fake GitHub ---------------------------------------------------


async def test_open_pr_runs_the_full_git_data_sequence_and_opens_a_pr(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    args = OpenPrArgs(
        title="Fix the timeout",
        body="Closes the bug.",
        branch_slug="fix-timeout",
        paths=["src/app.py"],
    )

    async with fake.client() as client:
        result = await open_pr(
            sandbox, _settings(), args, ToolContext(claims=claims, session=session), client=client
        )

    assert "opened PR #1" in result
    assert fake.pulls[0]["_branch"] == f"warden/{str(claims.task_id)[:8]}-fix-timeout"

    methods_and_paths = [(r.method, r.url.path) for r in fake.requests]
    assert ("GET", "/repos/acme/widgets/git/ref/heads/main") in methods_and_paths
    assert ("POST", "/repos/acme/widgets/git/blobs") in methods_and_paths
    assert ("POST", "/repos/acme/widgets/git/trees") in methods_and_paths
    assert ("POST", "/repos/acme/widgets/git/commits") in methods_and_paths
    assert ("POST", "/repos/acme/widgets/pulls") in methods_and_paths

    # The brokered token, not a placeholder: every request warden sent carries it.
    assert all(r.headers["authorization"] == f"Bearer {FAKE_TOKEN}" for r in fake.requests)


async def test_open_pr_is_idempotent_for_the_same_task_and_branch(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    args = OpenPrArgs(title="Fix", body="body", branch_slug="fix", paths=["src/app.py"])

    async with fake.client() as client:
        first = await open_pr(
            sandbox, _settings(), args, ToolContext(claims=claims, session=session), client=client
        )
        second = await open_pr(
            sandbox, _settings(), args, ToolContext(claims=claims, session=session), client=client
        )

    assert first == second
    assert len(fake.pulls) == 1, "a second call for the same task must not open a second PR"
    pull_posts = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pulls")]
    assert len(pull_posts) == 1


async def test_open_pr_publishes_the_files_content_read_from_the_sandbox(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    """No new container code: this is `sandboxed.read_file` under the hood, so the blob
    content warden sends to GitHub is exactly what is on disk in the sandbox."""
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    args = OpenPrArgs(title="t", body="b", branch_slug="content-check", paths=["src/app.py"])

    async with fake.client() as client:
        await open_pr(
            sandbox, _settings(), args, ToolContext(claims=claims, session=session), client=client
        )

    blob_post = next(
        r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/git/blobs")
    )
    assert json.loads(blob_post.content)["content"] == "print('hello')\n"


async def test_the_pr_body_names_the_task_and_files(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    args = OpenPrArgs(
        title="t", body="Fixed the race condition.", branch_slug="report", paths=["src/app.py"]
    )

    async with fake.client() as client:
        await open_pr(
            sandbox, _settings(), args, ToolContext(claims=claims, session=session), client=client
        )

    pr_post = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pulls"))
    body = json.loads(pr_post.content)["body"]
    assert str(claims.task_id) in body
    assert "src/app.py" in body
    assert "Fixed the race condition." in body


# --- refusals: the token itself, judged by tools/gateway.py, not this module -----------------
#
# gateway.py's own test suite (test_gateway.py) already covers expired/revoked/wrong-task/
# missing-scope refusals generically. What is specific to this tool is that `broker.
# get_credential` is a second, independent check underneath the gateway's: a token that
# passed the gateway (it carries `github:pr:open`, `required_scope`'s own check) but whose
# scope the broker's static map does not recognise, or whose secret is not configured, must
# still fail as a normal tool error, not a crash.


async def test_open_pr_fails_as_a_tool_error_when_the_secret_is_not_configured(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    claims = await _claims(session, keys)
    args = OpenPrArgs(title="t", body="b", branch_slug="no-secret", paths=["src/app.py"])
    unconfigured = Settings(github_token=SecretStr(""), github_repo="acme/widgets")

    with pytest.raises(ToolError, match="credential denied"):
        await open_pr(sandbox, unconfigured, args, ToolContext(claims=claims, session=session))
