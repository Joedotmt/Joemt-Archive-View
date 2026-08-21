from __future__ import annotations

import re

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# A compact, neutral dark theme inspired by Blender's workspace chrome and
# GNOME's Adwaita controls. Green marks interactive and selected states while
# the subdued surfaces keep dense catalogue data easy to scan.
APP_STYLESHEET = """
QWidget {
    color: #d7d9dd;
    selection-background-color: #2f9e63;
    selection-color: #ffffff;
}

QMainWindow, QDialog, QStackedWidget {
    background-color: #1b1d20;
}

QWidget#welcomePage, QWidget#loadingPage {
    background-color: #1b1d20;
}

QLabel#welcomeTitle {
    color: #f0f1f2;
}

QLabel#mutedLabel, QLabel#loadingPath, QLabel#propertySubtitle {
    color: #979ba2;
}

QLabel#detailKey {
    color: #92969d;
}

QLabel#offlineNotice {
    color: #efbf78;
    background-color: #3a3024;
    border: 1px solid #57442c;
    border-radius: 5px;
    padding: 4px 7px;
}

QLabel#emptyStateTitle {
    color: #e4e6e9;
    font-size: 18px;
    font-weight: 600;
}

QLabel#emptyStateDescription {
    color: #8f939a;
}

QMenuBar {
    background-color: #24262a;
    border-bottom: 1px solid #101114;
    padding: 0;
}

QMenuBar::item {
    background: transparent;
    padding: 4px 9px;
}

QMenuBar::item:selected, QMenuBar::item:pressed {
    background-color: #3a3d43;
}

QMenu {
    background-color: #2b2d32;
    border: 1px solid #111215;
    border-radius: 6px;
    padding: 4px 0;
}

QMenu::item {
    padding: 5px 28px 5px 22px;
}

QMenu::item:selected {
    background-color: #2f9e63;
    color: #ffffff;
}

QMenu::item:disabled {
    color: #71747a;
}

QMenu::separator {
    height: 1px;
    background-color: #17191c;
    margin: 4px 8px;
}

QPushButton, QToolButton {
    color: #dedfe2;
    background-color: #34363c;
    border: 1px solid #151619;
    border-radius: 5px;
    min-height: 20px;
    padding: 2px 9px;
}

QPushButton:hover, QToolButton:hover {
    background-color: #41444b;
}

QPushButton:pressed, QToolButton:pressed,
QPushButton:checked, QToolButton:checked {
    background-color: #282a2f;
}

QPushButton:focus, QToolButton:focus {
    border-color: #52d893;
}

QPushButton:disabled, QToolButton:disabled {
    color: #70737a;
    background-color: #292b30;
    border-color: #1a1b1e;
}

QPushButton#primaryButton, QPushButton#searchButton, QPushButton:default {
    color: #ffffff;
    background-color: #2f9e63;
    border-color: #237849;
}

QPushButton#primaryButton:hover, QPushButton#searchButton:hover,
QPushButton:default:hover {
    background-color: #39b978;
}

QPushButton#primaryButton:pressed, QPushButton#searchButton:pressed,
QPushButton:default:pressed {
    background-color: #267d4e;
}

QToolButton#navigationButton {
    padding: 2px;
}

QLineEdit, QComboBox, QDateEdit, QPlainTextEdit, QTextBrowser {
    color: #d7d9dd;
    background-color: #202226;
    border: 1px solid #101114;
    border-radius: 5px;
    min-height: 20px;
    padding: 2px 6px;
}

QLineEdit:hover, QComboBox:hover, QDateEdit:hover,
QPlainTextEdit:hover, QTextBrowser:hover {
    border-color: #4a4d54;
}

QLineEdit:focus, QComboBox:focus, QDateEdit:focus,
QPlainTextEdit:focus, QTextBrowser:focus {
    border-color: #52d893;
}

QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled,
QPlainTextEdit:disabled, QTextBrowser:disabled {
    color: #71747a;
    background-color: #1d1f22;
}

QLineEdit#pathField {
    color: #cfd2d7;
    background-color: #191b1e;
}

QComboBox::drop-down, QDateEdit::drop-down {
    width: 20px;
    border: 0;
    border-left: 1px solid #151619;
}

QAbstractItemView {
    color: #d4d6da;
    background-color: #202226;
    alternate-background-color: #24262a;
    border: 1px solid #101114;
    border-radius: 5px;
    outline: 0;
}

QAbstractItemView::item {
    padding: 2px 5px;
    border: 0;
}

QAbstractItemView::item:hover:!selected {
    background-color: #30333a;
}

QAbstractItemView::item:selected {
    color: #ffffff;
    background-color: #2f9e63;
}

QAbstractItemView::item:selected:!active {
    background-color: #326e50;
}

QHeaderView {
    background-color: #303238;
}

QHeaderView::section {
    color: #c9cbd0;
    background-color: #303238;
    border: 0;
    border-right: 1px solid #1c1e22;
    border-bottom: 1px solid #111215;
    padding: 4px 6px;
    font-weight: 600;
}

QHeaderView::section:hover {
    background-color: #3a3d43;
}

QTableView {
    gridline-color: #2d2f34;
}

QTreeWidget {
    background-color: #1e2024;
}

QTabWidget::pane {
    background-color: #202226;
    border: 1px solid #101114;
    border-radius: 5px;
    top: -1px;
}

QStackedWidget#searchResultsStack {
    background-color: #202226;
    border: 1px solid #101114;
    border-radius: 5px;
}

QTabBar::tab {
    color: #aeb1b7;
    background-color: #292b30;
    border: 1px solid #17181b;
    border-bottom: 0;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    padding: 4px 12px;
    margin-right: 1px;
}

QTabBar::tab:hover {
    color: #e4e5e7;
    background-color: #35383e;
}

QTabBar::tab:selected {
    color: #ffffff;
    background-color: #3a3d43;
    border-top: 2px solid #2ec27e;
    padding-top: 3px;
}

QGroupBox {
    background-color: #23252a;
    border: 1px solid #101114;
    border-radius: 6px;
    margin-top: 9px;
    padding-top: 7px;
    font-weight: 600;
}

QGroupBox::title {
    color: #c9cbd0;
    background-color: #303238;
    border: 1px solid #101114;
    border-radius: 4px;
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 7px;
    padding: 1px 7px;
}

QSplitter::handle {
    background-color: #101114;
}

QSplitter::handle:hover {
    background-color: #2ec27e;
}

QProgressBar {
    color: #d9dbe0;
    background-color: #181a1d;
    border: 1px solid #101114;
    border-radius: 5px;
    text-align: center;
}

QProgressBar::chunk {
    background-color: #2f9e63;
    border-radius: 4px;
}

QStatusBar {
    color: #aeb1b7;
    background-color: #24262a;
    border-top: 1px solid #101114;
}

QStatusBar::item {
    border: 0;
}

QScrollBar:vertical {
    background-color: #1a1c1f;
    width: 12px;
    margin: 0;
}

QScrollBar:horizontal {
    background-color: #1a1c1f;
    height: 12px;
    margin: 0;
}

QScrollBar::handle {
    background-color: #45484f;
    border-radius: 5px;
    min-width: 24px;
    min-height: 24px;
    margin: 2px;
}

QScrollBar::handle:hover {
    background-color: #5a5e66;
}

QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    width: 0;
    height: 0;
    background: transparent;
}

QCheckBox {
    spacing: 6px;
}

QCheckBox::indicator {
    width: 14px;
    height: 14px;
}

QToolTip {
    color: #f0f1f2;
    background-color: #303238;
    border: 1px solid #111215;
    border-radius: 4px;
    padding: 4px;
}
"""


