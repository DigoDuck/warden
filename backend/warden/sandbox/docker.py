"""A hardened container to run tool calls in.

Briefing section 21, and the sentence this module exists to honour: **a container is not a
secure sandbox by default**. Shared kernel, default capabilities, networking on, root
inside. A sandbox is the set of restrictions you apply *and test*, which is why the real
deliverable next to this file is `tests/test_sandbox.py`.

The workspace is copied in rather than bind mounted. Copying makes the host filesystem
irrelevant, which matters on Windows where path translation and ownership semantics of a
bind mount are mushy, and it means the workspace dies with the container.
"""

import asyncio
import contextlib
import io
import pathlib
import tarfile
import time
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import NotFound
from docker.models.containers import Container

from warden.tools.workspace import is_ignored

# A uid that exists neither in the image nor on the host: if a file ever escaped, it would
# not be owned by a real account on either side.
SANDBOX_UID = 10001
# How long to wait for a killed container to actually be gone.
KILL_GRACE_SECONDS = 10
# The volume mounts at MOUNT_ROOT and the workspace is a directory inside it. That extra
# level is not decoration: a named volume's root always mounts owned by root with mode 755,
# and the sandbox user cannot create files in it. The tar creates the subdirectory with the
# right ownership, so the agent can actually write where it works.
MOUNT_ROOT = "/sandbox"
WORKSPACE = f"{MOUNT_ROOT}/workspace"


class SandboxError(RuntimeError):
    """The sandbox could not be created, or a command could not be run."""


class CommandTimeout(SandboxError):
    """A command ran past its deadline and the container was killed."""


@dataclass(frozen=True)
class SandboxProfile:
    """What a sandbox is allowed to be.

    A frozen dataclass rather than the `sandbox_profiles` table of section 13: nothing reads
    a profile from the database yet, and a table nobody queries is the same mistake as
    shipping `audit_log` with no writer. The table arrives when the registry UI edits these.
    """

    name: str = "python-restricted"
    image: str = "python:3.13-slim"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 128
    tmp_size_mb: int = 16
    # Present so the field is explicit rather than implied. Turning it on is not supported:
    # the design is that the sandbox has no network and every external action is a gateway
    # tool run by the control plane (ADR-004).
    network: str = "none"
    env: dict[str, str] = field(default_factory=dict)


