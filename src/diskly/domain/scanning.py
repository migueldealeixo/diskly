from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ScanStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class IssueCode(StrEnum):
    ACCESS_ERROR = "access_error"
    ENTRY_CHANGED = "entry_changed"
    DEVICE_BOUNDARY = "device_boundary"
    REVISITED_DIRECTORY = "revisited_directory"
    DEPTH_LIMIT = "depth_limit"
    MOUNT_BOUNDARY = "mount_boundary"
    POLICY_UNAVAILABLE = "policy_unavailable"
    STORAGE_ERROR = "storage_error"


@dataclass(frozen=True, slots=True)
class ScanIssue:
    path: Path
    code: IssueCode
    message: str
    errno: int | None = None


@dataclass(frozen=True, slots=True)
class ScanProgress:
    root: Path
    current_path: Path
    status: ScanStatus
    elapsed_seconds: float
    entries_count: int
    files_count: int
    unique_files_count: int
    directories_count: int
    logical_bytes: int
    allocated_bytes: int | None
    issues_count: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    root: Path
    status: ScanStatus
    files_count: int
    unique_files_count: int
    directories_count: int
    symlinks_count: int
    special_entries_count: int
    skipped_directories_count: int
    logical_bytes: int
    allocated_bytes: int | None
    issues_count: int
    issues: tuple[ScanIssue, ...]
    excluded_entries_count: int
    skipped_mounts_count: int
    filesystem_type: str | None
    excluded_paths: tuple[Path, ...]
    entries_count: int
    elapsed_seconds: float

    @property
    def issues_truncated(self) -> bool:
        return self.issues_count > len(self.issues)