_PIXEL_METRIC_RE = re.compile(r"(?<![\w#])(-?\d+)px")


def application_stylesheet(scale: float = 1.0) -> str:
    """Return the theme with every pixel metric scaled consistently."""
    scale = max(0.1, float(scale))

    def replace_metric(match: re.Match[str]) -> str:
        value = int(match.group(1))
        if value == 0:
            return "0px"
        direction = -1 if value < 0 else 1
        scaled = max(1, round(abs(value) * scale)) * direction
        return f"{scaled}px"

    return _PIXEL_METRIC_RE.sub(replace_metric, APP_STYLESHEET)


def application_palette() -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#1b1d20",
        QPalette.ColorRole.WindowText: "#d7d9dd",
        QPalette.ColorRole.Base: "#202226",
        QPalette.ColorRole.AlternateBase: "#24262a",
        QPalette.ColorRole.ToolTipBase: "#303238",
        QPalette.ColorRole.ToolTipText: "#f0f1f2",
        QPalette.ColorRole.Text: "#d7d9dd",
        QPalette.ColorRole.Button: "#34363c",
        QPalette.ColorRole.ButtonText: "#dedfe2",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: "#2f9e63",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Light: "#4a4d54",
        QPalette.ColorRole.Midlight: "#383b42",
        QPalette.ColorRole.Mid: "#202226",
        QPalette.ColorRole.Dark: "#101114",
        QPalette.ColorRole.Shadow: "#090a0c",
        QPalette.ColorRole.Link: "#57d996",
        QPalette.ColorRole.LinkVisited: "#a48ad4",
        QPalette.ColorRole.PlaceholderText: "#82868d",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))

    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#70737a"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#365b48"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, QColor("#a6a9ae"))
    return palette


def apply_application_theme(app: QApplication, scale: float = 1.0) -> None:
    """Install or rescale the application-wide compact dark theme."""
    normalized_scale = round(max(0.1, float(scale)), 2)
    if not app.property("jvvvThemeApplied"):
        app.setStyle("Fusion")
        app.setPalette(application_palette())
        app.setProperty("jvvvThemeApplied", True)
    if app.property("jvvvThemeScale") == normalized_scale:
        return
    app.setStyleSheet(application_stylesheet(normalized_scale))
    app.setProperty("jvvvThemeScale", normalized_scale)
