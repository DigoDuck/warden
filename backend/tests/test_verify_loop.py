"""The loop's side of verification: RUNNING -> VERIFYING -> SUCCEEDED | FAILED (ADR-026, ADR-010).

A scripted collector stands in for the container and a scripted reviewer for the model, so
these run wherever Postgres runs. What is under test is the control plane's bookkeeping around
them: order, the success definition, durability, cancellation and resume. The checks run
against a real container in tests/test_verify_runner.py, which also kills a real worker process
during verification.
"""

import pathlib
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_tools import FakeWorkspace
from warden.core import cancel, loop
from warden.core.events import read_events
from warden.core.loop import run_task
from warden.core.replay import rebuild
from warden.identity.jwt import KeyPair
from warden.models import Evidence, ModelCall, Task, User, Verdict
from warden.policy.engine import Effect, Policy, Rule
from warden.providers.base import Completion, Message, ToolSchema, Usage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.verify.reviewer import ProviderReviewer, Review, VerdictPayload
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


def _verdict_step(passed: bool = True, findings: list[str] | None = None) -> ScriptStep:
    call = ProviderToolCall(
        id="call-verdict",
        name="submit_verdict",
        arguments={"passed": passed, "findings": findings or []},
    )
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


class _Reviewer:
    """Answers with a canned verdict and remembers what it was shown."""

    def __init__(
        self,
        *,
        passed: bool = True,
        findings: list[str] | None = None,
        malformed: str | None = None,
        probe: AsyncSession | None = None,
    ) -> None:
        self._passed = passed
        self._findings = findings or []
        self._malformed = malformed
        self._probe = probe
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.in_transaction: list[bool] = []

    async def review(self, spec: str, evidence: Mapping[str, Mapping[str, Any]]) -> Review:
        self.calls.append((spec, {kind: dict(row) for kind, row in evidence.items()}))
        if self._probe is not None:
            self.in_transaction.append(self._probe.in_transaction())
        completion = Completion(
            provider="fake", model="fake-model", stop_reason="tool_use", usage=Usage()
        )
        if self._malformed is not None:
            return Review(completion, None, self._malformed)
        return Review(
            completion, VerdictPayload(passed=self._passed, findings=self._findings), None
        )


class _Crash(Exception):
    pass


async def _evidence_kinds(session: AsyncSession, task_id: uuid.UUID) -> list[str]:
    rows = await session.scalars(
        select(Evidence.kind).where(Evidence.task_id == task_id).order_by(Evidence.created_at)
    )
    return list(rows)


async def _verdicts(session: AsyncSession, task_id: uuid.UUID) -> list[Verdict]:
    return list(await session.scalars(select(Verdict).where(Verdict.task_id == task_id)))


async def _run(
    session: AsyncSession,
    task: Task,
    keys: KeyPair,
    workspace: pathlib.Path,
    *,
    checks: _Checks | None,
    reviewer: Any,
    steps: Sequence[ScriptStep] | None = None,
) -> Any:
    return await run_task(
        session,
        task,
        FakeProvider(list(steps or [_finish("fixed the average")])),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=workspace,
        verifier=checks,
        reviewer=reviewer,
    )


# --- the success definition, through the loop -------------------------------------------


async def test_green_evidence_and_an_approving_reviewer_succeed(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)
    checks, reviewer = _Checks(), _Reviewer()

    result = await _run(session, task, keys, tmp_path, checks=checks, reviewer=reviewer)

    assert result.status == "SUCCEEDED"
    assert checks.asked == ["diff", "lint", "types", "tests"]
    assert await _evidence_kinds(session, task.id) == ["diff", "lint", "types", "tests"]

    # The reviewer ran once, after all four checks, and was shown the recorded evidence.
    [(spec, shown)] = reviewer.calls
    assert spec == "fix the bug"
    assert set(shown) == set(KINDS)

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

    [verdict] = await _verdicts(session, task.id)
    assert (verdict.verifier, verdict.passed, verdict.findings) == ("independent", True, [])
    assert verdict.malformed_reason is None


