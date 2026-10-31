import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from diskly.domain.scanning import IssueCode, ScanIssue, ScanProgress, ScanResult, ScanStatus
from diskly.scanner import CancellationToken, scan_directory

if TYPE_CHECKING:
    from pytestqt.qtbot import QtBot

    from diskly.ui.window import MainWindow

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")


@pytest.fixture
def window(qtbot: "QtBot", tmp_path: Path) -> "MainWindow":
    from diskly.ui.window import MainWindow

    widget = MainWindow(directory=str(tmp_path))
    qtbot.addWidget(widget)
    widget.show()
    return widget


def wait_for_scan(qtbot: "QtBot", window: "MainWindow") -> None:
    window.start_scan()
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)


def test_background_scan_sort_filter_and_plain_text_details(
    window: "MainWindow", qtbot: "QtBot", tmp_path: Path
) -> None:
    from PySide6.QtCore import Qt

    folder = tmp_path / "nested"
    folder.mkdir()
    (folder / "large.bin").write_bytes(b"x" * 2048)
    (tmp_path / "small.bin").write_bytes(b"abc")
    (tmp_path / "<b>literal</b>".replace("/", "_")).write_bytes(b"x" * 20)
    wait_for_scan(qtbot, window)

    assert "Completed" in window.status.text()
    assert window.metrics[0].text() == "2.0 KiB"
    assert window.metrics[2].text() == "3"
    assert window.files_model.rowCount() == 3
    assert window.directories_model.rowCount() == 1
    proxy = window.proxies[0]
    assert proxy.index(0, 1).data(Qt.ItemDataRole.UserRole) == 2048
    window.tables[0].sortByColumn(1, Qt.SortOrder.AscendingOrder)
    assert proxy.index(0, 1).data(Qt.ItemDataRole.UserRole) == 3
    window.search.setText("LARGE")
    assert proxy.rowCount() == 1
    window.tables[0].selectRow(0)
    assert "large.bin" in window.details.toPlainText()
    assert "2,048 bytes" in window.details.toPlainText()
    window.search.setText("<b>")
    assert proxy.rowCount() == 1
    window.tables[0].selectRow(0)
    assert "<b>literal" in window.details.toPlainText()
    assert window.path.isEnabled()
    assert not window.cancel.isEnabled()


def test_empty_directory_and_restart_clear_old_results(
    window: "MainWindow", qtbot: "QtBot", tmp_path: Path
) -> None:
    file = tmp_path / "old.bin"
    file.write_bytes(b"old")
    wait_for_scan(qtbot, window)
    assert window.files_model.rowCount() == 1
    file.unlink()
    wait_for_scan(qtbot, window)
    assert window.files_model.rowCount() == 0
    assert window.metrics[0].text() == "0 B"
    assert "Coverage: complete" in window.details.toPlainText()


def test_invalid_root_reports_failure_and_recovers(
    window: "MainWindow", qtbot: "QtBot", tmp_path: Path
) -> None:
    window.path.setText(str(tmp_path / "missing"))
    wait_for_scan(qtbot, window)
    assert "failed" in window.status.text()
    assert "FileNotFoundError" in window.details.toPlainText()
    assert window.analyze.isEnabled()
    window.path.setText(str(tmp_path))
    wait_for_scan(qtbot, window)
    assert "Completed" in window.status.text()


def test_partial_result_reports_coverage_and_truncated_issues(
    qtbot: "QtBot", tmp_path: Path
) -> None:
    from diskly.ui.window import MainWindow

    (tmp_path / "a" / "b").mkdir(parents=True)

    def limited_scan(
        root: str,
        *,
        cancellation_token: CancellationToken,
        on_progress: Callable[[ScanProgress], None],
    ) -> ScanResult:
        return scan_directory(
            root,
            max_depth=1,
            max_issues=0,
            cancellation_token=cancellation_token,
            on_progress=on_progress,
        )

    widget = MainWindow(directory=str(tmp_path), scan=limited_scan)
    qtbot.addWidget(widget)
    wait_for_scan(qtbot, widget)
    assert "Partial" in widget.status.text()
    assert "0 of 1 issues" in widget.scope.text()
    assert widget.directories_model.index(0, 4).data() == "Incomplete"
    assert "Coverage: incomplete" in widget.details.toPlainText()


