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
import os
import pathlib
import signal
import socket
import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.config import get_settings
from warden.core import events, queue
from warden.core.loop import Budget, RunResult, run_task
from warden.core.replay import ResumeState, rebuild
from warden.db import make_engine, make_session_factory
from warden.models import Task
from warden.policy.engine import Policy, load_policy, never_readable
from warden.providers.base import ModelProvider
from warden.sandbox.docker import Sandbox, SandboxProfile, discard_workspace_volume
from warden.tools.registry import ToolRegistry
from warden.tools.sandboxed import build_registry

HEARTBEAT_FRACTION = 0.4
IDLE_POLL_SECONDS = 1.0

# States a task does not come back from. Only then is its workspace thrown away: a task
# merely between workers still needs whatever it changed before it was interrupted.
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXCEEDED"})

# Resolved at import: touching the filesystem inside the async entry point would block the
# event loop, and these never change while the process runs.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKSPACE = REPO_ROOT / "examples" / "target-repo"
POLICY_FILE = REPO_ROOT / "policies" / "default.yaml"
DEMO_SCRIPT = REPO_ROOT / "examples" / "demo-script.yaml"


def worker_id() -> str:
    """Identifies the holder of a lease. Host and pid make a dead one recognisable."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


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
        async with session_factory() as session:
            alive = await queue.heartbeat(session, task_id, holder, lease_seconds=lease_seconds)
            await session.commit()
        if not alive:
            # The task was reclaimed by someone else. Stop beating; the run will finish and
            # find it no longer owns the row.
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
        lease_lost = False
        try:
            async with self._sessions() as session:
                claimed = await session.get(Task, task_id)
                assert claimed is not None
                try:
                    result = await run_claimed_task(
                        session,
                        claimed,
                        self._provider_factory(),
                        self._policy,
                        self._workspace,
                        build_registry(sandbox),
                        budget=self._budget,
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
            # The container always goes. The workspace only goes when the task is over: a
            # task between workers still needs what it changed before it was interrupted.
            await sandbox.destroy()
            if not lease_lost:
                async with self._sessions() as session:
                    finished = await session.get(Task, task_id)
                    if finished is not None and finished.status in TERMINAL_STATUSES:
                        await asyncio.to_thread(discard_workspace_volume, str(task_id))

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            result = await self.run_once()
            if result is None:
                # Polling, not LISTEN/NOTIFY. ponytail: a one second poll on a queue this
                # size costs nothing measurable; swap for LISTEN/NOTIFY if latency between
                # submission and pickup ever shows up in the metrics.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=IDLE_POLL_SECONDS)


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
