from __future__ import annotations

import re

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


CUSTOM_THEME = "custom"
FUSION_THEME = "fusion"
ADOBE_THEME = "adobe"
VSCODE_THEME = "vscode"
LIGHT_MODE = "light"
DARK_MODE = "dark"
DEFAULT_THEME_STYLE = CUSTOM_THEME
DEFAULT_COLOR_MODE = DARK_MODE
DEFAULT_ACCENT_COLOR = "#2f9e63"
ADOBE_ACCENT_COLOR = "#5681ff"
VSCODE_ACCENT_COLOR = "#0078d4"

THEME_STYLES = frozenset(
    {CUSTOM_THEME, FUSION_THEME, ADOBE_THEME, VSCODE_THEME}
)


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
    color: __TAB_SELECTED_TEXT__;
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


# Adobe and VS Code both use flat, layered workspaces, but with different
# surface ramps and control proportions. This template keeps the app-specific
# widget coverage in one place while the token sets below preserve each
# product's recognizable chrome.
PRESET_STYLESHEET = """
QWidget {
    color: __FOREGROUND__;
    selection-background-color: __SELECTION__;
    selection-color: __SELECTION_TEXT__;
}

QMainWindow, QDialog, QStackedWidget,
QWidget#welcomePage, QWidget#loadingPage {
    background-color: __WINDOW__;
}

QWidget#volumePane, QWidget#contentPane {
    background-color: __WINDOW__;
}

QLabel#welcomeTitle, QLabel#emptyStateTitle {
    color: __STRONG_TEXT__;
}

QLabel#mutedLabel, QLabel#loadingPath, QLabel#propertySubtitle,
QLabel#detailKey, QLabel#emptyStateDescription {
    color: __MUTED__;
}

QLabel#offlineNotice {
    color: __WARNING_TEXT__;
    background-color: __WARNING_BACKGROUND__;
    border: 1px solid __WARNING_BORDER__;
    border-radius: __RADIUS__px;
    padding: 4px 7px;
}

QLabel#emptyStateTitle {
    font-size: 18px;
    font-weight: 600;
}

QMenuBar {
    background-color: __CHROME__;
    border-bottom: 1px solid __BORDER__;
    padding: 0;
}

QMenuBar::item {
    background: transparent;
    padding: 4px 9px;
}

QMenuBar::item:selected, QMenuBar::item:pressed {
    background-color: __HOVER__;
}

QMenu {
    color: __FOREGROUND__;
    background-color: __MENU__;
    border: 1px solid __BORDER__;
    border-radius: __RADIUS__px;
    padding: 3px 0;
}

QMenu::item {
    padding: 5px 28px 5px 22px;
}

QMenu::item:selected {
    color: __ACCENT_TEXT__;
    background-color: __ACCENT__;
}

QMenu::item:disabled {
    color: __DISABLED_TEXT__;
}

QMenu::separator {
    height: 1px;
    background-color: __BORDER__;
    margin: 4px 8px;
}

QPushButton, QToolButton {
    color: __FOREGROUND__;
    background-color: __BUTTON__;
    border: 1px solid __CONTROL_BORDER__;
    border-radius: __RADIUS__px;
    min-height: 20px;
    padding: 2px 9px;
}

QPushButton:hover, QToolButton:hover {
    background-color: __BUTTON_HOVER__;
}

QPushButton:pressed, QToolButton:pressed,
QPushButton:checked, QToolButton:checked {
    background-color: __BUTTON_PRESSED__;
}

QPushButton:focus, QToolButton:focus {
    border-color: __ACCENT__;
}

QPushButton:disabled, QToolButton:disabled {
    color: __DISABLED_TEXT__;
    background-color: __DISABLED_BACKGROUND__;
    border-color: __BORDER__;
}

QPushButton#primaryButton, QPushButton#searchButton, QPushButton:default {
    color: __ACCENT_TEXT__;
    background-color: __ACCENT__;
    border-color: __ACCENT_BORDER__;
}

QPushButton#primaryButton:hover, QPushButton#searchButton:hover,
QPushButton:default:hover {
    background-color: __ACCENT_HOVER__;
}

QPushButton#primaryButton:pressed, QPushButton#searchButton:pressed,
QPushButton:default:pressed {
    background-color: __ACCENT_PRESSED__;
}

QToolButton#navigationButton {
    padding: 2px;
}

QLineEdit, QComboBox, QDateEdit, QPlainTextEdit, QTextBrowser {
    color: __FOREGROUND__;
    background-color: __FIELD__;
    border: 1px solid __CONTROL_BORDER__;
    border-radius: __RADIUS__px;
    min-height: 20px;
    padding: 2px 6px;
}

QLineEdit:hover, QComboBox:hover, QDateEdit:hover,
QPlainTextEdit:hover, QTextBrowser:hover {
    border-color: __CONTROL_HOVER_BORDER__;
}

QLineEdit:focus, QComboBox:focus, QDateEdit:focus,
QPlainTextEdit:focus, QTextBrowser:focus {
    border-color: __ACCENT__;
}

QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled,
QPlainTextEdit:disabled, QTextBrowser:disabled {
    color: __DISABLED_TEXT__;
    background-color: __DISABLED_BACKGROUND__;
}

QLineEdit#pathField {
    color: __FOREGROUND__;
    background-color: __BASE__;
}

QComboBox::drop-down, QDateEdit::drop-down {
    width: 20px;
    border: 0;
    border-left: 1px solid __CONTROL_BORDER__;
}

QAbstractItemView {
    color: __FOREGROUND__;
    background-color: __BASE__;
    alternate-background-color: __ALTERNATE__;
    border: 1px solid __BORDER__;
    border-radius: __RADIUS__px;
    outline: 0;
}

QAbstractItemView::item {
    padding: 2px 5px;
    border: 0;
}

QAbstractItemView::item:hover:!selected {
    background-color: __LIST_HOVER__;
}

QAbstractItemView::item:selected {
    color: __SELECTION_TEXT__;
    background-color: __SELECTION__;
}

QAbstractItemView::item:selected:!active {
    color: __FOREGROUND__;
    background-color: __INACTIVE_SELECTION__;
}

QHeaderView, QHeaderView::section {
    color: __FOREGROUND__;
    background-color: __HEADER__;
}

QHeaderView::section {
    border: 0;
    border-right: 1px solid __BORDER__;
    border-bottom: 1px solid __BORDER__;
    padding: 4px 6px;
    font-weight: 600;
}

QHeaderView::section:hover {
    background-color: __HOVER__;
}

QTableView {
    gridline-color: __BORDER__;
}

QTreeWidget#folderTree {
    background-color: __SIDEBAR__;
}

QTabWidget::pane, QStackedWidget#searchResultsStack {
    background-color: __BASE__;
    border: 1px solid __BORDER__;
    border-radius: __RADIUS__px;
    top: -1px;
}

QTabBar::tab {
    color: __MUTED__;
    background-color: __TAB_INACTIVE__;
    border: 1px solid __BORDER__;
    border-bottom: 0;
    border-radius: 0;
    padding: 4px 12px;
    margin-right: 0;
}

QTabBar::tab:hover {
    color: __STRONG_TEXT__;
    background-color: __TAB_HOVER__;
}

QTabBar::tab:selected {
    color: __STRONG_TEXT__;
    background-color: __TAB_ACTIVE__;
    border-top: 2px solid __ACCENT__;
    padding-top: 3px;
}

QGroupBox {
    color: __FOREGROUND__;
    background-color: __SURFACE__;
    border: 1px solid __BORDER__;
    border-radius: __RADIUS__px;
    margin-top: 9px;
    padding-top: 7px;
    font-weight: 600;
}

QGroupBox::title {
    color: __FOREGROUND__;
    background-color: __HEADER__;
    border: 1px solid __BORDER__;
    border-radius: __RADIUS__px;
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 7px;
    padding: 1px 7px;
}

QSplitter::handle {
    background-color: __SPLITTER__;
}

QSplitter::handle:hover {
    background-color: __ACCENT__;
}

QProgressBar {
    color: __FOREGROUND__;
    background-color: __PROGRESS_BACKGROUND__;
    border: 1px solid __BORDER__;
    border-radius: __RADIUS__px;
    text-align: center;
}

QProgressBar::chunk {
    background-color: __ACCENT__;
    border-radius: __RADIUS__px;
}

QStatusBar {
    color: __FOREGROUND__;
    background-color: __CHROME__;
    border-top: 1px solid __BORDER__;
}

QStatusBar::item {
    border: 0;
}

QScrollBar:vertical {
    background-color: __SCROLL_TRACK__;
    width: 12px;
    margin: 0;
}

QScrollBar:horizontal {
    background-color: __SCROLL_TRACK__;
    height: 12px;
    margin: 0;
}

QScrollBar::handle {
    background-color: __SCROLL_HANDLE__;
    border-radius: 0;
    min-width: 24px;
    min-height: 24px;
    margin: 2px;
}

QScrollBar::handle:hover {
    background-color: __SCROLL_HANDLE_HOVER__;
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
    color: __STRONG_TEXT__;
    background-color: __TOOLTIP__;
    border: 1px solid __CONTROL_BORDER__;
    border-radius: __RADIUS__px;
    padding: 4px;
}
"""


