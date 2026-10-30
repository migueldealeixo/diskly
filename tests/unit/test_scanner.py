import errno
import os
from pathlib import Path

import pytest

from diskly.domain.scanning import IssueCode, ScanStatus
from diskly.scanner import engine, scan_directory


def test_empty_directory(tmp_path: Path) -> None:
    result = scan_directory(tmp_path)

    assert result.root == tmp_path
    assert result.status is ScanStatus.COMPLETED
    assert result.directories_count == 1
    assert result.files_count == result.unique_files_count == 0
    assert result.logical_bytes == result.allocated_bytes == 0
    assert result.issues == ()


def test_nested_files_have_known_sizes(tmp_path: Path) -> None:
    child = tmp_path / "nested"
    child.mkdir()
    files = [tmp_path / "first.bin", child / "second.bin", child / "empty.bin"]
    for path, content in zip(files, [b"abc", b"12345", b""], strict=True):
        path.write_bytes(content)

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.COMPLETED
    assert result.files_count == result.unique_files_count == 3
    assert result.directories_count == 2
    assert result.logical_bytes == 8
    assert result.allocated_bytes == sum(path.stat().st_blocks * 512 for path in files)
    assert files[0].read_bytes() == b"abc"
    assert files[1].read_bytes() == b"12345"


def test_symlinks_never_traverse_their_targets(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * 1000)
    (root / "local.bin").write_bytes(b"abc")
    (root / "external-directory").symlink_to(outside, target_is_directory=True)
    (root / "external-file").symlink_to(outside / "large.bin")
    (root / "cycle").symlink_to(root, target_is_directory=True)
    (root / "broken").symlink_to(tmp_path / "missing")

    result = scan_directory(root)

    assert result.status is ScanStatus.COMPLETED
    assert result.logical_bytes == 3
    assert result.files_count == result.unique_files_count == 1
    assert result.directories_count == 1
    assert result.symlinks_count == 4


def test_hard_links_are_deduplicated_but_copies_are_not(tmp_path: Path) -> None:
    original = tmp_path / "original.bin"
    original.write_bytes(b"payload")
    nested = tmp_path / "nested"
    nested.mkdir()
    os.link(original, nested / "alias.bin")
    independent = nested / "copy.bin"
    independent.write_bytes(b"payload")

    result = scan_directory(tmp_path)

    assert result.files_count == 3
    assert result.unique_files_count == 2
    assert result.logical_bytes == 14
    assert (
        result.allocated_bytes == (original.stat().st_blocks + independent.stat().st_blocks) * 512
    )


def test_hard_link_outside_root_does_not_remove_observed_size(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    original = root / "original.bin"
    original.write_bytes(b"payload")
    os.link(original, tmp_path / "outside-alias.bin")

    result = scan_directory(root)

    assert result.files_count == result.unique_files_count == 1
    assert result.logical_bytes == 7


def test_sparse_file_distinguishes_logical_and_allocated_size(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse.bin"
    with sparse.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)

    result = scan_directory(tmp_path)

    assert result.logical_bytes == 16 * 1024 * 1024
    assert result.allocated_bytes == sparse.stat().st_blocks * 512


def test_fifo_is_counted_without_being_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.COMPLETED
    assert result.special_entries_count == 1
    assert result.files_count == 0
    assert result.logical_bytes == 0


def test_non_ascii_names(tmp_path: Path) -> None:
    nested = tmp_path / "documentação"
    nested.mkdir()
    (nested / "análise.txt").write_bytes(b"abc")

    result = scan_directory(tmp_path)

    assert result.logical_bytes == 3
    assert result.directories_count == 2


def test_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_directory(tmp_path / "missing")


def test_regular_file_root_raises(tmp_path: Path) -> None:
    file = tmp_path / "file.bin"
    file.write_bytes(b"abc")

    with pytest.raises(NotADirectoryError):
        scan_directory(file)


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(OSError):
        scan_directory(link)


def test_depth_limit_marks_partial_and_bounds_issue_details(tmp_path: Path) -> None:
    for name in ("one", "two"):
        child = tmp_path / name
        child.mkdir()
        (child / "unvisited.bin").write_bytes(b"abc")

    result = scan_directory(tmp_path, max_depth=0, max_issues=1)

    assert result.status is ScanStatus.PARTIAL
    assert result.directories_count == 1
    assert result.logical_bytes == 0
    assert result.skipped_directories_count == 2
    assert result.issues_count == 2
    assert len(result.issues) == 1
    assert result.issues[0].code is IssueCode.DEPTH_LIMIT
    assert result.issues_truncated


def test_zero_issue_limit_preserves_partial_status(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()

    result = scan_directory(tmp_path, max_depth=0, max_issues=0)

    assert result.status is ScanStatus.PARTIAL
    assert result.issues_count == 1
    assert result.issues == ()
    assert result.issues_truncated


@pytest.mark.parametrize("max_depth,max_issues", [(-1, 100), (128, -1)])
def test_negative_limits_are_rejected(tmp_path: Path, max_depth: int, max_issues: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        scan_directory(tmp_path, max_depth=max_depth, max_issues=max_issues)


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require a non-root user")
def test_inaccessible_descendant_returns_partial(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"hidden")
    (tmp_path / "visible.bin").write_bytes(b"ok")
    blocked.chmod(0)
    try:
        result = scan_directory(tmp_path)
    finally:
        blocked.chmod(0o700)

    assert result.status is ScanStatus.PARTIAL
    assert result.logical_bytes == 2
    assert result.skipped_directories_count == 1
    assert result.issues[0].code is IssueCode.ACCESS_ERROR
    assert result.issues[0].errno == errno.EACCES


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require a non-root user")
def test_inaccessible_root_raises(tmp_path: Path) -> None:
    root = tmp_path / "blocked"
    root.mkdir()
    root.chmod(0)
    try:
        with pytest.raises(PermissionError):
            scan_directory(root)
    finally:
        root.chmod(0o700)


def test_directory_replaced_with_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.bin").write_bytes(b"x" * 1000)
    open_directory = engine._open_directory

    def replace_before_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        if path == child:
            child.rename(tmp_path / "original-child")
            child.symlink_to(outside, target_is_directory=True)
        return open_directory(path, parent_fd=parent_fd)

    monkeypatch.setattr(engine, "_open_directory", replace_before_open)

    result = scan_directory(root)

    assert result.status is ScanStatus.PARTIAL
    assert result.logical_bytes == 0
    assert result.directories_count == 1
    assert result.skipped_directories_count == 1


def test_directory_identity_change_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    open_directory = engine._open_directory

    def replace_before_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        if path == child:
            child.rename(tmp_path / "original-child")
            child.mkdir()
            (child / "replacement.bin").write_bytes(b"replacement")
        return open_directory(path, parent_fd=parent_fd)

    monkeypatch.setattr(engine, "_open_directory", replace_before_open)

    result = scan_directory(root)

    assert result.status is ScanStatus.PARTIAL
    assert result.logical_bytes == 0
    assert result.issues[0].code is IssueCode.ENTRY_CHANGED


def test_directory_descriptors_are_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "nested").mkdir()
    descriptors: list[int] = []
    open_directory = engine._open_directory

    def record_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        descriptors.append(frame.descriptor)
        return frame

    monkeypatch.setattr(engine, "_open_directory", record_open)

    scan_directory(tmp_path)

    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF
