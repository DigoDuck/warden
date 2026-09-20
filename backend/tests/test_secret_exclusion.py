"""Secrets stay out of the sandbox, because a path-based deny does not survive code execution.

Found twice, independently, in the review of the branch that introduced write_file and the
test runner: by the Opus review gate running the exploit, and by the final review reading
what the workspace copy excludes. The exploit needs only two calls the default policy
allows. Write a test that opens `.env`, run the test runner, read the secret in the output.

ADR-018 records the decision. The first half of this file is pure and proves the predicate
and the tar; the second half runs the real exploit against a real container, both ways.
"""

import os
import pathlib
import tarfile
from collections.abc import AsyncIterator

import pytest

from warden.policy.engine import PolicyContext, UserRef, load_policy, never_readable
from warden.sandbox.docker import Sandbox, SandboxProfile, _workspace_tar
from warden.tools.sandboxed import (
    ReadFileArgs,
    RunCommandArgs,
    WriteFileArgs,
    read_file,
    run_command,
    write_file,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"
SECRET = "sk-ant-the-agent-must-never-see-this"

# What an agent holding repo:write and tests:run would write. Raising puts the file's
# contents into pytest's failure output, which is what comes back as the tool result.
LEAK_TEST = (
    "import pathlib\n"
    "\n"
    "def test_leak():\n"
    "    raise AssertionError(pathlib.Path('.env').read_text())\n"
)


def _names(workspace: pathlib.Path, exclude: object = None) -> set[str]:
    with tarfile.open(fileobj=_workspace_tar(workspace, exclude)) as tar:  # type: ignore[arg-type]
        return set(tar.getnames())


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    for directory in ("src", "tests", "config", ".github"):
        (root / directory).mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    (root / ".github" / "ci.yml").write_text("ci: true\n", encoding="utf-8", newline="\n")
    (root / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET}\n", encoding="utf-8", newline="\n")
    (root / "config" / ".env").write_text(f"NESTED={SECRET}\n", encoding="utf-8", newline="\n")
    return root


# --- the predicate ------------------------------------------------------------------------


def test_an_explicit_deny_is_told_apart_from_the_default_deny() -> None:
    """A CI config is unreadable through read_file, but it is not a secret.

    It is a normal part of the project that the test suite may need on disk. Only what a
    deny RULE names is kept out of the container, never everything that merely lacks an
    allow rule, or the sandbox would receive half a repository.
    """
    policy = load_policy(DEFAULT_POLICY)
    user = UserRef(role="worker")

    def explicit(path: str) -> bool:
        return policy.explicitly_denies(PolicyContext(tool="read_file", path=path, user=user))

    assert explicit(".env")
    assert not explicit(".github/ci.yml"), "default deny is not an explicit deny"
    assert not explicit("src/app.py")
    # And the ordinary decision is still deny for both of the first two.
    for path in (".env", ".github/ci.yml"):
        context = PolicyContext(tool="read_file", path=path, user=user)
        assert policy.evaluate(context).effect.value == "deny"


def test_never_readable_follows_the_rules_in_the_policy_file() -> None:
    """One list drives both layers, so they cannot drift apart."""
    excluded = never_readable(load_policy(DEFAULT_POLICY))

    for secret in (".env", ".env.local", "config/.env", "deploy/server.pem", "src/secrets/x.json"):
        assert excluded(secret), secret
    for ordinary in ("src/app.py", "tests/test_app.py", "README.md", ".github/ci.yml"):
        assert not excluded(ordinary), ordinary


# --- the tar ------------------------------------------------------------------------------


def test_the_workspace_copy_leaves_secrets_out_at_any_depth(workspace: pathlib.Path) -> None:
    names = _names(workspace, never_readable(load_policy(DEFAULT_POLICY)))

    assert "workspace/src/app.py" in names
    assert "workspace/.github/ci.yml" in names, "a default-denied file is still part of the repo"
    assert "workspace/.env" not in names
    assert "workspace/config/.env" not in names


def test_a_directory_does_not_carry_its_excluded_children_in(workspace: pathlib.Path) -> None:
    """Regression for a latent bug the secret filter would have made fatal.

    tarfile adds a directory together with everything under it unless told otherwise, and
    that recursive add bypasses the per-path checks. Skipping `config/.env` meant nothing
    while adding `config` had already carried it in, and a nested `pkg/node_modules` had
    been getting into the sandbox the same way since the ignore list was written.
    """
    vendored = workspace / "pkg" / "node_modules" / "left-pad"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text("vendored\n", encoding="utf-8", newline="\n")
    (workspace / "pkg" / "main.js").write_text("ok\n", encoding="utf-8", newline="\n")

    with tarfile.open(fileobj=_workspace_tar(workspace)) as tar:
        listed = tar.getnames()

    assert "workspace/pkg/main.js" in listed
    assert not [name for name in listed if "node_modules" in name]
    assert len(listed) == len(set(listed)), "every entry exactly once"


# --- the exploit, against a real container ------------------------------------------------


@pytest.fixture(scope="session")
def docker_available() -> None:
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


async def _run_the_exploit(box: Sandbox) -> str:
    """The two calls the default policy allows, exactly as an agent would make them."""
    await write_file(box, WriteFileArgs(path="tests/test_leak.py", content=LEAK_TEST))
    return await run_command(box, RunCommandArgs(cmd="python -m pytest -q tests/test_leak.py"))


@pytest.fixture
async def filtered_sandbox(
    docker_available: None, workspace: pathlib.Path
) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(
        SandboxProfile(), workspace, exclude=never_readable(load_policy(DEFAULT_POLICY))
    )
    try:
        yield box
    finally:
        await box.destroy()


@pytest.mark.sandbox
async def test_code_the_agent_writes_and_runs_cannot_read_the_secret(
    filtered_sandbox: Sandbox,
) -> None:
    output = await _run_the_exploit(filtered_sandbox)

    assert SECRET not in output
    # It failed for the right reason: the file is not there, rather than the test not running.
    assert "FileNotFoundError" in output or "No such file" in output

    present = await filtered_sandbox.exec(["sh", "-c", "ls -A . config"])
    assert ".env" not in present.output


@pytest.mark.sandbox
async def test_the_rest_of_the_project_still_works_without_its_secrets(
    filtered_sandbox: Sandbox,
) -> None:
    """The filter must not cost the agent the repository it is there to work on."""
    assert await read_file(filtered_sandbox, ReadFileArgs(path="src/app.py")) == "x = 1\n"
    result = await run_command(
        filtered_sandbox, RunCommandArgs(cmd="python -m pytest -q tests/test_ok.py")
    )
    assert result.startswith("exit code: 0")


@pytest.mark.sandbox
async def test_without_the_filter_the_same_two_calls_do_leak(
    docker_available: None, workspace: pathlib.Path
) -> None:
    """The control. It documents the hole, and it is what makes the test above mean something.

    If this ever stops leaking, the exploit has stopped being a real exploit and the
    assertion above would be passing for a reason nobody understands.
    """
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        assert SECRET in await _run_the_exploit(box)
    finally:
        await box.destroy()
