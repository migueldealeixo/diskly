from pathlib import Path
from typing import cast

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from diskly.domain.scanning import ScanProgress, ScanResult, ScanStatus
from diskly.scanner import scan_directory
from diskly.ui.formatting import format_bytes, safe_text
from diskly.ui.models import ResultsModel, populate_results
from diskly.ui.theme import apply_theme
from diskly.ui.worker import ScanController, ScanFunction


def label(text: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class MainWindow(QMainWindow):
    def __init__(
        self, *, directory: str | None = None, scan: ScanFunction = scan_directory
    ) -> None:
        super().__init__()
        self.setWindowTitle("Diskly — Storage analysis")
        self.resize(1120, 820)
        self._closing = False
        self._cancelling = False
        self._overview = ""
        self.controller = ScanController(self, scan=scan)
        self.controller.progress.connect(self._progress)
        self.controller.completed.connect(self._completed)
        self.controller.failed.connect(self._failed)
        self.controller.busy_changed.connect(self._busy_changed)
        self._build(directory)

    def _build(self, directory: str | None) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        self.setCentralWidget(central)
        header = QHBoxLayout()
        title = label("Diskly")
        font = QFont(title.font())
        font.setPointSize(24)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)
        header.addWidget(label("Storage analysis · Offline"))
        header.addStretch()
        header.addWidget(label("Analysis only"))
        self.theme = QComboBox()
        self.theme.addItems(["System", "Light", "Dark"])
        self.theme.setAccessibleName("Color theme")
        self.theme.currentTextChanged.connect(self._theme_changed)
        header.addWidget(self.theme)
        layout.addLayout(header)

        path_row = QHBoxLayout()
        self.path = QLineEdit(directory if directory is not None else str(Path.home()))
        self.path.setAccessibleName("Directory to analyze")
        self.path.returnPressed.connect(self.start_scan)
        path_label = QLabel("&Directory")
        path_label.setBuddy(self.path)
        path_row.addWidget(path_label)
        path_row.addWidget(self.path, 1)
        self.browse = QPushButton("&Browse…")
        self.browse.clicked.connect(self._browse)
        path_row.addWidget(self.browse)
        self.analyze = QPushButton("&Analyze")
        self.analyze.setObjectName("analyze")
        self.analyze.clicked.connect(self.start_scan)
        path_row.addWidget(self.analyze)
        self.cancel = QPushButton("&Cancel")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_scan)
        path_row.addWidget(self.cancel)
        layout.addLayout(path_row)
        layout.addWidget(
            label(
                "Scope: selected local directory. Symlinks are not followed; descendant mounts are "
                "skipped. Diskly's own data and scan workspace are excluded.",
                wrap=True,
            )
        )

        cards = QGridLayout()
        self.metrics: list[QLabel] = []
        for column, caption in enumerate(
            (
                "Logical size · unique files",
                "Reported allocation",
                "Unique files",
                "Directories visited",
            )
        ):
            card = QFrame()
            card.setObjectName("metricCard")
            box = QVBoxLayout(card)
            value = label("—")
            font = QFont(value.font())
            font.setPointSize(19)
            font.setBold(True)
            value.setFont(font)
            value.setAccessibleName(caption)
            box.addWidget(value)
            box.addWidget(label(caption))
            self.metrics.append(value)
            cards.addWidget(card, 0, column)
        layout.addLayout(cards)
        self.status = label("Ready — choose a directory and select Analyze.", wrap=True)
        self.status.setAccessibleName("Scan status")
        layout.addWidget(self.status)
        self.activity = label("No scan started.", wrap=True)
        layout.addWidget(self.activity)
        self.progress_bar = QProgressBar()
        self.progress_bar.setAccessibleName("Analysis activity")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search retained rows…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search retained results")
        search_label = QLabel("&Search")
        search_label.setBuddy(self.search)
        search_row.addWidget(search_label)
        search_row.addWidget(self.search)
        layout.addLayout(search_row)
        self.scope = label(
            "Rankings retain up to 200 entries each; search filters these rows only.", wrap=True
        )
        layout.addWidget(self.scope)

        self.tabs = QTabWidget()
        self.files_model = ResultsModel(
            ("File path", "Logical size", "Allocation", "Links"), byte_columns=(1, 2)
        )
        self.directories_model = ResultsModel(
            ("Directory path", "Apparent size", "Apparent allocation", "File names", "Coverage"),
            byte_columns=(1, 2),
        )
        self.issues_model = ResultsModel(("Path", "Code", "Message"))
        self.proxies: list[QSortFilterProxyModel] = []
        self.tables: list[QTableView] = []
        for caption, model in (
            ("Largest files", self.files_model),
            ("Largest directories", self.directories_model),
            ("Issues", self.issues_model),
        ):
            proxy = QSortFilterProxyModel(self)
            proxy.setSourceModel(model)
            proxy.setSortRole(Qt.ItemDataRole.UserRole)
            proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            proxy.setFilterKeyColumn(-1)
            self.search.textChanged.connect(proxy.setFilterFixedString)
            table = QTableView()
            table.setAccessibleName(caption)
            table.setModel(proxy)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.setSortingEnabled(True)
            table.sortByColumn(
                1 if model is not self.issues_model else 0, Qt.SortOrder.DescendingOrder
            )
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().hide()
            table.selectionModel().selectionChanged.connect(self._selection_changed)
            self.tabs.addTab(table, caption)
            self.proxies.append(proxy)
            self.tables.append(table)
        self.tabs.currentChanged.connect(self._selection_changed)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(
            label(
                "Directory sizes count hard-link names and overlap parent/child totals. "
                "Do not add them. Allocation is not recoverable space.",
                wrap=True,
            )
        )
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Selected result details")
        self.details.setPlaceholderText("Select a row to inspect its measurements and coverage.")
        self.details.setMaximumHeight(150)
        layout.addWidget(self.details)
        self.setStyleSheet(
            "QFrame#metricCard { background: palette(base); border: 1px solid palette(mid); "
            "border-radius: 8px; } QLineEdit { padding: 6px; } "
            "QPushButton { padding: 6px 12px; } "
            "QPushButton#analyze { background: palette(highlight); "
            "color: palette(highlighted-text); }"
        )
        for key, action in (
            ("Ctrl+L", self.path.setFocus),
            ("Ctrl+O", self._browse),
            ("Escape", self.cancel_scan),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(action)
        app = cast(QApplication, QApplication.instance())
        app.styleHints().colorSchemeChanged.connect(self._system_theme_changed)
        self._theme_changed("System")

    @Slot(str)
    def _theme_changed(self, mode: str) -> None:
        apply_theme(cast(QApplication, QApplication.instance()), mode)
        self.setStyleSheet(self.styleSheet())

    def _system_theme_changed(self, scheme: Qt.ColorScheme) -> None:
        if self.theme.currentText() == "System":
            self._theme_changed("System")

    @Slot()
    def _browse(self) -> None:
        if self.controller.busy:
            return
        directory = QFileDialog.getExistingDirectory(self, "Select a directory", self.path.text())
        if directory:
            self.path.setText(directory)

    @Slot()
    def start_scan(self) -> None:
        if self.controller.busy or self._closing:
            return
        if not self.path.text():
            self.status.setText("Select a directory before starting.")
            self.path.setFocus()
            return
        self._cancelling = False
        self._overview = ""
        self.search.clear()
        self.details.clear()
        for model in (self.files_model, self.directories_model, self.issues_model):
            model.replace((), ())
        for value in self.metrics:
            value.setText("—")
        self.status.setText("Analyzing…")
        self.activity.setText("Preparing analysis…")
        self.scope.setText("Rankings will appear after analysis. Search covers retained rows only.")
        self.controller.start(self.path.text())

    @Slot()
    def cancel_scan(self) -> None:
        if self.controller.busy:
            self._cancelling = True
            self.controller.cancel()
            self.cancel.setEnabled(False)
            self.status.setText("Cancelling… waiting for current filesystem work to finish.")

    @Slot(bool)
    def _busy_changed(self, busy: bool) -> None:
        for widget in (self.path, self.browse, self.analyze):
            widget.setEnabled(not busy)
        self.cancel.setEnabled(busy and not self._cancelling)
        self.progress_bar.setVisible(busy)
        self.progress_bar.setRange(0, 0 if busy else 1)
        if not busy and self._closing:
            QTimer.singleShot(0, self.close)

    def _update_metrics(self, value: ScanProgress | ScanResult) -> None:
        for widget, text in zip(
            self.metrics,
            (
                format_bytes(value.logical_bytes),
                format_bytes(value.allocated_bytes),
                f"{value.unique_files_count:,}",
                f"{value.directories_count:,}",
            ),
            strict=True,
        ):
            widget.setText(text)

    @Slot(object)
    def _progress(self, value: ScanProgress) -> None:
        self._update_metrics(value)
        self.activity.setText(
            f"{value.elapsed_seconds:,.1f}s · {value.entries_count:,} entries · "
            f"{value.issues_count:,} issues\n{safe_text(value.current_path)}"
        )

    @Slot(object)
    def _completed(self, result: ScanResult) -> None:
        self._update_metrics(result)
        populate_results(result, self.files_model, self.directories_model, self.issues_model)
        messages = {
            ScanStatus.COMPLETED: "Completed within scan scope.",
            ScanStatus.PARTIAL: "Partial analysis — some entries could not be measured.",
            ScanStatus.CANCELLED: "Cancelled — totals and rankings cover observed entries only.",
        }
        self.status.setText(messages.get(result.status, "Analysis stopped."))
        self.activity.setText(
            f"{result.elapsed_seconds:,.1f}s · {result.entries_count:,} entries · "
            f"{result.files_count:,} file names · {result.issues_count:,} issues\n"
            f"{safe_text(result.root)}"
        )
        self.scope.setText(
            f"Retained: {len(result.largest_files):,} files and "
            f"{len(result.largest_directories):,} directories (limit {result.top_limit:,} each); "
            f"{len(result.issues):,} of {result.issues_count:,} issues. "
            "Search filters retained rows only."
        )
        root = result.root_directory
        overview = (
            f"Root: {safe_text(result.root)}\n"
            f"Filesystem: {result.filesystem_type or 'unavailable'}\n"
            f"Excluded entries: {result.excluded_entries_count:,} · "
            f"Skipped mounts: {result.skipped_mounts_count:,} · "
            f"Skipped directories: {result.skipped_directories_count:,}\n"
            f"Symlinks: {result.symlinks_count:,} · "
            f"Special entries: {result.special_entries_count:,}"
        )
        if root is not None:
            overview += (
                f"\nRoot apparent logical: {root.apparent_logical_bytes:,} bytes · "
                f"Coverage: {'complete within scope' if root.is_complete else 'incomplete'}"
            )
        overview += "\nExcluded paths:" + "".join(
            f"\n{safe_text(p)}" for p in result.excluded_paths
        )
        self._overview = overview
        self.details.setPlainText(self._overview)

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.status.setText(
            "Analysis failed — review the details and choose an accessible local directory."
        )
        self.activity.setText("No result is available for this analysis.")
        self.scope.setText("No results retained for this analysis.")
        for value in self.metrics:
            value.setText("—")
        self._overview = message
        self.details.setPlainText(message)

    def _selection_changed(self) -> None:
        index = self.tabs.currentIndex()
        selected = self.tables[index].selectionModel().selectedRows()
        if not selected:
            self.details.setPlainText(self._overview)
            return
        source = self.proxies[index].mapToSource(selected[0])
        model = (self.files_model, self.directories_model, self.issues_model)[index]
        self.details.setPlainText(model.details[source.row()])

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.controller.busy:
            self._closing = True
            self.cancel_scan()
            event.ignore()
        else:
            event.accept()