def test_unavailable_allocation_and_untrusted_names(qtbot: "QtBot", tmp_path: Path) -> None:
    from diskly.ui.window import MainWindow

    (tmp_path / "line\nbreak-\udcff").write_bytes(b"x")

    def unavailable_scan(
        root: str,
        *,
        cancellation_token: CancellationToken,
        on_progress: Callable[[ScanProgress], None],
    ) -> ScanResult:
        result = scan_directory(
            root, cancellation_token=cancellation_token, on_progress=on_progress
        )
        return replace(
            result,
            allocated_bytes=None,
            largest_files=tuple(replace(f, allocated_bytes=None) for f in result.largest_files),
            issues=(ScanIssue(tmp_path, IssueCode.ACCESS_ERROR, "<b>plain</b>\nmessage"),),
            issues_count=1,
            status=ScanStatus.PARTIAL,
        )

    widget = MainWindow(directory=str(tmp_path), scan=unavailable_scan)
    qtbot.addWidget(widget)
    wait_for_scan(qtbot, widget)
    assert widget.metrics[1].text() == "Unavailable"
    assert widget.files_model.index(0, 2).data() == "Unavailable"
    assert "line\\nbreak-\\udcff" in widget.files_model.index(0, 0).data()
    widget.tabs.setCurrentIndex(2)
    widget.tables[2].selectRow(0)
    assert "<b>plain</b>\\nmessage" in widget.details.toPlainText()


@pytest.mark.parametrize("close_window", [False, True])
def test_cancellation_and_close_keep_event_loop_responsive(
    qtbot: "QtBot", tmp_path: Path, close_window: bool
) -> None:
    from PySide6.QtCore import QThread, QTimer

    from diskly.ui.window import MainWindow

    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    worker_ids: list[int] = []

    def slow_scan(
        root: str,
        *,
        cancellation_token: CancellationToken,
        on_progress: Callable[[ScanProgress], None],
    ) -> ScanResult:
        worker_ids.append(threading.get_ident())
        started.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release worker")
        if cancellation_token.is_cancelled:
            cancelled.set()
        return scan_directory(root, cancellation_token=cancellation_token, on_progress=on_progress)

    widget = MainWindow(directory=str(tmp_path), scan=slow_scan)
    qtbot.addWidget(widget)
    widget.show()
    main_id = threading.get_ident()
    delivery_threads: list[QThread] = []
    widget.controller.completed.connect(lambda _: delivery_threads.append(QThread.currentThread()))
    widget.start_scan()
    try:
        qtbot.waitUntil(started.is_set)
        assert worker_ids[0] != main_id
        assert not widget.path.isEnabled()
        assert not widget.controller.start(str(tmp_path))
        if close_window:
            assert not widget.close()
            assert widget.isVisible()
        else:
            widget.cancel_scan()
        ticks: list[bool] = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
        assert widget.controller.busy
        assert "Cancelling" in widget.status.text()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not widget.controller.busy, timeout=6000)
    assert cancelled.is_set()
    assert "Cancelled" in widget.status.text()
    assert delivery_threads == [widget.thread()]
    if close_window:
        qtbot.waitUntil(lambda: not widget.isVisible())


def test_controller_ignores_stale_events(qtbot: "QtBot", tmp_path: Path) -> None:
    from diskly.ui.worker import ScanController

    result = scan_directory(tmp_path)
    received: list[ScanResult] = []
    controller = ScanController()
    controller.completed.connect(received.append)
    assert controller.start(str(tmp_path))
    controller._completed(0, result)
    controller._failed(0, "stale")
    assert received == []
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    assert len(received) == 1
    controller._completed(1, result)
    assert len(received) == 1


def test_keyboard_shortcuts_and_theme(window: "MainWindow", qtbot: "QtBot") -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPalette
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    window.search.setFocus()
    QTest.keyClick(window, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(window.path.hasFocus)
    window.theme.setCurrentText("Dark")
    dark = QApplication.palette().color(QPalette.ColorRole.Window).lightness()
    assert window.metrics[0].palette().color(QPalette.ColorRole.WindowText).lightness() > 200
    assert window.path.palette().color(QPalette.ColorRole.Base).lightness() < 50
    window.theme.setCurrentText("Light")
    light = QApplication.palette().color(QPalette.ColorRole.Window).lightness()
    assert window.metrics[0].palette().color(QPalette.ColorRole.WindowText).lightness() < 50
    assert window.path.palette().color(QPalette.ColorRole.Base).lightness() > 200
    assert dark < light


@pytest.mark.skipif(sys.platform != "linux", reason="Linux platform-theme startup")
def test_entrypoint_launches_without_snap_or_desktop_variables(tmp_path: Path) -> None:
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    for key in ("SNAP_NAME", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "QT_QPA_PLATFORMTHEME"):
        environment.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from diskly.__main__ import main
from diskly.ui import app

class CheckedApplication(QApplication):
    def __init__(self, arguments):
        assert arguments[1:] == ['-platformtheme', 'generic'], arguments
        super().__init__(arguments)
        QTimer.singleShot(100, self.quit)

app.QApplication = CheckedApplication
raise SystemExit(main([]))
""",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
