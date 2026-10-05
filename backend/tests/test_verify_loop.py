"""The loop's side of verification: RUNNING -> VERIFYING -> SUCCEEDED | FAILED.

ADR-026 (evidence) and ADR-010 (verdict). A scripted collector stands in for the container
and a scripted provider for the reviewer, so these run wherever Postgres runs. What is under
test is the control plane's bookkeeping: order, durability, the verdict deciding the final
status, cancellation and resume. The checks themselves run against a real container in
tests/test_verify_runner.py, and resuming after a real `kill` of the worker process is proved
there too.
"""

import pathlib
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_tools import FakeWorkspace
from warden.core import cancel
from warden.core import loop as loop_module
from warden.core.events import read_events
from warden.core.loop import Budget, run_task
from warden.core.replay import rebuild
from warden.identity.jwt import KeyPair
from warden.models import Evidence, ModelCall, Task, User, Verdict
from warden.policy.engine import Effect, Policy, Rule
from warden.providers.base import Completion, Usage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.verify.reviewer import SUBMIT_VERDICT, Reviewer
from warden.verify.runner import KINDS


def _allow_all() -> Policy:
    return Policy(
        [Rule(id="allow-all", effect=Effect.ALLOW, when={"tool": "*"})],
        default=Effect.DENY,
        policy_hash="test",
    )


async def _a_task(session: AsyncSession) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(user)
    await session.flush()
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec="fix the bug")
    session.add(task)
    await session.flush()
    return task


def _finish(summary: str = "fixed it") -> ScriptStep:
    call = ProviderToolCall(id="call-finish", name="finish", arguments={"summary": summary})
    return ScriptStep(tool_calls=[call])


class _Checks:
    """Answers each kind with a canned payload and remembers what it was asked."""

    kinds: tuple[str, ...] = KINDS

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self._statuses = statuses or {}
        self.asked: list[str] = []

    async def check(self, kind: str) -> dict[str, Any]:
        self.asked.append(kind)
        return {"kind": kind, "status": self._statuses.get(kind, "passed")}


class _Crash(Exception):
    pass


def _reviewer(passed: bool = True, summary: str = "meets the spec", **extra: Any) -> Reviewer:
    """A reviewer whose one answer is scripted, through the same FakeProvider as the coder."""
    call = ProviderToolCall(
        id="verdict",
        name=SUBMIT_VERDICT,
        arguments={"passed": passed, "summary": summary, "findings": [], **extra},
    )
    return Reviewer(FakeProvider([ScriptStep(tool_calls=[call])], model="reviewer-model"))


async def _verdict(session: AsyncSession, task_id: uuid.UUID) -> Verdict:
    row = await session.scalar(select(Verdict).where(Verdict.task_id == task_id))
    assert row is not None
    return row


async def _evidence_kinds(session: AsyncSession, task_id: uuid.UUID) -> list[str]:
    rows = await session.scalars(
        select(Evidence.kind).where(Evidence.task_id == task_id).order_by(Evidence.created_at)
    )
    return list(rows)


