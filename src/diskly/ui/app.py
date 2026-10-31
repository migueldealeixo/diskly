import os
import sys

from PySide6.QtWidgets import QApplication

from diskly.ui.startup import application_arguments
from diskly.ui.window import MainWindow


def launch(directory: str | None = None) -> int:
    app = QApplication(application_arguments(sys.argv[0], os.environ))
    app.setApplicationName("Diskly")
    app.setOrganizationName("Diskly")
    app.setStyle("Fusion")
    window = MainWindow(directory=directory)
    window.show()
    return app.exec()
