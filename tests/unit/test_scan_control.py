import errno
import os
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from itertools import pairwise
from pathlib import Path
from threading import Event

import pytest

from diskly.domain.scanning import IssueCode, ScanStatus
from diskly.scanner import (
    CancellationToken,
    ProgressCallbackError,
    ScanProgress,
    engine,
    scan_directory,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _advance_with_entries(monkeypatch: pytest.MonkeyPatch, *, step: float = 0.25) -> _Clock:
    clock = _Clock()
    open_directory = engine._open_directory

    def timed_entries(entries: Iterator[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        for entry in entries:
            clock.now += step
            yield entry

    def open_timed(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        frame.entries = timed_entries(frame.entries)
        return frame

    monkeypatch.setattr(engine, "monotonic", clock)
    monkeypatch.setattr(engine, "_open_directory", open_timed)
    return clock


def test_pre_cancelled_scan_emits_only_terminal_progress_without_opening_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = CancellationToken()
    token.cancel()
    events: list[ScanProgress] = []

    def reject_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        raise AssertionError("A pre-cancelled scan must not open the root")

    monkeypatch.setattr(engine, "_open_directory", reject_open)

    result = scan_directory(
        tmp_path / "missing", cancellation_token=token, on_progress=events.append
    )

    assert result.status is ScanStatus.CANCELLED
    assert result.directories_count == result.files_count == result.entries_count == 0
    assert result.logical_bytes == result.allocated_bytes == 0
    assert result.filesystem_type is None
    assert len(events) == 1
    assert events[0].status is ScanStatus.CANCELLED
    assert events[0].elapsed_seconds == result.elapsed_seconds


def test_initial_progress_callback_can_cancel_before_root_access(tmp_path: Path) -> None:
    token = CancellationToken()
    events: list[ScanProgress] = []

    def cancel_on_start(progress: ScanProgress) -> None:
        events.append(progress)
        if progress.status is ScanStatus.RUNNING:
            token.cancel()

    result = scan_directory(
        tmp_path / "missing", cancellation_token=token, on_progress=cancel_on_start
    )

    assert result.status is ScanStatus.CANCELLED
    assert result.directories_count == 0
    assert [event.status for event in events] == [ScanStatus.RUNNING, ScanStatus.CANCELLED]


def test_cancelled_scan_retains_only_processed_file_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(10):
        (tmp_path / str(number)).write_bytes(b"abc")
    _advance_with_entries(monkeypatch)
    token = CancellationToken()
    events: list[ScanProgress] = []

    def stop_after_three(progress: ScanProgress) -> None:
        events.append(progress)
        if progress.status is ScanStatus.RUNNING and progress.files_count >= 3:
            token.cancel()

    result = scan_directory(tmp_path, cancellation_token=token, on_progress=stop_after_three)

    assert result.status is ScanStatus.CANCELLED
    assert result.files_count == result.unique_files_count == result.entries_count == 3
    assert result.logical_bytes == 9
    assert result.issues_count == 0
    assert events[-1].status is ScanStatus.CANCELLED
    assert events[-1].logical_bytes == result.logical_bytes
    assert len(list(tmp_path.iterdir())) == 10


def test_progress_is_throttled_and_snapshots_are_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(8):
        (tmp_path / str(number)).write_bytes(b"abc")
    _advance_with_entries(monkeypatch, step=0.125)
    events: list[ScanProgress] = []

    result = scan_directory(tmp_path, on_progress=events.append, progress_interval=0.25)

    running = [event for event in events if event.status is ScanStatus.RUNNING]
    assert [event.elapsed_seconds for event in running] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert [event.files_count for event in running] == [0, 2, 4, 6, 8]
    assert events[-1].status is ScanStatus.COMPLETED
    assert result.elapsed_seconds == events[-1].elapsed_seconds == 1.0
    assert events[0].files_count == events[0].directories_count == 0
    for name in ("entries_count", "files_count", "logical_bytes"):
        with pytest.raises(FrozenInstanceError):
            setattr(events[0], name, 999)


def test_empty_scan_emits_initial_and_terminal_progress(tmp_path: Path) -> None:
    events: list[ScanProgress] = []

    result = scan_directory(tmp_path, on_progress=events.append, progress_interval=3600)

    assert [event.status for event in events] == [ScanStatus.RUNNING, ScanStatus.COMPLETED]
    assert events[-1].directories_count == 1
    assert events[-1].entries_count == 0
    assert result.elapsed_seconds >= 0


def test_final_progress_matches_returned_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (child / "data.bin").write_bytes(b"abc")
    (tmp_path / "excluded.bin").write_bytes(b"ignored")
    _advance_with_entries(monkeypatch)
    events: list[ScanProgress] = []

    result = scan_directory(tmp_path, exclude_paths=["excluded.bin"], on_progress=events.append)

    assert result.entries_count == 3
    assert result.files_count == 1
    final = events[-1]
    for name in (
        "root",
        "status",
        "elapsed_seconds",
        "entries_count",
        "files_count",
        "unique_files_count",
        "directories_count",
        "logical_bytes",
        "allocated_bytes",
        "issues_count",
    ):
        assert getattr(final, name) == getattr(result, name)
    for before, after in pairwise(events):
        assert before.elapsed_seconds <= after.elapsed_seconds
        assert before.entries_count <= after.entries_count
        assert before.logical_bytes <= after.logical_bytes


def test_partial_result_has_terminal_progress(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    events: list[ScanProgress] = []

    result = scan_directory(tmp_path, max_depth=0, on_progress=events.append)

    assert result.status is ScanStatus.PARTIAL
    assert events[-1].status is ScanStatus.PARTIAL
    assert events[-1].issues_count == result.issues_count == 1


@pytest.mark.parametrize("interval", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_invalid_progress_interval_is_rejected(tmp_path: Path, interval: float) -> None:
    with pytest.raises(ValueError, match="progress_interval"):
        scan_directory(tmp_path, progress_interval=interval)


def test_cancellation_from_another_thread_waits_for_current_io_and_closes_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    (root / "child").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    opened = Event()
    release = Event()
    token = CancellationToken()
    descriptors: list[int] = []
    open_directory = engine._open_directory

    def blocked_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        descriptors.append(frame.descriptor)
        if parent_fd is not None:
            opened.set()
            if not release.wait(timeout=5):
                frame.resources.close()
                raise TimeoutError("Test did not release the simulated I/O operation")
        return frame

    monkeypatch.setattr(engine, "_open_directory", blocked_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            scan_directory, root, scratch_directory=scratch, cancellation_token=token
        )
        try:
            assert opened.wait(timeout=5)
            token.cancel()
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=5)

    assert result.status is ScanStatus.CANCELLED
    assert result.directories_count == 1
    assert result.entries_count == 1
    assert list(scratch.iterdir()) == []
    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize(
    "callback_error", [ValueError("callback"), sqlite3.OperationalError("callback")]
)
def test_callback_failures_propagate_and_cleanup_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback_error: Exception
) -> None:
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "data.bin").write_bytes(b"abc")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _advance_with_entries(monkeypatch)
    descriptors: list[int] = []
    open_directory = engine._open_directory

    def record_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        descriptors.append(frame.descriptor)
        return frame

    def fail_after_a_file(progress: ScanProgress) -> None:
        if progress.files_count:
            raise callback_error

    monkeypatch.setattr(engine, "_open_directory", record_open)

    with pytest.raises(ProgressCallbackError) as caught:
        scan_directory(root, scratch_directory=scratch, on_progress=fail_after_a_file)

    assert caught.value.__cause__ is callback_error
    assert list(scratch.iterdir()) == []
    for descriptor in descriptors:
        with pytest.raises(OSError) as descriptor_error:
            os.fstat(descriptor)
        assert descriptor_error.value.errno == errno.EBADF


def test_terminal_progress_occurs_after_scratch_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    events: list[ScanProgress] = []

    def observe_cleanup(progress: ScanProgress) -> None:
        if progress.status is not ScanStatus.RUNNING:
            assert list(scratch.iterdir()) == []
        events.append(progress)

    result = scan_directory(root, scratch_directory=scratch, on_progress=observe_cleanup)

    assert result.status is ScanStatus.COMPLETED
    assert events[-1].status is ScanStatus.COMPLETED


def test_terminal_callback_cannot_retroactively_cancel_completed_scan(tmp_path: Path) -> None:
    token = CancellationToken()

    def cancel_on_completion(progress: ScanProgress) -> None:
        if progress.status is ScanStatus.COMPLETED:
            token.cancel()

    result = scan_directory(tmp_path, cancellation_token=token, on_progress=cancel_on_completion)

    assert token.is_cancelled
    assert result.status is ScanStatus.COMPLETED
    assert scan_directory(tmp_path, cancellation_token=token).status is ScanStatus.CANCELLED


def test_cancelled_scan_preserves_existing_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "child").mkdir()
    _advance_with_entries(monkeypatch)
    token = CancellationToken()

    def cancel_after_issue(progress: ScanProgress) -> None:
        if progress.status is ScanStatus.RUNNING and progress.issues_count:
            token.cancel()

    result = scan_directory(
        tmp_path, max_depth=0, cancellation_token=token, on_progress=cancel_after_issue
    )

    assert result.status is ScanStatus.CANCELLED
    assert result.issues_count == 1
    assert result.issues[0].code is IssueCode.DEPTH_LIMIT


def test_root_failure_does_not_emit_successful_terminal_progress(tmp_path: Path) -> None:
    events: list[ScanProgress] = []

    with pytest.raises(FileNotFoundError):
        scan_directory(tmp_path / "missing", on_progress=events.append)

    assert [event.status for event in events] == [ScanStatus.RUNNING]


def test_terminal_callback_failure_propagates_after_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    original_error = ValueError("terminal callback failed")

    def fail_terminal(progress: ScanProgress) -> None:
        if progress.status is not ScanStatus.RUNNING:
            assert list(scratch.iterdir()) == []
            raise original_error

    with pytest.raises(ProgressCallbackError) as caught:
        scan_directory(root, scratch_directory=scratch, on_progress=fail_terminal)

    assert caught.value.__cause__ is original_error
