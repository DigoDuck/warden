"""A process that claims tasks and runs them.

Separate from the API on purpose: a long agent run must not hold a request open, and the
two scale for different reasons. Several workers can run at once, and `FOR UPDATE SKIP
LOCKED` in `queue.py` is what keeps them from fighting over the same row.

Crash recovery has no special path. A worker that dies simply stops extending its lease;
when the lease expires another worker claims the task and rebuilds the conversation from
the event log. Recovery is the normal claim path meeting a task that already has history.
"""

import argparse
import asyncio
import contextlib
import math
import os
import pathlib
import signal
import socket
import uuid
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.config import get_settings
from warden.core import cancel, events, queue
from warden.core.loop import Budget, RunResult, run_task
from warden.core.replay import ResumeState, rebuild
from warden.db import make_engine, make_session_factory
from warden.models import Task
from warden.policy.engine import Policy, load_policy, never_readable
from warden.providers.base import ModelProvider
from warden.sandbox.docker import (
    Sandbox,
    SandboxProfile,
    discard_workspace_volume,
    list_task_ids_with_workspace_volumes,
)
from warden.tools.registry import ToolRegistry
from warden.tools.sandboxed import build_registry

HEARTBEAT_FRACTION = 0.4
IDLE_POLL_SECONDS = 1.0
# Fast on purpose, unlike the heartbeat: a lease is minutes long, but a cancelled task has
# to stop within a few seconds of the request, not within a fraction of the lease.
CANCEL_POLL_SECONDS = 1.0
# What a single poll from `_beat` or `_watch_cancel` may hit and survive: the connection
# dropping (OSError) or the database refusing one statement (SQLAlchemyError wraps asyncpg).
# Anything else is a bug and still propagates.
# ponytail: retried silently, there is no logging yet; log it once structlog lands (week 7).
_TRANSIENT_DB_ERRORS = (OSError, SQLAlchemyError)

# States a task does not come back from. Only then is its workspace thrown away: a task
# merely between workers still needs whatever it changed before it was interrupted.
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXCEEDED"})

# How often run_forever sweeps for orphaned workspace volumes, on top of the one sweep at
# worker start. Modest on purpose: an orphaned volume costs disk, not correctness (nothing
# reads it, nothing depends on it disappearing quickly), so there is no reason to check more
# often than a claim's own idle poll.
JANITOR_INTERVAL_SECONDS = 300.0

# Resolved at import: touching the filesystem inside the async entry point would block the
# event loop, and these never change while the process runs.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKSPACE = REPO_ROOT / "examples" / "target-repo"
POLICY_FILE = REPO_ROOT / "policies" / "default.yaml"
DEMO_SCRIPT = REPO_ROOT / "examples" / "demo-script.yaml"


def worker_id() -> str:
    """Identifies the holder of a lease. Host and pid make a dead one recognisable."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _tightened_int(ceiling: int, value: Any) -> int:
    """`min(ceiling, value)`, but only when `value` is actually a usable positive int.
    Anything else keeps `ceiling`: a task-level budget only ever narrows a limit, never
    invents one out of garbage."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return ceiling
    return min(ceiling, value)


