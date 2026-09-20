"""`apply_patch`: authorisation of a call that can touch more than one path.

ADR-017 is the design this file proves: the policy engine cannot parse a diff itself
without risking a parser differential against what git apply actually does, so inspection
asks git (`--numstat -z`), judges every path it reports, and `combine()` folds the results
before the call is allowed to run at all. Most tests here drive the real loop with the real
default policy, because the claim under test is what gets *authorised*, not merely what the
tool refuses to write outside of.

A handful of tests at the top are plain functions with no container: they pin the numstat
parsing and rename-pairing logic (ADR-017's verified byte layout) the way `test_policy.py`
pins the engine, and there is no reason to pay for a container to run them. The rest carry
`@pytest.mark.sandbox` individually rather than as a module-level `pytestmark`, because this
file, unlike its siblings, is not *all* container tests.
"""

import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.core import events
from warden.core.events import read_events
from warden.core.loop import run_task
from warden.models import PolicyDecision, Task, ToolCall, User
from warden.policy.engine import load_policy
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.sandbox.docker import Sandbox, SandboxProfile
from warden.tools.sandboxed import (
    ApplyPatchArgs,
    ReadFileArgs,
    _parse_numstat_z,
    _touched_paths,
    build_registry,
    read_file,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"


# --- pure: the parsing ADR-017 verified, no container needed ------------------------------


def test_numstat_z_splits_added_deleted_and_path_on_tabs_and_nul() -> None:
    # Byte layout confirmed empirically in a throwaway sandbox: "<added>\t<deleted>\t<path>",
    # NUL-terminated, one record per touched file, no trailing separator beyond the last NUL.
    output = "1\t1\tsrc/a.py\x001\t0\t.github/ci.yml\x00"
    assert _parse_numstat_z(output) == ["src/a.py", ".github/ci.yml"]


def test_numstat_z_of_nothing_is_nothing() -> None:
    assert _parse_numstat_z("") == []


def test_touched_paths_is_just_numstat_for_a_plain_modify() -> None:
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
    assert _touched_paths(diff, "1\t1\tsrc/a.py\x00") == ["src/a.py"]


def test_touched_paths_adds_the_source_of_a_rename() -> None:
    """Verified in ADR-017: `--numstat -z` reports only the destination of a rename. The
    source comes from the patch's own `rename from`/`rename to` pair, accepted only because
    the destination it names also appears in git's own numstat output for this same patch.
    """
    diff = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 100%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
    )
    numstat = "0\t0\tsrc/new.py\x00"
    assert _touched_paths(diff, numstat) == ["src/new.py", "src/old.py"]


def test_touched_paths_still_judges_a_rename_header_numstat_never_confirmed() -> None:
    """The old design here cross-checked a header pair against numstat before trusting it,
    on the reasoning that git ignores a header numstat never confirmed. That check was
    itself the bypass (see the three tests below): there is no cross-check left, so a
    header naming a path numstat never reported is still judged, on top of numstat's real
    destination, rather than silently dropped. Fail closed: an extra path can only make the
    combined decision more restrictive, never less.
    """
    diff = "rename from decoy_old.py\nrename to decoy_new.py\n"
    touched = _touched_paths(diff, "1\t0\tsrc/real.py\x00")
    assert touched == ["src/real.py", "decoy_old.py", "decoy_new.py"]


def test_touched_paths_unquotes_a_c_quoted_rename_source() -> None:
    """POLICY BYPASS this closes: git accepts a C-quoted extended-header path even when
    nothing about it required quoting (`rename from ".env"` applies identically to `rename
    from .env`). A bare regex capture would keep the quote marks, and the quoted string
    would then never match a `never-read-secrets` rule written against the real `.env`.
    """
    diff = (
        "diff --git a/.env b/src/leaked.py\n"
        "similarity index 0%\n"
        'rename from ".env"\n'
        "rename to src/leaked.py\n"
    )
    assert _touched_paths(diff, "0\t0\tsrc/leaked.py\x00") == ["src/leaked.py", ".env"]


def test_touched_paths_strips_a_trailing_cr_from_a_rename_header() -> None:
    """POLICY BYPASS this closes: a CRLF-terminated diff leaves `\\r` on the regex capture
    (`$` in MULTILINE mode matches before `\\n`, not before `\\r\\n`), which used to break
    the byte-identical match against numstat's LF-terminated destination and drop the
    source entirely.
    """
    diff = (
        "diff --git a/.env b/src/leaked.py\r\n"
        "similarity index 0%\r\n"
        "rename from .env\r\n"
        "rename to src/leaked.py\r\n"
    )
    assert _touched_paths(diff, "0\t0\tsrc/leaked.py\x00") == ["src/leaked.py", ".env"]


