from __future__ import annotations

import re

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


CUSTOM_THEME = "custom"
FUSION_THEME = "fusion"
LIGHT_MODE = "light"
DARK_MODE = "dark"
DEFAULT_THEME_STYLE = CUSTOM_THEME
DEFAULT_COLOR_MODE = DARK_MODE
DEFAULT_ACCENT_COLOR = "#2f9e63"


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


def normalize_theme_style(theme_style: str | None) -> str:
    return FUSION_THEME if str(theme_style).lower() == FUSION_THEME else CUSTOM_THEME


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

    def replace_metric(match: re.Match[str]) -> str:
        value = int(match.group(1))
        if value == 0:
            return "0px"
        direction = -1 if value < 0 else 1
        scaled = max(1, round(abs(value) * scale)) * direction
        return f"{scaled}px"

    return _PIXEL_METRIC_RE.sub(replace_metric, stylesheet)


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
        else:
            app.setStyleSheet("")
            app.setPalette(fusion_palette(app, color_mode, accent_color))
        app.setProperty("jvvvThemeKey", theme_key)
        app.setProperty("jvvvThemeScale", normalized_scale)
        return

    if app.property("jvvvThemeScale") == normalized_scale:
        return
    if theme_style == CUSTOM_THEME:
        app.setStyleSheet(application_stylesheet(normalized_scale, color_mode, accent_color))
    app.setProperty("jvvvThemeScale", normalized_scale)
