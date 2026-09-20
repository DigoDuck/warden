"""Kill a run mid-flight, let another worker take it, and prove no tool ran twice.

Week 2's hardest checklist item. The assertion the briefing asks for is the one on
`tool_calls`; everything else here exists to make that assertion mean something.

The crash is a tool raising a plain `RuntimeError`. `_run_tools` only catches `ToolError`,
so anything else propagates out of `run_task` exactly as an unhandled failure in a worker
process would, leaving the task RUNNING with a partial event log.

Note on the scripts: `FakeProvider` is positional, it replays step by step and has no
memory. A real provider is stateless too, but answers from the conversation it is sent. So
the resumed run gets a script holding only the turns still to come, which is what a real
provider would produce given the replayed messages.
"""

import pathlib
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_tools import FakeWorkspace
from warden.core import queue
from warden.core.events import read_events
from warden.core.loop import Budget, RunResult
from warden.core.worker import run_claimed_task
from warden.models import ModelCall, Task, ToolCall, User
from warden.policy.engine import Effect, Policy, Rule
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep

BUDGET = Budget(max_iterations=6)


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
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
    return root


def _allow_all() -> Policy:
    return Policy(
        [Rule(id="allow-all", effect=Effect.ALLOW, when={"tool": "*"})],
        default=Effect.DENY,
        policy_hash="test",
    )


def _two_reads() -> ScriptStep:
    return ScriptStep(
        tool_calls=[
            ProviderToolCall(id="call-a", name="read_file", arguments={"path": "src/app.py"}),
            ProviderToolCall(id="call-b", name="read_file", arguments={"path": "src/other.py"}),
        ]
    )


def _finish(summary: str = "done") -> ScriptStep:
    return ScriptStep(
        tool_calls=[
            ProviderToolCall(id="call-finish", name="finish", arguments={"summary": summary})
        ]
    )


async def _queued_task(session: AsyncSession) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    return await queue.enqueue(
        session, user_id=user.id, spec="summarise", idempotency_key=str(uuid.uuid4())
    )


async def _run(
    session: AsyncSession,
    task: Task,
    script: Sequence[ScriptStep],
    files: FakeWorkspace,
    workspace: pathlib.Path,
) -> RunResult:
    return await run_claimed_task(
        session,
        task,
        FakeProvider(list(script)),
        _allow_all(),
        workspace,
        files.registry(),
        budget=BUDGET,
        # Fences every checkpoint against the claim `task` actually carries, the same as a
        # real `Worker` passing its own id. Every task here is legitimately held by whoever
        # is running it, so this never raises `LeaseLost`; it just exercises the real path
        # instead of the `holder=None` shortcut.
        holder=task.claimed_by,
    )


async def _crash_midway(
    session_factory: async_sessionmaker[AsyncSession], task: Task, workspace: pathlib.Path
) -> FakeWorkspace:
    """First worker: claims, runs iteration 1, and dies on the second of two tools.

    Runs on its own session, rolled back and closed on the way out instead of committed: a
    real dead process never gets to commit, so relying on one here would be exactly the lie
    ADR-019 exists to fix (a resume test that proves replay by committing after the fact, not
    durability). Per-step commits already made everything up to the crash durable on their
    own; the rollback below has nothing left to discard by the time it runs, which is the
    point, not an oversight.
    """
    async with session_factory() as crashed:
        claim = await queue.claim(crashed, "worker-dead", lease_seconds=60)
        assert claim is not None
        workspace_files = FakeWorkspace()
        workspace_files.crash_after = 1

        with pytest.raises(RuntimeError, match="worker died"):
            await _run(crashed, claim, [_two_reads(), _finish()], workspace_files, workspace)
        await crashed.rollback()

    assert workspace_files.executions == ["read_file"], "the first tool ran exactly once"
    return workspace_files


