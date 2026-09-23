"""The loop, end to end, against a scripted provider and a temporary workspace.

Nothing here touches the network. This is the shape every later test of the control plane
takes: the FakeProvider decides what the model "wanted", and the assertions are about what
the control plane did with it.
"""

import pathlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_tools import FakeWorkspace
from warden.core import approvals, cancel, queue
from warden.core.events import read_events
from warden.core.loop import Budget, run_task
from warden.core.replay import ResumeState, rebuild
from warden.models import Approval, AuditLog, ModelCall, PolicyDecision, Task, ToolCall, User
from warden.policy.engine import Effect, Policy, Rule, load_policy
from warden.providers.base import (
    Completion,
    Message,
    ToolResultsMessage,
    Usage,
    UserMessage,
)
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.tools.registry import ToolRegistry

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"


def _allow_all() -> Policy:
    """Used where the test is about the loop, not about the rules."""
    return Policy(
        [Rule(id="allow-all", effect=Effect.ALLOW, when={"tool": "*"})],
        default=Effect.DENY,
        policy_hash="test",
    )


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=nope\n", encoding="utf-8")
    return root


async def _a_task(session: AsyncSession, spec: str = "summarise the repo") -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(user)
    await session.flush()
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec=spec)
    session.add(task)
    await session.flush()
    return task


def _step(tool: str, **args: object) -> ScriptStep:
    return ScriptStep(
        tool_calls=[ProviderToolCall(id=f"call-{uuid.uuid4()}", name=tool, arguments=args)]
    )


class _CostlyProvider:
    """A provider that reports a fixed, expensive usage. Used to drive the budget path."""

    name = "costly"

    def __init__(self, model: str = "claude-opus-5") -> None:
        self._model = model
        self.calls = 0

    async def generate(self, messages: object, **kwargs: object) -> Completion:
        self.calls += 1
        return Completion(
            provider=self.name,
            model=self._model,
            stop_reason="tool_use",
            tool_calls=[ProviderToolCall(id=f"c{self.calls}", name="list_files", arguments={})],
            usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
            raw_content=[],
        )


async def test_successful_run_records_everything(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _a_task(session)
    provider = FakeProvider(
        [
            _step("list_files", pattern="**/*.py"),
            _step("read_file", path="src/app.py"),
            _step("finish", summary="one module that prints hello"),
        ]
    )

    result = await run_task(
        session, task, provider, FakeWorkspace().registry(), _allow_all(), workspace=workspace
    )

    assert result.status == "SUCCEEDED"
    assert result.summary == "one module that prints hello"
    assert task.status == "SUCCEEDED"
    assert task.finished_at is not None

    kinds = [event.type for event in await read_events(session, task.id)]
    assert kinds[0] == "task.created"
    assert kinds[-1] == "task.finished"
    assert kinds.count("tool.executed") == 2  # finish is not dispatched to the registry

    tool_rows = await session.scalar(
        select(func.count()).select_from(ToolCall).where(ToolCall.task_id == task.id)
    )
    assert tool_rows == 3  # two reads plus the finish call

    model_rows = await session.scalar(
        select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task.id)
    )
    assert model_rows == 3


async def test_a_refused_tool_does_not_kill_the_task(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """A denied call is a normal event in an agent loop: the model sees it and carries on."""
    task = await _a_task(session)
    provider = FakeProvider(
        [
            _step("read_file", path="../.env"),
            _step("finish", summary="could not read that"),
        ]
    )

    result = await run_task(
        session, task, provider, FakeWorkspace().registry(), _allow_all(), workspace=workspace
    )

    assert result.status == "SUCCEEDED"
    failed = await session.scalars(
        select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.error.is_not(None))
    )
    errors = [row.error for row in failed if row.error is not None]
    assert len(errors) == 1
    assert "outside the workspace" in errors[0]


async def test_a_run_that_never_finishes_stops_at_max_iterations(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _a_task(session)
    provider = FakeProvider([_step("list_files") for _ in range(20)])

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _allow_all(),
        workspace=workspace,
        budget=Budget(max_iterations=3),
    )

    assert result.status == "TIMED_OUT"
    assert result.iterations == 3
    assert task.status == "TIMED_OUT"