# The custom theme was originally designed as a dark theme. Keeping its color
# tokens in one stylesheet makes the widget geometry shared by both modes; the
# neutral colors below are translated when light mode is requested.
_LIGHT_COLOR_REPLACEMENTS = {
    "#d7d9dd": "#24262b",
    "#1b1d20": "#f3f4f6",
    "#f0f1f2": "#111318",
    "#979ba2": "#667085",
    "#92969d": "#667085",
    "#efbf78": "#7a4b08",
    "#3a3024": "#fff4dc",
    "#57442c": "#dfb968",
    "#e4e6e9": "#181a1f",
    "#8f939a": "#6d727c",
    "#24262a": "#e9ebef",
    "#101114": "#c7cbd2",
    "#3a3d43": "#d9dce2",
    "#2b2d32": "#ffffff",
    "#111215": "#bfc4cc",
    "#71747a": "#9a9fa8",
    "#17191c": "#d9dce2",
    "#dedfe2": "#272a30",
    "#34363c": "#e2e5ea",
    "#151619": "#b9bec7",
    "#41444b": "#d5d9e0",
    "#282a2f": "#c9ced6",
    "#70737a": "#9a9fa8",
    "#292b30": "#eceef2",
    "#1a1b1e": "#d3d6dc",
    "#202226": "#ffffff",
    "#4a4d54": "#989faa",
    "#1d1f22": "#f1f2f4",
    "#cfd2d7": "#3f444c",
    "#191b1e": "#f7f8fa",
    "#d4d6da": "#24262b",
    "#30333a": "#edf0f4",
    "#c9cbd0": "#343840",
    "#303238": "#e1e4e9",
    "#1c1e22": "#cbd0d8",
    "#2d2f34": "#e2e5ea",
    "#1e2024": "#fafbfc",
    "#aeb1b7": "#535861",
    "#35383e": "#dfe3e8",
    "#23252a": "#f8f9fb",
    "#181a1d": "#f1f3f5",
    "#d9dbe0": "#24262b",
    "#1a1c1f": "#f0f1f3",
    "#45484f": "#c2c7cf",
    "#5a5e66": "#aeb5bf",
    "#82868d": "#858b95",
    "#090a0c": "#aeb3bb",
}