def test_touched_paths_keeps_the_real_source_despite_a_later_decoy_pair() -> None:
    """POLICY BYPASS this closes: the old dict comprehension kept only the last `old` seen
    for a given destination, so a second `rename from`/`rename to` pair anywhere in the
    diff text, even in prose git itself ignores, silently overwrote the real source. There
    is no dict keyed by destination left to overwrite: every header path found is judged.
    """
    diff = (
        "diff --git a/.env b/src/leaked.py\n"
        "similarity index 0%\n"
        "rename from .env\n"
        "rename to src/leaked.py\n"
        "-- decoy trailing prose git ignores --\n"
        "rename from src/decoy.py\n"
        "rename to src/leaked.py\n"
    )
    touched = _touched_paths(diff, "0\t0\tsrc/leaked.py\x00")
    assert "src/leaked.py" in touched
    assert ".env" in touched


def test_apply_patch_args_take_a_diff_string() -> None:
    assert ApplyPatchArgs(diff="x").diff == "x"


# --- diffs shared by the tests below --------------------------------------------------------

_MODIFY_A = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 0000000..1111111 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
    "+line2-changed\n"
    " line3\n"
)

_MULTI_ALLOWED_AND_DENIED = (
    _MODIFY_A + "diff --git a/.github/ci.yml b/.github/ci.yml\n"
    "index 0000000..2222222 100644\n"
    "--- a/.github/ci.yml\n"
    "+++ b/.github/ci.yml\n"
    "@@ -1 +1 @@\n"
    "-ci: true\n"
    "+ci: false\n"
)

_MODIFY_ENV = (
    "diff --git a/.env b/.env\n"
    "index 0000000..3333333 100644\n"
    "--- a/.env\n"
    "+++ b/.env\n"
    "@@ -1 +1 @@\n"
    "-SECRET=nope\n"
    "+SECRET=leaked\n"
)

_RENAME_INTO_DENIED_TREE = (
    "diff --git a/src/a.py b/.github/x.py\n"
    "similarity index 100%\n"
    "rename from src/a.py\n"
    "rename to .github/x.py\n"
)

# The exploit chain the review reproduced against a real sandbox: a C-quoted source lets
# `.env` move into `src/**` (`write-source` allows it) while the old pairing logic dropped
# the quoted source from the judged set entirely, so `never-read-secrets` never saw it.
_RENAME_ENV_INTO_SRC_QUOTED = (
    "diff --git a/.env b/src/leaked.py\n"
    "similarity index 0%\n"
    'rename from ".env"\n'
    "rename to src/leaked.py\n"
)

_CREATE_SYMLINK_TO_ENV = (
    "diff --git a/src/link b/src/link\n"
    "new file mode 120000\n"
    "index 0000000..8888888\n"
    "--- /dev/null\n"
    "+++ b/src/link\n"
    "@@ -0,0 +1 @@\n"
    "+../.env\n"
    "\\ No newline at end of file\n"
)

_DOTDOT_CREATE = (
    "diff --git a/../outside.py b/../outside.py\n"
    "new file mode 100644\n"
    "index 0000000..4444444\n"
    "--- /dev/null\n"
    "+++ b/../outside.py\n"
    "@@ -0,0 +1 @@\n"
    "+evil = 1\n"
)

_CREATE_THROUGH_SYMLINK = (
    "diff --git a/src/linked/through.py b/src/linked/through.py\n"
    "new file mode 100644\n"
    "index 0000000..5555555\n"
    "--- /dev/null\n"
    "+++ b/src/linked/through.py\n"
    "@@ -0,0 +1 @@\n"
    "+evil = 1\n"
)

_GARBAGE = "this is not a diff at all\njust some garbage text\n"

_CONTEXT_MISMATCH = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 0000000..6666666 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,3 +1,3 @@\n"
    " nonexistent_context_1\n"
    "-nonexistent_old_line\n"
    "+nonexistent_new_line\n"
    " nonexistent_context_2\n"
)


# --- fixtures: same pattern as test_write_tools.py and test_sandbox.py --------------------