async def test_finishing_runs_every_check_in_order_and_records_each(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)
    checks = _Checks()

    result = await run_task(
        session,
        task,
        FakeProvider([_finish("fixed the average")]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=checks,
    )

    assert result.status == "SUCCEEDED"
    assert checks.asked == ["diff", "lint", "types", "tests"]
    assert await _evidence_kinds(session, task.id) == ["diff", "lint", "types", "tests"]

    log = await read_events(session, task.id)
    kinds = [event.type for event in log]
    start = kinds.index("verify.started")
    assert kinds[start:] == [
        "verify.started",
        *["verify.recorded"] * 4,
        "verify.verdict",
        "task.finished",
    ]
    # The summary is on record before any check runs: a resumed worker needs it.
    assert log[start].payload == {"summary": "fixed the average", "iterations": 1}


async def test_red_tests_fail_the_task_even_when_the_reviewer_approves(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """ADR-010's central rule, end to end through the loop: the reviewer says yes, the gate
    says no, and the task is FAILED. Before this PR the same run ended SUCCEEDED."""
    task = await _a_task(session)

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks({"tests": "failed", "types": "timeout"}),
        reviewer=_reviewer(passed=True),
    )

    assert result.status == "FAILED"
    assert result.reason == "gate blocked by: tests"
    verdict = await _verdict(session, task.id)
    assert verdict.passed is False
    assert verdict.gate == {"passed": False, "blocked_by": ["tests"]}
    # The reviewer's opinion is kept, apart from the gate, so a reader sees both.
    assert verdict.summary == "meets the spec"


async def test_a_green_gate_and_an_approving_reviewer_succeed(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks(),
        reviewer=_reviewer(passed=True),
    )

    assert result.status == "SUCCEEDED"
    verdict = await _verdict(session, task.id)
    assert (verdict.passed, verdict.status, verdict.reviewer) == (True, "ok", "reviewer-model")
    review_call = await session.scalar(
        select(ModelCall).where(ModelCall.task_id == task.id, ModelCall.purpose == "review")
    )
    assert review_call is not None
    assert review_call.prompt_version == "review-v1"
    assert verdict.model_call_id == review_call.id


async def test_the_reviewer_can_fail_a_change_the_gate_let_through(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)
    blocker = {"severity": "blocker", "path": "src/app.py", "message": "spec not implemented"}

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks(),
        reviewer=_reviewer(passed=False, summary="does not do it", findings=[blocker]),
    )

    assert result.status == "FAILED"
    verdict = await _verdict(session, task.id)
    assert verdict.gate["passed"] is True
    assert verdict.findings == [blocker]


async def test_an_unusable_review_fails_closed(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks(),
        reviewer=Reviewer(FakeProvider([ScriptStep(text="LGTM, ship it")])),
    )

    assert result.status == "FAILED"
    verdict = await _verdict(session, task.id)
    assert (verdict.status, verdict.passed) == ("error", False)


async def test_without_a_reviewer_the_gate_decides_and_the_row_says_so(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks({"tests": "no_tests"}),
    )

    assert result.status == "SUCCEEDED"
    verdict = await _verdict(session, task.id)
    assert verdict.reviewer == "none"
    assert verdict.model_call_id is None


async def test_the_review_is_billed_but_never_trips_the_agents_budget(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The review costs real money and goes into the task's spend; it is the control plane's
    call, not the agent's, so it cannot turn a finished task into BUDGET_EXCEEDED."""
    task = await _a_task(session)

    class _PricedReviewer:
        name = "priced"

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            call = ProviderToolCall(
                id="v", name=SUBMIT_VERDICT, arguments={"passed": True, "summary": "ok"}
            )
            return Completion(
                provider=self.name,
                model="claude-opus-5",
                stop_reason="tool_use",
                tool_calls=[call],
                usage=Usage(input_tokens=200_000, output_tokens=10_000),
                raw_content=[],
            )

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        budget=Budget(max_usd=Decimal("0.01")),
        verifier=_Checks(),
        reviewer=Reviewer(_PricedReviewer()),
    )

    assert result.status == "SUCCEEDED"
    assert result.cost_usd > Decimal("0.01")
    assert task.spent == {"usd": str(result.cost_usd)}


async def test_a_cancel_during_the_review_stops_without_a_verdict(
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    task = await _a_task(session)

    class _CancelledWhileReviewing:
        name = "fake"

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            async with session_factory() as other:
                await cancel.request_cancel(other, task.id)
                await other.commit()
            call = ProviderToolCall(
                id="v", name=SUBMIT_VERDICT, arguments={"passed": True, "summary": "ok"}
            )
            return Completion(provider="fake", model="m", stop_reason="tool_use", tool_calls=[call])

    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks(),
        reviewer=Reviewer(_CancelledWhileReviewing()),
    )

    assert result.status == "CANCELLED"
    assert await session.scalar(select(Verdict).where(Verdict.task_id == task.id)) is None


