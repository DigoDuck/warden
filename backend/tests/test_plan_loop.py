"""The loop's side of planning (ADR-031): where the plan sits in the event log and in the coder's
conversation, what it costs, and what a crash does to it.

A scripted planner stands in for the model, so these run wherever Postgres runs. What is under
test is the bookkeeping: the plan is on record before iteration 1, the coder reads it labelled
as unverified, a malformed plan never stops the task, the call is billed, no transaction is open
while it waits, and a worker that dies around the call neither loses the plan nor buys it twice.
The same thing against a real worker process being killed lives in tests/test_durability.py.
"""

import pathlib
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fake_tools import FakeWorkspace
from tests.test_publish import APP_CHANGE, SPEC, SUMMARY, _OpenPr, _Reviewer, _Verifier
from warden.core import queue
from warden.core.events import read_events
from warden.core.loop import Budget, RunResult, run_task
from warden.core.replay import rebuild
from warden.core.worker import run_claimed_task
from warden.identity.jwt import KeyPair
from warden.models import ModelCall, Task, User
from warden.plan.planner import PLAN_TOOL, PlanPayload, PlanResult, plan_message
from warden.policy.engine import Effect, Policy, Rule, load_policy
from warden.providers.base import Completion, Message, Usage, UserMessage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = {
    "steps": ["read src/app.py", "fix average()"],
    "likely_files": ["src/app.py"],
    "risks": ["empty list"],
    "tests_to_add": ["average([]) raises"],
}


@pytest.fixture(autouse=True)
async def _clean(session: AsyncSession) -> Any:
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


def _finish(summary: str = "done") -> ScriptStep:
    call = ProviderToolCall(id="call-finish", name="finish", arguments={"summary": summary})
    return ScriptStep(tool_calls=[call])


class _Planner:
    """A canned plan (or none), remembering what it was asked and whether a transaction was
    open on the loop's session while it was."""

    def __init__(
        self,
        *,
        plan: dict[str, Any] | None = None,
        malformed: str | None = None,
        usage: Usage | None = None,
        model: str = "fake-model",
        probe: AsyncSession | None = None,
        crash: bool = False,
    ) -> None:
        self._plan = PLAN if plan is None and malformed is None else plan
        self._malformed = malformed
        self._usage = usage or Usage()
        self._model = model
        self._probe = probe
        self._crash = crash
        self.specs: list[str] = []
        self.in_transaction: list[bool] = []

    async def plan(self, spec: str) -> PlanResult:
        self.specs.append(spec)
        if self._probe is not None:
            self.in_transaction.append(self._probe.in_transaction())
        if self._crash:
            raise RuntimeError("worker died inside the planner call")
        completion = Completion(
            provider="fake",
            model=self._model,
            stop_reason="tool_use",
            tool_calls=[ProviderToolCall(id="p", name=PLAN_TOOL, arguments=self._plan or {})],
            usage=self._usage,
        )
        if self._malformed is not None:
            return PlanResult(completion, None, self._malformed)
        assert self._plan is not None
        return PlanResult(completion, PlanPayload.model_validate(self._plan), None)


class _SeenMessages:
    """A FakeProvider that remembers the conversation each coder turn was given."""

    name = "fake"

    def __init__(self, steps: Sequence[ScriptStep]) -> None:
        self._inner = FakeProvider(list(steps))
        self.seen: list[list[Message]] = []

    async def generate(self, messages: Sequence[Message], **kwargs: Any) -> Completion:
        self.seen.append(list(messages))
        return await self._inner.generate(messages, **kwargs)


class _Crash(_SeenMessages):
    async def generate(self, messages: Sequence[Message], **kwargs: Any) -> Completion:
        self.seen.append(list(messages))
        raise RuntimeError("worker died inside the coder's first call")


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
    task: Task,
    provider: Any,
    planner: _Planner | None,
    **kwargs: Any,
) -> RunResult:
    return await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        kwargs.pop("policy", _allow_all()),
        keys=keys,
        workspace=tmp_path,
        holder=task.claimed_by,
        planner=planner,
        **kwargs,
    )


async def _types(session: AsyncSession, task: Task) -> list[str]:
    return [event.type for event in await read_events(session, task.id)]


# --- order, content, labelling ---------------------------------------------------------------