async def test_spending_over_the_ceiling_stops_the_run(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """The ceiling has to bite before the next model call, or it is not a ceiling."""
    task = await _a_task(session)
    provider = _CostlyProvider()

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _allow_all(),
        workspace=workspace,
        budget=Budget(max_iterations=10, max_usd=Decimal("1.00")),
    )

    assert result.status == "BUDGET_EXCEEDED"
    assert provider.calls == 1
    assert result.cost_usd > Decimal("1.00")


async def test_scripted_run_costs_nothing(session: AsyncSession, workspace: pathlib.Path) -> None:
    task = await _a_task(session)
    provider = FakeProvider([_step("finish", summary="done")])

    result = await run_task(
        session, task, provider, FakeWorkspace().registry(), _allow_all(), workspace=workspace
    )

    assert result.cost_usd == Decimal("0")
    # Compared as Decimal, not as text: the stored string keeps the six-decimal quantum of
    # the Numeric(12, 6) column, so "0.000000" is the right value and "0" would not be.
    assert Decimal(task.spent["usd"]) == Decimal("0")


# --- the policy engine, from the loop's side ------------------------------------------------


async def test_policy_denies_a_readable_secret_and_the_task_carries_on(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """The decisive test: the file exists and is readable, and only the policy stops it.

    Putting a real .env inside the workspace removes the tool's containment from the picture,
    so what is proven here is the policy doing the work, not the path guard.
    """
    (workspace / ".env").write_text("SECRET=must-never-be-read\n", encoding="utf-8", newline="\n")
    task = await _a_task(session)
    provider = FakeProvider(
        [
            _step("read_file", path=".env"),
            _step("finish", summary="the secret was refused"),
        ]
    )

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    assert result.status == "SUCCEEDED"

    rows = list(
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "read_file")
        )
    )
    assert len(rows) == 1
    assert rows[0].decision == "deny"
    # The contents never reached the model, and never reached the database either.
    assert "must-never-be-read" not in (rows[0].result_summary or "")


async def test_a_denied_call_is_recorded_with_the_rule_that_decided(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    (workspace / ".env").write_text("SECRET=x\n", encoding="utf-8", newline="\n")
    task = await _a_task(session)
    provider = FakeProvider([_step("read_file", path=".env"), _step("finish", summary="done")])

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    decisions = list(
        await session.scalars(
            select(PolicyDecision)
            .join(ToolCall, PolicyDecision.tool_call_id == ToolCall.id)
            .where(ToolCall.task_id == task.id, ToolCall.tool_name == "read_file")
        )
    )
    assert len(decisions) == 1
    assert decisions[0].effect == "deny"
    assert "never-read-secrets" in decisions[0].matched_rules
    assert len(decisions[0].policy_hash) == 64


async def test_the_refusal_reaches_the_model_and_names_the_rule(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """A refusal the model cannot understand is a refusal it retries, burning iterations."""
    (workspace / ".env").write_text("SECRET=x\n", encoding="utf-8", newline="\n")
    task = await _a_task(session)
    provider = FakeProvider([_step("read_file", path=".env"), _step("finish", summary="done")])

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = (
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "read_file")
        )
    ).one()
    assert "Refused by policy" in (row.error or "")
    assert "never-read-secrets" in (row.error or "")


async def test_an_allowed_call_still_runs(session: AsyncSession, workspace: pathlib.Path) -> None:
    """The default policy must not be so tight that the agent cannot do its job."""
    task = await _a_task(session)
    provider = FakeProvider(
        [_step("read_file", path="src/app.py"), _step("finish", summary="read it")]
    )

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    assert result.status == "SUCCEEDED"
    row = (
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "read_file")
        )
    ).one()
    assert row.decision == "allow"
    assert row.error is None


async def _claimed_task(session: AsyncSession) -> Task:
    """A task actually claimed by a worker, so pausing it can meaningfully assert the lease
    was released (`_a_task` above builds a bare, unclaimed Task, which has no lease to
    release in the first place)."""
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    await queue.enqueue(
        session, user_id=user.id, spec="open a pull request", idempotency_key=str(uuid.uuid4())
    )
    await session.commit()
    claimed = await queue.claim(session, "worker-a")
    assert claimed is not None
    await session.commit()
    return claimed