def theme_default_accent(theme_style: str | None) -> str:
    """Return the signature accent used when switching to a theme preset."""
    theme_style = normalize_theme_style(theme_style)
    if theme_style == ADOBE_THEME:
        return ADOBE_ACCENT_COLOR
    if theme_style == VSCODE_THEME:
        return VSCODE_ACCENT_COLOR
    return DEFAULT_ACCENT_COLOR


def _preset_theme_tokens(
    theme_style: str,
    color_mode: str,
    accent_color: str,
) -> dict[str, str]:
    theme_style = normalize_theme_style(theme_style)
    color_mode = normalize_color_mode(color_mode)
    accent = QColor(normalize_accent_color(accent_color))

    if theme_style == ADOBE_THEME:
        # Adobe Spectrum 2 gray stops provide the surface ramp. Premiere's
        # dense workspace adds the near-black dividers between those layers.
        if color_mode == LIGHT_MODE:
            tokens = {
                "FOREGROUND": "#292929",
                "STRONG_TEXT": "#131313",
                "MUTED": "#717171",
                "WINDOW": "#f3f3f3",
                "CHROME": "#e9e9e9",
                "MENU": "#ffffff",
                "SURFACE": "#f8f8f8",
                "BASE": "#ffffff",
                "ALTERNATE": "#f8f8f8",
                "SIDEBAR": "#f3f3f3",
                "FIELD": "#ffffff",
                "BUTTON": "#e1e1e1",
                "BUTTON_HOVER": "#dadada",
                "BUTTON_PRESSED": "#c6c6c6",
                "DISABLED_BACKGROUND": "#e9e9e9",
                "DISABLED_TEXT": "#8f8f8f",
                "BORDER": "#c6c6c6",
                "CONTROL_BORDER": "#8f8f8f",
                "CONTROL_HOVER_BORDER": "#505050",
                "HEADER": "#e9e9e9",
                "HOVER": "#dadada",
                "LIST_HOVER": "#e9e9e9",
                "TAB_INACTIVE": "#e9e9e9",
                "TAB_HOVER": "#f3f3f3",
                "TAB_ACTIVE": "#ffffff",
                "SPLITTER": "#c6c6c6",
                "PROGRESS_BACKGROUND": "#e1e1e1",
                "SCROLL_TRACK": "#f3f3f3",
                "SCROLL_HANDLE": "#8f8f8f",
                "SCROLL_HANDLE_HOVER": "#717171",
                "TOOLTIP": "#ffffff",
                "WARNING_TEXT": "#6d4b00",
                "WARNING_BACKGROUND": "#fff1c2",
                "WARNING_BORDER": "#d5a000",
                "RADIUS": "3",
            }
        else:
            tokens = {
                "FOREGROUND": "#dbdbdb",
                "STRONG_TEXT": "#f2f2f2",
                "MUTED": "#afafaf",
                "WINDOW": "#1b1b1b",
                "CHROME": "#222222",
                "MENU": "#2c2c2c",
                "SURFACE": "#222222",
                "BASE": "#1b1b1b",
                "ALTERNATE": "#202020",
                "SIDEBAR": "#222222",
                "FIELD": "#2c2c2c",
                "BUTTON": "#393939",
                "BUTTON_HOVER": "#444444",
                "BUTTON_PRESSED": "#323232",
                "DISABLED_BACKGROUND": "#222222",
                "DISABLED_TEXT": "#6d6d6d",
                "BORDER": "#111111",
                "CONTROL_BORDER": "#444444",
                "CONTROL_HOVER_BORDER": "#6d6d6d",
                "HEADER": "#2c2c2c",
                "HOVER": "#393939",
                "LIST_HOVER": "#323232",
                "TAB_INACTIVE": "#222222",
                "TAB_HOVER": "#2c2c2c",
                "TAB_ACTIVE": "#2c2c2c",
                "SPLITTER": "#111111",
                "PROGRESS_BACKGROUND": "#111111",
                "SCROLL_TRACK": "#1b1b1b",
                "SCROLL_HANDLE": "#444444",
                "SCROLL_HANDLE_HOVER": "#6d6d6d",
                "TOOLTIP": "#2c2c2c",
                "WARNING_TEXT": "#f5c451",
                "WARNING_BACKGROUND": "#352900",
                "WARNING_BORDER": "#6b5100",
                "RADIUS": "3",
            }
        selection = accent.name()
        selection_text = contrasting_text_color(accent)
    elif theme_style == VSCODE_THEME:
        # These values mirror VS Code's built-in Light Modern and Dark Modern
        # workbench themes, including the distinct side-bar and editor layers.
        if color_mode == LIGHT_MODE:
            tokens = {
                "FOREGROUND": "#3b3b3b",
                "STRONG_TEXT": "#1f1f1f",
                "MUTED": "#616161",
                "WINDOW": "#ffffff",
                "CHROME": "#f8f8f8",
                "MENU": "#ffffff",
                "SURFACE": "#f8f8f8",
                "BASE": "#ffffff",
                "ALTERNATE": "#fafafa",
                "SIDEBAR": "#f8f8f8",
                "FIELD": "#ffffff",
                "BUTTON": "#e5e5e5",
                "BUTTON_HOVER": "#cccccc",
                "BUTTON_PRESSED": "#bdbdbd",
                "DISABLED_BACKGROUND": "#f2f2f2",
                "DISABLED_TEXT": "#868686",
                "BORDER": "#e5e5e5",
                "CONTROL_BORDER": "#cecece",
                "CONTROL_HOVER_BORDER": "#8b949e",
                "HEADER": "#f8f8f8",
                "HOVER": "#f2f2f2",
                "LIST_HOVER": "#f2f2f2",
                "TAB_INACTIVE": "#f8f8f8",
                "TAB_HOVER": "#ffffff",
                "TAB_ACTIVE": "#ffffff",
                "SPLITTER": "#e5e5e5",
                "PROGRESS_BACKGROUND": "#e5e5e5",
                "SCROLL_TRACK": "#ffffff",
                "SCROLL_HANDLE": "#c1c1c1",
                "SCROLL_HANDLE_HOVER": "#a8a8a8",
                "TOOLTIP": "#f8f8f8",
                "WARNING_TEXT": "#895503",
                "WARNING_BACKGROUND": "#fff4ce",
                "WARNING_BORDER": "#d6b656",
                "RADIUS": "2",
            }
            selection = "#e8e8e8"
            selection_text = "#000000"
        else:
            tokens = {
                "FOREGROUND": "#cccccc",
                "STRONG_TEXT": "#ffffff",
                "MUTED": "#9d9d9d",
                "WINDOW": "#1f1f1f",
                "CHROME": "#181818",
                "MENU": "#1f1f1f",
                "SURFACE": "#181818",
                "BASE": "#1f1f1f",
                "ALTERNATE": "#232323",
                "SIDEBAR": "#181818",
                "FIELD": "#313131",
                "BUTTON": "#313131",
                "BUTTON_HOVER": "#2b2b2b",
                "BUTTON_PRESSED": "#3c3c3c",
                "DISABLED_BACKGROUND": "#202020",
                "DISABLED_TEXT": "#868686",
                "BORDER": "#2b2b2b",
                "CONTROL_BORDER": "#3c3c3c",
                "CONTROL_HOVER_BORDER": "#616161",
                "HEADER": "#181818",
                "HOVER": "#2b2b2b",
                "LIST_HOVER": "#2a2d2e",
                "TAB_INACTIVE": "#181818",
                "TAB_HOVER": "#1f1f1f",
                "TAB_ACTIVE": "#1f1f1f",
                "SPLITTER": "#2b2b2b",
                "PROGRESS_BACKGROUND": "#313131",
                "SCROLL_TRACK": "#1f1f1f",
                "SCROLL_HANDLE": "#424242",
                "SCROLL_HANDLE_HOVER": "#4f4f4f",
                "TOOLTIP": "#202020",
                "WARNING_TEXT": "#e2c08d",
                "WARNING_BACKGROUND": "#352a18",
                "WARNING_BORDER": "#6a5126",
                "RADIUS": "2",
            }
            selection = _blend(accent, QColor("#1f1f1f"), 0.45).name()
            selection_text = "#ffffff"
    else:
        raise ValueError(f"{theme_style!r} is not a styled preset theme")

    tokens.update(
        {
            "ACCENT": accent.name(),
            "ACCENT_TEXT": contrasting_text_color(accent),
            "ACCENT_BORDER": accent.darker(118).name(),
            "ACCENT_HOVER": (
                accent.darker(110).name()
                if color_mode == LIGHT_MODE
                else accent.lighter(112).name()
            ),
            "ACCENT_PRESSED": accent.darker(120).name(),
            "SELECTION": selection,
            "SELECTION_TEXT": selection_text,
            "INACTIVE_SELECTION": _blend(
                QColor(selection), QColor(tokens["BASE"]), 0.55
            ).name(),
        }
    )
    return tokens


