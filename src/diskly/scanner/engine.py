import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from diskly.domain.scanning import IssueCode, ScanIssue, ScanResult, ScanStatus
from diskly.infrastructure.linux.mounts import read_mount_id
from diskly.infrastructure.persistence.identity_index import (
    DEFAULT_MAX_INDEX_BYTES,
    MIN_INDEX_BYTES,
    IdentityIndex,
)
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
    files: int = 0
    unique_files: int = 0
    directories: int = 1
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

    def result(self, root: Path, policy: ScanPolicy) -> ScanResult:
        return ScanResult(
            root=root,
            status=ScanStatus.PARTIAL if self.issues_count else ScanStatus.COMPLETED,
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
            filesystem_type=policy.filesystem_type,
            excluded_paths=tuple(
                sorted(
                    path
                    for path in policy.excluded_paths
                    if path.is_relative_to(policy.canonical_root)
                )
            ),
        )


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
) -> ScanResult:
    if max_depth < 0 or max_issues < 0:
        raise ValueError("max_depth and max_issues must be non-negative")
    if max_index_bytes < MIN_INDEX_BYTES:
        raise ValueError(f"max_index_bytes must be at least {MIN_INDEX_BYTES}")

    root_path = Path(os.path.abspath(root))
    with ExitStack() as resources:
        root_frame = _open_directory(root_path)
        resources.callback(root_frame.resources.close)
        policy = build_policy(root_path, root_frame.descriptor, exclude_paths)
        workspace = resources.enter_context(
            TemporaryDirectory(prefix="diskly-scan-", dir=scratch_directory)
        )
        workspace_path = Path(workspace).resolve(strict=True)
        policy = policy.with_exclusion(workspace_path)
        totals = _Totals()
        try:
            index = IdentityIndex(workspace_path / "identities.sqlite3", max_bytes=max_index_bytes)
            resources.callback(index.close)
            _scan(root_frame, policy, index, totals, max_depth=max_depth, max_issues=max_issues)
        except sqlite3.Error as error:
            totals.record(
                ScanIssue(
                    root_path,
                    IssueCode.STORAGE_ERROR,
                    f"Temporary identity index failed; analysis stopped: {error}",
                ),
                max_issues,
            )
        return totals.result(root_path, policy)


def _scan(
    root_frame: _DirectoryFrame,
    policy: ScanPolicy,
    index: IdentityIndex,
    totals: _Totals,
    *,
    max_depth: int,
    max_issues: int,
) -> None:
    root_device = root_frame.metadata.st_dev
    frames = [root_frame]

    try:
        index.add_directory(*_identity(root_frame.metadata))
        while frames:
            frame = frames[-1]
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
                    if not index.add_file(*_identity(metadata)):
                        totals.files += 1
                        continue
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

                if _identity(child.metadata) != _identity(metadata):
                    child.resources.close()
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
                    child.resources.close()
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
                except BaseException:
                    child.resources.close()
                    raise
                if child_mount_id != policy.mount_id:
                    child.resources.close()
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
                try:
                    first_visit = index.add_directory(*_identity(child.metadata))
                except BaseException:
                    child.resources.close()
                    raise
                if not first_visit:
                    child.resources.close()
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
                totals.directories += 1
                frames.append(child)
            else:
                totals.special_entries += 1
    finally:
        for frame in reversed(frames):
            frame.resources.close()
