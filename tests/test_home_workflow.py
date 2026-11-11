"""Home, recent catalogue history, and creating catalogues from repositories."""

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QPushButton

from jvvv.app import (
    LAST_CATALOGUE_PATH_SETTING,
    MAX_RECENT_CATALOGUES,
    RECENT_CATALOGUE_PATHS_SETTING,
    MainWindow,
    SnapshotHistoryDialog,
    SnapshotPickerDialog,
    load_recent_catalogue_paths,
)
from jvvv.database import create_catalogue
from jvvv.snapshot_repository import SnapshotRepository


@pytest.fixture
def window(monkeypatch, tmp_path):
    application = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("jvvv.app.QSettings", lambda *args: settings)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    monkeypatch.setattr("jvvv.app.list_connected_volume_snapshots", lambda: [])
    instance = MainWindow()
    yield instance
    instance.close()
    application.processEvents()


def wait_for_open(window, timeout=15):
    application = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if window.db is not None and not window._catalogue_open_in_progress():
            return
        time.sleep(0.01)
    pytest.fail("Catalogue did not finish opening")


def create_in_window(window, monkeypatch, target):
    monkeypatch.setattr(window, "_choose_new_catalogue_path", lambda: target)
    window.new_catalogue()
    assert window.catalogue_path == target


def test_startup_stays_on_home_and_migrates_the_previous_last_location(window, monkeypatch, tmp_path):
    previous = str(tmp_path / "Previously Open.jvvv")
    window.settings.setValue(LAST_CATALOGUE_PATH_SETTING, previous)
    monkeypatch.setattr(window, "open_catalogue_path", lambda *args, **kwargs: pytest.fail("Auto-open"))
    window.refresh_recent_catalogues()
    QApplication.instance().processEvents()
    assert window.db is None
    assert window.stack.currentWidget() is window.welcome_page
    assert load_recent_catalogue_paths(window.settings) == [previous]
    assert window.recent_catalogue_table.item(0, 1).text() == previous
    assert [action.text() for action in window.window_menu.actions() if action.isVisible()] == [
        "App Settings", "Help",
    ]


def test_successful_create_and_recent_open_return_to_the_same_catalogue(window, monkeypatch, tmp_path):
    path = tmp_path / "Archive.jvvv"
    create_in_window(window, monkeypatch, path)
    window.db.create_volume("Archive drive", str(tmp_path), {"drive_id": "AID-001"})
    assert load_recent_catalogue_paths(window.settings) == [str(path)]
    assert window.catalogue_name_label.text() == path.name
    assert window.catalogue_path_label.text() == str(path)
    assert window.close_catalogue()
    assert window.stack.currentWidget() is window.welcome_page
    window.recent_open_button.click()
    wait_for_open(window)
    assert window.catalogue_path == path
    assert window.db.list_volumes()[0]["name"] == "Archive drive"
    assert load_recent_catalogue_paths(window.settings) == [str(path)]


def test_recent_history_is_bounded_deduplicated_and_persistent(window, tmp_path):
    locations = [tmp_path / f"Archive {index}.jvvv" for index in range(MAX_RECENT_CATALOGUES + 2)]
    for location in locations:
        window.remember_recent_catalogue(location)
    window.remember_recent_catalogue(locations[-3])
    expected = [str(locations[-3])] + [
        str(location) for location in reversed(locations[-MAX_RECENT_CATALOGUES:])
        if location != locations[-3]
    ]
    reader = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    assert load_recent_catalogue_paths(reader) == expected
    assert window.recent_catalogue_table.rowCount() == MAX_RECENT_CATALOGUES


def test_recent_locations_do_not_probe_offline_drives(window, monkeypatch, tmp_path):
    locations = [str(tmp_path / "missing" / "Archive.jvvv"), str(tmp_path / "another" / "Archive.jvvv")]
    window.settings.setValue(RECENT_CATALOGUE_PATHS_SETTING, locations)
    monkeypatch.setattr(Path, "exists", lambda *args: pytest.fail("Home must not probe drives"))
    window.refresh_recent_catalogues()
    opened = []
    monkeypatch.setattr(window, "open_catalogue_path", lambda path: opened.append(path))
    window.recent_catalogue_table.selectRow(1)
    window.recent_catalogue_table.doubleClicked.emit(window.recent_catalogue_table.model().index(1, 0))
    assert opened == [Path(locations[1])]
    assert load_recent_catalogue_paths(window.settings) == locations


def test_failed_recent_open_keeps_history_and_returns_to_a_usable_home(window, monkeypatch, tmp_path):
    missing = tmp_path / "Offline.jvvv"
    window.remember_recent_catalogue(missing)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args))
    window.recent_open_button.click()
    deadline = time.monotonic() + 15
    while not errors and time.monotonic() < deadline:
        QApplication.instance().processEvents()
        time.sleep(0.01)
    assert errors
    assert window.db is None
    assert window.stack.currentWidget() is window.welcome_page
    assert load_recent_catalogue_paths(window.settings) == [str(missing)]
    assert all(action.isEnabled() for action in window.home_actions)


def test_removing_the_last_recent_entry_only_forgets_the_location(window, tmp_path):
    path = tmp_path / "Keep.jvvv"
    path.mkdir()
    marker = path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    window.remember_recent_catalogue(path)
    window.recent_remove_button.click()
    assert load_recent_catalogue_paths(window.settings) == []
    assert marker.read_text(encoding="utf-8") == "keep"
    reader = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    assert load_recent_catalogue_paths(reader) == []
    assert not window.recent_open_button.isEnabled()


