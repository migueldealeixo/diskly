from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from diskly.domain.scanning import ScanProgress, ScanResult
from diskly.scanner import CancellationToken, scan_directory
from diskly.ui.formatting import safe_text


class ScanFunction(Protocol):
    def __call__(
        self,
        root: str,
        *,
        cancellation_token: CancellationToken,
        on_progress: Callable[[ScanProgress], None],
    ) -> ScanResult: ...


class ScanThread(QThread):
    progress = Signal(int, object)
    completed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        job: int,
        root: str,
        token: CancellationToken,
        scan: ScanFunction,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self.job = job
        self.root = root
        self.token = token
        self.scan = scan

    def run(self) -> None:
        try:
            result = self.scan(
                self.root,
                cancellation_token=self.token,
                on_progress=lambda value: self.progress.emit(self.job, value),
            )
        except Exception as error:
            self.failed.emit(self.job, safe_text(f"{type(error).__name__}: {error}"))
        else:
            self.completed.emit(self.job, result)


class ScanController(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self, parent: QObject | None = None, *, scan: ScanFunction = scan_directory
    ) -> None:
        super().__init__(parent)
        self._scan = scan
        self._thread: ScanThread | None = None
        self._job = 0

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, root: str) -> bool:
        if self.busy:
            return False
        self._job += 1
        thread = ScanThread(self._job, root, CancellationToken(), self._scan, self)
        thread.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        thread.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        thread.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._thread = thread
        self.busy_changed.emit(True)
        thread.start()
        return True

    def cancel(self) -> None:
        if self._thread is not None:
            self._thread.token.cancel()

    @Slot(int, object)
    def _progress(self, job: int, value: ScanProgress) -> None:
        if self.busy and job == self._job:
            self.progress.emit(value)

    @Slot(int, object)
    def _completed(self, job: int, value: ScanResult) -> None:
        if self.busy and job == self._job:
            self.completed.emit(value)

    @Slot(int, str)
    def _failed(self, job: int, message: str) -> None:
        if self.busy and job == self._job:
            self.failed.emit(message)

    @Slot()
    def _finished(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        thread.deleteLater()
        self.busy_changed.emit(False)
