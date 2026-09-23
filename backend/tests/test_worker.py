"""Pure budget-merge math (`core/worker.py::merge_budget`), and one end-to-end proof that
`Worker.run_once` actually uses it.

The merge is the one place a value that arrived over the API (`tasks.budget`, validated by
`api.schemas.BudgetIn` on the way in, but just JSONB once it is sitting in the column) gets
to change how much a run is allowed to spend. Everything below is checking the SECURITY
RULE from the spec: a task can only ever tighten the worker's own ceiling, never loosen it,
and a value that does not parse as a sane positive number must not crash the worker.
"""

import contextlib
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.core import queue
from warden.core.loop import Budget
from warden.core.worker import Worker, discard_orphaned_workspace_volumes, merge_budget
from warden.models import Task, User
from warden.policy.engine import Effect, Policy, Rule
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import workspace_volume_name

DEFAULT = Budget(max_iterations=10, max_usd=Decimal("0.25"), max_seconds=None)


# --- no override, or an override that widens instead of tightens ---------------------------


def test_an_empty_task_budget_keeps_the_worker_ceiling_untouched() -> None:
    assert merge_budget(DEFAULT, {}) == DEFAULT


@pytest.mark.parametrize("junk", [None, [], "not a dict", 42, True])
def test_a_task_budget_that_is_not_a_dict_is_ignored_not_a_crash(junk: object) -> None:
    """`tasks.budget` is JSONB: nothing at the database level stops a write that never went
    through `BudgetIn` from putting something other than an object in the column."""
    assert merge_budget(DEFAULT, junk) == DEFAULT


# --- max_iterations --------------------------------------------------------------------


def test_a_tighter_max_iterations_wins() -> None:
    assert merge_budget(DEFAULT, {"max_iterations": 3}).max_iterations == 3


def test_a_looser_max_iterations_is_ignored() -> None:
    assert merge_budget(DEFAULT, {"max_iterations": 999}).max_iterations == 10


@pytest.mark.parametrize("bad", [0, -1, "3", 3.5, True, None])
def test_an_invalid_max_iterations_falls_back_to_the_ceiling(bad: object) -> None:
    assert merge_budget(DEFAULT, {"max_iterations": bad}).max_iterations == 10


# --- max_usd -----------------------------------------------------------------------------


def test_a_tighter_max_usd_wins_and_comes_back_as_a_decimal() -> None:
    merged = merge_budget(DEFAULT, {"max_usd": 0.1})
    assert merged.max_usd == Decimal("0.1")
    assert isinstance(merged.max_usd, Decimal)


def test_a_looser_max_usd_is_ignored() -> None:
    assert merge_budget(DEFAULT, {"max_usd": 100}).max_usd == Decimal("0.25")


def test_a_max_usd_given_as_a_numeric_string_still_parses() -> None:
    """JSONB round-trips plain numbers as int/float, but nothing enforces that a future
    writer stuck to that; a numeric string should tighten just as well."""
    assert merge_budget(DEFAULT, {"max_usd": "0.05"}).max_usd == Decimal("0.05")


@pytest.mark.parametrize("bad", [0, -1, "not a number", float("inf"), float("nan"), True, None])
def test_an_invalid_max_usd_falls_back_to_the_ceiling(bad: object) -> None:
    assert merge_budget(DEFAULT, {"max_usd": bad}).max_usd == Decimal("0.25")


# --- max_seconds -------------------------------------------------------------------------


def test_a_task_deadline_tightens_an_unbounded_worker_ceiling() -> None:
    # DEFAULT.max_seconds is None (no deadline); a task-level value is still a tightening.
    assert merge_budget(DEFAULT, {"max_seconds": 30}).max_seconds == 30.0


def test_a_looser_task_deadline_is_ignored() -> None:
    bounded = Budget(max_seconds=60.0)
    assert merge_budget(bounded, {"max_seconds": 120}).max_seconds == 60.0


def test_a_tighter_task_deadline_wins_over_a_bounded_ceiling() -> None:
    bounded = Budget(max_seconds=60.0)
    assert merge_budget(bounded, {"max_seconds": 10}).max_seconds == 10.0


def test_a_missing_max_seconds_keeps_the_ceiling_including_when_it_is_none() -> None:
    assert merge_budget(DEFAULT, {"max_usd": 0.1}).max_seconds is None


@pytest.mark.parametrize("bad", [0, -1, "soon", True])
def test_an_invalid_max_seconds_falls_back_to_the_ceiling(bad: object) -> None:
    bounded = Budget(max_seconds=60.0)
    assert merge_budget(bounded, {"max_seconds": bad}).max_seconds == 60.0


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 10**400])
def test_a_non_finite_or_unrepresentable_max_seconds_keeps_an_unbounded_ceiling(
    bad: object,
) -> None:
    """JSONB stores any numeric, and `json.loads` hands a 400-digit one back as a Python int
    that `float()` cannot represent: it must degrade to "not set" like any other bad value,
    not raise out of `Worker.run_once` and strand the claimed task RUNNING until its lease
    runs out, only for the next worker to claim it and crash the same way."""
    assert merge_budget(DEFAULT, {"max_seconds": bad}).max_seconds is None


