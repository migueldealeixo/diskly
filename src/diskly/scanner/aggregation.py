from dataclasses import dataclass
from pathlib import Path

from diskly.domain.scanning import DirectorySummary


@dataclass(slots=True)
class DirectoryAccumulator:
    logical_bytes: int = 0
    allocated_bytes: int | None = 0
    files_count: int = 0
    directories_count: int = 1
    excluded_entries_count: int = 0
    issues_count: int = 0
    is_complete: bool = True

    def add_file(self, logical_bytes: int, allocated_bytes: int | None) -> None:
        self.files_count += 1
        self.logical_bytes += logical_bytes
        if allocated_bytes is None:
            self.allocated_bytes = None
        elif self.allocated_bytes is not None:
            self.allocated_bytes += allocated_bytes

    def merge(self, child: DirectorySummary) -> None:
        self.logical_bytes += child.apparent_logical_bytes
        self.files_count += child.files_count
        self.directories_count += child.directories_count
        self.excluded_entries_count += child.excluded_entries_count
        self.issues_count += child.issues_count
        self.is_complete = self.is_complete and child.is_complete
        if child.apparent_allocated_bytes is None:
            self.allocated_bytes = None
        elif self.allocated_bytes is not None:
            self.allocated_bytes += child.apparent_allocated_bytes

    def summary(self, path: Path, *, exhausted: bool) -> DirectorySummary:
        return DirectorySummary(
            path=path,
            apparent_logical_bytes=self.logical_bytes,
            apparent_allocated_bytes=self.allocated_bytes,
            files_count=self.files_count,
            directories_count=self.directories_count,
            excluded_entries_count=self.excluded_entries_count,
            issues_count=self.issues_count,
            is_complete=self.is_complete and exhausted and self.issues_count == 0,
        )
