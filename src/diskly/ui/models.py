from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from diskly.domain.scanning import ScanResult
from diskly.ui.formatting import format_bytes, safe_text

type Cell = str | int | None
type Index = QModelIndex | QPersistentModelIndex


class ResultsModel(QAbstractTableModel):
    def __init__(self, headers: tuple[str, ...], *, byte_columns: tuple[int, ...] = ()) -> None:
        super().__init__()
        self.headers = headers
        self.byte_columns = byte_columns
        self.rows: tuple[tuple[Cell, ...], ...] = ()
        self.details: tuple[str, ...] = ()

    def replace(self, rows: tuple[tuple[Cell, ...], ...], details: tuple[str, ...]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.details = details
        self.endResetModel()

    def rowCount(self, parent: Index | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent: Index | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.headers)

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        if not 0 <= index.column() < len(self.headers):
            return None
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.UserRole:
            if isinstance(value, str):
                return safe_text(value)
            return -1 if value is None else value
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() in self.byte_columns:
                assert isinstance(value, int) or value is None
                return format_bytes(value)
            return safe_text(value) if value is not None else "Unavailable"
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, int):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.headers):
                return self.headers[section]
            if orientation == Qt.Orientation.Vertical:
                return section + 1
        return None


def populate_results(
    result: ScanResult, files: ResultsModel, directories: ResultsModel, issues: ResultsModel
) -> None:
    files.replace(
        tuple(
            (str(f.path), f.logical_bytes, f.allocated_bytes, f.link_count)
            for f in result.largest_files
        ),
        tuple(
            f"{safe_text(f.path)}\n\nLogical: {f.logical_bytes:,} bytes\n"
            f"Reported allocation: {format_bytes(f.allocated_bytes)}\n"
            f"Device / inode: {f.device} / {f.inode}\nHard links reported: {f.link_count}\n\n"
            "One observed name per unique inode. Links may exist outside this root.\n"
            "Reported allocation is not recoverable space."
            for f in result.largest_files
        ),
    )
    directories.replace(
        tuple(
            (
                str(d.path),
                d.apparent_logical_bytes,
                d.apparent_allocated_bytes,
                d.files_count,
                "Complete" if d.is_complete else "Incomplete",
            )
            for d in result.largest_directories
        ),
        tuple(
            f"{safe_text(d.path)}\n\nApparent logical: {d.apparent_logical_bytes:,} bytes\n"
            f"Apparent allocation: {format_bytes(d.apparent_allocated_bytes)}\n"
            f"File names: {d.files_count:,}\n"
            f"Directories including this one: {d.directories_count:,}\n"
            f"Excluded entries: {d.excluded_entries_count:,}\nIssues: {d.issues_count:,}\n"
            f"Coverage: {'complete within scan scope' if d.is_complete else 'incomplete'}\n\n"
            "Apparent sizes count hard-link names. "
            "Parent and child totals overlap; do not add them."
            for d in result.largest_directories
        ),
    )
    issues.replace(
        tuple((str(i.path), i.code.value, i.message) for i in result.issues),
        tuple(
            f"{safe_text(i.path)}\n\nCode: {i.code.value}\n{safe_text(i.message)}\n"
            f"Errno: {i.errno if i.errno is not None else 'unavailable'}"
            for i in result.issues
        ),
    )