async def test_red_tests_fail_the_task_even_when_the_reviewer_approves(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The model cannot override a red test. The verdict is still recorded as the reviewer
    gave it: the record says what each party said, the status says what the control plane
    decided."""
    task = await _a_task(session)

    result = await _run(
        session,
        task,
        keys,
        tmp_path,
        checks=_Checks({"tests": "failed", "types": "timeout"}),
        reviewer=_Reviewer(passed=True),
    )

    assert result.status == "FAILED"
    assert result.reason is not None and "tests" in result.reason and "types" in result.reason
    [verdict] = await _verdicts(session, task.id)
    assert verdict.passed is True
    refreshed = await session.get(Task, task.id)
    assert refreshed is not None and refreshed.status == "FAILED"


async def test_a_rejecting_reviewer_fails_a_green_task_and_its_findings_are_kept(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)

    result = await _run(
        session,
        task,
        keys,
        tmp_path,
        checks=_Checks(),
        reviewer=_Reviewer(passed=False, findings=["None still crashes average()"]),
    )

    assert result.status == "FAILED"
    [verdict] = await _verdicts(session, task.id)
    assert verdict.passed is False
    assert verdict.findings == ["None still crashes average()"]


async def test_a_malformed_verdict_fails_the_task_and_is_recorded_as_such(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)

    result = await _run(
        session,
        task,
        keys,
        tmp_path,
        checks=_Checks(),
        reviewer=_Reviewer(malformed="the reviewer did not call submit_verdict"),
    )

    assert result.status == "FAILED"
    [verdict] = await _verdicts(session, task.id)
    assert verdict.passed is False
    assert verdict.malformed_reason == "the reviewer did not call submit_verdict"


async def test_a_model_that_stops_without_finish_is_verified_and_reviewed_too(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)
    checks, reviewer = _Checks(), _Reviewer()

    result = await _run(
        session,
        task,
        keys,
        tmp_path,
        checks=checks,
        reviewer=reviewer,
        steps=[ScriptStep(text="all done, I think")],
    )

    assert result.status == "SUCCEEDED"
    assert checks.asked == list(KINDS)
    assert len(reviewer.calls) == 1


async def test_without_a_verifier_the_task_finishes_the_moment_the_agent_does(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """Loop tests with fake tools have no container to verify against: unchanged."""
    task = await _a_task(session)
    reviewer = _Reviewer()

    result = await _run(session, task, keys, tmp_path, checks=None, reviewer=reviewer)

    assert result.status == "SUCCEEDED"
    assert reviewer.calls == []
    assert await _verdicts(session, task.id) == []


async def test_a_verifier_without_a_reviewer_is_refused_up_front(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """There must be no path to SUCCEEDED that skips the verdict."""
    task = await _a_task(session)

    with pytest.raises(ValueError, match="reviewer"):
        await _run(session, task, keys, tmp_path, checks=_Checks(), reviewer=None)


# --- what the reviewer is, and is not, shown --------------------------------------------


class _RecordingProvider:
    """One provider for the agent and the reviewer, as the worker builds it, remembering every
    request it received."""

    name = "fake"

    def __init__(self, script: list[ScriptStep]) -> None:
        self._inner = FakeProvider(script)
        self.requests: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        self.requests.append({"messages": list(messages), "system": system})
        return await self._inner.generate(messages, tools=tools, system=system)


async def test_the_reviewer_never_receives_the_coders_summary(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The messages the provider actually received on the review call, not the signature:
    the summary the coder wrote must not appear anywhere in them."""
    task = await _a_task(session)
    canary = "CANARY-the-coder-says-everything-is-perfect"
    provider = _RecordingProvider([_finish(canary), _verdict_step()])

    result = await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks(),
        reviewer=ProviderReviewer(provider),
    )

    assert result.status == "SUCCEEDED"
    assert result.summary == canary  # the coder's words are kept for the API ...
    _agent_request, review_request = provider.requests
    # ... and absent from the whole review request: messages and system prompt alike.
    assert canary not in repr(review_request)
    assert [type(m).__name__ for m in review_request["messages"]] == ["UserMessage"]


# --- billing ----------------------------------------------------------------------------


