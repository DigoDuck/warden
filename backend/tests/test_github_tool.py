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
import shutil
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_github import FakeGitHub
from warden import identity
from warden.audit.log import _LOCK_KEY
from warden.config import Settings
from warden.core import approvals, queue
from warden.core.events import read_events
from warden.core.loop import RunResult, run_task
from warden.core.replay import rebuild
from warden.identity.jwt import KeyPair
from warden.models import Approval, AuditLog, Task, ToolCall, User
from warden.policy.engine import (
    Effect,
    Policy,
    PolicyContext,
    UserRef,
    combine,
    load_policy,
    never_readable,
)
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import Sandbox, SandboxProfile
from warden.tools.github import OpenPrArgs, open_pr, open_pr_paths
from warden.tools.registry import ToolContext, ToolError
from warden.tools.sandboxed import (
    ReadFileArgs,
    WriteFileArgs,
    build_publish_registry,
    build_registry,
    read_file,
    write_file,
)
from warden.tools.workspace import is_ignored
from warden.verify.reviewer import ProviderReviewer
from warden.verify.runner import Verifier

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


@pytest.fixture(autouse=True)
async def _empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    """`_claims()` commits QUEUED tasks, and `queue.claim` hands out the oldest one, so without
    this the end-to-end test could claim a task another test left behind instead of its own."""
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
    (root / "src" / "other.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
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
            sandbox,
            _settings(),
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
            client=client,
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
            sandbox,
            _settings(),
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
            client=client,
        )
        second = await open_pr(
            sandbox,
            _settings(),
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
            client=client,
        )

    assert first == second
    assert len(fake.pulls) == 1, "a second call for the same task must not open a second PR"
    pull_posts = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pulls")]
    assert len(pull_posts) == 1


async def test_a_second_publish_builds_on_the_branch_instead_of_overwriting_it(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    """Same task, same slug, a second batch of files (the agent fixed something after the
    PR was opened). The branch must move forward, keeping the first file, never be
    force-reset onto the base: that would silently drop already-published work from a PR a
    human may be reviewing."""
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    context = ToolContext(claims=claims, session=session, checkpoint=session.commit)
    branch = f"warden/{str(claims.task_id)[:8]}-grow"

    async with fake.client() as client:
        await open_pr(
            sandbox,
            _settings(),
            OpenPrArgs(title="t", body="b", branch_slug="grow", paths=["src/app.py"]),
            context,
            client=client,
        )
        first_head = fake.refs[f"heads/{branch}"]
        await open_pr(
            sandbox,
            _settings(),
            OpenPrArgs(title="t", body="b", branch_slug="grow", paths=["src/other.py"]),
            context,
            client=client,
        )

    assert set(fake.branch_files(branch)) == {"src/app.py", "src/other.py"}
    assert fake.commits[fake.refs[f"heads/{branch}"]]["parents"] == [first_head]
    patches = [json.loads(r.content) for r in fake.requests if r.method == "PATCH"]
    assert patches and not any(p.get("force") for p in patches), "a ref was force-updated"
    assert len(fake.pulls) == 1


async def test_replaying_the_same_publish_creates_no_new_commit(
    sandbox: Sandbox, session: AsyncSession, keys: KeyPair
) -> None:
    """ADR-019's at-least-once window: a crash after GitHub answered but before
    `tool.executed` committed replays the call. Same files on the same branch must be a
    no-op on the branch, not an empty commit per replay."""
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    context = ToolContext(claims=claims, session=session, checkpoint=session.commit)
    args = OpenPrArgs(title="t", body="b", branch_slug="replay", paths=["src/app.py"])

    async with fake.client() as client:
        await open_pr(sandbox, _settings(), args, context, client=client)
        await open_pr(sandbox, _settings(), args, context, client=client)

    commit_posts = [
        r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/git/commits")
    ]
    assert len(commit_posts) == 1


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
            sandbox,
            _settings(),
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
            client=client,
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
            sandbox,
            _settings(),
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
            client=client,
        )

    pr_post = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pulls"))
    body = json.loads(pr_post.content)["body"]
    assert str(claims.task_id) in body
    assert "src/app.py" in body
    assert "Fixed the race condition." in body