def normalize_theme_style(theme_style: str | None) -> str:
    normalized = str(theme_style).strip().lower()
    return normalized if normalized in THEME_STYLES else CUSTOM_THEME


def normalize_color_mode(color_mode: str | None) -> str:
    return LIGHT_MODE if str(color_mode).lower() == LIGHT_MODE else DARK_MODE


def normalize_accent_color(accent_color: str | QColor | None) -> str:
    color = QColor(accent_color or DEFAULT_ACCENT_COLOR)
    if not color.isValid():
        color = QColor(DEFAULT_ACCENT_COLOR)
    return color.name(QColor.NameFormat.HexRgb)


def contrasting_text_color(color: str | QColor) -> str:
    value = QColor(color)
    # WCAG relative luminance gives better results than HSV lightness for vivid
    # yellows and cyans.
    channels = []
    for channel in (value.redF(), value.greenF(), value.blueF()):
        channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    luminance = (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])
    return "#111318" if luminance > 0.43 else "#ffffff"


def _blend(color: QColor, background: QColor, color_weight: float) -> QColor:
    color_weight = max(0.0, min(1.0, color_weight))
    background_weight = 1.0 - color_weight
    return QColor(
        round((color.red() * color_weight) + (background.red() * background_weight)),
        round((color.green() * color_weight) + (background.green() * background_weight)),
        round((color.blue() * color_weight) + (background.blue() * background_weight)),
    )