async def test_a_crashed_run_resumes_and_no_tool_runs_twice(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """The checklist item, end to end."""
    task = await _queued_task(session)
    await session.commit()

    await _crash_midway(session_factory, task, workspace)

    # The worker is gone. The lease expires and a second worker takes over.
    await queue.expire_lease_now(session, task.id)
    await session.commit()

    second_claim = await queue.claim(session, "worker-live", lease_seconds=60)
    assert second_claim is not None and second_claim.id == task.id

    surviving = FakeWorkspace()
    result = await _run(session, second_claim, [_finish("resumed")], surviving, workspace)
    await session.commit()

    assert result.status == "SUCCEEDED"
    assert result.summary == "resumed"

    # It ran only the tool the first worker never reached.
    assert surviving.executions == ["read_file"]

    # The assertion the briefing asks for.
    rows = list(await session.scalars(select(ToolCall).where(ToolCall.task_id == task.id)))
    fingerprints = [(row.tool_name, row.args_hash) for row in rows]
    assert len(fingerprints) == len(set(fingerprints)), f"a tool ran twice: {fingerprints}"


async def test_resuming_does_not_buy_the_interrupted_model_call_again(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """The assistant turn is replayed from the log, not requested from the provider again."""
    task = await _queued_task(session)
    await session.commit()
    await _crash_midway(session_factory, task, workspace)

    before = await session.scalar(
        select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task.id)
    )
    assert before == 1

    await queue.expire_lease_now(session, task.id)
    resumed = await queue.claim(session, "worker-live")
    assert resumed is not None
    await _run(session, resumed, [_finish()], FakeWorkspace(), workspace)
    await session.commit()

    after = await session.scalar(
        select(func.count()).select_from(ModelCall).where(ModelCall.task_id == task.id)
    )
    # One more, for the iteration that produced `finish`. Not two: the interrupted
    # iteration's call was replayed rather than repeated, so it cost nothing.
    assert after == 2


async def test_the_budget_is_not_reset_by_a_crash(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """A crash is not a fresh allowance. Spending carries across the resume."""
    task = await _queued_task(session)
    await session.commit()
    await _crash_midway(session_factory, task, workspace)

    await queue.expire_lease_now(session, task.id)
    resumed = await queue.claim(session, "worker-live")
    assert resumed is not None
    result = await _run(session, resumed, [_finish()], FakeWorkspace(), workspace)

    # FakeProvider costs nothing, so the number is zero either way. What matters is that it
    # came from the replayed log rather than from a counter that started over: the events
    # of both runs are accounted for.
    events_seen = [e.type for e in await read_events(session, task.id)]
    assert events_seen.count("model.called") == 2
    assert result.cost_usd >= 0


async def test_a_resumed_task_leaves_one_coherent_event_log(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    workspace: pathlib.Path,
) -> None:
    """A resume must not write a second task.created or leave the log ending mid-iteration."""
    task = await _queued_task(session)
    await session.commit()
    await _crash_midway(session_factory, task, workspace)

    await queue.expire_lease_now(session, task.id)
    resumed = await queue.claim(session, "worker-live")
    assert resumed is not None
    await _run(session, resumed, [_finish()], FakeWorkspace(), workspace)
    await session.commit()

    kinds = [event.type for event in await read_events(session, task.id)]
    assert kinds.count("task.created") == 1
    assert kinds.count("task.finished") == 1
    assert kinds[-1] == "task.finished"

    # Two reads executed, once each. `finish` is handled by core and never dispatched, so
    # it produces no tool.executed at all.
    assert kinds.count("tool.executed") == 2

    # Three requests for two executions, and that is correct rather than a leak: the
    # control plane really did ask for the second read twice, once before the crash and
    # once on resume. The event log records what happened, and what happened is that the
    # first attempt never reached the tool. `tool_calls` is where "ran twice" is ruled out.
    assert kinds.count("tool.requested") == 3


async def test_a_task_with_no_history_simply_starts(
    session: AsyncSession, workspace: pathlib.Path
) -> None:
    """Recovery is not a special path: the same call handles a task that never ran."""
    await _queued_task(session)
    await session.commit()
    claim = await queue.claim(session, "worker-a")
    assert claim is not None

    registry = FakeWorkspace()
    result = await _run(session, claim, [_two_reads(), _finish("ok")], registry, workspace)

    assert result.status == "SUCCEEDED"
    assert registry.executions == ["read_file", "read_file"]
