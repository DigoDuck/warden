"""The diff half of the verifier (ADR-026), which is pure and needs no container.

The seed is read from a real directory and the "after" side from a real tar built the way
the Docker archive endpoint builds one (a `workspace/` prefix on every name), so what these
tests exercise is the parsing the verifier actually does, not a hand-made dict.
"""

import io
import pathlib
import tarfile

from warden.verify.runner import MAX_PATCH_CHARS, compute_diff, read_archive, read_seed


def _tar(files: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        directory = tarfile.TarInfo("workspace")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return stream.getvalue()


def _seed(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    return root


def test_an_untouched_workspace_has_an_empty_diff(tmp_path: pathlib.Path) -> None:
    seed = _seed(tmp_path, {"src/app.py": "x = 1\n"})
    after = read_archive(_tar({"workspace/src/app.py": b"x = 1\n"}))

    diff = compute_diff(read_seed(seed), after)

    assert diff.status == "ok"
    assert diff.files == []
    assert diff.patch == ""


def test_added_removed_and_modified_files_are_counted(tmp_path: pathlib.Path) -> None:
    seed = _seed(tmp_path, {"src/app.py": "a\nb\n", "old.txt": "gone\n"})
    after = read_archive(
        _tar({"workspace/src/app.py": b"a\nB\nc\n", "workspace/tests/test_new.py": b"t\n"})
    )

    diff = compute_diff(read_seed(seed), after)

    by_path = {change.path: change for change in diff.files}
    assert by_path["src/app.py"].change == "modified"
    assert (by_path["src/app.py"].additions, by_path["src/app.py"].deletions) == (2, 1)
    assert by_path["tests/test_new.py"].change == "added"
    assert by_path["tests/test_new.py"].additions == 1
    assert by_path["old.txt"].change == "removed"
    assert by_path["old.txt"].deletions == 1
    assert (diff.files_changed, diff.additions, diff.deletions) == (3, 3, 2)
    assert "--- a/src/app.py\n+++ b/src/app.py\n" in diff.patch
    assert "--- /dev/null\n+++ b/tests/test_new.py\n" in diff.patch


def test_a_file_excluded_by_policy_is_not_reported_as_removed(tmp_path: pathlib.Path) -> None:
    """ADR-018 keeps `.env` out of the container, so it is absent from the export. The seed
    has to be filtered the same way or every diff would claim the agent deleted it."""
    seed = _seed(tmp_path, {".env": "SECRET=1\n", "src/app.py": "x\n"})
    after = read_archive(_tar({"workspace/src/app.py": b"x\n"}))

    diff = compute_diff(read_seed(seed, exclude=lambda path: path == ".env"), after)

    assert diff.files == []


def test_caches_left_by_the_checks_are_ignored(tmp_path: pathlib.Path) -> None:
    seed = _seed(tmp_path, {"src/app.py": "x\n"})
    after = read_archive(
        _tar(
            {
                "workspace/src/app.py": b"x\n",
                "workspace/src/__pycache__/app.cpython-313.pyc": b"\x00\x01",
                "workspace/.mypy_cache/3.13/app.data.json": b"{}",
            }
        )
    )

    assert compute_diff(read_seed(seed), after).files == []


def test_a_binary_change_is_flagged_without_a_text_patch(tmp_path: pathlib.Path) -> None:
    seed = _seed(tmp_path, {"logo.png": "placeholder"})
    after = read_archive(_tar({"workspace/logo.png": b"\x89PNG\x00\x01"}))

    [change] = compute_diff(read_seed(seed), after).files

    assert change.binary is True
    assert (change.additions, change.deletions) == (0, 0)


def test_names_escaping_the_workspace_are_dropped_not_followed() -> None:
    """The tar comes from a container the agent controlled. A `../` name must never become a
    path in the diff, let alone a file written anywhere."""
    after = read_archive(
        _tar(
            {
                "workspace/../../etc/passwd": b"root\n",
                "elsewhere/file.txt": b"not under workspace/\n",
                "workspace/ok.txt": b"fine\n",
            }
        )
    )

    assert after == {"ok.txt": b"fine\n"}


def test_a_symlink_is_recorded_as_a_link_not_followed(tmp_path: pathlib.Path) -> None:
    after = read_archive(_tar({}, links={"workspace/src/config": "../../.env"}))

    [change] = compute_diff(read_seed(tmp_path), after).files

    assert change.path == "src/config"
    assert change.change == "added"
    assert "<link to ../../.env>" in compute_diff({}, after).patch


def test_a_file_without_a_trailing_newline_does_not_glue_the_next_header(
    tmp_path: pathlib.Path,
) -> None:
    seed = _seed(tmp_path, {"a.py": "one", "b.py": "two"})
    after = read_archive(_tar({"workspace/a.py": b"ONE", "workspace/b.py": b"TWO"}))

    patch = compute_diff(read_seed(seed), after).patch

    assert "+ONE\n--- a/b.py\n" in patch


def test_a_huge_patch_is_truncated_but_the_counts_stay_complete(
    tmp_path: pathlib.Path,
) -> None:
    lines = "".join(f"line {n}\n" for n in range(40_000))
    after = read_archive(_tar({"workspace/big.txt": lines.encode()}))

    diff = compute_diff(read_seed(tmp_path), after)

    assert diff.patch_truncated is True
    assert len(diff.patch) == MAX_PATCH_CHARS
    assert diff.additions == 40_000