def _accent_replacements(accent_color: str, color_mode: str) -> dict[str, str]:
    accent = QColor(accent_color)
    background = QColor("#f3f4f6" if color_mode == LIGHT_MODE else "#1b1d20")
    return {
        "#2f9e63": accent.name(),
        "#52d893": accent.lighter(135).name(),
        "#237849": accent.darker(135).name(),
        "#39b978": accent.lighter(115).name(),
        "#267d4e": accent.darker(125).name(),
        "#326e50": _blend(accent, background, 0.62).name(),
        "#2ec27e": accent.lighter(112).name(),
        "#57d996": accent.lighter(130).name(),
        "#365b48": _blend(accent, background, 0.38).name(),
    }


_PIXEL_METRIC_RE = re.compile(r"(?<![\w#])(-?\d+)px")


def _scale_stylesheet(stylesheet: str, scale: float) -> str:
    scale = max(0.1, float(scale))

    def replace_metric(match: re.Match[str]) -> str:
        value = int(match.group(1))
        if value == 0:
            return "0px"
        direction = -1 if value < 0 else 1
        scaled = max(1, round(abs(value) * scale)) * direction
        return f"{scaled}px"

    return _PIXEL_METRIC_RE.sub(replace_metric, stylesheet)


def application_stylesheet(
    scale: float = 1.0,
    color_mode: str = DEFAULT_COLOR_MODE,
    accent_color: str = DEFAULT_ACCENT_COLOR,
) -> str:
    """Return the custom theme with colors and pixel metrics applied."""
    scale = max(0.1, float(scale))
    color_mode = normalize_color_mode(color_mode)
    accent_color = normalize_accent_color(accent_color)

    stylesheet = APP_STYLESHEET.replace("#ffffff", "__ACCENT_TEXT__")
    stylesheet = stylesheet.replace(
        "__TAB_SELECTED_TEXT__",
        "#181a1f" if color_mode == LIGHT_MODE else "#ffffff",
    )
    if color_mode == LIGHT_MODE:
        for source, replacement in _LIGHT_COLOR_REPLACEMENTS.items():
            stylesheet = stylesheet.replace(source, replacement)
    for source, replacement in _accent_replacements(accent_color, color_mode).items():
        stylesheet = stylesheet.replace(source, replacement)
    stylesheet = stylesheet.replace("__ACCENT_TEXT__", contrasting_text_color(accent_color))

    return _scale_stylesheet(stylesheet, scale)


