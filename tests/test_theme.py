from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from jvvv.theme import (
    CUSTOM_THEME,
    DARK_MODE,
    DEFAULT_ACCENT_COLOR,
    FUSION_THEME,
    LIGHT_MODE,
    application_palette,
    application_stylesheet,
    contrasting_text_color,
    normalize_accent_color,
    normalize_color_mode,
    normalize_theme_style,
)


def test_theme_preferences_are_normalized_to_safe_defaults():
    assert normalize_theme_style(FUSION_THEME) == FUSION_THEME
    assert normalize_theme_style("unknown") == CUSTOM_THEME
    assert normalize_color_mode(LIGHT_MODE) == LIGHT_MODE
    assert normalize_color_mode("unknown") == DARK_MODE
    assert normalize_accent_color("not-a-color") == DEFAULT_ACCENT_COLOR
    assert normalize_accent_color("#ABCDEF") == "#abcdef"


def test_custom_stylesheet_applies_light_mode_accent_and_scale():
    stylesheet = application_stylesheet(1.5, LIGHT_MODE, "#3366cc")

    assert "background-color: #f3f4f6" in stylesheet
    assert "selection-background-color: #3366cc" in stylesheet
    assert "border-radius: 8px" in stylesheet
    assert "__ACCENT_TEXT__" not in stylesheet


def test_application_palette_uses_mode_and_accent():
    dark = application_palette(DARK_MODE, "#ffcc00")
    light = application_palette(LIGHT_MODE, "#3366cc")

    assert dark.color(QPalette.ColorRole.Window) == QColor("#1b1d20")
    assert dark.color(QPalette.ColorRole.Highlight) == QColor("#ffcc00")
    assert dark.color(QPalette.ColorRole.HighlightedText) == QColor("#111318")
    assert light.color(QPalette.ColorRole.Window) == QColor("#f3f4f6")
    assert light.color(QPalette.ColorRole.Highlight) == QColor("#3366cc")


def test_accent_contrast_handles_bright_and_dark_colors():
    assert contrasting_text_color("#ffdd00") == "#111318"
    assert contrasting_text_color("#223366") == "#ffffff"


def test_selected_tab_text_uses_its_surface_not_accent_contrast():
    dark = application_stylesheet(1.0, DARK_MODE, "#ffffff")
    light = application_stylesheet(1.0, LIGHT_MODE, "#ffffff")

    assert "selection-color: #111318" in dark
    assert "QTabBar::tab:selected {\n    color: #ffffff;" in dark
    assert "QTabBar::tab:selected {\n    color: #181a1f;" in light
    assert "__TAB_SELECTED_TEXT__" not in dark
