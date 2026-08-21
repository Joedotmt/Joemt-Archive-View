from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from jvvv.theme import (
    ADOBE_ACCENT_COLOR,
    ADOBE_THEME,
    CUSTOM_THEME,
    DARK_MODE,
    DEFAULT_ACCENT_COLOR,
    FUSION_THEME,
    LIGHT_MODE,
    VSCODE_ACCENT_COLOR,
    VSCODE_THEME,
    application_palette,
    application_stylesheet,
    contrasting_text_color,
    normalize_accent_color,
    normalize_color_mode,
    normalize_theme_style,
    preset_palette,
    preset_stylesheet,
    theme_default_accent,
)


def test_theme_preferences_are_normalized_to_safe_defaults():
    assert normalize_theme_style(FUSION_THEME) == FUSION_THEME
    assert normalize_theme_style(" Adobe ") == ADOBE_THEME
    assert normalize_theme_style("VSCode") == VSCODE_THEME
    assert normalize_theme_style("unknown") == CUSTOM_THEME
    assert normalize_color_mode(LIGHT_MODE) == LIGHT_MODE
    assert normalize_color_mode("unknown") == DARK_MODE
    assert normalize_accent_color("not-a-color") == DEFAULT_ACCENT_COLOR
    assert normalize_accent_color("#ABCDEF") == "#abcdef"


def test_preset_themes_have_recognizable_default_accents():
    assert theme_default_accent(ADOBE_THEME) == ADOBE_ACCENT_COLOR
    assert theme_default_accent(VSCODE_THEME) == VSCODE_ACCENT_COLOR
    assert theme_default_accent(FUSION_THEME) == DEFAULT_ACCENT_COLOR


def test_adobe_stylesheet_uses_spectrum_workspace_layers():
    stylesheet = preset_stylesheet(ADOBE_THEME, 1.0, DARK_MODE, ADOBE_ACCENT_COLOR)

    assert "background-color: #1b1b1b" in stylesheet
    assert "background-color: #222222" in stylesheet
    assert "border-color: #5681ff" in stylesheet
    assert "border-radius: 3px" in stylesheet
    assert "__WINDOW__" not in stylesheet


def test_vscode_stylesheet_matches_modern_workbench_colors_and_scales():
    dark = preset_stylesheet(VSCODE_THEME, 1.5, DARK_MODE, VSCODE_ACCENT_COLOR)
    light = preset_stylesheet(VSCODE_THEME, 1.0, LIGHT_MODE, "#005fb8")

    assert "background-color: #181818" in dark
    assert "background-color: #1f1f1f" in dark
    assert "background-color: #313131" in dark
    assert "border-top: 3px solid #0078d4" in dark
    assert "selection-background-color: #e8e8e8" in light
    assert "__ACCENT__" not in dark


def test_preset_palettes_preserve_theme_surface_hierarchy():
    adobe = preset_palette(ADOBE_THEME, DARK_MODE, ADOBE_ACCENT_COLOR)
    vscode = preset_palette(VSCODE_THEME, LIGHT_MODE, "#005fb8")

    assert adobe.color(QPalette.ColorRole.Window) == QColor("#1b1b1b")
    assert adobe.color(QPalette.ColorRole.Base) == QColor("#1b1b1b")
    assert vscode.color(QPalette.ColorRole.Window) == QColor("#ffffff")
    assert vscode.color(QPalette.ColorRole.Highlight) == QColor("#e8e8e8")


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
