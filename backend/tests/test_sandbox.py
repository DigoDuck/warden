"""Proof, one test per restriction.

A container is not a secure sandbox by default: shared kernel, default capabilities,
networking on, root inside. What makes it a sandbox is the set of restrictions applied, and
what makes those restrictions real is this file. Every flag in `SandboxProfile` that claims
to contain something has a test here that fails if it stops containing it.
"""

import os
import pathlib
from collections.abc import AsyncIterator

import pytest

from warden.sandbox.docker import (
    MOUNT_ROOT,
    SANDBOX_UID,
    CommandTimeout,
    Sandbox,
    SandboxProfile,
)

pytestmark = pytest.mark.sandbox


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Skip locally when Docker is not running, but never skip in CI.

    An unconditional skip would turn broken Docker in CI into a green suite, and it would do
    it precisely on the tests that matter most.
    """
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
async def sandbox(docker_available: None, workspace: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        yield box
    finally:
        await box.destroy()


async def test_runs_as_a_non_root_user(sandbox: Sandbox) -> None:
    result = await sandbox.exec(["id", "-u"])
    assert result.output.strip() == str(SANDBOX_UID)


async def test_has_no_network(sandbox: Sandbox) -> None:
    """Deliberately not `curl` against an address.

    `python:3.13-slim` has no curl, so a curl-based test would pass on "command not found"
    and prove nothing about networking. A socket connection fails for exactly one reason
    here, and the assertion below distinguishes that reason from a missing binary.
    """
    probe = (
        "import socket,sys\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
        "    print('CONNECTED')\n"
        "except OSError as exc:\n"
        "    print('BLOCKED', type(exc).__name__)\n"
    )
    result = await sandbox.exec(["python", "-c", probe], kill_after=30)

    assert "CONNECTED" not in result.output
    assert "BLOCKED" in result.output, f"expected a network error, got: {result.output!r}"


async def test_dns_does_not_resolve_either(sandbox: Sandbox) -> None:
    probe = (
        "import socket\n"
        "try:\n"
        "    print('RESOLVED', socket.gethostbyname('api.anthropic.com'))\n"
        "except OSError:\n"
        "    print('NO_DNS')\n"
    )
    result = await sandbox.exec(["python", "-c", probe], kill_after=30)
    assert "NO_DNS" in result.output


async def test_root_filesystem_is_read_only(sandbox: Sandbox) -> None:
    result = await sandbox.exec(["python", "-c", "open('/etc/warden-probe','w')"])
    assert result.exit_code != 0
    assert "Read-only file system" in result.output or "Permission denied" in result.output


async def test_workspace_is_writable(sandbox: Sandbox) -> None:
    """Read-only everywhere would also be useless: the agent has to work somewhere."""
    result = await sandbox.exec(["python", "-c", "open('probe','w').write('ok'); print('WROTE')"])
    assert result.exit_code == 0
    assert "WROTE" in result.output


async def test_tmp_is_writable_but_separate(sandbox: Sandbox) -> None:
    result = await sandbox.exec(["python", "-c", "open('/tmp/probe','w').write('ok'); print('OK')"])
    assert result.exit_code == 0


async def test_capabilities_are_dropped(sandbox: Sandbox) -> None:
    """Without CAP_CHOWN, changing ownership fails even on a file the user owns."""
    probe = (
        "import os\n"
        "open('own','w').write('x')\n"
        "try:\n"
        "    os.chown('own', 0, 0)\n"
        "    print('CHOWNED')\n"
        "except PermissionError:\n"
        "    print('NO_CAP_CHOWN')\n"
    )
    result = await sandbox.exec(["python", "-c", probe])
    assert "NO_CAP_CHOWN" in result.output


async def test_the_workspace_arrived(sandbox: Sandbox) -> None:
    result = await sandbox.exec(["cat", "src/app.py"])
    assert result.output.strip() == "print('hello')"


async def test_copied_files_belong_to_the_sandbox_user(sandbox: Sandbox) -> None:
    """Copied as root they would be unwritable later, and a chown inside is impossible."""
    result = await sandbox.exec(["python", "-c", "import os; print(os.stat('src/app.py').st_uid)"])
    assert result.output.strip() == str(SANDBOX_UID)


async def test_the_docker_socket_is_not_mounted(sandbox: Sandbox) -> None:
    """Mounting it would hand the agent root on the host, which is the whole threat."""
    result = await sandbox.exec(
        ["python", "-c", "import os; print(os.path.exists('/var/run/docker.sock'))"]
    )
    assert result.output.strip() == "False"

    # The workspace volume is the only mount, and nothing is bound from the host.
    mounts = sandbox.inspect()["Mounts"]
    assert [m["Destination"] for m in mounts] == [MOUNT_ROOT]
    assert all(m["Type"] == "volume" for m in mounts)


async def test_resource_limits_are_configured(sandbox: Sandbox) -> None:
    """An assertion about configuration, weaker than one about behaviour.

    Proving the pids limit by behaviour means running a fork bomb, and proving the memory
    limit means driving the container into the OOM killer. Both are slow and flaky in CI for
    a guarantee the daemon already enforces. The value here is catching a flag that silently
    stopped being passed.
    """
    host_config = sandbox.inspect()["HostConfig"]
    assert host_config["PidsLimit"] == 128
    assert host_config["Memory"] == 512 * 1024 * 1024
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["NetworkMode"] == "none"


async def test_a_command_past_its_deadline_is_killed(
    docker_available: None, workspace: pathlib.Path
) -> None:
    """Abandoning the wait would leave the command running; the container is killed instead."""
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        with pytest.raises(CommandTimeout):
            await box.exec(["sleep", "30"], kill_after=2)

        # Killed, not merely abandoned.
        assert box.inspect()["State"]["Running"] is False
    finally:
        await box.destroy()


async def test_destroy_removes_the_container(
    docker_available: None, workspace: pathlib.Path
) -> None:
    import docker as docker_sdk

    box = await Sandbox.create(SandboxProfile(), workspace)
    container_id = box.id
    await box.destroy()

    client = docker_sdk.from_env()
    with pytest.raises(docker_sdk.errors.NotFound):
        client.containers.get(container_id)


async def test_destroy_is_idempotent(docker_available: None, workspace: pathlib.Path) -> None:
    """Cleanup runs in a finally block, so a second call must not raise on the way out."""
    box = await Sandbox.create(SandboxProfile(), workspace)
    await box.destroy()
    await box.destroy()


async def test_a_failure_while_creating_leaves_nothing_behind(
    docker_available: None, workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller never gets a Sandbox, so nobody is left to call destroy().

    Regression test with a real history: before the cleanup existed, a failure on this exact
    line left fourteen containers running on the development machine.
    """
    import docker as docker_sdk

    import warden.sandbox.docker as sandbox_module

    client = docker_sdk.from_env()

    def count() -> tuple[int, int]:
        return (
            len(client.containers.list(all=True, filters={"label": "warden.sandbox=1"})),
            len(client.volumes.list(filters={"label": "warden.sandbox=1"})),
        )

    def boom(_: pathlib.Path) -> None:
        raise RuntimeError("copying the workspace failed")

    monkeypatch.setattr(sandbox_module, "_workspace_tar", boom)

    before = count()
    with pytest.raises(RuntimeError, match="copying the workspace failed"):
        await Sandbox.create(SandboxProfile(), workspace)

    assert count() == before