async def test_a_run_stopped_after_the_verdict_resumes_without_reviewing_again(
    session: AsyncSession,
    keys: KeyPair,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict is committed, then the run dies before `task.finished`. The resumed run
    finishes with the recorded verdict; a second review would be a second paid model call."""
    task = await _a_task(session)
    task_id = task.id
    real_finish = loop_module._finish

    async def _dies(*args: object, **kwargs: object) -> object:
        raise _Crash

    monkeypatch.setattr(loop_module, "_finish", _dies)
    with pytest.raises(_Crash):
        await run_task(
            session,
            task,
            FakeProvider([_finish("done")]),
            FakeWorkspace().registry(),
            _allow_all(),
            keys=keys,
            workspace=tmp_path,
            verifier=_Checks({"tests": "failed"}),
            reviewer=_reviewer(passed=True),
        )
    await session.rollback()
    monkeypatch.setattr(loop_module, "_finish", real_finish)

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    await session.refresh(refreshed)

    class _MustNotBeCalled:
        name = "fake"

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            raise AssertionError("the verdict is on record; nobody may review again")

    result = await run_task(
        session,
        refreshed,
        _MustNotBeCalled(),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        resume=rebuild(await read_events(session, task_id)),
        verifier=_Checks(),
        reviewer=Reviewer(_MustNotBeCalled()),
    )

    # The recorded verdict decides, not a fresh look at evidence.
    assert result.status == "FAILED"


async def test_a_model_that_stops_without_finish_is_verified_too(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)
    checks = _Checks()

    result = await run_task(
        session,
        task,
        FakeProvider([ScriptStep(text="all done, I think")]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=checks,
    )

    assert result.status == "SUCCEEDED"
    assert checks.asked == list(KINDS)


async def test_a_cancel_during_a_check_stops_before_that_check_is_recorded(
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """The cancel lands from a real second session while `lint` is running, the way the API
    would send it. What `lint` returns after that is evidence of the kill, not of the work,
    so it must not be recorded."""
    task = await _a_task(session)

    class _CancelledDuringLint(_Checks):
        async def check(self, kind: str) -> dict[str, Any]:
            if kind == "lint":
                async with session_factory() as other:
                    outcome = await cancel.request_cancel(other, task.id)
                    await other.commit()
                assert outcome is cancel.CancelOutcome.MARKED
            return await super().check(kind)

    checks = _CancelledDuringLint()
    result = await run_task(
        session,
        task,
        FakeProvider([_finish()]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=checks,
    )

    assert result.status == "CANCELLED"
    assert checks.asked == ["diff", "lint"]
    assert await _evidence_kinds(session, task.id) == ["diff"]


async def test_a_run_stopped_mid_verification_resumes_without_calling_the_model(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """Wiring between replay and the loop: the run stops after `diff` and `lint` are on
    record, and the resumed run finishes only `types` and `tests`, with no model call.

    The stop is an exception escaping the loop, which proves the bookkeeping, not
    survival of a dead process. That one is tests/test_verify_runner.py, which kills a real
    worker process during verification.
    """
    task = await _a_task(session)
    # Captured now: the rollback below expires every loaded attribute, and reading `task.id`
    # afterwards would be a lazy load, which an AsyncSession refuses (MissingGreenlet).
    task_id = task.id

    class _DiesAtTypes(_Checks):
        async def check(self, kind: str) -> dict[str, Any]:
            if kind == "types":
                raise _Crash
            return await super().check(kind)

    try:
        await run_task(
            session,
            task,
            FakeProvider([_finish("resumable")]),
            FakeWorkspace().registry(),
            _allow_all(),
            keys=keys,
            workspace=tmp_path,
            verifier=_DiesAtTypes(),
        )
    except _Crash:
        await session.rollback()
    else:
        raise AssertionError("the first run should have stopped at types")

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.status == "VERIFYING"

    class _MustNotBeCalled:
        name = "fake"

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            raise AssertionError("a resumed verification must not call the model")

    resume = rebuild(await read_events(session, task_id))
    checks = _Checks()
    result = await run_task(
        session,
        refreshed,
        _MustNotBeCalled(),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        resume=resume,
        verifier=checks,
    )

    assert result.status == "SUCCEEDED"
    assert result.summary == "resumable"
    assert checks.asked == ["types", "tests"]
    assert await _evidence_kinds(session, task_id) == ["diff", "lint", "types", "tests"]
    model_calls = await session.scalar(
        select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task_id)
    )
    assert model_calls == 1
