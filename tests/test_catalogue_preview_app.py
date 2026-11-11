"""Preview configuration follows the catalogue; FFmpeg follows the computer."""

import os
import sqlite3
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from jvvv.app import MainWindow, load_preview_settings
from jvvv.database import CatalogueError, Database, create_catalogue, open_catalogue
from jvvv.preview_config import (
    CATALOGUE_PREVIEW_SETTING_KEYS,
    PREVIEWS_FFMPEG_PATH_SETTING,
    PreviewSettings,
    VideoPreviewProfile,
)
from jvvv.preview_service import PreviewValidationReport, ValidationStep


@pytest.fixture
def window(monkeypatch, tmp_path):
    application = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("jvvv.app.QSettings", lambda *args: settings)
    monkeypatch.setattr("jvvv.app.list_connected_volume_snapshots", lambda: [])
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    instance = MainWindow()
    yield instance
    instance.close()
    application.processEvents()


def open_in_window(window, path, *, read_only=False):
    db = (
        Database(path, initialize=False, create=False, read_only=True, check_same_thread=False)
        if read_only else open_catalogue(path, check_same_thread=False)
    )
    window._open_catalogue_in_window(
        db,
        path,
        None,
        connected_volume_snapshots=[],
    )
    QApplication.instance().processEvents()


def save_legacy_globals(window, settings):
    for key, value in settings.as_mapping().items():
        window.settings.setValue(key, value)
    window.settings.sync()


def test_switching_catalogues_loads_independent_preferences_and_close_clears_them(window, tmp_path):
    first_path = tmp_path / "First.jvvv"
    second_path = tmp_path / "Second.jvvv"
    first_settings = PreviewSettings(
        root_directory=str(tmp_path / "first-previews"),
        video=VideoPreviewProfile(fps=0.5, max_height=480),
    )
    second_settings = PreviewSettings(
        root_directory=str(tmp_path / "second-previews"),
        video=VideoPreviewProfile(fps=2.0, max_height=720),
    )
    for path, settings in ((first_path, first_settings), (second_path, second_settings)):
        db = create_catalogue(path)
        db.set_preview_settings(settings)
        db.close()
    assert window.persist_ffmpeg_path("local-ffmpeg.exe")
    for path, settings in (
        (first_path, first_settings), (second_path, second_settings), (first_path, first_settings)
    ):
        open_in_window(window, path)
        assert window.preview_settings == replace(settings, ffmpeg_path="local-ffmpeg.exe")
        window.last_preview_statistics = object()
        assert window.close_catalogue()
        assert window.preview_settings == PreviewSettings(ffmpeg_path="local-ffmpeg.exe")
        assert window.last_preview_statistics is None


def test_legacy_preferences_are_imported_once_and_later_global_changes_do_not_override(window, tmp_path):
    path = tmp_path / "Legacy.jvvv"
    create_catalogue(path).close()
    with sqlite3.connect(path / "catalogue.sqlite") as registry:
        registry.executemany(
            "DELETE FROM catalogue_meta WHERE key = ?",
            [(key,) for key in CATALOGUE_PREVIEW_SETTING_KEYS],
        )
    legacy = PreviewSettings(
        enabled=True,
        root_directory=str(tmp_path / "legacy-previews"),
        ffmpeg_path="local-ffmpeg.exe",
        video=VideoPreviewProfile(fps=0.25, max_height=720, crf=28, preset="fast"),
    )
    save_legacy_globals(window, legacy)
    open_in_window(window, path)
    assert window.db.has_preview_settings()
    assert window.preview_settings == legacy
    assert PREVIEWS_FFMPEG_PATH_SETTING not in window.db.catalogue_meta()
    assert window.close_catalogue()
    save_legacy_globals(window, replace(legacy, video=VideoPreviewProfile(fps=3.0, max_height=480)))
    open_in_window(window, path)
    assert window.preview_settings == legacy


def test_new_catalogue_uses_its_defaults_instead_of_legacy_global_preferences(window, tmp_path):
    legacy = PreviewSettings(
        enabled=True,
        root_directory=str(tmp_path / "old-previews"),
        ffmpeg_path="local-ffmpeg.exe",
        video=VideoPreviewProfile(fps=3.0, max_height=1080),
    )
    save_legacy_globals(window, legacy)
    path = tmp_path / "New.jvvv"
    create_catalogue(path).close()
    open_in_window(window, path)
    assert window.preview_settings == PreviewSettings(ffmpeg_path=legacy.ffmpeg_path)
    assert load_preview_settings(window.settings) == PreviewSettings(ffmpeg_path=legacy.ffmpeg_path)


