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

from warden.core.events import read_events
from warden.core.loop import Budget, run_task
from warden.models import ModelCall, Task, ToolCall, User
from warden.providers.base import Completion, Usage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.tools.local import build_registry


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

    result = await run_task(session, task, provider, build_registry(workspace))

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

    result = await run_task(session, task, provider, build_registry(workspace))

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
        session, task, provider, build_registry(workspace), budget=Budget(max_iterations=3)
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
        build_registry(workspace),
        budget=Budget(max_iterations=10, max_usd=Decimal("1.00")),
    )

    assert result.status == "BUDGET_EXCEEDED"
    assert provider.calls == 1
    assert result.cost_usd > Decimal("1.00")


async def test_scripted_run_costs_nothing(session: AsyncSession, workspace: pathlib.Path) -> None:
    task = await _a_task(session)
    provider = FakeProvider([_step("finish", summary="done")])

    result = await run_task(session, task, provider, build_registry(workspace))

    assert result.cost_usd == Decimal("0")
    # Compared as Decimal, not as text: the stored string keeps the six-decimal quantum of
    # the Numeric(12, 6) column, so "0.000000" is the right value and "0" would not be.
    assert Decimal(task.spent["usd"]) == Decimal("0")
