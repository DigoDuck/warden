"""The publication phase, through the loop (ADR-028): what happens after an approving verdict.

A scripted verifier stands in for the container and a scripted `github.open_pr` for GitHub, so
these run wherever Postgres runs. What is under test is the control plane's bookkeeping: that
the pull request is proposed by the control plane (never the model), only after the verdict,
decided by the real default policy, parked for a human, and that every branch (approve, reject,
deny, GitHub error, nothing to publish) ends the task the way the ADR says. The same phase
against a real container, a real GitHub-shaped server and a killed worker process lives in
tests/test_github_tool.py and tests/test_publish_resume.py.
"""

import pathlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_tools import FakeWorkspace
from warden.core import approvals, cancel, queue
from warden.core.events import read_events
from warden.core.loop import RunResult, run_task
from warden.core.replay import rebuild
from warden.identity.jwt import KeyPair
from warden.models import Approval, AuditLog, Task, ToolCall, User
from warden.policy.engine import load_policy
from warden.providers.base import Completion, Usage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.tools.github import OpenPrArgs, open_pr_paths
from warden.tools.registry import ToolContext, ToolError, ToolRegistry
from warden.verify.reviewer import Review, VerdictPayload
from warden.verify.runner import KINDS, compute_diff

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"
SPEC = "Fix the average\n\nIt crashes on an empty list."
SUMMARY = "fixed the average"


@pytest.fixture(autouse=True)
async def _clean_queue(session: AsyncSession) -> Any:
    """These tests claim tasks through the real queue, which hands out any QUEUED row, so a
    task another test left behind would be claimed instead of ours."""
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()


# --- doubles ---------------------------------------------------------------------------------


def _diff(after: Mapping[str, bytes], before: Mapping[str, bytes] | None = None) -> dict[str, Any]:
    """A diff payload with the exact shape the real verifier records, digests included."""
    return compute_diff(dict(before or {}), dict(after)).model_dump(mode="json")


APP_CHANGE = _diff({"src/app.py": b"print('fixed')\n"}, {"src/app.py": b"print('old')\n"})


class _Verifier:
    """Answers each kind with a canned payload and remembers what it was asked."""

    kinds: tuple[str, ...] = KINDS

    def __init__(self, diff: dict[str, Any] | None = None, **statuses: str) -> None:
        self._diff = diff if diff is not None else APP_CHANGE
        self._statuses = statuses
        self.asked: list[str] = []

    async def check(self, kind: str) -> dict[str, Any]:
        self.asked.append(kind)
        if kind == "diff":
            return self._diff
        return {"kind": kind, "status": self._statuses.get(kind, "passed")}


class _Reviewer:
    def __init__(self, *, passed: bool = True, findings: Sequence[str] = ()) -> None:
        self._passed = passed
        self._findings = list(findings)
        self.calls = 0
        self.on_review: Any = None

    async def review(self, spec: str, evidence: Mapping[str, Mapping[str, Any]]) -> Review:
        self.calls += 1
        if self.on_review is not None:
            await self.on_review()
        completion = Completion(
            provider="fake", model="fake-model", stop_reason="tool_use", usage=Usage()
        )
        return Review(
            completion, VerdictPayload(passed=self._passed, findings=self._findings), None
        )


class _OpenPr:
    """A stand-in `github.open_pr` that counts what actually executed."""

    def __init__(self) -> None:
        self.calls: list[OpenPrArgs] = []
        self.error: str | None = None
        self.in_transaction: list[bool] = []

    async def __call__(self, args: OpenPrArgs, context: ToolContext) -> str:
        self.in_transaction.append(context.session.in_transaction())
        if self.error is not None:
            raise ToolError(self.error)
        self.calls.append(args)
        return f"opened PR #{len(self.calls)}: https://example.test/pull/{len(self.calls)}"

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            name="github.open_pr",
            description="Open a pull request.",
            args_model=OpenPrArgs,
            execute=self,
            path_inspector=open_pr_paths,
            required_scope="github:pr:open",
            needs_identity=True,
        )
        return registry


def _step(name: str, **arguments: object) -> ScriptStep:
    return ScriptStep(
        tool_calls=[ProviderToolCall(id=f"call-{name}", name=name, arguments=arguments)]
    )


# --- helpers ---------------------------------------------------------------------------------


async def _claimed_task(session: AsyncSession, spec: str = SPEC) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    await queue.enqueue(session, user_id=user.id, spec=spec, idempotency_key=str(uuid.uuid4()))
    await session.commit()
    claimed = await queue.claim(session, "worker-a")
    assert claimed is not None
    return claimed