def test_saving_ffmpeg_changes_only_local_setting_and_keeps_pending_preview_edits(window, tmp_path):
    path = tmp_path / "Archive.jvvv"
    create_catalogue(path).close()
    open_in_window(window, path)
    before = window.db.catalogue_meta()
    window.show_preview_cache()
    dialog = window.preview_cache_dialog
    dialog.preview_widget.video_fps_spin.setValue(2.0)
    assert window.persist_ffmpeg_path("new-ffmpeg.exe")
    assert window.db.catalogue_meta() == before
    assert window.preview_settings == PreviewSettings(ffmpeg_path="new-ffmpeg.exe")
    assert dialog.preview_widget.video_fps_spin.value() == 2.0
    assert dialog.preview_widget.settings().ffmpeg_path == "new-ffmpeg.exe"
    assert load_preview_settings(window.settings) == window.preview_settings
    assert not any(window.settings.contains(key) for key in CATALOGUE_PREVIEW_SETTING_KEYS)


def test_failed_catalogue_save_keeps_runtime_and_database_configuration(window, tmp_path, monkeypatch):
    path = tmp_path / "Archive.jvvv"
    create_catalogue(path).close()
    open_in_window(window, path)
    before = window.preview_settings
    metadata = window.db.catalogue_meta()
    failures = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: failures.append(args))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: failures.append(args))

    def fail_save(_settings):
        raise CatalogueError("The registry is not writable")

    monkeypatch.setattr(window.db, "set_preview_settings", fail_save)
    assert not window.persist_preview_settings(replace(before, video=VideoPreviewProfile(fps=2.0)))
    assert failures
    assert window.preview_settings == before
    assert window.db.catalogue_meta() == metadata


@pytest.mark.parametrize("blocked_by", ["read_only", "busy"])
def test_read_only_or_busy_catalogue_rejects_preview_configuration_changes(
    window, tmp_path, monkeypatch, blocked_by
):
    path = tmp_path / "Archive.jvvv"
    create_catalogue(path).close()
    open_in_window(window, path, read_only=blocked_by == "read_only")
    before = window.preview_settings
    metadata = window.db.catalogue_meta()
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, lambda *args: QMessageBox.StandardButton.Ok)
    if blocked_by == "busy":
        monkeypatch.setattr(window, "_catalogue_job_running", lambda: True)
    assert not window.persist_preview_settings(replace(before, video=VideoPreviewProfile(fps=2.0)))
    assert window.preview_settings == before
    assert window.db.catalogue_meta() == metadata


def test_enabling_previews_is_staged_until_manager_apply_and_then_survives_reopen(window, tmp_path):
    path = tmp_path / "Archive.jvvv"
    create_catalogue(path).close()
    open_in_window(window, path)
    window.show_preview_cache()
    dialog = window.preview_cache_dialog
    widget = dialog.preview_widget
    validated = []

    def validate(candidate, *, include_encode_tests):
        validated.append(candidate)
        return PreviewValidationReport(
            passed=True,
            steps=(ValidationStep("configuration", "Configuration", True, "Valid"),),
            settings=candidate,
            root=candidate.root_directory,
            free_bytes=10**9,
            total_bytes=2 * 10**9,
            ffmpeg_path=candidate.ffmpeg_path or "ffmpeg",
            ffmpeg_version="test",
            encoder_available=True,
            image_backend="test",
            image_profile_id=candidate.image.profile_id,
            video_profile_id=candidate.video.profile_id,
            include_encode_tests=include_encode_tests,
        )

    widget._validator = validate
    widget.root_edit.setText(str(tmp_path / "previews"))
    widget.video_fps_spin.setValue(2.0)
    widget.video_max_height_spin.setValue(480)
    widget.enable_check.setChecked(True)
    assert validated
    assert not window.preview_settings.enabled
    assert not window.db.preview_settings().enabled
    assert dialog.apply_button.isEnabled()
    dialog.apply_button.click()
    expected = widget.settings()
    assert window.preview_settings == expected
    assert window.db.preview_settings(ffmpeg_path=expected.ffmpeg_path) == expected
    assert not any(window.settings.contains(key) for key in CATALOGUE_PREVIEW_SETTING_KEYS)
    assert window.close_catalogue()
    open_in_window(window, path)
    assert window.preview_settings == expected


def test_scan_preflight_waits_for_preview_manager_changes_before_retrying(window, tmp_path, monkeypatch):
    path = tmp_path / "Archive.jvvv"
    db = create_catalogue(path)
    initial = PreviewSettings(enabled=True, root_directory=str(tmp_path / "previews"))
    db.set_preview_settings(initial)
    db.close()
    open_in_window(window, path)
    checked = []

    def preflight(candidate):
        checked.append(candidate)
        return type("Report", (), {"passed": candidate.video.fps == 2.0})()

    monkeypatch.setattr(window, "run_preview_preflight", preflight)
    monkeypatch.setattr(window, "ask_preview_preflight_decision", lambda report: "settings")

    def configure():
        dialog = window.preview_cache_dialog
        dialog.preview_widget.video_fps_spin.setValue(2.0)
        candidate = dialog.preview_widget.settings()
        monkeypatch.setattr(dialog.preview_widget, "validate_for_save", lambda: (candidate, None))
        dialog.apply_button.click()
        dialog.close()

    QTimer.singleShot(0, configure)
    result = window.plan_scan_previews()
    assert len(checked) == 2
    assert result == (replace(initial, video=VideoPreviewProfile(fps=2.0)), None)
    assert window.db.preview_settings().video.fps == 2.0