def test_home_commands_are_blocked_while_open_and_return_home_can_be_cancelled(window, monkeypatch, tmp_path):
    path = tmp_path / "Active.jvvv"
    create_in_window(window, monkeypatch, path)
    monkeypatch.setattr(window, "_choose_new_catalogue_path", lambda: pytest.fail("New while open"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: pytest.fail("Picker while open"))
    window.new_catalogue()
    window.open_catalogue_from_dialog()
    window.restore_snapshot_from_dialog()
    window.open_catalogue_path(tmp_path / "Other.jvvv")
    assert window.catalogue_path == path
    assert all(not action.isVisible() and not action.isEnabled() for action in window.home_actions)
    monkeypatch.setattr(window, "_confirm_snapshot_before_close", lambda: False)
    window.home_button.click()
    assert window.catalogue_path == path
    assert window.stack.currentWidget() is window.catalogue_page
    monkeypatch.setattr(window, "_confirm_snapshot_before_close", lambda: True)
    window.home_button.click()
    assert window.db is None
    assert all(action.isVisible() and action.isEnabled() for action in window.home_actions)


def test_loading_and_repository_work_disable_home_actions_and_recent_opening(window, tmp_path):
    window.remember_recent_catalogue(tmp_path / "Recent.jvvv")
    window._set_catalogue_loading(True)
    assert all(not action.isEnabled() for action in window.home_actions)
    assert not window.recent_open_button.isEnabled()
    window._set_catalogue_loading(False)
    assert window.recent_open_button.isEnabled()
    window._set_repository_running(True)
    assert not window.welcome_restore_button.isEnabled()
    assert not window.recent_open_button.isEnabled()
    window._set_repository_running(False)
    assert window.welcome_restore_button.isEnabled()
    assert window.recent_open_button.isEnabled()


def test_home_cancel_button_cancels_repository_creation(window):
    cancelled = []

    class Worker:
        def cancel(self):
            cancelled.append(True)

    window.repository_worker = Worker()
    window.repository_operation = "restore"
    try:
        window._set_repository_running(True)
        assert window.catalogue_loading_title_label.text() == "Creating Catalogue from Repository"
        window.catalogue_loading_cancel_button.click()
        assert cancelled == [True]
        assert not window.catalogue_loading_cancel_button.isEnabled()
    finally:
        window.repository_worker = None
        window.repository_operation = ""
        window._set_repository_running(False)


@pytest.mark.parametrize("cancel", [False, True])
def test_new_destinations_never_overwrite_existing_catalogues(window, monkeypatch, tmp_path, cancel):
    existing = tmp_path / "Existing.jvvv"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    fresh = tmp_path / "Fresh.jvvv"
    outcomes = iter([QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected if cancel else QDialog.DialogCode.Accepted])
    locations = iter([str(existing), str(fresh)])
    monkeypatch.setattr(QFileDialog, "exec", lambda self: next(outcomes))
    monkeypatch.setattr(QFileDialog, "selectedFiles", lambda self: [next(locations)])
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    assert window._choose_new_catalogue_destination("New", tmp_path) == (None if cancel else fresh)
    assert len(warnings) == 1
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("accepted", [True, False])
def test_create_catalogue_from_repository_uses_the_selected_snapshot_and_opens_it(
    window, monkeypatch, tmp_path, accepted
):
    original = tmp_path / "Original.jvvv"
    db = create_catalogue(original)
    db.create_volume("Saved drive", str(tmp_path), {"drive_id": "AID-001"})
    db.close()
    repository = SnapshotRepository.create(tmp_path / "History.jvvvrepo")
    snapshot = repository.create_snapshot(original, label="Before changes").manifest
    target = tmp_path / "New.jvvv"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(repository.path))
    monkeypatch.setattr(SnapshotPickerDialog, "choose", classmethod(lambda cls, *args: snapshot))
    monkeypatch.setattr(window, "_choose_restored_catalogue_path", lambda *args: target)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    def confirm(box):
        assert box.windowTitle() == "Create New Catalogue from Repository"
        assert str(target) in box.informativeText()
        assert snapshot.label in box.informativeText()
        text = "Create and Open Catalogue" if accepted else "Cancel"
        next(button for button in box.buttons() if button.text() == text).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", confirm)
    window.welcome_restore_button.click()
    if accepted:
        wait_for_open(window)
        assert window.catalogue_path == target
        assert window.db.list_volumes()[0]["name"] == "Saved drive"
        assert load_recent_catalogue_paths(window.settings) == [str(target)]
    else:
        assert window.repository_worker is None
        assert window.db is None
        assert not target.exists()
        assert load_recent_catalogue_paths(window.settings) == []


def test_snapshot_history_manages_the_current_catalogue_without_whole_catalogue_creation(window):
    dialog = SnapshotHistoryDialog(window)
    try:
        texts = {button.text() for button in dialog.findChildren(QPushButton)}
        assert not any("Restore" in text for text in texts)
        assert "Restore Catalogue…" not in texts
        assert "Create New Catalogue from Repository…" not in texts
    finally:
        dialog.close()
