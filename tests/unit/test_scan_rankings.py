import os
import sqlite3
from collections.abc import Iterator
from itertools import count
from pathlib import Path
from unittest.mock import Mock

import pytest

from diskly.domain.scanning import ScanStatus
from diskly.infrastructure.persistence.identity_index import IdentityIndex
from diskly.scanner import CancellationToken, ScanProgress, engine, scan_directory


def test_empty_root_has_complete_summary_and_empty_rankings(tmp_path: Path) -> None:
    result = scan_directory(tmp_path)

    assert result.top_limit == 200
    assert result.largest_files == ()
    assert result.largest_directories == ()
    assert result.root_directory is not None
    assert result.root_directory.path == tmp_path
    assert result.root_directory.is_complete
    assert result.root_directory.directories_count == 1
    assert result.root_directory.files_count == 0
    assert result.root_directory.apparent_logical_bytes == 0
    assert result.root_directory.apparent_allocated_bytes == 0


def test_largest_files_are_bounded_sorted_and_ties_use_path(tmp_path: Path) -> None:
    for name, size in (("small", 1), ("z-equal", 4), ("a-equal", 4), ("huge", 10)):
        (tmp_path / name).write_bytes(b"x" * size)

    result = scan_directory(tmp_path, top_limit=2)

    assert [item.path.name for item in result.largest_files] == ["huge", "a-equal"]
    assert [item.logical_bytes for item in result.largest_files] == [10, 4]
    assert result.logical_bytes == 19
    for item in result.largest_files:
        metadata = item.path.stat()
        assert item.allocated_bytes == metadata.st_blocks * 512
        assert item.device == metadata.st_dev
        assert item.inode == metadata.st_ino
        assert item.link_count == 1


def test_nested_directory_summaries_are_recursive_and_root_is_not_ranked(tmp_path: Path) -> None:
    a = tmp_path / "a"
    inner = a / "inner"
    inner.mkdir(parents=True)
    b = tmp_path / "b"
    b.mkdir()
    (inner / "four.bin").write_bytes(b"1234")
    (a / "three.bin").write_bytes(b"123")
    (b / "eight.bin").write_bytes(b"12345678")
    (tmp_path / "two.bin").write_bytes(b"12")

    result = scan_directory(tmp_path)

    assert [item.path for item in result.largest_directories] == [b, a, inner]
    assert [item.apparent_logical_bytes for item in result.largest_directories] == [8, 7, 4]
    assert [item.files_count for item in result.largest_directories] == [1, 2, 1]
    assert [item.directories_count for item in result.largest_directories] == [1, 2, 1]
    assert all(item.is_complete for item in result.largest_directories)
    assert result.root_directory is not None
    assert result.root_directory.apparent_logical_bytes == result.logical_bytes == 17
    assert result.root_directory.directories_count == result.directories_count == 4
    assert result.root_directory.files_count == result.files_count == 4