async def test_the_review_call_is_billed_as_its_own_model_call_and_linked(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    task = await _a_task(session)
    provider = _RecordingProvider([_finish(), _verdict_step()])

    await run_task(
        session,
        task,
        provider,
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        verifier=_Checks(),
        reviewer=ProviderReviewer(provider),
    )

    calls = (
        await session.scalars(
            select(ModelCall).where(ModelCall.task_id == task.id).order_by(ModelCall.purpose)
        )
    ).all()
    assert [c.purpose for c in calls] == ["agent", "reviewer"]
    [verdict] = await _verdicts(session, task.id)
    reviewer_call = next(c for c in calls if c.purpose == "reviewer")
    assert verdict.model_call_id == reviewer_call.id


# --- durability -------------------------------------------------------------------------


async def test_no_transaction_is_held_open_across_the_review_call(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """ADR-019: an open transaction holds a key-share lock on the task row, and `claim()`
    skips locked rows, so a reviewer call that hangs would make the task unclaimable long
    after its lease expired. The reviewer reports what it saw from inside the call."""
    task = await _a_task(session)
    reviewer = _Reviewer(probe=session)

    await _run(session, task, keys, tmp_path, checks=_Checks(), reviewer=reviewer)

    assert reviewer.in_transaction == [False]


async def test_a_review_call_lost_to_a_crash_is_made_again_and_recorded_once(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """The run stops inside the reviewer after all four checks are on record. The resume does
    not re-run a check and does not call the agent's model; it calls the reviewer again (the
    lost call was never recorded, so there is nothing to reuse) and ends with exactly one
    verdict. The stop is an exception: tests/test_verify_runner.py kills a real process."""
    task = await _a_task(session)
    task_id = task.id  # the rollback below expires every attribute

    class _CrashesOnce(_Reviewer):
        async def review(self, spec: str, evidence: Mapping[str, Mapping[str, Any]]) -> Review:
            raise _Crash

    with pytest.raises(_Crash):
        await _run(session, task, keys, tmp_path, checks=_Checks(), reviewer=_CrashesOnce())
    await session.rollback()

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.status == "VERIFYING"
    assert await _verdicts(session, task_id) == []

    class _MustNotBeCalled:
        name = "fake"

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            raise AssertionError("a resumed verification must not call the agent's model")

    checks, reviewer = _Checks(), _Reviewer()
    resume = rebuild(await read_events(session, task_id))
    assert resume.verification is not None and not resume.verification.verdict_recorded
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
        reviewer=reviewer,
    )

    assert result.status == "SUCCEEDED"
    assert result.summary == "fixed the average"
    assert checks.asked == []  # every check was already on record
    assert len(reviewer.calls) == 1
    assert len(await _verdicts(session, task_id)) == 1


async def test_a_recorded_verdict_is_never_asked_for_again(
    session: AsyncSession,
    keys: KeyPair,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker dies after the verdict committed and before `task.finished` did. The resume
    goes straight to the final status: no check, no model call, no second verdict."""
    task = await _a_task(session)
    task_id = task.id

    async def _dies_before_finishing(*args: Any, **kwargs: Any) -> Any:
        raise _Crash

    # `_finish` is what writes the terminal row, so make the first run stop right there.
    monkeypatch.setattr(loop, "_finish", _dies_before_finishing)
    with pytest.raises(_Crash):
        await _run(session, task, keys, tmp_path, checks=_Checks(), reviewer=_Reviewer())
    monkeypatch.undo()
    await session.rollback()

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.status == "VERIFYING"
    assert len(await _verdicts(session, task_id)) == 1

    resume = rebuild(await read_events(session, task_id))
    assert resume.verification is not None and resume.verification.verdict_recorded
    checks, reviewer = _Checks(), _Reviewer()
    result = await run_task(
        session,
        refreshed,
        FakeProvider([]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        resume=resume,
        verifier=checks,
        reviewer=reviewer,
    )

    assert result.status == "SUCCEEDED"
    assert checks.asked == [] and reviewer.calls == []
    assert len(await _verdicts(session, task_id)) == 1


async def test_a_cancel_before_the_review_call_stops_without_making_it(
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    task = await _a_task(session)
    task_id = task.id

    class _CrashesOnce(_Reviewer):
        async def review(self, spec: str, evidence: Mapping[str, Mapping[str, Any]]) -> Review:
            raise _Crash

    with pytest.raises(_Crash):
        await _run(session, task, keys, tmp_path, checks=_Checks(), reviewer=_CrashesOnce())
    await session.rollback()

    async with session_factory() as other:
        outcome = await cancel.request_cancel(other, task_id)
        await other.commit()
    assert outcome is cancel.CancelOutcome.MARKED

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    await session.refresh(refreshed)
    reviewer = _Reviewer()
    result = await run_task(
        session,
        refreshed,
        FakeProvider([]),
        FakeWorkspace().registry(),
        _allow_all(),
        keys=keys,
        workspace=tmp_path,
        resume=rebuild(await read_events(session, task_id)),
        verifier=_Checks(),
        reviewer=reviewer,
    )

    assert result.status == "CANCELLED"
    assert reviewer.calls == []
    assert await _verdicts(session, task_id) == []


async def test_a_cancel_during_a_check_stops_before_that_check_is_recorded(
    session: AsyncSession,
    keys: KeyPair,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """The cancel lands from a real second session while `lint` is running, the way the API
    would send it. What `lint` returns after that is evidence of the kill, not of the work,
    so it must not be recorded, and the reviewer is never called."""
    task = await _a_task(session)

    class _CancelledDuringLint(_Checks):
        async def check(self, kind: str) -> dict[str, Any]:
            if kind == "lint":
                async with session_factory() as other:
                    outcome = await cancel.request_cancel(other, task.id)
                    await other.commit()
                assert outcome is cancel.CancelOutcome.MARKED
            return await super().check(kind)

    checks, reviewer = _CancelledDuringLint(), _Reviewer()
    result = await _run(session, task, keys, tmp_path, checks=checks, reviewer=reviewer)

    assert result.status == "CANCELLED"
    assert checks.asked == ["diff", "lint"]
    assert await _evidence_kinds(session, task.id) == ["diff"]
    assert reviewer.calls == []


async def test_a_run_stopped_mid_verification_resumes_without_calling_the_agents_model(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """Wiring between replay and the loop: the run stops after `diff` and `lint` are on
    record, and the resumed run finishes only `types` and `tests`, then reviews, with no
    agent model call. The stop is an exception escaping the loop, which proves the bookkeeping,
    not survival of a dead process: that is tests/test_verify_runner.py."""
    task = await _a_task(session)
    task_id = task.id

    class _DiesAtTypes(_Checks):
        async def check(self, kind: str) -> dict[str, Any]:
            if kind == "types":
                raise _Crash
            return await super().check(kind)

    with pytest.raises(_Crash):
        await _run(
            session,
            task,
            keys,
            tmp_path,
            checks=_DiesAtTypes(),
            reviewer=_Reviewer(),
            steps=[_finish("resumable")],
        )
    await session.rollback()

    refreshed = await session.get(Task, task_id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.status == "VERIFYING"

    class _MustNotBeCalled:
        name = "fake"

        async def generate(self, *args: object, **kwargs: object) -> Completion:
            raise AssertionError("a resumed verification must not call the agent's model")

    resume = rebuild(await read_events(session, task_id))
    checks, reviewer = _Checks(), _Reviewer()
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
        reviewer=reviewer,
    )

    assert result.status == "SUCCEEDED"
    assert result.summary == "resumable"
    assert checks.asked == ["types", "tests"]
    assert await _evidence_kinds(session, task_id) == ["diff", "lint", "types", "tests"]
    assert len(reviewer.calls) == 1
    agent_calls = await session.scalar(
        select(func.count())
        .select_from(ModelCall)
        .where(ModelCall.task_id == task_id, ModelCall.purpose == "agent")
    )
    assert agent_calls == 1


def test_the_reviewers_cost_is_added_back_when_a_resume_reads_the_log() -> None:
    """`spent` after a resume has to include the (unbudgeted) review call, or the final
    `task.spent` would forget it whenever a worker died after the verdict."""
    from warden.models import TaskEvent

    def event(seq: int, type_: str, payload: dict[str, Any]) -> TaskEvent:
        return TaskEvent(task_id=uuid.uuid4(), seq=seq, type=type_, payload=payload)

    state = rebuild(
        [
            event(1, "task.created", {"spec": "x"}),
            event(2, "iteration.started", {"n": 1}),
            event(3, "model.called", {"cost_usd": "0.010000"}),
            event(4, "verify.started", {"summary": "s", "iterations": 1}),
            event(5, "verify.verdict", {"passed": True, "cost_usd": "0.002500"}),
        ]
    )

    assert state.spent == Decimal("0.012500")
    assert state.verification is not None and state.verification.verdict_recorded
