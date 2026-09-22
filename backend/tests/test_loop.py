"""The loop, end to end, against a scripted provider and a temporary workspace.

Nothing here touches the network. This is the shape every later test of the control plane
takes: the FakeProvider decides what the model "wanted", and the assertions are about what
the control plane did with it.
"""

import pathlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fake_tools import FakeWorkspace
from warden.core.events import read_events
from warden.core.loop import Budget, run_task
from warden.models import ModelCall, PolicyDecision, Task, ToolCall, User
from warden.policy.engine import Effect, Policy, Rule, load_policy
from warden.providers.base import Completion, Usage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep

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


async def test_require_approval_degrades_to_a_refusal_until_week_3(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """Without the approval machinery, the loop errs on the restrictive side."""
    task = await _a_task(session)
    policy = Policy(
        [Rule(id="needs-human", effect=Effect.REQUIRE_APPROVAL, when={"tool": "read_file"})],
        default=Effect.DENY,
        policy_hash="test",
    )
    provider = FakeProvider(
        [_step("read_file", path="src/app.py"), _step("finish", summary="could not")]
    )

    await run_task(session, task, provider, FakeWorkspace().registry(), policy, workspace=workspace)

    row = (
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task.id, ToolCall.tool_name == "read_file")
        )
    ).one()
    assert row.decision == "require_approval"
    assert "human approval" in (row.error or "")


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