async def _run(
    session: AsyncSession,
    keys: KeyPair,
    tmp_path: pathlib.Path,
    *,
    verifier: _Verifier | None = None,
    reviewer: _Reviewer | None = None,
    publisher: _OpenPr | None = None,
    task: Task | None = None,
    steps: Sequence[ScriptStep] | None = None,
) -> tuple[Task, RunResult]:
    task = task or await _claimed_task(session)
    result = await run_task(
        session,
        task,
        FakeProvider(list(steps or [_step("finish", summary=SUMMARY)])),
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        keys=keys,
        workspace=tmp_path,
        holder=task.claimed_by,
        verifier=verifier or _Verifier(),
        reviewer=reviewer or _Reviewer(),
        publisher=publisher.registry() if publisher is not None else None,
    )
    return task, result


async def _decide(session: AsyncSession, task: Task, *, approve: bool, note: str | None) -> None:
    approval = (await session.scalars(select(Approval).where(Approval.task_id == task.id))).one()
    await approvals.decide_approval(
        session, approval.id, approve=approve, user_id=task.user_id, note=note
    )
    await session.commit()


async def _resume(
    session: AsyncSession,
    keys: KeyPair,
    tmp_path: pathlib.Path,
    task: Task,
    *,
    verifier: _Verifier,
    reviewer: _Reviewer,
    publisher: _OpenPr,
) -> RunResult:
    """What a worker does when it claims a decided task: replay, then run on. The provider is
    empty, so any model call from the resumed run raises: the agent's conversation is over."""
    resumed = await queue.claim(session, "worker-b")
    assert resumed is not None and resumed.id == task.id
    await session.commit()
    resume = rebuild(await read_events(session, task.id))
    return await run_task(
        session,
        resumed,
        FakeProvider([]),
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        keys=keys,
        workspace=tmp_path,
        resume=resume,
        holder=resumed.claimed_by,
        verifier=verifier,
        reviewer=reviewer,
        publisher=publisher.registry(),
    )


async def _event_types(session: AsyncSession, task: Task) -> list[str]:
    return [event.type for event in await read_events(session, task.id)]


async def _approvals(session: AsyncSession, task: Task) -> list[Approval]:
    return list(await session.scalars(select(Approval).where(Approval.task_id == task.id)))


# --- the happy path: proposed after the verdict, parked for a human, run once ---------------


async def test_an_approved_verdict_proposes_the_pull_request_and_waits_for_a_human(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    github = _OpenPr()
    task, result = await _run(session, keys, tmp_path, publisher=github, reviewer=_Reviewer())

    assert result.status == "WAITING_APPROVAL"
    assert task.status == "WAITING_APPROVAL"
    assert github.calls == []  # proposed, not executed

    kinds = await _event_types(session, task)
    # The order is the guarantee: verdict first, then the control plane's own proposal, then
    # the pause. Nothing here is the model asking for anything.
    start = kinds.index("verify.verdict")
    assert kinds[start:] == [
        "verify.verdict",
        "publish.requested",
        "policy.decided",
        "approval.requested",
    ]
    assert "task.finished" not in kinds

    [approval] = await _approvals(session, task)
    assert approval.tool_call_id == f"publish-{task.id}"
    assert approval.tool == "github.open_pr"
    assert approval.status == "pending"
    assert approval.args_safe["title"] == "Fix the average"
    assert approval.args_safe["paths"] == ["src/app.py"]
    assert approval.args_safe["branch_slug"] == "fix-the-average"

    # The report a reviewer reads in the pull request: the evidence, the verdict, the cost and
    # the agent's own words, labelled as the agent's.
    body = approval.args_safe["body"]
    assert "| lint | passed |" in body
    assert "| tests | passed |" in body
    assert "| diff | 1 file(s), +1 -1 |" in body
    assert "approved" in body
    assert "Cost" in body
    assert "generated by the agent" in body and SUMMARY in body


async def test_approving_runs_the_pull_request_exactly_once_and_succeeds(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    github, verifier, reviewer = _OpenPr(), _Verifier(), _Reviewer()
    task, paused = await _run(
        session, keys, tmp_path, publisher=github, verifier=verifier, reviewer=reviewer
    )
    assert paused.status == "WAITING_APPROVAL"
    task_id = task.id

    await _decide(session, task, approve=True, note=None)
    result = await _resume(
        session, keys, tmp_path, task, verifier=verifier, reviewer=reviewer, publisher=github
    )

    assert result.status == "SUCCEEDED"
    assert result.summary == SUMMARY
    assert len(github.calls) == 1
    # The tool ran with no transaction open: nothing holds the audit lock while GitHub answers.
    assert github.in_transaction == [False]
    assert reviewer.calls == 1  # the verdict was never bought again

    events = await read_events(session, task_id)
    [executed] = [
        e for e in events if e.type == "tool.executed" and e.payload["id"] == f"publish-{task_id}"
    ]
    assert executed.payload["ok"] is True and executed.payload["effect"] == "allow"
    assert executed.payload["output"].startswith("opened PR #1")
    assert [e.type for e in events].count("approval.requested") == 1  # asked once, ever

    [row] = await session.scalars(
        select(ToolCall).where(ToolCall.task_id == task_id, ToolCall.tool_name == "github.open_pr")
    )
    assert row.decision == "allow"
    assert events[-1].type == "task.finished" and events[-1].payload["status"] == "SUCCEEDED"


# --- a human says no -------------------------------------------------------------------------


async def test_rejecting_the_pull_request_cancels_the_task_with_the_note(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """There is no agent turn left to hand a rejection to, so unlike a rejected agent call
    (ADR-022) it ends the task: CANCELLED, with the reviewer's note on the record."""
    github, verifier, reviewer = _OpenPr(), _Verifier(), _Reviewer()
    task, _ = await _run(
        session, keys, tmp_path, publisher=github, verifier=verifier, reviewer=reviewer
    )
    task_id = task.id
    await _decide(session, task, approve=False, note="not this one")

    result = await _resume(
        session, keys, tmp_path, task, verifier=verifier, reviewer=reviewer, publisher=github
    )

    assert result.status == "CANCELLED"
    assert "not this one" in (result.reason or "")
    assert github.calls == []

    finished = (await read_events(session, task_id))[-1]
    assert finished.type == "task.finished"
    assert finished.payload["status"] == "CANCELLED"
    assert "not this one" in finished.payload["reason"]

    [row] = await session.scalars(
        select(ToolCall).where(ToolCall.task_id == task_id, ToolCall.tool_name == "github.open_pr")
    )
    assert row.decision == "rejected"

    audit = list(
        await session.scalars(
            select(AuditLog).where(AuditLog.target_type.in_(["approval", "task"]))
        )
    )
    actions = {(a.action, a.details.get("note") or a.details.get("reason")) for a in audit}
    assert ("approval.rejected", "not this one") in actions


# --- the policy and GitHub still have a say --------------------------------------------------


async def test_a_denied_path_fails_the_task_without_asking_anyone(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The agent's own result touched a path the policy forbids (`never-read-secrets`). Nothing
    is proposed to a human and nothing is sent."""
    github = _OpenPr()
    diff = _diff({"src/app.py": b"x = 1\n", "config/.env": b"A=1\n"})
    task, result = await _run(session, keys, tmp_path, publisher=github, verifier=_Verifier(diff))

    assert result.status == "FAILED"
    assert "refused" in (result.reason or "")
    assert github.calls == []
    assert await _approvals(session, task) == []
    denies = list(
        await session.scalars(
            select(AuditLog).where(
                AuditLog.action == "policy.deny", AuditLog.target_id == str(task.id)
            )
        )
    )
    assert denies and "never-read-secrets" in denies[0].details["matched_rules"]


async def test_a_github_error_fails_the_task_with_its_message(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    github, verifier, reviewer = _OpenPr(), _Verifier(), _Reviewer()
    task, _ = await _run(
        session, keys, tmp_path, publisher=github, verifier=verifier, reviewer=reviewer
    )
    task_id = task.id
    await _decide(session, task, approve=True, note=None)
    github.error = "github.open_pr: could not reach GitHub: connection refused"

    result = await _resume(
        session, keys, tmp_path, task, verifier=verifier, reviewer=reviewer, publisher=github
    )

    assert result.status == "FAILED"
    assert "could not reach GitHub" in (result.reason or "")
    events = await read_events(session, task_id)
    [executed] = [e for e in events if e.type == "tool.executed"]
    assert executed.payload["is_error"] is True and executed.payload["ok"] is False
    # No retry here: backoff and retry policy are week 8's, a failed publication is a failure.
    assert github.in_transaction == [False]


# --- nothing is proposed -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "verifier",
    [
        _Verifier(tests="failed"),
        _Verifier(lint="failed"),
        _Verifier(types="error"),
    ],
    ids=["red-tests", "red-lint", "types-error"],
)
async def test_red_evidence_fails_the_task_and_nothing_is_proposed(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path, verifier: _Verifier
) -> None:
    github = _OpenPr()
    task, result = await _run(session, keys, tmp_path, publisher=github, verifier=verifier)

    assert result.status == "FAILED"
    assert github.calls == []
    assert await _approvals(session, task) == []
    assert "publish.requested" not in await _event_types(session, task)


async def test_a_rejecting_verdict_fails_the_task_and_nothing_is_proposed(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    github = _OpenPr()
    task, result = await _run(
        session, keys, tmp_path, publisher=github, reviewer=_Reviewer(passed=False, findings=["no"])
    )

    assert result.status == "FAILED"
    assert github.calls == [] and await _approvals(session, task) == []
    assert "publish.requested" not in await _event_types(session, task)


async def test_an_empty_diff_succeeds_without_a_publication_phase(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    github = _OpenPr()
    task, result = await _run(
        session, keys, tmp_path, publisher=github, verifier=_Verifier(_diff({}))
    )

    assert result.status == "SUCCEEDED"
    assert github.calls == [] and await _approvals(session, task) == []
    kinds = await _event_types(session, task)
    assert "publish.requested" not in kinds and "publish.skipped" not in kinds


async def test_without_github_configured_there_is_no_publication_phase(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task, result = await _run(session, keys, tmp_path, publisher=None)

    assert result.status == "SUCCEEDED"
    assert await _approvals(session, task) == []
    assert "publish.requested" not in await _event_types(session, task)


@pytest.mark.parametrize(
    ("diff", "reason"),
    [
        (_diff({}, {"src/gone.py": b"x = 1\n"}), "src/gone.py"),
        (_diff({"assets/logo.bin": b"\0\1\2"}), "assets/logo.bin"),
        ({"kind": "diff", "status": "error", "error": "export failed"}, "unavailable"),
    ],
    ids=["removed-file", "binary-file", "diff-unavailable"],
)
async def test_what_open_pr_cannot_publish_is_skipped_with_the_reason(
    session: AsyncSession,
    keys: KeyPair,
    tmp_path: pathlib.Path,
    diff: dict[str, Any],
    reason: str,
) -> None:
    """`open_pr` only creates UTF-8 blobs. A change it cannot carry in full is not published in
    part: the task still succeeds (the work is verified), says why there is no PR, and asks no
    one to approve something that was never going to be sent."""
    github = _OpenPr()
    task, result = await _run(session, keys, tmp_path, publisher=github, verifier=_Verifier(diff))

    assert result.status == "SUCCEEDED"
    assert reason in (result.reason or "")
    assert github.calls == [] and await _approvals(session, task) == []
    events = await read_events(session, task.id)
    [skipped] = [e for e in events if e.type == "publish.skipped"]
    assert reason in skipped.payload["reason"]
    assert events[-1].type == "task.finished" and events[-1].payload["status"] == "SUCCEEDED"


# --- what the model can and cannot do ----------------------------------------------------------


async def test_the_model_cannot_open_a_pull_request_itself(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The agent's registry has no `github.open_pr`, so naming it is refused like any unknown
    tool. The only pull request that gets proposed is the control plane's, after the verdict."""
    github = _OpenPr()
    task, result = await _run(
        session,
        keys,
        tmp_path,
        publisher=github,
        steps=[
            _step(
                "github.open_pr",
                title="sneaky",
                body="b",
                branch_slug="sneaky",
                paths=["src/app.py"],
            ),
            _step("finish", summary=SUMMARY),
        ],
    )

    assert github.calls == []
    [agent_call] = await session.scalars(
        select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "github.open_pr")
    )
    assert agent_call.decision == "deny"
    # The run then reached the verdict and the control plane proposed its own.
    assert result.status == "WAITING_APPROVAL"
    [approval] = await _approvals(session, task)
    assert approval.tool_call_id == f"publish-{task.id}"


async def test_a_publisher_without_a_verifier_is_refused(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    with pytest.raises(ValueError, match="verifier"):
        await run_task(
            session,
            task,
            FakeProvider([]),
            FakeWorkspace().registry(),
            load_policy(DEFAULT_POLICY),
            keys=keys,
            workspace=tmp_path,
            publisher=_OpenPr().registry(),
        )


async def test_a_cancel_before_the_proposal_is_decided_stops_the_task(
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """Cancellation is the one budget that still applies here (max_seconds and max_usd are the
    agent's): a cancel that lands during the review wins over the publication."""
    task = await _claimed_task(session)
    task_id = task.id
    reviewer = _Reviewer()

    async def cancel_during_review() -> None:
        async with session_factory() as other:
            await cancel.request_cancel(other, task_id)
            await other.commit()

    reviewer.on_review = cancel_during_review
    github = _OpenPr()
    task, result = await _run(
        session, keys, tmp_path, task=task, publisher=github, reviewer=reviewer
    )

    assert result.status == "CANCELLED"
    assert github.calls == []
    assert await _approvals(session, task) == []
