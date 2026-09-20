"""The symlink refusal asks git, so whatever git reads as a symlink is what gets refused.

Found by the Opus review gate. The first version of this refusal matched the diff text with
`^new file mode 120000$`, while git parses that field with strtoul. Four header shapes git
accepts identically slipped past the regex, and each one created `src/link -> ../.env` under
a policy `allow`. It is a parser differential of exactly the kind ADR-017 exists to avoid,
reintroduced by the one refusal the branch added. The fix reads `git apply --summary`, which
normalises every shape to the same line.
"""

import os
import pathlib
from collections.abc import AsyncIterator

import pytest

from warden.sandbox.docker import Sandbox, SandboxProfile
from warden.tools.registry import ToolError
from warden.tools.sandboxed import ApplyPatchArgs, apply_patch

pytestmark = pytest.mark.sandbox

_BODY = "--- /dev/null\n+++ b/src/link\n@@ -0,0 +1 @@\n+../.env\n\\ No newline at end of file\n"

# Each value is the extended-header line, terminator included, because the terminator is
# part of what differs.
HEADER_SHAPES = {
    "canonical": "new file mode 120000\n",
    "crlf": "new file mode 120000\r\n",
    "leading-zero": "new file mode 0120000\n",
    "trailing-space": "new file mode 120000 \n",
    "trailing-tab": "new file mode 120000\t\n",
}


def _symlink_diff(header: str) -> str:
    return "diff --git a/src/link b/src/link\n" + header + _BODY


@pytest.fixture(scope="session")
def docker_available() -> None:
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


@pytest.fixture
async def sandbox(docker_available: None, tmp_path: pathlib.Path) -> AsyncIterator[Sandbox]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    box = await Sandbox.create(SandboxProfile(), root)
    try:
        yield box
    finally:
        await box.destroy()


async def _link_exists(box: Sandbox) -> bool:
    # -L, not -e. -e follows the link, so a dangling one reads as "nothing there" and the
    # assertion would stay green with the symlink created.
    result = await box.exec(["sh", "-c", "test -L src/link && echo YES || echo NO"])
    return result.output.strip() == "YES"


@pytest.mark.parametrize("shape", sorted(HEADER_SHAPES))
async def test_every_header_shape_git_reads_as_a_symlink_is_refused(
    shape: str, sandbox: Sandbox
) -> None:
    with pytest.raises(ToolError, match="symlink"):
        await apply_patch(sandbox, ApplyPatchArgs(diff=_symlink_diff(HEADER_SHAPES[shape])))

    assert not await _link_exists(sandbox)


async def test_a_regular_file_named_like_the_symlink_mode_is_not_refused(
    sandbox: Sandbox,
) -> None:
    """The match is anchored on the mode position, not on the digits merely appearing.

    git prints ` create mode 100644 src/120000` for this, and a looser match would refuse
    an ordinary file for what it happens to be called.
    """
    diff = (
        "diff --git a/src/120000 b/src/120000\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/120000\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    await apply_patch(sandbox, ApplyPatchArgs(diff=diff))

    created = await sandbox.exec(["cat", "src/120000"])
    assert created.output.strip() == "x = 1"