def preset_stylesheet(
    theme_style: str,
    scale: float = 1.0,
    color_mode: str = DEFAULT_COLOR_MODE,
    accent_color: str = DEFAULT_ACCENT_COLOR,
) -> str:
    """Return an Adobe- or VS Code-inspired stylesheet."""
    tokens = _preset_theme_tokens(theme_style, color_mode, accent_color)
    stylesheet = PRESET_STYLESHEET
    for name, value in tokens.items():
        stylesheet = stylesheet.replace(f"__{name}__", value)
    return _scale_stylesheet(stylesheet, scale)


def application_palette(
    color_mode: str = DEFAULT_COLOR_MODE,
    accent_color: str = DEFAULT_ACCENT_COLOR,
) -> QPalette:
    color_mode = normalize_color_mode(color_mode)
    accent_color = normalize_accent_color(accent_color)
    palette = QPalette()
    if color_mode == LIGHT_MODE:
        colors = {
            QPalette.ColorRole.Window: "#f3f4f6",
            QPalette.ColorRole.WindowText: "#24262b",
            QPalette.ColorRole.Base: "#ffffff",
            QPalette.ColorRole.AlternateBase: "#f5f6f8",
            QPalette.ColorRole.ToolTipBase: "#ffffff",
            QPalette.ColorRole.ToolTipText: "#111318",
            QPalette.ColorRole.Text: "#24262b",
            QPalette.ColorRole.Button: "#e2e5ea",
            QPalette.ColorRole.ButtonText: "#272a30",
            QPalette.ColorRole.BrightText: "#ffffff",
            QPalette.ColorRole.Light: "#ffffff",
            QPalette.ColorRole.Midlight: "#e8eaee",
            QPalette.ColorRole.Mid: "#c7cbd2",
            QPalette.ColorRole.Dark: "#9da3ad",
            QPalette.ColorRole.Shadow: "#737984",
            QPalette.ColorRole.LinkVisited: "#7655a8",
            QPalette.ColorRole.PlaceholderText: "#858b95",
        }
        disabled_text = "#9298a2"
        disabled_highlight = _blend(QColor(accent_color), QColor("#f3f4f6"), 0.38)
    else:
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
            QPalette.ColorRole.Light: "#4a4d54",
            QPalette.ColorRole.Midlight: "#383b42",
            QPalette.ColorRole.Mid: "#202226",
            QPalette.ColorRole.Dark: "#101114",
            QPalette.ColorRole.Shadow: "#090a0c",
            QPalette.ColorRole.LinkVisited: "#a48ad4",
            QPalette.ColorRole.PlaceholderText: "#82868d",
        }
        disabled_text = "#70737a"
        disabled_highlight = _blend(QColor(accent_color), QColor("#1b1d20"), 0.38)

    colors[QPalette.ColorRole.Highlight] = accent_color
    colors[QPalette.ColorRole.HighlightedText] = contrasting_text_color(accent_color)
    colors[QPalette.ColorRole.Link] = QColor(accent_color).lighter(125).name()
    for role, color in colors.items():
        palette.setColor(role, QColor(color))

    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled_text))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, disabled_highlight)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, QColor(disabled_text))
    return palette


