import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from diskly.domain.scanning import IssueCode, ScanIssue, ScanProgress, ScanResult, ScanStatus
from diskly.infrastructure.linux.mounts import read_mount_id
from diskly.infrastructure.persistence.identity_index import (
    DEFAULT_MAX_INDEX_BYTES,
    MIN_INDEX_BYTES,
    IdentityIndex,
)
from diskly.scanner.control import CancellationToken, ProgressCallbackError
from diskly.scanner.policy import ScanPolicy, build_policy


@dataclass(slots=True)
class _DirectoryFrame:
    path: Path
    descriptor: int
    entries: Iterator[os.DirEntry[str]]
    metadata: os.stat_result
    resources: ExitStack


@dataclass(slots=True)
class _Totals:
    entries: int = 0
    files: int = 0
    unique_files: int = 0
    directories: int = 0
    symlinks: int = 0
    special_entries: int = 0
    skipped_directories: int = 0
    excluded_entries: int = 0
    skipped_mounts: int = 0
    logical_bytes: int = 0
    allocated_bytes: int | None = 0
    issues_count: int = 0
    issues: list[ScanIssue] = field(default_factory=list)

    def record(self, issue: ScanIssue, limit: int) -> None:
        self.issues_count += 1
        if len(self.issues) < limit:
            self.issues.append(issue)

    def result(
        self,
        root: Path,
        policy: ScanPolicy | None,
        *,
        cancelled: bool,
        elapsed_seconds: float,
    ) -> ScanResult:
        status = ScanStatus.PARTIAL if self.issues_count else ScanStatus.COMPLETED
        if cancelled:
            status = ScanStatus.CANCELLED
        return ScanResult(
            root=root,
            status=status,
            files_count=self.files,
            unique_files_count=self.unique_files,
            directories_count=self.directories,
            symlinks_count=self.symlinks,
            special_entries_count=self.special_entries,
            skipped_directories_count=self.skipped_directories,
            logical_bytes=self.logical_bytes,
            allocated_bytes=self.allocated_bytes,
            issues_count=self.issues_count,
            issues=tuple(self.issues),
            excluded_entries_count=self.excluded_entries,
            skipped_mounts_count=self.skipped_mounts,
            filesystem_type=policy.filesystem_type if policy else None,
            excluded_paths=tuple(
                sorted(
                    path
                    for path in policy.excluded_paths
                    if path.is_relative_to(policy.canonical_root)
                )
            )
            if policy
            else (),
            entries_count=self.entries,
            elapsed_seconds=elapsed_seconds,
        )


@dataclass(slots=True)
class _ProgressReporter:
    root: Path
    totals: _Totals
    callback: Callable[[ScanProgress], None] | None
    interval: float
    started: float
    current_path: Path
    last_emitted: float | None = None

    def emit(
        self,
        current_path: Path,
        *,
        status: ScanStatus = ScanStatus.RUNNING,
        force: bool = False,
        elapsed_seconds: float | None = None,
    ) -> None:
        self.current_path = current_path
        if self.callback is None:
            return
        now = monotonic()
        if not force and self.last_emitted is not None and now - self.last_emitted < self.interval:
            return
        self.last_emitted = now
        progress = ScanProgress(
            root=self.root,
            current_path=current_path,
            status=status,
            elapsed_seconds=now - self.started if elapsed_seconds is None else elapsed_seconds,
            entries_count=self.totals.entries,
            files_count=self.totals.files,
            unique_files_count=self.totals.unique_files,
            directories_count=self.totals.directories,
            logical_bytes=self.totals.logical_bytes,
            allocated_bytes=self.totals.allocated_bytes,
            issues_count=self.totals.issues_count,
        )
        try:
            self.callback(progress)
        except Exception as error:
            raise ProgressCallbackError("The scan progress callback failed") from error


class _ScanCancelled(Exception):
    pass