@pytest.fixture(scope="session")
def docker_available() -> None:
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
    (root / ".github").mkdir()
    (root / "src" / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8", newline="\n")
    (root / ".github" / "ci.yml").write_text("ci: true\n", encoding="utf-8", newline="\n")
    (root / ".env").write_text("SECRET=nope\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
async def sandbox(docker_available: None, workspace: pathlib.Path) -> AsyncIterator[Sandbox]:
    box = await Sandbox.create(SandboxProfile(), workspace)
    try:
        yield box
    finally:
        await box.destroy()


def _step(tool: str, **args: object) -> ScriptStep:
    return ScriptStep(
        tool_calls=[ProviderToolCall(id=f"call-{uuid.uuid4()}", name=tool, arguments=args)]
    )


async def _a_task(session: AsyncSession, spec: str) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
    session.add(user)
    await session.flush()
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec=spec)
    session.add(task)
    await session.flush()
    return task


async def _apply_patch_row(session: AsyncSession, task_id: UUID) -> ToolCall:
    return (
        await session.scalars(
            select(ToolCall).where(ToolCall.task_id == task_id, ToolCall.tool_name == "apply_patch")
        )
    ).one()


# --- authorisation, driven through the real loop with the real default policy -------------


@pytest.mark.sandbox
async def test_a_valid_patch_under_src_applies_and_the_file_changes(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    task = await _a_task(session, "fix a line")
    provider = FakeProvider([_step("apply_patch", diff=_MODIFY_A), _step("finish", summary="done")])

    result = await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    assert result.status == "SUCCEEDED"
    row = await _apply_patch_row(session, task.id)
    assert row.decision == "allow"
    assert row.error is None
    # The workspace is copied into the container at creation (docker.py), not bind-mounted,
    # so the change is verified by reading it back from there, not from the host `workspace`
    # directory, which the container never writes to.
    changed = await read_file(sandbox, ReadFileArgs(path="src/a.py"))
    assert changed == "line1\nline2-changed\nline3\n"


@pytest.mark.sandbox
async def test_a_patch_touching_an_allowed_and_a_denied_path_is_denied_as_a_whole(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    """One call, two files: src/a.py alone would be allowed, .github/ci.yml alone would not
    be. The combined decision must be the denial, and neither file changes, src/a.py
    included, because a partially-denied call does not get to keep the allowed half.
    """
    task = await _a_task(session, "sneak a CI change in with a source fix")
    provider = FakeProvider(
        [_step("apply_patch", diff=_MULTI_ALLOWED_AND_DENIED), _step("finish", summary="done")]
    )

    result = await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    assert result.status == "SUCCEEDED"
    row = await _apply_patch_row(session, task.id)
    assert row.decision == "deny"
    assert await read_file(sandbox, ReadFileArgs(path="src/a.py")) == "line1\nline2\nline3\n"
    assert await read_file(sandbox, ReadFileArgs(path=".github/ci.yml")) == "ci: true\n"


@pytest.mark.sandbox
async def test_a_patch_touching_a_secret_is_denied_by_never_read_secrets(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    task = await _a_task(session, "rotate the secret")
    provider = FakeProvider(
        [_step("apply_patch", diff=_MODIFY_ENV), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "deny"
    decision = (
        await session.scalars(select(PolicyDecision).where(PolicyDecision.tool_call_id == row.id))
    ).one()
    assert "never-read-secrets" in decision.matched_rules
    assert await read_file(sandbox, ReadFileArgs(path=".env")) == "SECRET=nope\n"


@pytest.mark.sandbox
async def test_a_rename_into_a_denied_tree_is_denied_and_the_destination_is_judged_too(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    task = await _a_task(session, "move a.py under .github")
    provider = FakeProvider(
        [_step("apply_patch", diff=_RENAME_INTO_DENIED_TREE), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "deny"
    # Nothing moved: the source is still there, the denied destination never appeared.
    assert await read_file(sandbox, ReadFileArgs(path="src/a.py")) == "line1\nline2\nline3\n"
    missing = await sandbox.exec(["sh", "-c", "test -e .github/x.py && echo YES || echo NO"])
    assert missing.output.strip() == "NO"

    # Proof the destination was actually judged, not merely that the call as a whole was
    # denied: the audit trail names both paths that went into the combined decision.
    decided = [
        event.payload
        for event in await read_events(session, task.id)
        if event.type == events.POLICY_DECIDED and event.payload["tool"] == "apply_patch"
    ]
    assert len(decided) == 1
    assert set(decided[0]["paths"]) == {"src/a.py", ".github/x.py"}


@pytest.mark.sandbox
async def test_a_quoted_rename_source_is_still_judged_and_denied(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    """The full exploit the review reproduced in a real sandbox: `.env` renamed into
    `src/**`, its source quoted in the extended header. `write-source` would allow the
    destination alone; `never-read-secrets` has to see `.env` regardless, or the secret
    ends up readable at `src/leaked.py`.
    """
    task = await _a_task(session, "add a helper module")
    provider = FakeProvider(
        [_step("apply_patch", diff=_RENAME_ENV_INTO_SRC_QUOTED), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "deny"
    decision = (
        await session.scalars(select(PolicyDecision).where(PolicyDecision.tool_call_id == row.id))
    ).one()
    assert "never-read-secrets" in decision.matched_rules
    assert await read_file(sandbox, ReadFileArgs(path=".env")) == "SECRET=nope\n"
    leaked = await sandbox.exec(["sh", "-c", "test -e src/leaked.py && echo YES || echo NO"])
    assert leaked.output.strip() == "NO"


@pytest.mark.sandbox
async def test_apply_patch_refuses_to_create_a_symlink(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    """The other half of the symlink bypass: even with `_CONTAIN` now checking the
    *effective* path against the judged one, a control plane that authorises by path
    string cannot safely let the agent create a new name for an existing file at all.
    Policy has no reason to refuse `src/link` (it looks like an ordinary source path), so
    the refusal has to come from the tool itself, before git ever creates the link.
    """
    task = await _a_task(session, "add a helper module")
    provider = FakeProvider(
        [_step("apply_patch", diff=_CREATE_SYMLINK_TO_ENV), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "allow"  # policy had no reason to refuse the textual path
    assert row.error is not None
    assert "symlink" in row.error
    exists = await sandbox.exec(["sh", "-c", "test -e src/link && echo YES || echo NO"])
    assert exists.output.strip() == "NO"


@pytest.mark.sandbox
async def test_a_dotdot_path_is_refused(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    task = await _a_task(session, "escape the workspace")
    provider = FakeProvider(
        [_step("apply_patch", diff=_DOTDOT_CREATE), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "deny"
    leaked = await sandbox.exec(["sh", "-c", "test -e /sandbox/outside.py && echo YES || echo NO"])
    assert leaked.output.strip() == "NO"


@pytest.mark.sandbox
async def test_creating_through_a_symlinked_directory_creates_nothing_outside(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    """Policy judges the string `src/linked/through.py`, which looks like an ordinary
    allowed path; it has no way to know `src/linked` is a symlink leaving the workspace.
    git does (ADR-017, verified): it refuses to write through it, and that refusal, not a
    policy denial, is what this test is actually proving.
    """
    setup = await sandbox.exec(
        ["sh", "-c", "mkdir -p /tmp/outside_target && ln -s /tmp/outside_target src/linked"]
    )
    assert setup.exit_code == 0, setup.output

    task = await _a_task(session, "add a file under src/linked")
    provider = FakeProvider(
        [_step("apply_patch", diff=_CREATE_THROUGH_SYMLINK), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "allow"  # policy had no reason to refuse the textual path
    assert row.error is not None  # git refused to write through the symlink

    leaked = await sandbox.exec(["find", "/tmp/outside_target", "-iname", "through.py"])
    assert leaked.output.strip() == ""


@pytest.mark.sandbox
async def test_a_malformed_patch_is_refused_at_inspection_with_no_allow_decision(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    task = await _a_task(session, "apply garbage")
    provider = FakeProvider([_step("apply_patch", diff=_GARBAGE), _step("finish", summary="done")])

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "deny"
    decision = (
        await session.scalars(select(PolicyDecision).where(PolicyDecision.tool_call_id == row.id))
    ).one()
    assert decision.effect == "deny"
    assert decision.matched_rules == []
    assert "could not be determined" in decision.reason


@pytest.mark.sandbox
async def test_a_patch_that_passes_policy_but_does_not_apply_is_a_tool_error(
    session: AsyncSession, workspace: pathlib.Path, sandbox: Sandbox
) -> None:
    """`--numstat -z` only parses the patch, so a context mismatch is invisible to policy:
    the call is allowed and only fails for real when the executor runs `--check`.
    """
    task = await _a_task(session, "apply a patch whose context does not match")
    provider = FakeProvider(
        [_step("apply_patch", diff=_CONTEXT_MISMATCH), _step("finish", summary="done")]
    )

    await run_task(
        session,
        task,
        provider,
        build_registry(sandbox),
        load_policy(DEFAULT_POLICY),
        workspace=workspace,
    )

    row = await _apply_patch_row(session, task.id)
    assert row.decision == "allow"
    assert row.error is not None
    assert "does not apply" in row.error
    assert await read_file(sandbox, ReadFileArgs(path="src/a.py")) == "line1\nline2\nline3\n"
    assert (workspace / "src" / "a.py").read_text(encoding="utf-8") == "line1\nline2\nline3\n"
