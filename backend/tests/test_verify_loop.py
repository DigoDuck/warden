"""The loop's side of verification (ADR-026): RUNNING -> VERIFYING -> SUCCEEDED.

A scripted collector stands in for the container here, so these run wherever Postgres runs.
What is under test is the control plane's bookkeeping around the checks: order, durability,
cancellation and resume. The checks themselves run against a real container in
tests/test_verify_runner.py, and resuming after a real `kill` of the worker process is proved
there too.
"""

import pathlib
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_tools import FakeWorkspace
from warden.core import cancel
from warden.core.events import read_events
from warden.core.loop import run_task
from warden.core.replay import rebuild
from warden.identity.jwt import KeyPair
from warden.models import Evidence, ModelCall, Task, User
from warden.policy.engine import Effect, Policy, Rule
from warden.providers.base import Completion
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
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
    assert kinds[start:] == ["verify.started", *["verify.recorded"] * 4, "task.finished"]
    # The summary is on record before any check runs: a resumed worker needs it.
    assert log[start].payload == {"summary": "fixed the average", "iterations": 1}


async def test_red_evidence_is_recorded_and_the_task_still_succeeds(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """Deciding pass/fail is the verdict's job (ADR-010), not the runner's."""
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
    )

    assert result.status == "SUCCEEDED"
    tests_row = await session.scalar(
        select(Evidence).where(Evidence.task_id == task.id, Evidence.kind == "tests")
    )
    assert tests_row is not None and tests_row.payload["status"] == "failed"


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