def _workspace_tar(workspace: pathlib.Path) -> io.BytesIO:
    """Pack the workspace into a tar owned by the sandbox user.

    Ownership is set here rather than fixed up afterwards with chown: the container has no
    CAP_CHOWN, so a chown inside would fail, and running the fixup as root would mean
    starting the container as root.
    """
    stream = io.BytesIO()
    root = workspace.resolve()
    with tarfile.open(fileobj=stream, mode="w") as tar:

        def owned_by_sandbox(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = SANDBOX_UID
            info.uname = info.gname = "sandbox"
            return info

        # The workspace directory itself is the first entry, so extraction creates it owned
        # by the sandbox user instead of inheriting the volume root's root-owned 755.
        workspace_dir = tarfile.TarInfo("workspace")
        workspace_dir.type = tarfile.DIRTYPE
        workspace_dir.mode = 0o755
        tar.addfile(owned_by_sandbox(workspace_dir))

        for path in sorted(root.rglob("*")):
            # Same exclusion list the listing tool uses, imported rather than repeated: two
            # copies would drift, and the agent would see files the sandbox does not have.
            if is_ignored(pathlib.PurePosixPath(path.relative_to(root).as_posix())):
                continue
            arcname = f"workspace/{path.relative_to(root).as_posix()}"
            tar.add(path, arcname=arcname, filter=owned_by_sandbox)
    stream.seek(0)
    return stream


def workspace_volume_name(task_id: str) -> str:
    return f"warden-workspace-{task_id}"


def _workspace_volume(client: docker.DockerClient, task_id: str | None) -> tuple[Any, bool]:
    """The task's workspace volume, and whether this call created it.

    Anonymous when there is no task id, which is the one-off case in tests: nothing is meant
    to survive, so there is nothing to attach to.
    """
    if task_id is None:
        return client.volumes.create(labels={"warden.sandbox": "1"}), True

    name = workspace_volume_name(task_id)
    try:
        return client.volumes.get(name), False
    except NotFound:
        return (
            client.volumes.create(
                name=name, labels={"warden.sandbox": "1", "warden.task": task_id}
            ),
            True,
        )


def discard_workspace_volume(task_id: str, *, client: docker.DockerClient | None = None) -> None:
    """Remove a task's workspace without needing a live sandbox to do it."""
    client = client or docker.from_env()
    with contextlib.suppress(NotFound, docker.errors.APIError):
        client.volumes.get(workspace_volume_name(task_id)).remove(force=True)


@dataclass
class ExecResult:
    exit_code: int
    output: str


class Sandbox:
    """One container, hardened, holding one task's workspace."""

    def __init__(
        self,
        container: Container,
        client: docker.DockerClient,
        volume_name: str,
        *,
        task_scoped: bool,
    ) -> None:
        self._container = container
        self._client = client
        self._volume_name = volume_name
        # A task-scoped volume outlives its container because another worker may attach to
        # it. An anonymous one cannot be re-attached by anyone, so keeping it is pure leak.
        self._task_scoped = task_scoped

    @property
    def id(self) -> str:
        return str(self._container.id)

    @classmethod
    async def create(
        cls,
        profile: SandboxProfile,
        workspace: pathlib.Path,
        *,
        task_id: str | None = None,
        client: docker.DockerClient | None = None,
    ) -> "Sandbox":
        """Start a container holding `task_id`'s workspace.

        With a `task_id` the workspace belongs to the task rather than to this container:
        a second sandbox for the same task attaches to what the first one left behind.
        That is what makes resume after a crash honest. Without it, a task that wrote a
        file, died and resumed would get a fresh copy of the original workspace while its
        own event log told the model the file was there, so the model would reason about a
        world the disk had stopped agreeing with.
        """
        client = client or docker.from_env()
        return await asyncio.to_thread(cls._create_sync, profile, workspace, client, task_id)

    @staticmethod
    def _create_sync(
        profile: SandboxProfile,
        workspace: pathlib.Path,
        client: docker.DockerClient,
        task_id: str | None,
    ) -> "Sandbox":
        volume, is_new = _workspace_volume(client, task_id)
        container = client.containers.run(
            image=profile.image,
            # Idles so that commands can be exec'd into it. The container has no entrypoint
            # work of its own: it exists to be a contained filesystem and process namespace.
            command=["sleep", "infinity"],
            detach=True,
            user=f"{SANDBOX_UID}:{SANDBOX_UID}",
            working_dir=WORKSPACE,
            environment=profile.env,
            # --- the hardening, every line of which has a test ---
            network_mode=profile.network,
            read_only=True,
            # A named volume, not tmpfs. Docker's archive endpoint refuses to write into a
            # container whose rootfs is read-only unless the target is a real mount, and a
            # tmpfs target silently swallows the upload: put_archive reports success while
            # writing underneath the mount, where nothing can read it. Both were verified
            # against the daemon rather than assumed.
            volumes={volume.name: {"bind": MOUNT_ROOT, "mode": "rw"}},
            tmpfs={"/tmp": f"rw,size={profile.tmp_size_mb}m,mode=1777"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=profile.pids_limit,
            mem_limit=f"{profile.memory_mb}m",
            nano_cpus=int(profile.cpus * 1_000_000_000),
            # The workspace volume above is the only mount. No bind mount from the host,
            # and in particular never the Docker socket: mounting it would hand the agent
            # root on the host.
            labels={"warden.sandbox": "1", "warden.profile": profile.name},
        )

        try:
            # The mount exists only once the container is running, so the copy happens after.
            # An existing task volume already holds the workspace, including whatever the
            # task changed before it was interrupted. Copying over it would undo that.
            if is_new:
                container.put_archive(MOUNT_ROOT, _workspace_tar(workspace).getvalue())
        except BaseException:
            # Anything failing past this point must not leave the container or the volume
            # behind: the caller never receives a Sandbox, so nobody is left to call
            # destroy(). This is not hypothetical. An earlier version of this module raised
            # here on every call and left fourteen containers running.
            with contextlib.suppress(Exception):
                container.remove(force=True)
            if is_new:
                # Only if this call created it. An existing task volume holds work that
                # predates this container and must survive a failure to start one.
                with contextlib.suppress(Exception):
                    volume.remove(force=True)
            raise
        return Sandbox(container, client, volume.name, task_scoped=task_id is not None)

    # Named kill_after, not timeout, and the difference is the behaviour. A timeout says
    # "stop waiting", which for a container means abandoning a command that keeps running.
    # This says "kill the container at this point", which is the only thing that stops it.
    async def exec(self, command: list[str], *, kill_after: float = 60.0) -> ExecResult:
        """Run a command inside the container, or kill the container trying.

        `exec_run` is blocking, so it runs in a thread. `asyncio.wait_for` alone would only
        abandon the wait while the command kept running, so the deadline is enforced by
        killing the container, which is also how cooperative cancellation will stop a long
        command later.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._exec_sync, command), timeout=kill_after
            )
        except TimeoutError:
            await asyncio.to_thread(self._kill_sync)
            raise CommandTimeout(
                f"command {command!r} exceeded {kill_after}s; the container was killed"
            ) from None

    def _exec_sync(self, command: list[str]) -> ExecResult:
        result = self._container.exec_run(command, user=str(SANDBOX_UID), workdir=WORKSPACE)
        raw = result.output or b""
        return ExecResult(exit_code=int(result.exit_code), output=raw.decode("utf-8", "replace"))

    def _kill_sync(self) -> None:
        # Already gone or already stopped; either way the deadline is satisfied.
        with contextlib.suppress(NotFound, docker.errors.APIError):
            self._container.kill()

        # kill() only sends the signal, so the caller could otherwise get CommandTimeout
        # while the process is briefly still alive. Under load that window is wide enough
        # to matter, and it made the deadline test flaky in a full suite run.
        #
        # Polled rather than container.wait(timeout=...): that timeout is the HTTP read
        # timeout on the daemon socket, so a busy daemon raises a connection error instead
        # of reporting that the container is still up. Own deadline, own semantics.
        deadline = time.monotonic() + KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                self._container.reload()
            except (NotFound, docker.errors.APIError):
                return
            if not self._container.attrs.get("State", {}).get("Running"):
                return
            time.sleep(0.05)
        # Still up after the grace period. destroy() removes it by force, and the caller
        # already gets CommandTimeout, so this is reported rather than raised over it.

    def inspect(self) -> dict[str, Any]:
        """Raw daemon JSON. Typed as Any because that is what it is: the shape belongs to
        the Docker API, not to this codebase, and pretending otherwise would mean writing a
        model that drifts from the daemon without anyone noticing."""
        self._container.reload()
        return dict(self._container.attrs)

    async def destroy(self) -> None:
        await asyncio.to_thread(self._destroy_sync)

    def _destroy_sync(self) -> None:
        # The container always goes. A task's workspace stays, because discarding it here
        # would destroy the work of a task that is merely between workers; only a task in a
        # terminal state should call `discard_workspace`. An anonymous workspace has no such
        # future and goes with the container.
        with contextlib.suppress(NotFound):
            self._container.remove(force=True)
        if not self._task_scoped:
            self._discard_sync()

    async def discard_workspace(self) -> None:
        """Delete the workspace volume. Only for a task that will not run again."""
        await asyncio.to_thread(self._discard_sync)

    def _discard_sync(self) -> None:
        with contextlib.suppress(NotFound, docker.errors.APIError):
            self._client.volumes.get(self._volume_name).remove(force=True)


async def run_in_sandbox(
    profile: SandboxProfile,
    workspace: pathlib.Path,
    command: list[str],
    *,
    kill_after: float = 60.0,
) -> ExecResult:
    """Create, run one command, destroy. Convenience for one-shot use and for tests."""
    sandbox = await Sandbox.create(profile, workspace)
    try:
        return await sandbox.exec(command, kill_after=kill_after)
    finally:
        await sandbox.destroy()