async def test_no_transaction_or_audit_lock_is_held_while_github_is_called(
    sandbox: Sandbox,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    keys: KeyPair,
) -> None:
    """`broker.get_credential` appends a `credential.granted` audit row, and `audit.append`
    takes the chain's transaction-scoped advisory lock. If that transaction is still open
    while GitHub is being called, every other task's audit write in the whole control plane
    waits on GitHub's latency (ADR-019: no transaction open while waiting on the outside).

    Observed from a second connection, from inside the fake GitHub, at the moment of each
    request: the lock must be free and the grant must already be committed."""
    fake = FakeGitHub()
    claims = await _claims(session, keys)
    await session.commit()
    observed: list[tuple[bool, int]] = []

    async def probing_handler(request: httpx.Request) -> httpx.Response:
        async with session_factory() as other:
            # A try-lock, never a blocking one: if the loop's session holds it, this returns
            # false immediately instead of deadlocking the test. Released by the rollback.
            free = await other.scalar(select(func.pg_try_advisory_xact_lock(_LOCK_KEY)))
            granted = await other.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "credential.granted",
                    AuditLog.target_id == str(claims.jti),
                )
            )
            await other.rollback()
        observed.append((bool(free), int(granted or 0)))
        return fake.handler(request)

    args = OpenPrArgs(title="t", body="b", branch_slug="no-lock", paths=["src/app.py"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(probing_handler)) as client:
        await open_pr(
            sandbox,
            _settings(),
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
            client=client,
        )

    assert observed, "the fake GitHub was never called"
    assert all(free for free, _ in observed), "the audit advisory lock was held during HTTP"
    assert all(granted == 1 for _, granted in observed), "the grant was not committed first"


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
        await open_pr(
            sandbox,
            unconfigured,
            args,
            ToolContext(claims=claims, session=session, checkpoint=session.commit),
        )


# --- conditional registration -----------------------------------------------------------------


def test_the_agents_registry_never_offers_github_open_pr(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model cannot even name the tool: publishing is the control plane's phase after the
    verdict (ADR-028), so it is absent from the agent's registry whether or not GitHub is
    configured."""
    monkeypatch.setattr("warden.config.get_settings", _settings)
    assert not build_registry(sandbox).has("github.open_pr")


def test_the_publish_registry_holds_only_open_pr_and_only_when_configured(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "warden.config.get_settings",
        lambda: Settings(github_token=SecretStr(""), github_repo=""),
    )
    assert build_publish_registry(sandbox) is None

    monkeypatch.setattr("warden.config.get_settings", _settings)
    registry = build_publish_registry(sandbox)
    assert registry is not None
    assert [schema.name for schema in registry.schemas()] == ["github.open_pr"]
    assert registry.required_scope("github.open_pr") == "github:pr:open"


# --- end to end: nothing secret ever reaches the log (ADR-025, deliverable 5) -----------------


async def _claimed_task(session: AsyncSession) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    await queue.enqueue(
        session, user_id=user.id, spec="Add a notes module", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    claimed = await queue.claim(session, "worker-e2e")
    assert claimed is not None
    return claimed


NOTES = '"""Notes the agent added."""\n\nNOTE = "hello"\n'


def _step(name: str, **arguments: object) -> ScriptStep:
    return ScriptStep(
        tool_calls=[ProviderToolCall(id=f"call-{name}", name=name, arguments=arguments)]
    )


@pytest.fixture
def clean_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A private copy of the demo target repo, which passes lint, types and tests as it is, so
    an agent that adds a well-formed file gets a green verification and a verdict to publish."""
    copy = tmp_path / "target-repo"
    shutil.copytree(
        REPO_ROOT / "examples" / "target-repo",
        copy,
        ignore=lambda _dir, names: [n for n in names if is_ignored(pathlib.PurePosixPath(n))],
    )
    return copy


@pytest.fixture
async def clean_sandbox(docker_available: None, clean_repo: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), clean_repo)
    try:
        yield box
    finally:
        await box.destroy()


def _github_for(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> None:
    """Configure GitHub and route every client `tools/github.py` builds to `fake`."""
    monkeypatch.setattr(
        "warden.config.get_settings",
        lambda: Settings(github_token=SecretStr(FAKE_TOKEN), github_repo="acme/widgets"),
    )
    # Captured before patching: the lambda below must build a *real* client, not recurse into
    # itself through the module-global `httpx.AsyncClient` name it is about to replace.
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "warden.tools.github.httpx.AsyncClient",
        lambda *a, **k: real_async_client(transport=httpx.MockTransport(fake.handler)),
    )


async def _agent_run(
    session: AsyncSession,
    keys: KeyPair,
    sandbox: Sandbox,
    repo: pathlib.Path,
    task: Task,
    policy: Policy,
    script: list[ScriptStep],
) -> tuple[RunResult, FakeProvider]:
    """The agent writes files, finishes, and is verified and reviewed for real: the real
    container, the real checks, the real publication registry. Only the model is a script."""
    provider = FakeProvider([*script, _step("submit_verdict", passed=True, findings=[])])
    result = await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        policy,
        keys=keys,
        workspace=repo,
        holder=task.claimed_by,
        verifier=Verifier(sandbox, repo, exclude=never_readable(policy)),
        reviewer=ProviderReviewer(provider),
        publisher=build_publish_registry(sandbox),
    )
    return result, provider