# --- Worker.run_once wiring, end to end -----------------------------------------------------
#
# Needs a real container (`build_registry` gives the loop a real `list_files` tool bound to
# a real `Sandbox`, and `Worker.run_once` creates one unconditionally): marked `sandbox` and
# not run as part of this track's own verification, same convention as test_durability.py.
# The pure tests above are what this track ran and watched go red -> green; this one is
# reported instead of executed, per the wave's instructions, and the final verification
# pass (which owns the Docker tests on this machine right now) runs it for real.


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Same skip-locally-fail-in-CI pattern as test_sandbox.py / test_durability.py."""
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture
async def _empty_tasks_and_users(session: AsyncSession) -> AsyncIterator[None]:
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()
    yield
    await session.execute(text("TRUNCATE tasks, users CASCADE"))
    await session.commit()


@pytest.fixture
def _repo_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8", newline="\n")
    return root


def _allow_all() -> Policy:
    return Policy(
        [Rule(id="allow-all", effect=Effect.ALLOW, when={"tool": "*"})],
        default=Effect.DENY,
        policy_hash="test",
    )


@pytest.mark.sandbox
async def test_run_once_applies_the_tightened_per_task_budget(
    docker_available: None,
    _empty_tasks_and_users: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    _repo_workspace: pathlib.Path,
) -> None:
    """Black-box, not a mock: a task whose stored `budget` tightens `max_iterations` to 1
    has to stop `TIMED_OUT` after exactly one iteration through a *real* `Worker.run_once`,
    even though the worker's own default ceiling (10) would happily run the second step
    below. If `merge_budget` were not wired into `run_once`, this same script would instead
    finish `SUCCEEDED` on its second turn.
    """
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    await queue.enqueue(
        session,
        user_id=user.id,
        spec="budget probe",
        idempotency_key=str(uuid.uuid4()),
        budget={"max_iterations": 1},
    )
    await session.commit()

    list_files = ProviderToolCall(id="c1", name="list_files", arguments={})
    finish = ProviderToolCall(id="c2", name="finish", arguments={"summary": "done"})

    def provider_factory() -> FakeProvider:
        return FakeProvider([ScriptStep(tool_calls=[list_files]), ScriptStep(tool_calls=[finish])])

    worker = Worker(session_factory, provider_factory, _allow_all(), _repo_workspace)
    result = await worker.run_once()

    assert result is not None
    assert result.status == "TIMED_OUT"
    assert result.iterations == 1


# --- the orphaned-workspace-volume janitor (maintenance track, item 2) ----------------------
#
# ADR-022's open item: cancelling a WAITING_APPROVAL task never runs Worker.run_once's own
# `finally` (there is no worker holding it), so its workspace volume is never discarded. Real
# Docker volumes, not a mock: whether a volume with a given label still exists afterward is
# exactly the fact this janitor exists to get right.


@pytest.mark.sandbox
async def test_the_janitor_discards_terminal_and_missing_tasks_but_never_a_live_one(
    docker_available: None,
    _empty_tasks_and_users: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import docker as docker_sdk

    client = docker_sdk.from_env()
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()

    def _task(status: str) -> Task:
        task = Task(
            user_id=user.id, spec="janitor probe", idempotency_key=str(uuid.uuid4()), status=status
        )
        session.add(task)
        return task

    terminal_task = _task("SUCCEEDED")
    live_task = _task("WAITING_APPROVAL")
    await session.commit()
    missing_task_id = uuid.uuid4()

    task_ids = [terminal_task.id, live_task.id, missing_task_id]
    volumes = [
        client.volumes.create(
            name=workspace_volume_name(str(tid)),
            labels={"warden.sandbox": "1", "warden.task": str(tid)},
        )
        for tid in task_ids
    ]
    try:
        await discard_orphaned_workspace_volumes(session_factory)

        def exists(task_id: uuid.UUID) -> bool:
            return bool(
                client.volumes.list(filters={"label": f"warden.task={task_id}"})
            )

        assert not exists(terminal_task.id), "a terminal task's volume must be discarded"
        assert not exists(missing_task_id), "a volume for a task that no longer exists must go"
        assert exists(live_task.id), "a WAITING_APPROVAL task's volume must survive: it resumes onto it"
    finally:
        for volume in volumes:
            with contextlib.suppress(docker_sdk.errors.NotFound, docker_sdk.errors.APIError):
                volume.remove(force=True)