def preset_palette(
    theme_style: str,
    color_mode: str = DEFAULT_COLOR_MODE,
    accent_color: str = DEFAULT_ACCENT_COLOR,
) -> QPalette:
    """Return the palette backing an Adobe or VS Code preset."""
    tokens = _preset_theme_tokens(theme_style, color_mode, accent_color)
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: tokens["WINDOW"],
        QPalette.ColorRole.WindowText: tokens["FOREGROUND"],
        QPalette.ColorRole.Base: tokens["BASE"],
        QPalette.ColorRole.AlternateBase: tokens["ALTERNATE"],
        QPalette.ColorRole.ToolTipBase: tokens["TOOLTIP"],
        QPalette.ColorRole.ToolTipText: tokens["STRONG_TEXT"],
        QPalette.ColorRole.Text: tokens["FOREGROUND"],
        QPalette.ColorRole.Button: tokens["BUTTON"],
        QPalette.ColorRole.ButtonText: tokens["FOREGROUND"],
        QPalette.ColorRole.BrightText: tokens["STRONG_TEXT"],
        QPalette.ColorRole.Light: tokens["CONTROL_HOVER_BORDER"],
        QPalette.ColorRole.Midlight: tokens["BUTTON_HOVER"],
        QPalette.ColorRole.Mid: tokens["BORDER"],
        QPalette.ColorRole.Dark: tokens["SPLITTER"],
        QPalette.ColorRole.Shadow: tokens["BORDER"],
        QPalette.ColorRole.Highlight: tokens["SELECTION"],
        QPalette.ColorRole.HighlightedText: tokens["SELECTION_TEXT"],
        QPalette.ColorRole.Link: tokens["ACCENT"],
        QPalette.ColorRole.LinkVisited: QColor(tokens["ACCENT"]).lighter(125).name(),
        QPalette.ColorRole.PlaceholderText: tokens["MUTED"],
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))

    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            role,
            QColor(tokens["DISABLED_TEXT"]),
        )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        QColor(tokens["INACTIVE_SELECTION"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.HighlightedText,
        QColor(tokens["DISABLED_TEXT"]),
    )
    return palette


def fusion_palette(
    app: QApplication,
    color_mode: str = DEFAULT_COLOR_MODE,
    accent_color: str = DEFAULT_ACCENT_COLOR,
) -> QPalette:
    """Return an unstyled Fusion palette with the requested mode and accent."""
    color_mode = normalize_color_mode(color_mode)
    accent_color = normalize_accent_color(accent_color)
    if color_mode == LIGHT_MODE:
        palette = app.style().standardPalette()
    else:
        palette = application_palette(DARK_MODE, accent_color)

    accent = QColor(accent_color)
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(contrasting_text_color(accent)))
    palette.setColor(QPalette.ColorRole.Link, accent.lighter(125))
    mode_background = QColor("#f3f4f6" if color_mode == LIGHT_MODE else "#1b1d20")
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        _blend(accent, mode_background, 0.38),
    )
    return palette