def _check_cancelled(token: CancellationToken) -> None:
    if token.is_cancelled:
        raise _ScanCancelled


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _open_directory(path: Path, *, parent_fd: int | None = None) -> _DirectoryFrame:
    with ExitStack() as resources:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        target = path if parent_fd is None else Path(path.name)
        descriptor = os.open(target, flags, dir_fd=parent_fd)
        resources.callback(os.close, descriptor)
        metadata = os.fstat(descriptor)
        entries = resources.enter_context(os.scandir(descriptor))
        return _DirectoryFrame(path, descriptor, entries, metadata, resources.pop_all())


def scan_directory(
    root: str | os.PathLike[str],
    *,
    max_depth: int = 128,
    max_issues: int = 100,
    exclude_paths: Sequence[str | os.PathLike[str]] = (),
    scratch_directory: str | os.PathLike[str] | None = None,
    max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
    cancellation_token: CancellationToken | None = None,
    on_progress: Callable[[ScanProgress], None] | None = None,
    progress_interval: float = 0.2,
) -> ScanResult:
    if max_depth < 0 or max_issues < 0:
        raise ValueError("max_depth and max_issues must be non-negative")
    if max_index_bytes < MIN_INDEX_BYTES:
        raise ValueError(f"max_index_bytes must be at least {MIN_INDEX_BYTES}")
    if not isfinite(progress_interval) or progress_interval <= 0:
        raise ValueError("progress_interval must be finite and positive")

    root_path = Path(os.path.abspath(root))
    token = cancellation_token if cancellation_token is not None else CancellationToken()
    totals = _Totals()
    reporter = _ProgressReporter(
        root_path, totals, on_progress, progress_interval, monotonic(), root_path
    )
    policy: ScanPolicy | None = None
    cancelled = False
    try:
        _check_cancelled(token)
        reporter.emit(root_path, force=True)
        _check_cancelled(token)
        with ExitStack() as resources:
            root_frame = _open_directory(root_path)
            resources.callback(root_frame.resources.close)
            totals.directories = 1
            _check_cancelled(token)
            policy = build_policy(root_path, root_frame.descriptor, exclude_paths)
            _check_cancelled(token)
            workspace = resources.enter_context(
                TemporaryDirectory(prefix="diskly-scan-", dir=scratch_directory)
            )
            workspace_path = Path(workspace).resolve(strict=True)
            policy = policy.with_exclusion(workspace_path)
            _check_cancelled(token)
            try:
                index = IdentityIndex(
                    workspace_path / "identities.sqlite3", max_bytes=max_index_bytes
                )
                resources.callback(index.close)
                _check_cancelled(token)
                _scan(
                    root_frame,
                    policy,
                    index,
                    totals,
                    token,
                    reporter,
                    max_depth=max_depth,
                    max_issues=max_issues,
                )
            except sqlite3.Error as error:
                totals.record(
                    ScanIssue(
                        root_path,
                        IssueCode.STORAGE_ERROR,
                        f"Temporary identity index failed; analysis stopped: {error}",
                    ),
                    max_issues,
                )
        _check_cancelled(token)
    except _ScanCancelled:
        cancelled = True
    result = totals.result(
        root_path, policy, cancelled=cancelled, elapsed_seconds=monotonic() - reporter.started
    )
    reporter.emit(
        reporter.current_path,
        status=result.status,
        force=True,
        elapsed_seconds=result.elapsed_seconds,
    )
    return result


