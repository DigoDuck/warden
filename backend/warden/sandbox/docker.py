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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import ImageNotFound, NotFound
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


def _default_env() -> dict[str, str]:
    """Environment every sandbox gets unless a profile overrides it.

    The image user (10001) has no passwd entry, so tools that assume a real home directory
    (ruff, mypy, pip's own cache) need one pointed somewhere writable; the rootfs is
    read-only everywhere except the workspace volume and /tmp. Caches are kept out of the
    workspace so they never show up in a listing or a diff the agent produces.
    """
    return {
        "HOME": "/tmp",
        "RUFF_CACHE_DIR": "/tmp/ruff",
        "MYPY_CACHE_DIR": "/tmp/mypy",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@dataclass(frozen=True)
class SandboxProfile:
    """What a sandbox is allowed to be.

    A frozen dataclass rather than the `sandbox_profiles` table of section 13: nothing reads
    a profile from the database yet, and a table nobody queries is the same mistake as
    shipping `audit_log` with no writer. The table arrives when the registry UI edits these.
    """

    name: str = "python-restricted"
    # Built by `make sandbox-image` (sandbox-images/python/Dockerfile), not pulled: the
    # sandbox has no network, so whatever a tool needs has to be baked in ahead of time.
    image: str = "warden-sandbox:dev"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 128
    tmp_size_mb: int = 16
    # Present so the field is explicit rather than implied. Turning it on is not supported:
    # the design is that the sandbox has no network and every external action is a gateway
    # tool run by the control plane (ADR-004).
    network: str = "none"
    env: dict[str, str] = field(default_factory=_default_env)


def _owned_by_sandbox(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """A tar filter that reassigns ownership to the sandbox user.

    Shared by every tar this module builds: the container has no CAP_CHOWN, so a chown after
    extraction would fail, and running the fixup as root would mean starting the container
    as root. Ownership has to be set on the way in.
    """
    info.uid = info.gid = SANDBOX_UID
    info.uname = info.gname = "sandbox"
    return info


def _workspace_tar(
    workspace: pathlib.Path, exclude: Callable[[str], bool] | None = None
) -> io.BytesIO:
    """Pack the workspace into a tar owned by the sandbox user.

    `exclude` takes a workspace-relative POSIX path and says whether the file must stay out
    of the container. It is how secrets are kept from code the agent gets to execute: see
    `never_readable` in the policy engine and ADR-018. This module stays ignorant of policy
    on purpose and only takes a predicate.
    """
    stream = io.BytesIO()
    root = workspace.resolve()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        # The workspace directory itself is the first entry, so extraction creates it owned
        # by the sandbox user instead of inheriting the volume root's root-owned 755.
        workspace_dir = tarfile.TarInfo("workspace")
        workspace_dir.type = tarfile.DIRTYPE
        workspace_dir.mode = 0o755
        tar.addfile(_owned_by_sandbox(workspace_dir))

        for path in sorted(root.rglob("*")):
            # Same exclusion list the listing tool uses, imported rather than repeated: two
            # copies would drift, and the agent would see files the sandbox does not have.
            relative = path.relative_to(root).as_posix()
            if is_ignored(pathlib.PurePosixPath(relative)):
                continue
            if exclude is not None and exclude(relative):
                continue
            # recursive=False is load-bearing. rglob already yields every descendant, and
            # tarfile's default is to add a directory together with everything under it,
            # which does not go through the two checks above. With the default, skipping
            # `config/.env` here meant nothing, because adding `config` had already carried
            # it in, and a nested `pkg/node_modules` got in the same way.
            tar.add(
                path, arcname=f"workspace/{relative}", recursive=False, filter=_owned_by_sandbox
            )
    stream.seek(0)
    return stream


def _file_tar(relative_path: str, data: bytes) -> io.BytesIO:
    """Pack one file, plus a directory entry for each parent, into a tar.

    The directory entries are not decoration: `put_archive` extracts into the mount root,
    and a parent directory that does not already exist there (`.warden/` on the first call,
    for instance) makes the whole archive fail to extract without one.
    """
    stream = io.BytesIO()
    parts = pathlib.PurePosixPath(relative_path).parts
    with tarfile.open(fileobj=stream, mode="w") as tar:
        built = pathlib.PurePosixPath()
        for part in parts[:-1]:
            built = built / part
            directory = tarfile.TarInfo(str(built))
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            tar.addfile(_owned_by_sandbox(directory))

        info = tarfile.TarInfo(relative_path)
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(_owned_by_sandbox(info), io.BytesIO(data))
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


def list_task_ids_with_workspace_volumes(client: docker.DockerClient | None = None) -> set[str]:
    """Every task id that still owns a workspace volume (labelled `warden.task`, see
    `_workspace_volume`). An anonymous volume (no such label; the one-off case in tests) never
    appears here: nobody can ever re-attach to it, so there is nothing to decide about it.

    For `core/worker.py`'s janitor to find discard candidates. This module only lists them;
    whether a given one should actually be thrown away is a question about *task* state, which
    lives in `core`, not here (briefing §10: `sandbox` creates/destroys, it does not decide).
    """
    client = client or docker.from_env()
    volumes = client.volumes.list(filters={"label": "warden.task"})
    return {v.attrs["Labels"]["warden.task"] for v in volumes}


def _remove_orphan_container(client: docker.DockerClient, task_id: str) -> None:
    """Force-remove whatever container still carries this task's `warden.task` label.

    A worker whose process is killed outright never runs `Sandbox.destroy()`, so its
    container can outlive it, possibly still mid-command against the workspace volume a new
    sandbox is about to attach to. The lease holder claiming the task now is the only
    legitimate owner (fencing in `core/loop.py` stops the old worker from writing anything
    else regardless), so whatever is still labelled for this task belongs to nobody that
    matters any more, and the safe thing is to remove it before the new container starts.
    """
    for stray in client.containers.list(all=True, filters={"label": f"warden.task={task_id}"}):
        with contextlib.suppress(NotFound, docker.errors.APIError):
            stray.remove(force=True)


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
        exclude: Callable[[str], bool] | None = None,
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
        return await asyncio.to_thread(
            cls._create_sync, profile, workspace, client, task_id, exclude
        )

    @staticmethod
    def _create_sync(
        profile: SandboxProfile,
        workspace: pathlib.Path,
        client: docker.DockerClient,
        task_id: str | None,
        exclude: Callable[[str], bool] | None = None,
    ) -> "Sandbox":
        if task_id is not None:
            _remove_orphan_container(client, task_id)

        volume, is_new = _workspace_volume(client, task_id)

        def discard_new_volume() -> None:
            # Only if this call created it. An existing task volume holds work that
            # predates this container and must survive a failure to start one.
            if is_new:
                with contextlib.suppress(Exception):
                    volume.remove(force=True)

        try:
            # containers.run belongs inside the try too: it is the call most likely to fail
            # (a missing image, most often), and it used to sit outside this block, so a
            # volume this call had just created was never cleaned up when it did. Regression
            # test: test_a_missing_image_does_not_leak_the_volume.
            container = client.containers.run(
                image=profile.image,
                # Idles so that commands can be exec'd into it. The container has no
                # entrypoint work of its own: it exists to be a contained filesystem and
                # process namespace.
                command=["sleep", "infinity"],
                detach=True,
                user=f"{SANDBOX_UID}:{SANDBOX_UID}",
                working_dir=WORKSPACE,
                environment=profile.env,
                # --- the hardening, every line of which has a test ---
                network_mode=profile.network,
                read_only=True,
                # A named volume, not tmpfs. Docker's archive endpoint refuses to write into
                # a container whose rootfs is read-only unless the target is a real mount,
                # and a tmpfs target silently swallows the upload: put_archive reports
                # success while writing underneath the mount, where nothing can read it.
                # Both were verified against the daemon rather than assumed.
                volumes={volume.name: {"bind": MOUNT_ROOT, "mode": "rw"}},
                tmpfs={"/tmp": f"rw,size={profile.tmp_size_mb}m,mode=1777"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=profile.pids_limit,
                mem_limit=f"{profile.memory_mb}m",
                nano_cpus=int(profile.cpus * 1_000_000_000),
                # The workspace volume above is the only mount. No bind mount from the host,
                # and in particular never the Docker socket: mounting it would hand the
                # agent root on the host.
                # `warden.task`, when there is one, is what `_remove_orphan_container` looks
                # for later, the same label the workspace volume already carries.
                labels={
                    "warden.sandbox": "1",
                    "warden.profile": profile.name,
                    **({"warden.task": task_id} if task_id is not None else {}),
                },
            )
            # The mount exists only once the container is running, so the copy happens after.
            # An existing task volume already holds the workspace, including whatever the
            # task changed before it was interrupted. Copying over it would undo that.
            if is_new:
                container.put_archive(MOUNT_ROOT, _workspace_tar(workspace, exclude).getvalue())
        except ImageNotFound as exc:
            discard_new_volume()
            raise SandboxError(
                f"sandbox image {profile.image!r} was not found; run `make sandbox-image` "
                "to build it"
            ) from exc
        except BaseException:
            # Anything failing past this point must not leave the container or the volume
            # behind: the caller never receives a Sandbox, so nobody is left to call
            # destroy(). This is not hypothetical. An earlier version of this module raised
            # here on every call and left fourteen containers running.
            with contextlib.suppress(Exception):
                container.remove(force=True)
            discard_new_volume()
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

    async def put_file(self, relative_path: str, data: bytes) -> None:
        """Upload one file under the mount root, owned by the sandbox user.

        The staging mechanism for `write_file`: content travels here as bytes on an archive
        upload, never interpolated into a command or a script argument the model influenced.
        """
        await asyncio.to_thread(self._put_file_sync, relative_path, data)

    def _put_file_sync(self, relative_path: str, data: bytes) -> None:
        self._container.put_archive(MOUNT_ROOT, _file_tar(relative_path, data).getvalue())

    def _kill_sync(self) -> None:
        # Already gone or already stopped; either way the deadline is satisfied.
        with contextlib.suppress(NotFound, docker.errors.APIError):
            self._container.kill()

        # kill() only sends the signal, so the caller could otherwise see the container
        # still briefly alive. Under load that window is wide enough to matter, and it made
        # the deadline test flaky in a full suite run.
        self._wait_until_stopped()

        # A dead container would otherwise end the whole task the first time a command
        # hangs, even though the workspace volume is task-scoped and survived the kill just
        # fine. Restarting hands the caller a live container again; only the command that
        # overran its deadline is lost, not the rest of the task.
        with contextlib.suppress(NotFound, docker.errors.APIError):
            self._container.start()
        self._wait_until_started()

    async def kill_for_cancel(self) -> None:
        """Kill the container promptly and leave it dead, for a cancel request.

        Deliberately not `_kill_sync`: that one restarts the container, because a command
        that merely overran its own `kill_after` still has the rest of the task ahead of it
        and needs a live container to keep going. A cancelled task has no "rest of the
        task" — the loop is about to finish it, and `Worker.run_once` destroys this sandbox
        right after — so restarting here would be wasted work racing that teardown, and the
        container would briefly look alive again for no one. Reuses `_wait_until_stopped`
        unchanged: same wait, just without the `start()` that follows it in `_kill_sync`.
        """
        await asyncio.to_thread(self._kill_for_cancel_sync)

    def _kill_for_cancel_sync(self) -> None:
        with contextlib.suppress(NotFound, docker.errors.APIError):
            self._container.kill()
        self._wait_until_stopped()

    def _wait_until_stopped(self) -> None:
        """Poll until the container is confirmed dead, or give up and report nothing.

        Not finding out either way is treated as "stopped": the caller already gets
        `CommandTimeout` for the command, and `destroy()` removes the container by force
        regardless, so there is nothing further to report here.
        """
        deadline = time.monotonic() + KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                self._container.reload()
            except (NotFound, docker.errors.APIError):
                return
            if not self._container.attrs.get("State", {}).get("Running"):
                return
            time.sleep(0.05)

    def _wait_until_started(self) -> None:
        """Poll until the container is confirmed running, or raise trying.

        Unlike `_wait_until_stopped`, a failed or inconclusive check here cannot be treated
        as success: silently returning let a caller's next `exec` hit a stopped container
        and fail with an opaque "cannot exec in a stopped state" instead of the honest
        `SandboxError` below. `reload()` erroring is retried rather than trusted either way,
        since docker start briefly returns before the daemon reports the new state.
        """
        deadline = time.monotonic() + KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                self._container.reload()
            except (NotFound, docker.errors.APIError):
                time.sleep(0.05)
                continue
            if self._container.attrs.get("State", {}).get("Running"):
                return
            time.sleep(0.05)
        raise SandboxError("the sandbox did not come back up after a command was killed")

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