def apply_application_theme(
    app: QApplication,
    scale: float = 1.0,
    theme_style: str = DEFAULT_THEME_STYLE,
    color_mode: str = DEFAULT_COLOR_MODE,
    accent_color: str = DEFAULT_ACCENT_COLOR,
) -> None:
    """Apply the selected theme, mode, accent, or custom-theme scale."""
    normalized_scale = round(max(0.1, float(scale)), 2)
    theme_style = normalize_theme_style(theme_style)
    color_mode = normalize_color_mode(color_mode)
    accent_color = normalize_accent_color(accent_color)
    theme_key = f"{theme_style}|{color_mode}|{accent_color}"

    if app.property("jvvvThemeKey") != theme_key:
        app.setStyle("Fusion")
        if theme_style == CUSTOM_THEME:
            app.setPalette(application_palette(color_mode, accent_color))
            app.setStyleSheet(application_stylesheet(normalized_scale, color_mode, accent_color))
        elif theme_style == FUSION_THEME:
            app.setStyleSheet("")
            app.setPalette(fusion_palette(app, color_mode, accent_color))
        else:
            app.setPalette(preset_palette(theme_style, color_mode, accent_color))
            app.setStyleSheet(
                preset_stylesheet(
                    theme_style,
                    normalized_scale,
                    color_mode,
                    accent_color,
                )
            )
        app.setProperty("jvvvThemeKey", theme_key)
        app.setProperty("jvvvThemeScale", normalized_scale)
        return

    if app.property("jvvvThemeScale") == normalized_scale:
        return
    if theme_style == CUSTOM_THEME:
        app.setStyleSheet(application_stylesheet(normalized_scale, color_mode, accent_color))
    elif theme_style != FUSION_THEME:
        app.setStyleSheet(
            preset_stylesheet(
                theme_style,
                normalized_scale,
                color_mode,
                accent_color,
            )
        )
    app.setProperty("jvvvThemeScale", normalized_scale)