async def test_the_verified_diff_is_published_after_approval_and_no_secret_reaches_the_log(
    clean_sandbox: Sandbox,
    clean_repo: pathlib.Path,
    session: AsyncSession,
    keys: KeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole publication phase end to end (ADR-028): the agent writes a file, the control
    plane verifies it for real, the reviewer approves, the control plane proposes the pull
    request, a human approves it, and the fake GitHub receives exactly the verified file and
    a report with the evidence and the verdict. Then every task_events payload, tool_calls row
    and audit_log row this run produced is scanned for the configured GitHub token and for
    every agent JWT minted along the way. Neither may appear anywhere: task_events/tool_calls go
    through core/events.py's broker.redact() choke point, and the GitHub token itself is only
    ever read into an HTTP header this module builds, never into a string it raises or logs.
    """
    fake = FakeGitHub()
    _github_for(monkeypatch, fake)

    # A spy, not a mock: the real issue_agent_token still runs, its return value (the actual
    # JWT string this task's tool calls were authorised with) is just also captured here, so
    # the scan at the bottom has something concrete to look for.
    minted_tokens: list[str] = []
    original_issue = identity.issue_agent_token

    async def _spy_issue(*args: object, **kwargs: object) -> str:
        token = await original_issue(*args, **kwargs)  # type: ignore[arg-type]
        minted_tokens.append(token)
        return token

    monkeypatch.setattr(identity, "issue_agent_token", _spy_issue)

    audit_baseline = await session.scalar(select(func.max(AuditLog.id))) or 0
    task = await _claimed_task(session)
    policy = load_policy(DEFAULT_POLICY)

    paused, _ = await _agent_run(
        session,
        keys,
        clean_sandbox,
        clean_repo,
        task,
        policy,
        [_step("write_file", path="src/notes.py", content=NOTES), _step("finish", summary="done")],
    )
    assert paused.status == "WAITING_APPROVAL", paused.reason
    # Proposed, not sent: GitHub has not been touched while the human decides.
    assert fake.requests == []

    approval = (await session.scalars(select(Approval).where(Approval.task_id == task.id))).one()
    assert approval.tool_call_id == f"publish-{task.id}"
    assert approval.args_safe["paths"] == ["src/notes.py"]
    await approvals.decide_approval(
        session, approval.id, approve=True, user_id=task.user_id, note=None
    )
    await session.commit()

    resumed = await queue.claim(session, "worker-e2e-2")
    assert resumed is not None
    await session.commit()
    resume = rebuild(await read_events(session, task.id))
    provider = FakeProvider([])  # the agent's conversation is over: any model call raises
    result = await run_task(
        session,
        resumed,
        provider,
        build_registry(clean_sandbox),
        policy,
        keys=keys,
        workspace=clean_repo,
        resume=resume,
        holder=resumed.claimed_by,
        verifier=Verifier(clean_sandbox, clean_repo, exclude=never_readable(policy)),
        reviewer=ProviderReviewer(provider),
        publisher=build_publish_registry(clean_sandbox),
    )

    assert result.status == "SUCCEEDED", result.reason
    assert len(fake.pulls) == 1, "the PR really was opened, this is not a vacuously passing scan"
    branch = f"warden/{str(task.id)[:8]}-add-a-notes-module"
    assert fake.branch_files(branch).keys() == {"src/notes.py"}
    blob = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/blobs"))
    assert json.loads(blob.content)["content"] == NOTES  # exactly the verified file

    pr_post = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pulls"))
    body = json.loads(pr_post.content)["body"]
    assert "| tests | passed |" in body and "| lint | passed |" in body
    assert "Independent verdict:** approved" in body
    assert "Add a notes module" in json.loads(pr_post.content)["title"]
    # One token for the agent's write_file, one for the publication.
    assert len(minted_tokens) == 2

    # The scan.
    event_payloads = [event.payload for event in await read_events(session, task.id)]
    tool_calls = list(await session.scalars(select(ToolCall).where(ToolCall.task_id == task.id)))
    audit_rows = list(await session.scalars(select(AuditLog).where(AuditLog.id > audit_baseline)))

    haystack = "\n".join(
        [
            *(json.dumps(payload, default=str) for payload in event_payloads),
            *(row.result_summary or "" for row in tool_calls),
            *(row.error or "" for row in tool_calls),
            *(json.dumps(row.args_safe, default=str) for row in tool_calls),
            *(json.dumps(row.details, default=str) for row in audit_rows),
        ]
    )

    assert FAKE_TOKEN not in haystack
    for token in minted_tokens:
        assert token not in haystack


# --- publishing exactly what was verified (ADR-028) --------------------------------------------

FEATURE = '"""A feature."""\n\nVALUE = 1\n'
# Runs under the control plane's own `pytest` check, which is the only check that executes the
# agent's code. It rewrites a file the diff (taken before any test ran) already recorded.
REWRITER = '''"""Rewrites a verified file while the tests run."""

import pathlib

FEATURE = pathlib.Path(__file__).resolve().parents[1] / "src" / "feature.py"


def test_rewrites_the_feature() -> None:
    FEATURE.write_text("VALUE = 2\\n", encoding="utf-8")
'''


async def _resume_and_publish(
    session: AsyncSession,
    keys: KeyPair,
    sandbox: Sandbox,
    repo: pathlib.Path,
    task: Task,
    policy: Policy,
) -> RunResult:
    approval = (await session.scalars(select(Approval).where(Approval.task_id == task.id))).one()
    await approvals.decide_approval(
        session, approval.id, approve=True, user_id=task.user_id, note=None
    )
    await session.commit()
    resumed = await queue.claim(session, "worker-tamper-2")
    assert resumed is not None
    await session.commit()
    provider = FakeProvider([])
    return await run_task(
        session,
        resumed,
        provider,
        build_registry(sandbox),
        policy,
        keys=keys,
        workspace=repo,
        resume=rebuild(await read_events(session, task.id)),
        holder=resumed.claimed_by,
        verifier=Verifier(sandbox, repo, exclude=never_readable(policy)),
        reviewer=ProviderReviewer(provider),
        publisher=build_publish_registry(sandbox),
    )


async def _assert_refused_for_a_changed_workspace(
    session: AsyncSession, task: Task, fake: FakeGitHub, result: RunResult
) -> None:
    assert result.status == "FAILED"
    assert "workspace changed after verification" in (result.reason or "")
    assert fake.requests == [] and fake.pulls == []  # nothing was sent, no PR exists
    events = await read_events(session, task.id)
    assert not [
        e for e in events if e.type == "tool.executed" and e.payload["id"].startswith("publish-")
    ]
    audit = await session.scalars(
        select(AuditLog).where(
            AuditLog.action == "publish.workspace_changed", AuditLog.target_id == str(task.id)
        )
    )
    assert audit.all()


async def test_a_file_rewritten_by_the_tests_after_the_diff_is_never_published(
    clean_sandbox: Sandbox,
    clean_repo: pathlib.Path,
    session: AsyncSession,
    keys: KeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real attack: the agent ships a test that rewrites `src/feature.py` when it runs. The
    diff was taken first, so the evidence (and the reviewer's verdict) describe `VALUE = 1`;
    the volume now holds `VALUE = 2`. The change is real, made by pytest inside the container,
    and the publication phase must notice it by comparing digests of a second export."""
    fake = FakeGitHub()
    _github_for(monkeypatch, fake)
    task = await _claimed_task(session)
    policy = load_policy(DEFAULT_POLICY)

    paused, _ = await _agent_run(
        session,
        keys,
        clean_sandbox,
        clean_repo,
        task,
        policy,
        [
            _step("write_file", path="src/feature.py", content=FEATURE),
            _step("write_file", path="tests/test_rewrites.py", content=REWRITER),
            _step("finish", summary="added a feature"),
        ],
    )
    assert paused.status == "WAITING_APPROVAL", paused.reason
    # Precondition: the tamper really happened in the volume, it is not simulated here.
    on_disk = await read_file(clean_sandbox, ReadFileArgs(path="src/feature.py"))
    assert on_disk == "VALUE = 2\n"

    result = await _resume_and_publish(session, keys, clean_sandbox, clean_repo, task, policy)

    await _assert_refused_for_a_changed_workspace(session, task, fake, result)


async def test_a_file_changed_while_the_task_waited_for_approval_is_never_published(
    clean_sandbox: Sandbox,
    clean_repo: pathlib.Path,
    session: AsyncSession,
    keys: KeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The volume outlives the pause. Rewrite a verified file in it while the task is parked:
    what the human approved is no longer what is on disk."""
    fake = FakeGitHub()
    _github_for(monkeypatch, fake)
    task = await _claimed_task(session)
    policy = load_policy(DEFAULT_POLICY)

    paused, _ = await _agent_run(
        session,
        keys,
        clean_sandbox,
        clean_repo,
        task,
        policy,
        [_step("write_file", path="src/notes.py", content=NOTES), _step("finish", summary="done")],
    )
    assert paused.status == "WAITING_APPROVAL", paused.reason

    await write_file(
        clean_sandbox,
        WriteFileArgs(path="src/notes.py", content='"""Swapped."""\n\nNOTE = "evil"\n'),
    )

    result = await _resume_and_publish(session, keys, clean_sandbox, clean_repo, task, policy)

    await _assert_refused_for_a_changed_workspace(session, task, fake, result)