def _scan(
    root_frame: _DirectoryFrame,
    policy: ScanPolicy,
    index: IdentityIndex,
    totals: _Totals,
    token: CancellationToken,
    reporter: _ProgressReporter,
    *,
    max_depth: int,
    max_issues: int,
) -> None:
    root_device = root_frame.metadata.st_dev
    frames = [root_frame]

    try:
        _check_cancelled(token)
        index.add_directory(*_identity(root_frame.metadata))
        while frames:
            frame = frames[-1]
            _check_cancelled(token)
            reporter.emit(frame.path)
            _check_cancelled(token)
            try:
                entry = next(frame.entries)
            except StopIteration:
                frames.pop().resources.close()
                continue
            except OSError as error:
                totals.record(
                    ScanIssue(frame.path, IssueCode.ACCESS_ERROR, str(error), error.errno),
                    max_issues,
                )
                frames.pop().resources.close()
                continue

            entry_path = frame.path / entry.name
            totals.entries += 1
            _check_cancelled(token)
            if policy.is_excluded(entry_path):
                totals.excluded_entries += 1
                continue
            if policy.is_mount_point(entry_path):
                totals.skipped_mounts += 1
                totals.record(
                    ScanIssue(
                        entry_path,
                        IssueCode.MOUNT_BOUNDARY,
                        "Entry is a separate mount; entry and any subtree skipped.",
                    ),
                    max_issues,
                )
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                totals.record(
                    ScanIssue(entry_path, IssueCode.ACCESS_ERROR, str(error), error.errno),
                    max_issues,
                )
                continue

            _check_cancelled(token)
            if metadata.st_dev != root_device:
                if stat.S_ISDIR(metadata.st_mode):
                    totals.skipped_directories += 1
                totals.record(
                    ScanIssue(
                        entry_path,
                        IssueCode.DEVICE_BOUNDARY,
                        "Entry is on a different device; entry and any subtree skipped.",
                    ),
                    max_issues,
                )
                continue

            if stat.S_ISLNK(metadata.st_mode):
                totals.symlinks += 1
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink > 1:
                    _check_cancelled(token)
                    if not index.add_file(*_identity(metadata)):
                        totals.files += 1
                        continue
                _check_cancelled(token)
                totals.files += 1
                totals.unique_files += 1
                totals.logical_bytes += metadata.st_size
                blocks: int | None = getattr(metadata, "st_blocks", None)
                if blocks is None:
                    totals.allocated_bytes = None
                elif totals.allocated_bytes is not None:
                    totals.allocated_bytes += blocks * 512
            elif stat.S_ISDIR(metadata.st_mode):
                code: IssueCode | None = None
                message = ""
                if len(frames) > max_depth:
                    code = IssueCode.DEPTH_LIMIT
                    message = "Directory depth limit reached; subtree skipped."

                if code is not None:
                    totals.skipped_directories += 1
                    totals.record(ScanIssue(entry_path, code, message), max_issues)
                    continue

                try:
                    child = _open_directory(entry_path, parent_fd=frame.descriptor)
                except OSError as error:
                    totals.skipped_directories += 1
                    totals.record(
                        ScanIssue(entry_path, IssueCode.ACCESS_ERROR, str(error), error.errno),
                        max_issues,
                    )
                    continue

                with child.resources as child_resources:
                    _check_cancelled(token)
                    if _identity(child.metadata) != _identity(metadata):
                        totals.skipped_directories += 1
                        totals.record(
                            ScanIssue(
                                entry_path,
                                IssueCode.ENTRY_CHANGED,
                                "Directory changed before opening; subtree skipped.",
                            ),
                            max_issues,
                        )
                        continue

                    try:
                        child_mount_id = read_mount_id(child.descriptor)
                    except OSError as error:
                        totals.skipped_directories += 1
                        totals.record(
                            ScanIssue(
                                entry_path,
                                IssueCode.POLICY_UNAVAILABLE,
                                f"Cannot verify the directory mount; subtree skipped: {error}",
                                error.errno,
                            ),
                            max_issues,
                        )
                        continue
                    _check_cancelled(token)
                    if child_mount_id != policy.mount_id:
                        totals.skipped_mounts += 1
                        totals.skipped_directories += 1
                        totals.record(
                            ScanIssue(
                                entry_path,
                                IssueCode.MOUNT_BOUNDARY,
                                "Directory belongs to a different mount; subtree skipped.",
                            ),
                            max_issues,
                        )
                        continue
                    first_visit = index.add_directory(*_identity(child.metadata))
                    _check_cancelled(token)
                    if not first_visit:
                        totals.skipped_directories += 1
                        totals.record(
                            ScanIssue(
                                entry_path,
                                IssueCode.REVISITED_DIRECTORY,
                                "Directory identity was already visited; subtree skipped.",
                            ),
                            max_issues,
                        )
                        continue
                    frames.append(child)
                    child.resources = child_resources.pop_all()
                    totals.directories += 1
            else:
                totals.special_entries += 1
    finally:
        for frame in reversed(frames):
            frame.resources.close()
