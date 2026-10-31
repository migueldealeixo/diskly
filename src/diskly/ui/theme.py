from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication, mode: str) -> None:
    dark = mode == "Dark" or (
        mode == "System" and app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    )
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#171e27" if dark else "#f3f6f8",
        QPalette.ColorRole.WindowText: "#e7edf4" if dark else "#172631",
        QPalette.ColorRole.Base: "#10171f" if dark else "#ffffff",
        QPalette.ColorRole.AlternateBase: "#1e2934" if dark else "#edf3f5",
        QPalette.ColorRole.Text: "#e7edf4" if dark else "#172631",
        QPalette.ColorRole.Button: "#263442" if dark else "#e2ebef",
        QPalette.ColorRole.ButtonText: "#e7edf4" if dark else "#172631",
        QPalette.ColorRole.Highlight: "#167d87" if dark else "#086673",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#263442" if dark else "#ffffff",
        QPalette.ColorRole.ToolTipText: "#e7edf4" if dark else "#172631",
        QPalette.ColorRole.Mid: "#455766" if dark else "#b8c9d1",
        QPalette.ColorRole.Light: "#354858" if dark else "#ffffff",
        QPalette.ColorRole.Midlight: "#2c3b4b" if dark else "#eaf0f4",
        QPalette.ColorRole.Dark: "#0b1016" if dark else "#a2b4c0",
        QPalette.ColorRole.Shadow: "#070b10" if dark else "#8c9da7",
        QPalette.ColorRole.Link: "#66d6df" if dark else "#006877",
        QPalette.ColorRole.LinkVisited: "#b6a4ee" if dark else "#694899",
        QPalette.ColorRole.PlaceholderText: "#a0b1bf" if dark else "#536b78",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
    ):
        palette.setColor(
            QPalette.ColorGroup.Disabled, role, QColor("#8293a0" if dark else "#657986")
        )
    app.setPalette(palette)