async def test_the_plan_is_on_record_before_the_first_iteration(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    planner = _Planner()

    result = await _run(session, keys, tmp_path, task, FakeProvider([_finish()]), planner)

    assert result.status == "SUCCEEDED"
    assert planner.specs == [task.spec]
    kinds = await _types(session, task)
    assert kinds[:3] == ["task.created", "plan.recorded", "iteration.started"]
    recorded = next(e for e in await read_events(session, task.id) if e.type == "plan.recorded")
    assert recorded.payload["plan"] == PLAN
    assert recorded.payload["cost_usd"] == "0.000000"
    assert "malformed_reason" not in recorded.payload

    purposes = await session.scalars(select(ModelCall.purpose).where(ModelCall.task_id == task.id))
    assert sorted(purposes) == ["agent", "planner"]


async def test_the_coder_receives_the_plan_labelled_as_unverified(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    provider = _SeenMessages([_finish()])

    await _run(session, keys, tmp_path, task, provider, _Planner())

    [first_turn] = provider.seen
    assert first_turn[0] == UserMessage(text=task.spec)
    assert first_turn[1] == UserMessage(text=plan_message(PLAN))
    assert len(first_turn) == 2
    assert "not verified" in first_turn[1].text


async def test_a_malformed_plan_does_not_stop_the_task_and_the_coder_runs_without_it(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    provider = _SeenMessages([_finish()])
    planner = _Planner(malformed="submit_plan arguments are malformed at steps: too short")

    result = await _run(session, keys, tmp_path, task, provider, planner)

    assert result.status == "SUCCEEDED"
    assert provider.seen == [[UserMessage(text=task.spec)]]
    recorded = next(e for e in await read_events(session, task.id) if e.type == "plan.recorded")
    assert (
        recorded.payload["malformed_reason"]
        == "submit_plan arguments are malformed at steps: too short"
    )
    assert "plan" not in recorded.payload
    # The call was still made and paid for: it is recorded even when its answer is unusable.
    purposes = await session.scalars(select(ModelCall.purpose).where(ModelCall.task_id == task.id))
    assert sorted(purposes) == ["agent", "planner"]


async def test_without_a_planner_the_log_and_the_conversation_are_what_they_were(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    provider = _SeenMessages([_finish()])

    await _run(session, keys, tmp_path, task, provider, None)

    assert "plan.recorded" not in await _types(session, task)
    assert provider.seen == [[UserMessage(text=task.spec)]]


# --- cost and transactions -------------------------------------------------------------------


async def test_the_planners_cost_counts_against_the_budget(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The ceiling is checked after the coder's first call, and by then the planner's call is
    already in `spent`: a task whose planning alone is over budget stops at iteration 1."""
    task = await _claimed_task(session)
    # 1M input tokens of claude-sonnet-5 at $2.00/Mtok.
    planner = _Planner(model="claude-sonnet-5", usage=Usage(input_tokens=1_000_000))

    result = await _run(
        session,
        keys,
        tmp_path,
        task,
        FakeProvider([_finish()]),
        planner,
        budget=Budget(max_usd=Decimal("1.00")),
    )

    assert result.status == "BUDGET_EXCEEDED"
    assert result.cost_usd == Decimal("2.000000")
    recorded = next(e for e in await read_events(session, task.id) if e.type == "plan.recorded")
    assert recorded.payload["cost_usd"] == "2.000000"


async def test_no_transaction_is_open_while_the_planner_is_called(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """ADR-019: an open transaction holds a lock on the task row for as long as the call waits."""
    task = await _claimed_task(session)
    planner = _Planner(probe=session)

    await _run(session, keys, tmp_path, task, FakeProvider([_finish()]), planner)

    assert planner.in_transaction == [False]


# --- crash and resume ------------------------------------------------------------------------


async def test_a_plan_on_record_is_replayed_not_bought_again(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)
    # The first worker dies in the coder's first call, after the plan is on record.
    with pytest.raises(RuntimeError, match="worker died"):
        await _run(session, keys, tmp_path, task, _Crash([]), _Planner())
    await session.rollback()

    state = rebuild(await read_events(session, task.id))
    assert state.plan_recorded is True
    assert state.messages == [
        UserMessage(text=task.spec),
        UserMessage(text=plan_message(PLAN)),
    ]

    await queue.expire_lease_now(session, task.id)
    resumed = await queue.claim(session, "worker-live")
    assert resumed is not None
    second = _Planner()
    provider = _SeenMessages([_finish()])
    result = await run_claimed_task(
        session,
        resumed,
        provider,
        _allow_all(),
        tmp_path,
        FakeWorkspace().registry(),
        keys=keys,
        holder=resumed.claimed_by,
        planner=second,
    )
    await session.commit()

    assert result.status == "SUCCEEDED"
    assert second.specs == [], "the resume asked the planner again"
    assert provider.seen[0] == state.messages
    plans = [e for e in await read_events(session, task.id) if e.type == "plan.recorded"]
    assert len(plans) == 1


async def test_a_worker_that_died_before_recording_the_plan_plans_once_on_resume(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """Dies inside the planner call: `task.created` is durable, `plan.recorded` is not. The
    next worker plans (the lost call left nothing to reuse), exactly once."""
    task = await _claimed_task(session)
    with pytest.raises(RuntimeError, match="inside the planner"):
        await _run(session, keys, tmp_path, task, FakeProvider([_finish()]), _Planner(crash=True))
    await session.rollback()

    assert await _types(session, task) == ["task.created"]
    state = rebuild(await read_events(session, task.id))
    assert state.plan_recorded is False

    await queue.expire_lease_now(session, task.id)
    resumed = await queue.claim(session, "worker-live")
    assert resumed is not None
    planner = _Planner()
    provider = _SeenMessages([_finish()])
    result = await run_claimed_task(
        session,
        resumed,
        provider,
        _allow_all(),
        tmp_path,
        FakeWorkspace().registry(),
        keys=keys,
        holder=resumed.claimed_by,
        planner=planner,
    )
    await session.commit()

    assert result.status == "SUCCEEDED"
    assert planner.specs == [task.spec]
    plans = [e for e in await read_events(session, task.id) if e.type == "plan.recorded"]
    assert len(plans) == 1
    assert provider.seen[0][1] == UserMessage(text=plan_message(PLAN))
    purposes = await session.scalars(select(ModelCall.purpose).where(ModelCall.task_id == task.id))
    assert sorted(purposes) == ["agent", "planner"]


async def test_a_run_already_past_its_first_turn_is_not_planned_on_resume(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """A task that began before planning existed (or without a planner) must not grow a plan
    halfway through: planning only ever happens before the coder's first model call."""
    task = await _claimed_task(session)
    # Iteration 1 completes (a read), then the coder dies in iteration 2's call.
    read = ScriptStep(
        tool_calls=[ProviderToolCall(id="r", name="read_file", arguments={"path": "src/app.py"})]
    )

    class _DiesOnSecondCall(_SeenMessages):
        async def generate(self, messages: Sequence[Message], **kwargs: Any) -> Completion:
            if self.seen:
                raise RuntimeError("worker died on turn two")
            return await super().generate(messages, **kwargs)

    with pytest.raises(RuntimeError, match="worker died"):
        await _run(session, keys, tmp_path, task, _DiesOnSecondCall([read]), None)
    await session.rollback()

    await queue.expire_lease_now(session, task.id)
    resumed = await queue.claim(session, "worker-live")
    assert resumed is not None
    planner = _Planner()
    await run_claimed_task(
        session,
        resumed,
        FakeProvider([_finish()]),
        _allow_all(),
        tmp_path,
        FakeWorkspace().registry(),
        keys=keys,
        holder=resumed.claimed_by,
        planner=planner,
    )
    await session.commit()

    assert planner.specs == []
    assert "plan.recorded" not in await _types(session, task)


# --- the pull request ------------------------------------------------------------------------


async def _proposed_body(session: AsyncSession, task: Task) -> str:
    event = next(e for e in await read_events(session, task.id) if e.type == "publish.requested")
    return str(event.payload["arguments"]["body"])


async def test_the_pull_request_body_carries_the_plan_labelled_as_generated(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)

    await _run(
        session,
        keys,
        tmp_path,
        task,
        FakeProvider([_finish(SUMMARY)]),
        _Planner(),
        policy=load_policy(REPO_ROOT / "policies" / "default.yaml"),
        verifier=_Verifier(APP_CHANGE),
        reviewer=_Reviewer(),
        publisher=_OpenPr().registry(),
    )

    body = await _proposed_body(session, task)
    assert "Plan" in body
    assert "generated by the planner" in body
    for line in (*PLAN["steps"], *PLAN["likely_files"], *PLAN["risks"], *PLAN["tests_to_add"]):
        assert line in body
    # Both model-written sections are labelled; the verification table still comes first.
    assert body.index("**Verification**") < body.index("generated by the planner")


async def test_a_malformed_plan_leaves_no_plan_section_in_the_pull_request(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _claimed_task(session)

    await _run(
        session,
        keys,
        tmp_path,
        task,
        FakeProvider([_finish(SUMMARY)]),
        _Planner(malformed="no plan"),
        policy=load_policy(REPO_ROOT / "policies" / "default.yaml"),
        verifier=_Verifier(APP_CHANGE),
        reviewer=_Reviewer(),
        publisher=_OpenPr().registry(),
    )

    assert "generated by the planner" not in await _proposed_body(session, task)