def _tightened_decimal(ceiling: Decimal, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return ceiling
    try:
        # str(value) first: Decimal(0.1) carries float's own binary imprecision
        # (0.1000000000000000055511151231257827021181583404541015625); Decimal(str(0.1))
        # does not. Cheap and exact for the int/str cases too.
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return ceiling
    if not parsed.is_finite() or parsed <= 0:
        return ceiling
    return min(ceiling, parsed)


def _tightened_seconds(ceiling: float | None, value: Any) -> float | None:
    """Like the other two, except `ceiling=None` means "unbounded", not "zero": a task
    deadline still tightens it, it just has nothing to be compared against."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return ceiling
    try:
        parsed = float(value)
    except OverflowError:
        # An int too large for a float (JSONB keeps any numeric): no real deadline anyway.
        return ceiling
    if not math.isfinite(parsed) or parsed <= 0:
        return ceiling
    return parsed if ceiling is None else min(ceiling, parsed)


def merge_budget(worker_ceiling: Budget, task_budget: object) -> Budget:
    """Tighten `worker_ceiling` with the per-task limits stored in `tasks.budget`.

    SECURITY RULE: `task_budget` reflects a value an API caller chose. `api.schemas
    .BudgetIn` validates it on the way in, but the column is JSONB and nothing at the
    database level stops other code from writing something else into it later, so this
    function is the one place that value gets to change what a run is allowed to spend.
    It therefore only ever narrows a field (`min()` of the two, or the worker's own value
    when the task did not set one), never widens one, and a value that fails to parse as a
    positive, finite number of the right shape is treated exactly like "the task did not
    set this field" instead of raising: a foreign write to this column, or a bug upstream,
    degrades to "no extra limit from the task" rather than taking the whole run down. The
    alternative (fail the task on a bad value) was rejected for the same reason `api`
    already returns 422 instead of 500 on a malformed body elsewhere in this project: a
    caller-shaped problem should not read as a control-plane crash. See ADR-021's addendum.
    """
    if not isinstance(task_budget, dict):
        return worker_ceiling

    return Budget(
        max_iterations=_tightened_int(
            worker_ceiling.max_iterations, task_budget.get("max_iterations")
        ),
        max_usd=_tightened_decimal(worker_ceiling.max_usd, task_budget.get("max_usd")),
        max_seconds=_tightened_seconds(worker_ceiling.max_seconds, task_budget.get("max_seconds")),
    )


async def _beat(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    holder: str,
    lease_seconds: int,
) -> None:
    """Extend the lease while the task runs.

    On its own session: the run's transaction can stay open for a long time, and a
    heartbeat that could not commit until the run finished would defeat the point.
    """
    interval = max(1.0, lease_seconds * HEARTBEAT_FRACTION)
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                alive = await queue.heartbeat(session, task_id, holder, lease_seconds=lease_seconds)
                await session.commit()
        except _TRANSIENT_DB_ERRORS:
            # One failed beat costs one interval; the lease is several intervals long. Dying
            # here instead would let `run_once`'s `finally` re-raise the error, skip
            # `sandbox.destroy()` and take `run_forever` down over a blip.
            continue
        if not alive:
            # The task was reclaimed by someone else. Stop beating; the run will finish and
            # find it no longer owns the row.
            return


async def _watch_cancel(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID, sandbox: Sandbox
) -> None:
    """Kill the sandbox the moment a cancel is requested, instead of waiting for whatever
    deadline the in-flight command was given.

    A sibling to `_beat`, not folded into it, for the same reason it polls faster: the two
    exist to answer different questions on different clocks. `_beat` extends a lease that is
    minutes long; this has to notice within a few seconds. `core/loop.py::_check_stoppable`
    is what actually ends the *task* once it notices the marker; this only makes sure a long
    `run_command`/`run_tests` already in flight does not sit there until its own timeout
    before the loop gets a turn to check anything. Cancelled the same way `_beat` is, from
    `run_once`'s `finally`, once the task has actually finished.
    """
    while True:
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        try:
            async with session_factory() as session:
                requested = await cancel.is_requested(session, task_id)
        except _TRANSIENT_DB_ERRORS:
            # Same reasoning as `_beat`: one bad poll costs one poll. A watcher that died
            # here would stop killing the sandbox for the rest of the run.
            continue
        if requested:
            await sandbox.kill_for_cancel()
            return


async def run_claimed_task(
    session: AsyncSession,
    task: Task,
    provider: ModelProvider,
    policy: Policy,
    workspace: pathlib.Path,
    registry: ToolRegistry,
    *,
    budget: Budget | None = None,
    holder: str | None = None,
) -> RunResult:
    """Run a task from wherever it left off.

    A task with no prior events starts fresh; one with history resumes. The caller does not
    have to know which, and neither does the loop. `holder` fences every checkpoint the run
    makes (ADR-019): the `Worker` below passes its own id, so a run that outlives its lease
    stops with `LeaseLost` instead of writing over the next owner.
    """
    history = await events.read_events(session, task.id)
    resume: ResumeState | None = None
    if history:
        resume = rebuild(history)

    return await run_task(
        session,
        task,
        provider,
        registry,
        policy,
        workspace=workspace,
        budget=budget,
        resume=resume,
        holder=holder,
    )


async def discard_orphaned_workspace_volumes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Throw away every task-scoped workspace volume nothing will ever resume onto again.

    ADR-022's open item: cancelling a WAITING_APPROVAL task marks it CANCELLED with no
    worker behind it to run `Worker.run_once`'s own `finally`, so that task's volume is
    never discarded there. This is the only other place that ever calls
    `discard_workspace_volume`.

    Race safety: every candidate task's status is read in ONE query, up front, before this
    discards anything, and a task is only ever discarded if that single read already found it
    terminal (`TERMINAL_STATUSES`) or missing a row entirely. Nothing in this state machine
    ever moves a task's status *out of* a terminal one (`core/queue.py`, `core/cancel.py` and
    `core/approvals.py` only ever move a row *into* one), so whatever this reads as terminal
    stays terminal forever — there is no later moment at which a volume this call decided to
    discard could still belong to a task that resumes onto it. A task read as QUEUED, RUNNING
    or WAITING_APPROVAL is left alone unconditionally: even a stale read only costs one more
    sweep before an already-terminal task's volume is noticed, never a live task's volume
    disappearing under it.
    """
    try:
        task_ids = await asyncio.to_thread(list_task_ids_with_workspace_volumes)
    except _TRANSIENT_DB_ERRORS:
        return
    if not task_ids:
        return

    try:
        async with session_factory() as session:
            uuids = [uuid.UUID(task_id) for task_id in task_ids]
            rows = (
                await session.execute(select(Task.id, Task.status).where(Task.id.in_(uuids)))
            ).all()
    except _TRANSIENT_DB_ERRORS:
        return

    status_by_id = {str(task_id): status for task_id, status in rows}
    for task_id in task_ids:
        status = status_by_id.get(task_id)
        if status is not None and status not in TERMINAL_STATUSES:
            continue  # QUEUED, RUNNING or WAITING_APPROVAL: a resume still needs this volume.
        with contextlib.suppress(*_TRANSIENT_DB_ERRORS):
            await asyncio.to_thread(discard_workspace_volume, task_id)


class Worker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider_factory: Callable[[], ModelProvider],
        policy: Policy,
        workspace: pathlib.Path,
        *,
        lease_seconds: int = queue.DEFAULT_LEASE_SECONDS,
        budget: Budget | None = None,
        profile: SandboxProfile | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider_factory = provider_factory
        self._policy = policy
        self._workspace = workspace
        self._lease_seconds = lease_seconds
        self._budget = budget
        self._profile = profile or SandboxProfile()
        self.id = worker_id()
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask the worker to finish the current task and then exit."""
        self._stopping.set()

    async def run_once(self) -> RunResult | None:
        """Claim one task and run it. None means the queue was empty."""
        async with self._sessions() as session:
            task = await queue.claim(session, self.id, lease_seconds=self._lease_seconds)
            await session.commit()
            if task is None:
                return None
            task_id = task.id

        beat = asyncio.create_task(_beat(self._sessions, task_id, self.id, self._lease_seconds))
        # The workspace volume is named after the task, so a sandbox created here attaches
        # to whatever a previous worker left behind rather than starting from a fresh copy.
        # `Sandbox.create` also force-removes any container still labelled for this task
        # (sandbox/docker.py): a worker that was killed outright never runs this `finally`
        # block, so its container could otherwise sit there orphaned, possibly still running
        # a command against the volume this one is about to attach to.
        sandbox = await Sandbox.create(
            self._profile,
            self._workspace,
            task_id=str(task_id),
            # What a deny rule names never enters the container (ADR-018).
            exclude=never_readable(self._policy),
        )
        watcher = asyncio.create_task(_watch_cancel(self._sessions, task_id, sandbox))
        lease_lost = False
        try:
            async with self._sessions() as session:
                claimed = await session.get(Task, task_id)
                assert claimed is not None
                # `self._budget` is this worker's own ceiling (`None` means `run_task`'s
                # default, `Budget()`); `claimed.budget` is whatever the API caller asked
                # for on this one task. `merge_budget` decides which of the two wins per
                # field, and only ever in the caller's favour when it is stricter.
                budget = merge_budget(self._budget or Budget(), claimed.budget)
                try:
                    result = await run_claimed_task(
                        session,
                        claimed,
                        self._provider_factory(),
                        self._policy,
                        self._workspace,
                        build_registry(sandbox),
                        budget=budget,
                        holder=self.id,
                    )
                except queue.LeaseLost:
                    # Another worker already reclaimed this task. Every checkpoint fences
                    # its own commit (ADR-019), so nothing this run wrote after losing the
                    # lease survived; stop quietly rather than keep spending on a task that
                    # is no longer ours to finish, and let the `finally` below leave the
                    # workspace alone for whoever owns it now.
                    lease_lost = True
                    return None
                await session.commit()
                return result
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            # The container always goes. The workspace only goes when the task is over: a
            # task between workers still needs what it changed before it was interrupted.
            await sandbox.destroy()
            if not lease_lost:
                async with self._sessions() as session:
                    finished = await session.get(Task, task_id)
                    if finished is not None and finished.status in TERMINAL_STATUSES:
                        await asyncio.to_thread(discard_workspace_volume, str(task_id))

    async def run_forever(self) -> None:
        # Once at start (an orphan from before this process existed has been waiting
        # regardless), then every JANITOR_INTERVAL_SECONDS. A monotonic clock, not
        # wall-clock: immune to the system clock stepping backward or forward mid-run.
        await discard_orphaned_workspace_volumes(self._sessions)
        next_sweep = asyncio.get_running_loop().time() + JANITOR_INTERVAL_SECONDS

        while not self._stopping.is_set():
            result = await self.run_once()
            if result is None:
                # Polling, not LISTEN/NOTIFY. ponytail: a one second poll on a queue this
                # size costs nothing measurable; swap for LISTEN/NOTIFY if latency between
                # submission and pickup ever shows up in the metrics.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=IDLE_POLL_SECONDS)
            if asyncio.get_running_loop().time() >= next_sweep:
                await discard_orphaned_workspace_volumes(self._sessions)
                next_sweep = asyncio.get_running_loop().time() + JANITOR_INTERVAL_SECONDS


async def main() -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--script",
        type=pathlib.Path,
        default=DEMO_SCRIPT,
        help="FakeProvider YAML script to replay instead of the demo one.",
    )
    # First needed by tests/test_durability.py: the default policy has no allow rule for a
    # long-running command, on purpose, and loosening it so a probe can run one would loosen
    # every other task along with it. Not a back door: `task.created` records the
    # `policy_hash` of whatever was loaded, so a run under another policy says so in its log.
    parser.add_argument(
        "--policy",
        type=pathlib.Path,
        default=POLICY_FILE,
        help="Policy YAML file to load instead of the default.",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)

    def provider_factory() -> ModelProvider:
        from warden.providers.fake import FakeProvider

        return FakeProvider.from_yaml(args.script)

    worker = Worker(sessions, provider_factory, load_policy(args.policy), WORKSPACE)

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, signame), worker.stop)

    print(f"worker {worker.id} waiting for tasks")
    try:
        await worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
    print("worker stopped")
    await engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