def test_file_ranking_deduplicates_links_and_directory_sizes_remain_apparent(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    original = a / "original.bin"
    original.write_bytes(b"payload")
    alias = b / "alias.bin"
    os.link(original, alias)
    independent = b / "copy.bin"
    independent.write_bytes(b"payload")

    result = scan_directory(tmp_path)

    assert result.files_count == 3
    assert result.unique_files_count == len(result.largest_files) == 2
    assert len({(item.device, item.inode) for item in result.largest_files}) == 2
    linked = [item for item in result.largest_files if item.link_count > 1]
    assert len(linked) == 1
    assert linked[0].path in (original, alias)
    assert result.logical_bytes == 14
    assert result.root_directory is not None
    assert result.root_directory.apparent_logical_bytes == 21
    assert result.root_directory.files_count == 3
    assert (
        result.root_directory.apparent_allocated_bytes
        == (2 * original.stat().st_blocks + independent.stat().st_blocks) * 512
    )
    assert [(item.path, item.apparent_logical_bytes) for item in result.largest_directories] == [
        (b, 14),
        (a, 7),
    ]


def test_sparse_files_are_ranked_by_logical_size(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse.bin"
    with sparse.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    (tmp_path / "regular.bin").write_bytes(b"regular")

    result = scan_directory(tmp_path, top_limit=1)

    assert result.largest_files[0].path == sparse
    assert result.largest_files[0].logical_bytes == 16 * 1024 * 1024
    assert result.largest_files[0].allocated_bytes == sparse.stat().st_blocks * 512


def test_exclusions_are_reflected_in_directory_scope(tmp_path: Path) -> None:
    a = tmp_path / "a"
    ignored = a / "ignored"
    ignored.mkdir(parents=True)
    (ignored / "hidden.bin").write_bytes(b"hidden")
    (a / "excluded.bin").write_bytes(b"excluded")
    (a / "included.bin").write_bytes(b"abc")

    result = scan_directory(tmp_path, exclude_paths=["a/ignored", "a/excluded.bin"])

    assert result.status is ScanStatus.COMPLETED
    assert len(result.largest_directories) == 1
    summary = result.largest_directories[0]
    assert summary.path == a
    assert summary.is_complete
    assert summary.apparent_logical_bytes == 3
    assert summary.excluded_entries_count == 2
    assert result.root_directory is not None
    assert result.root_directory.excluded_entries_count == 2
    assert [item.path.name for item in result.largest_files] == ["included.bin"]


@pytest.mark.parametrize("max_issues", [0, 100])
def test_directory_completeness_and_issues_are_scoped_to_the_subtree(
    tmp_path: Path, max_issues: int
) -> None:
    a = tmp_path / "a"
    deep = a / "deep"
    deep.mkdir(parents=True)
    b = tmp_path / "b"
    b.mkdir()
    (deep / "unvisited.bin").write_bytes(b"hidden")
    (a / "visible.bin").write_bytes(b"abc")
    (b / "visible.bin").write_bytes(b"12345")

    result = scan_directory(tmp_path, max_depth=1, max_issues=max_issues)

    summaries = {item.path: item for item in result.largest_directories}
    assert result.status is ScanStatus.PARTIAL
    assert set(summaries) == {a, b}
    assert summaries[a].apparent_logical_bytes == 3
    assert not summaries[a].is_complete
    assert summaries[a].issues_count == 1
    assert summaries[b].is_complete
    assert summaries[b].issues_count == 0
    assert result.root_directory is not None
    assert result.root_directory.apparent_logical_bytes == 8
    assert result.root_directory.issues_count == 1
    assert not result.root_directory.is_complete


def test_cancelled_scan_finalizes_open_directory_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    for number in range(3):
        (child / str(number)).write_bytes(b"abc")
    ticks = count()
    monkeypatch.setattr(engine, "monotonic", lambda: next(ticks) * 0.25)
    token = CancellationToken()

    def cancel_after_file(progress: ScanProgress) -> None:
        if progress.status is ScanStatus.RUNNING and progress.files_count:
            token.cancel()

    result = scan_directory(tmp_path, cancellation_token=token, on_progress=cancel_after_file)

    assert result.status is ScanStatus.CANCELLED
    assert result.logical_bytes == 3
    assert len(result.largest_files) == len(result.largest_directories) == 1
    assert result.largest_files[0].logical_bytes == 3
    summary = result.largest_directories[0]
    assert summary.path == child
    assert summary.apparent_logical_bytes == 3
    assert not summary.is_complete
    assert result.root_directory is not None
    assert result.root_directory.apparent_logical_bytes == 3
    assert result.root_directory.files_count == result.files_count == 1
    assert not result.root_directory.is_complete
    assert len(list(child.iterdir())) == 3


def test_storage_failure_preserves_observed_summaries_and_marks_root_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a"
    (a / "inner").mkdir(parents=True)
    (a / "data.bin").write_bytes(b"abc")
    calls = 0
    add_directory = IdentityIndex.add_directory

    def fail_inner(index: IdentityIndex, device: int, inode: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.OperationalError("database or disk is full")
        return add_directory(index, device, inode)

    monkeypatch.setattr(IdentityIndex, "add_directory", fail_inner)

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.PARTIAL
    assert result.root_directory is not None
    assert not result.root_directory.is_complete
    assert result.root_directory.issues_count == result.issues_count == 1
    assert result.root_directory.apparent_logical_bytes == result.logical_bytes
    assert len(result.largest_directories) == 1
    assert result.largest_directories[0].path == a
    assert not result.largest_directories[0].is_complete


def test_zero_limit_disables_rankings_but_keeps_root_summary(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (child / "data.bin").write_bytes(b"abc")

    result = scan_directory(tmp_path, top_limit=0)

    assert result.largest_files == ()
    assert result.largest_directories == ()
    assert result.top_limit == 0
    assert result.logical_bytes == 3
    assert result.root_directory is not None
    assert result.root_directory.apparent_logical_bytes == 3


def test_negative_limit_is_rejected_before_root_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="top_limit"):
        scan_directory(tmp_path / "missing", top_limit=-1)


def test_precancelled_scan_has_no_directory_summary(tmp_path: Path) -> None:
    token = CancellationToken()
    token.cancel()

    result = scan_directory(tmp_path, cancellation_token=token)

    assert result.root_directory is None
    assert result.largest_files == ()
    assert result.largest_directories == ()


def test_default_rankings_keep_only_the_largest_two_hundred_entries(tmp_path: Path) -> None:
    for number in range(225):
        directory = tmp_path / f"directory-{number:03}"
        directory.mkdir()
        (directory / "data.bin").write_bytes(b"x" * (number + 1))

    result = scan_directory(tmp_path)

    assert len(result.largest_files) == len(result.largest_directories) == 200
    assert [item.logical_bytes for item in result.largest_files] == list(range(225, 25, -1))
    assert [item.apparent_logical_bytes for item in result.largest_directories] == list(
        range(225, 25, -1)
    )
    assert result.root_directory is not None
    assert result.root_directory.files_count == result.files_count == 225
    assert result.root_directory.directories_count == result.directories_count == 226
    assert result.root_directory.apparent_logical_bytes == sum(range(1, 226))


def test_missing_allocation_metric_propagates_through_directory_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    file = child / "data.bin"
    file.write_bytes(b"abc")
    metadata = os.stat_result(tuple(file.stat()))
    entry = Mock(spec=os.DirEntry)
    entry.name = file.name
    entry.stat.return_value = metadata
    open_directory = engine._open_directory

    def replace_child_entries(
        path: Path, *, parent_fd: int | None = None
    ) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        if path == child:
            frame.entries = iter([entry])
        return frame

    monkeypatch.setattr(engine, "_open_directory", replace_child_entries)

    result = scan_directory(tmp_path)

    assert result.logical_bytes == 3
    assert result.allocated_bytes is None
    assert result.largest_files[0].allocated_bytes is None
    assert result.largest_directories[0].apparent_allocated_bytes is None
    assert result.root_directory is not None
    assert result.root_directory.apparent_allocated_bytes is None
    assert result.root_directory.is_complete


def test_iterator_failure_preserves_the_observed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("one.bin", "two.bin"):
        (tmp_path / name).write_bytes(b"abc")
    open_directory = engine._open_directory

    def fail_after_one(entries: Iterator[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        yield next(entries)
        raise OSError("Directory iteration failed")

    def replace_entries(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        frame.entries = fail_after_one(frame.entries)
        return frame

    monkeypatch.setattr(engine, "_open_directory", replace_entries)

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.PARTIAL
    assert result.files_count == len(result.largest_files) == 1
    assert result.root_directory is not None
    assert result.root_directory.apparent_logical_bytes == result.logical_bytes == 3
    assert result.root_directory.issues_count == 1
    assert not result.root_directory.is_complete


def test_file_ranking_preserves_non_utf8_names(tmp_path: Path) -> None:
    file = tmp_path / os.fsdecode(b"raw-\xff.bin")
    file.write_bytes(b"abc")

    result = scan_directory(tmp_path)

    assert result.largest_files[0].path == file
    assert result.largest_files[0].logical_bytes == 3