def _require_approval_policy(tool: str = "github.open_pr") -> Policy:
    return Policy(
        [
            Rule(id="allow-read", effect=Effect.ALLOW, when={"tool": "read_file"}),
            Rule(
                id="needs-human",
                effect=Effect.REQUIRE_APPROVAL,
                reason="opening a pull request is visible outside the control plane",
                scopes=["github:pr:open"],
                when={"tool": tool},
            ),
        ],
        default=Effect.DENY,
        policy_hash="test",
    )


async def test_require_approval_pauses_the_task_and_releases_the_lease(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """ADR-022: a REQUIRE_APPROVAL decision used to degrade to a refusal (week 2). Now it
    parks the task for a human instead, with the lease released so a worker can pick up
    something else while it waits.
    """
    task = await _claimed_task(session)
    holder = task.claimed_by
    provider = FakeProvider([_step("github.open_pr", title="x")])

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _require_approval_policy(),
        workspace=workspace,
        holder=holder,
    )

    assert result.status == "WAITING_APPROVAL"
    assert task.status == "WAITING_APPROVAL"
    assert task.claimed_by is None
    assert task.claimed_until is None

    rows = list(await session.scalars(select(Approval).where(Approval.task_id == task.id)))
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].tool == "github.open_pr"
    assert rows[0].matched_rules == ["needs-human"]

    kinds = [event.type for event in await read_events(session, task.id)]
    assert "approval.requested" in kinds
    assert "tool.executed" not in kinds
    # WAITING_APPROVAL is not terminal (worker.py's TERMINAL_STATUSES excludes it): no
    # task.finished, and finished_at stays unset.
    assert "task.finished" not in kinds
    assert task.finished_at is None


async def test_calls_before_the_paused_one_still_ran_and_calls_after_stay_pending(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    provider = FakeProvider(
        [
            ScriptStep(
                tool_calls=[
                    ProviderToolCall(
                        id="call-read", name="read_file", arguments={"path": "src/app.py"}
                    ),
                    ProviderToolCall(id="call-pr", name="github.open_pr", arguments={"title": "x"}),
                    ProviderToolCall(id="call-list", name="list_files", arguments={}),
                ]
            )
        ]
    )

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _require_approval_policy(),
        workspace=workspace,
        holder=task.claimed_by,
    )

    assert result.status == "WAITING_APPROVAL"
    events_by_type: dict[str, list[str]] = {}
    for event in await read_events(session, task.id):
        events_by_type.setdefault(event.type, []).append(str(event.payload.get("tool")))

    # requested: all three, in one batch, before any of them ran (ADR-019 checkpoint b).
    assert events_by_type["tool.requested"] == ["read_file", "github.open_pr", "list_files"]
    # decided: read_file (allowed) and github.open_pr (paused); list_files never even got a
    # policy decision, because the loop stopped at the call before it.
    assert events_by_type["policy.decided"] == ["read_file", "github.open_pr"]
    # executed: only the call before the pause.
    assert events_by_type["tool.executed"] == ["read_file"]
    assert events_by_type["approval.requested"] == ["github.open_pr"]


# --- audit log entries the loop itself writes (ADR-022 / week 3 list) -----------------------


async def test_a_policy_deny_writes_an_audit_entry(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    (workspace / ".env").write_text("SECRET=x\n", encoding="utf-8", newline="\n")
    task = await _a_task(session)
    provider = FakeProvider([_step("read_file", path=".env"), _step("finish", summary="done")])

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    rows = list(
        await session.scalars(
            select(AuditLog).where(
                AuditLog.action == "policy.deny", AuditLog.target_id == str(task.id)
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].details["tool"] == "read_file"
    assert "never-read-secrets" in rows[0].details["matched_rules"]


async def test_an_approval_request_writes_an_audit_entry(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    provider = FakeProvider([_step("github.open_pr", title="x")])

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _require_approval_policy(),
        workspace=workspace,
        holder=task.claimed_by,
    )

    approval = (await session.scalars(select(Approval).where(Approval.task_id == task.id))).one()
    rows = list(
        await session.scalars(
            select(AuditLog).where(
                AuditLog.action == "approval.requested",
                AuditLog.target_id == str(approval.id),
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].details["tool"] == "github.open_pr"


async def test_a_finished_task_writes_an_audit_entry(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _a_task(session)
    provider = FakeProvider([_step("finish", summary="done")])

    await run_task(
        session, task, provider, FakeWorkspace().registry(), _allow_all(), workspace=workspace
    )

    rows = list(
        await session.scalars(
            select(AuditLog).where(
                AuditLog.action == "task.finished", AuditLog.target_id == str(task.id)
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].details["status"] == "SUCCEEDED"


# --- resuming after a human decides (ADR-022) -----------------------------------------------


class _OpenPrArgs(BaseModel):
    title: str


class _OpenPrCounter:
    """A fake `github.open_pr` with an execution count, the same technique
    `tests/fake_tools.py::FakeWorkspace` uses: the `tool_calls` row proves no duplicate was
    *recorded*, this proves the side effect itself did not happen twice.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def open_pr(self, args: _OpenPrArgs) -> str:
        self.calls += 1
        return f"opened PR: {args.title}"


class _RecordingProvider:
    """Wraps a FakeProvider and keeps every message list it was asked to continue, so a test
    can assert on what the model actually received rather than on a side table."""

    name = "fake"

    def __init__(self, inner: FakeProvider) -> None:
        self._inner = inner
        self.seen: list[list[Message]] = []

    async def generate(self, messages: Sequence[Message], **kwargs: Any) -> Completion:
        self.seen.append(list(messages))
        return await self._inner.generate(messages, **kwargs)


def _registry_with_open_pr(counter: _OpenPrCounter) -> ToolRegistry:
    registry = FakeWorkspace().registry()
    registry.register(
        name="github.open_pr",
        description="Open a pull request.",
        args_model=_OpenPrArgs,
        execute=counter.open_pr,
    )
    return registry


async def _pending_approval(session: AsyncSession, task_id: object) -> Approval:
    return (await session.scalars(select(Approval).where(Approval.task_id == task_id))).one()


async def test_approving_a_paused_call_resumes_and_executes_it_exactly_once(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    counter = _OpenPrCounter()
    registry = _registry_with_open_pr(counter)
    paused = await run_task(
        session,
        task,
        FakeProvider([_step("github.open_pr", title="x")]),
        registry,
        _require_approval_policy(),
        workspace=workspace,
        holder=task.claimed_by,
    )
    assert paused.status == "WAITING_APPROVAL"
    assert counter.calls == 0  # paused, not executed

    approval = await _pending_approval(session, task.id)
    approval_id, tool_call_id = approval.id, approval.tool_call_id  # before expire_all() runs
    decided = await approvals.decide_approval(
        session, approval_id, approve=True, user_id=task.user_id, note=None
    )
    await session.commit()
    assert decided.status == "approved"

    resumed = await queue.claim(session, "worker-b")
    assert resumed is not None and resumed.id == task.id
    await session.commit()
    resume = rebuild(await read_events(session, task.id))
    assert resume.approval_decisions[tool_call_id].status == "approved"

    result = await run_task(
        session,
        resumed,
        FakeProvider([_step("finish", summary="pr opened")]),
        registry,
        _require_approval_policy(),
        workspace=workspace,
        resume=resume,
        holder=resumed.claimed_by,
    )

    assert result.status == "SUCCEEDED"
    assert counter.calls == 1  # the approved call ran exactly once, never re-executed

    pr_calls = list(
        await session.scalars(
            select(ToolCall).where(
                ToolCall.task_id == task.id, ToolCall.tool_name == "github.open_pr"
            )
        )
    )
    assert len(pr_calls) == 1
    assert pr_calls[0].decision == "allow"

    policy_decision = (
        await session.scalars(
            select(PolicyDecision).where(PolicyDecision.tool_call_id == pr_calls[0].id)
        )
    ).one()
    assert str(approval_id) in policy_decision.reason


async def test_an_approval_never_overrides_a_deny_added_after_the_request(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """ADR-003's ordering still holds after ADR-022: the most restrictive effect wins, and an
    approval is not a way around a deny the policy grows later."""
    task = await _claimed_task(session)
    paused = await run_task(
        session,
        task,
        FakeProvider([_step("github.open_pr", title="x")]),
        FakeWorkspace().registry(),
        _require_approval_policy(),
        workspace=workspace,
        holder=task.claimed_by,
    )
    assert paused.status == "WAITING_APPROVAL"

    approval = await _pending_approval(session, task.id)
    await approvals.decide_approval(
        session, approval.id, approve=True, user_id=task.user_id, note=None
    )
    await session.commit()

    resumed = await queue.claim(session, "worker-b")
    assert resumed is not None
    await session.commit()
    resume = rebuild(await read_events(session, task.id))

    # An incident closed this off after the approval was requested: a plain DENY now.
    stricter_policy = Policy(
        [
            Rule(
                id="now-denied",
                effect=Effect.DENY,
                reason="a new incident closed this off",
                when={"tool": "github.open_pr"},
            )
        ],
        default=Effect.DENY,
        policy_hash="test-stricter",
    )

    result = await run_task(
        session,
        resumed,
        FakeProvider([_step("finish", summary="blocked")]),
        FakeWorkspace().registry(),
        stricter_policy,
        workspace=workspace,
        resume=resume,
        holder=resumed.claimed_by,
    )

    assert result.status == "SUCCEEDED"
    row = (
        await session.scalars(
            select(ToolCall).where(
                ToolCall.task_id == task.id, ToolCall.tool_name == "github.open_pr"
            )
        )
    ).one()
    assert row.decision == "deny"
    assert "now-denied" in (row.error or "")


async def test_rejecting_a_paused_call_injects_the_note_and_the_loop_continues(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    provider = FakeProvider(
        [
            ScriptStep(
                tool_calls=[
                    ProviderToolCall(
                        id="call-read", name="read_file", arguments={"path": "src/app.py"}
                    ),
                    ProviderToolCall(id="call-pr", name="github.open_pr", arguments={"title": "x"}),
                    ProviderToolCall(id="call-list", name="list_files", arguments={}),
                ]
            )
        ]
    )
    paused = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _require_approval_policy(),
        workspace=workspace,
        holder=task.claimed_by,
    )
    assert paused.status == "WAITING_APPROVAL"

    approval = await _pending_approval(session, task.id)
    await approvals.decide_approval(
        session, approval.id, approve=False, user_id=task.user_id, note="too risky right now"
    )
    await session.commit()

    resumed = await queue.claim(session, "worker-b")
    assert resumed is not None
    await session.commit()
    resume = rebuild(await read_events(session, task.id))

    recording = _RecordingProvider(FakeProvider([_step("finish", summary="done")]))
    result = await run_task(
        session,
        resumed,
        recording,
        FakeWorkspace().registry(),
        _require_approval_policy(),
        workspace=workspace,
        resume=resume,
        holder=resumed.claimed_by,
    )

    assert result.status == "SUCCEEDED"

    # What the model itself was sent on the resumed turn, not just what the tool_calls row
    # says: the whole point of rejecting (ADR-022) is that the model reads the note and
    # takes another path, so the tool_result for the rejected call must be an error that
    # carries it.
    sent = recording.seen[0][-1]
    assert isinstance(sent, ToolResultsMessage)
    by_id = {r.tool_call_id: r for r in sent.results}
    assert by_id["call-pr"].is_error is True
    assert "too risky right now" in by_id["call-pr"].content

    pr_row = (
        await session.scalars(
            select(ToolCall).where(
                ToolCall.task_id == task.id, ToolCall.tool_name == "github.open_pr"
            )
        )
    ).one()
    assert pr_row.decision == "rejected"
    assert pr_row.error is not None and "too risky right now" in pr_row.error

    # The other pending call (list_files) is decided normally: no rule allows it, so the
    # default deny applies, exactly as it would for a call the model had just made.
    list_row = (
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "list_files")
        )
    ).one()
    assert list_row.decision == "deny"

    kinds = [event.type for event in await read_events(session, task.id)]
    # read_file (before the pause) + github.open_pr (rejected, resumed) + list_files
    # (decided normally, resumed).
    assert kinds.count("tool.executed") == 3


async def test_a_path_escaping_the_workspace_is_denied_by_policy_too(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """Belt and braces: the tool would refuse it, and the policy never sees a matchable path."""
    task = await _a_task(session)
    provider = FakeProvider([_step("read_file", path="../.env"), _step("finish", summary="no")])

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = (
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "read_file")
        )
    ).one()
    assert row.decision == "deny"


async def test_every_tool_call_leaves_a_policy_event(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """The audit trail has to show a decision for each call, allowed or not."""
    task = await _a_task(session)
    provider = FakeProvider(
        [
            _step("list_files", pattern="**/*.py"),
            _step("read_file", path="src/app.py"),
            _step("finish", summary="done"),
        ]
    )

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    kinds = [event.type for event in await read_events(session, task.id)]
    assert kinds.count("policy.decided") == 2


async def test_finish_leaves_no_tool_requested_or_tool_executed_event(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """ADR-019: `finish` deliberately gets no `tool.requested`/`tool.executed` event.

    Replay derives "still pending" as requested-minus-executed, and `finish` is never
    registered in the tool registry: a `tool.requested` for it, with no `tool.executed` to
    cancel it out, would make a resumed run try to dispatch a tool that does not exist. The
    `tool_calls` row it still gets, for the audit trail, is a separate table this test does
    not need to touch.
    """
    task = await _a_task(session)
    provider = FakeProvider([_step("finish", summary="done")])

    result = await run_task(
        session, task, provider, FakeWorkspace().registry(), _allow_all(), workspace=workspace
    )

    assert result.status == "SUCCEEDED"
    kinds = [event.type for event in await read_events(session, task.id)]
    assert "tool.requested" not in kinds
    assert "tool.executed" not in kinds


# --- cancellation and the max_seconds deadline (briefing §16) ------------------------------


async def test_a_cancel_requested_before_the_run_starts_stops_before_any_model_call(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """The check at the top of iteration 1 has to run before `provider.generate`, not after:
    an empty `FakeProvider` blows up the instant anything calls it, so this only stays green
    if the model is never asked for a turn at all.
    """
    task = await _a_task(session)
    # `ck_tasks_cancel_requested_only_after_claim` requires status != QUEUED for this
    # column to be set, so this stands in for "already claimed and running" rather than
    # going through a real `queue.claim()`, which the loop itself does not need here.
    task.status = "RUNNING"
    task.cancel_requested_at = datetime.now(UTC)
    await session.flush()

    result = await run_task(
        session,
        task,
        FakeProvider([]),
        FakeWorkspace().registry(),
        _allow_all(),
        workspace=workspace,
    )

    assert result.status == "CANCELLED"
    assert task.status == "CANCELLED"
    kinds = [event.type for event in await read_events(session, task.id)]
    assert "model.called" not in kinds
    assert kinds[-1] == "task.finished"


async def test_a_cancel_requested_mid_turn_stops_before_the_next_tool_runs(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """Cancellation arriving *between* the model call and the tool call it asked for:
    proven with a real second session, the same way `request_cancel` would really be called
    from an API request or a worker's cancel watcher, not by poking the ORM object in
    process. `read_file` must be requested (the turn really happened) but never executed.
    """
    task = await _a_task(session)

    class _CancelsAfterGenerating:
        name = "fake"

        def __init__(self, inner: FakeProvider) -> None:
            self._inner = inner

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            completion = await self._inner.generate(*args, **kwargs)  # type: ignore[arg-type]
            async with session_factory() as other:
                outcome = await cancel.request_cancel(other, task.id)
                await other.commit()
            assert outcome is cancel.CancelOutcome.MARKED
            return completion

    provider = _CancelsAfterGenerating(
        FakeProvider([_step("read_file", path="src/app.py"), _step("finish", summary="done")])
    )

    result = await run_task(
        session, task, provider, FakeWorkspace().registry(), _allow_all(), workspace=workspace
    )

    assert result.status == "CANCELLED"
    kinds = [event.type for event in await read_events(session, task.id)]
    assert kinds.count("model.called") == 1
    assert "tool.requested" in kinds  # the batch was requested...
    assert "tool.executed" not in kinds  # ...but the check ran before it executed
    assert "policy.decided" not in kinds


async def test_a_run_past_its_max_seconds_deadline_stops_before_the_next_tool(
    session: AsyncSession, workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wall clock, not sleep: `_now` is swapped for a clock this test drives by hand, so the
    deadline trips deterministically instead of racing a real clock in CI.
    """
    task = await _a_task(session)
    clock = {"t": datetime(2026, 1, 1, tzinfo=UTC)}
    monkeypatch.setattr("warden.core.loop._now", lambda: clock["t"])

    class _SlowProvider:
        name = "fake"

        def __init__(self, inner: FakeProvider) -> None:
            self._inner = inner

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            # Each model call "takes" 20 seconds of wall clock, whatever else happens.
            clock["t"] += timedelta(seconds=20)
            return await self._inner.generate(*args, **kwargs)  # type: ignore[arg-type]

    provider = _SlowProvider(
        FakeProvider([_step("list_files"), _step("list_files")])  # a second call must never run
    )

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _allow_all(),
        workspace=workspace,
        budget=Budget(max_iterations=10, max_seconds=15),
    )

    assert result.status == "TIMED_OUT"
    assert "max_seconds" in (result.reason or "")
    kinds = [event.type for event in await read_events(session, task.id)]
    # One model call happened (20s already exceeds the 15s budget), and the deadline caught
    # it before the tool it asked for ran, and long before a second call was ever attempted.
    assert kinds.count("model.called") == 1
    assert "tool.executed" not in kinds


async def test_max_iterations_still_ends_timed_out_when_no_deadline_is_set(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """Regression guard: adding max_seconds must not change the existing, deadline-less
    max_iterations behaviour that `test_a_run_that_never_finishes_stops_at_max_iterations`
    already covers, so this only pins the parts that test does not: no `max_seconds` set at
    all, and `_check_stoppable` running every iteration without ever raising.
    """
    task = await _a_task(session)
    provider = FakeProvider([_step("list_files") for _ in range(5)])

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _allow_all(),
        workspace=workspace,
        budget=Budget(max_iterations=2),
    )

    assert result.status == "TIMED_OUT"
    assert "max_iterations" in (result.reason or "")


async def test_a_resumed_run_keeps_the_original_clock_for_max_seconds(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """The deadline is measured from the persisted `task.started_at`, so a task that crashed
    and was reclaimed an hour later is already out of time: the resume path must not stamp
    a fresh start. An empty `FakeProvider` blows up if the model is asked for a turn.
    """
    task = await _a_task(session)
    task.status = "RUNNING"
    task.started_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()
    resume = ResumeState(messages=[UserMessage(text=task.spec)], next_iteration=2)

    result = await run_task(
        session,
        task,
        FakeProvider([]),
        FakeWorkspace().registry(),
        _allow_all(),
        workspace=workspace,
        budget=Budget(max_iterations=10, max_seconds=60),
        resume=resume,
    )

    assert result.status == "TIMED_OUT"
    assert "max_seconds" in (result.reason or "")
    kinds = [event.type for event in await read_events(session, task.id)]
    assert kinds == ["task.finished"]


async def test_a_cancel_landing_while_the_call_is_being_decided_wins_over_the_pause(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """The loop checks for a cancel before deciding a call, but a cancel can still commit
    after that check and before the pause does (deciding can take a sandbox round trip for
    `apply_patch`). While the task is still RUNNING, `request_cancel` only sets the marker
    and answers MARKED, promising the loop will stop. If the loop then parks the task
    WAITING_APPROVAL anyway, that promise is broken: nothing is running to see the marker,
    and a later approve/reject would try to put a task with a cancel marker back in QUEUED,
    which `ck_tasks_cancel_requested_only_after_claim` refuses (a 500 on every decision).
    """
    task = await _claimed_task(session)
    task_id = task.id
    registry = FakeWorkspace().registry()
    real_touched_paths = registry.touched_paths

    async def cancel_while_deciding(name: str, arguments: dict[str, Any]) -> list[str | None]:
        if name == "github.open_pr":
            async with session_factory() as other:
                outcome = await cancel.request_cancel(other, task_id)
                await other.commit()
            assert outcome is cancel.CancelOutcome.MARKED
        return await real_touched_paths(name, arguments)

    registry.touched_paths = cancel_while_deciding  # type: ignore[method-assign]

    result = await run_task(
        session,
        task,
        FakeProvider([_step("github.open_pr", title="x")]),
        registry,
        _require_approval_policy(),
        workspace=workspace,
        holder=task.claimed_by,
    )

    assert result.status == "CANCELLED"
    async with session_factory() as probe:
        row = await probe.get(Task, task_id)
        assert row is not None and row.status == "CANCELLED"
        pending = await probe.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.task_id == task_id, Approval.status == "pending")
        )
        assert pending == 0
