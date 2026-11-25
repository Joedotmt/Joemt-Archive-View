from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QProgressBar

from jvvv.app import CatalogueItemRef, ItemPropertiesDialog, MainWindow
from jvvv.backup_analysis import BackupAnalysisEngine
from jvvv.backup_ui import BackupsPanel, BackupSource, BackupsSplitter
from jvvv.database import Database
from test_backup_analysis import HASH_A, HASH_B, add_catalogued_volume


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        QTest.qWait(15)
    assert predicate()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def copies(tmp_path):
    db = Database(tmp_path / "Copies.jvvv")
    src, files, folders = add_catalogued_volume(
        db, tmp_path, "Source", {"Project/a.txt": HASH_A, "Project/b.txt": HASH_B, "Project/c.txt": None},
    )
    target, _, _ = add_catalogued_volume(db, tmp_path, "Other", {"Somewhere/renamed.txt": HASH_A})
    add_catalogued_volume(db, tmp_path, "Unscanned", {}, scanned=False)
    engine = BackupAnalysisEngine(db)
    engine.analyse()
    try:
        yield db, engine, src, files, folders, target
    finally:
        db.close()


def test_panel_shows_drives_counts_paths_and_gaps_without_hover(app, copies):
    db, _, source, _, folders, target = copies
    panel = BackupsPanel()
    opened = []
    panel.location_requested.connect(opened.append)
    panel.resize(640, 760)
    panel.show()
    try:
        panel.set_source(db.path, BackupSource("folder", folders["Project"], source, "Project", "Project", "Source"),
                         can_compare=True)
        wait_for(lambda: panel.files.table.rowCount() == 3)
        assert panel.status.text().startswith("1/3 file contents found")
        assert "1 source file needs a saved hash" in panel.note.text()
        assert panel.drives.rowCount() == 2
        assert panel.drives.item(1, 1).text() == "Not scanned"
        coverage = panel.drives.cellWidget(0, 2)
        assert isinstance(coverage, QProgressBar)
        assert coverage.text() == "1/3"
        assert panel.files.path_label.text() == "a.txt\nSomewhere/renamed.txt"
        panel.files.open_button.setFocus()
        QTest.keyClick(panel.files.open_button, Qt.Key.Key_Space)
        assert (opened[0].volume_id, opened[0].relative_path) == (target, "Somewhere/renamed.txt")

        panel.file_filter.setCurrentIndex(panel.file_filter.findData("no_match"))
        wait_for(lambda: panel.files.table.rowCount() == 1)
        assert panel.files.table.item(0, 0).text() == "b.txt"
        assert not panel.files.open_button.isEnabled()
        panel.file_filter.setCurrentIndex(panel.file_filter.findData("needs_rescan"))
        wait_for(lambda: panel.files.table.item(0, 0).text() == "c.txt")
        assert panel.files.table.item(0, 1).text() == "Needs rescan"
    finally:
        panel.close()


def test_panel_reveals_actual_exact_folder_and_uses_selected_tree(app, tmp_path):
    db = Database(tmp_path / "Exact.jvvv")
    source, _, folders = add_catalogued_volume(db, tmp_path, "Source", {"Photos/a.txt": HASH_A})
    add_catalogued_volume(
        db, tmp_path, "Destination", {"First/a.txt": HASH_A, "Second/a.txt": HASH_A},
    )
    BackupAnalysisEngine(db).analyse()
    panel = BackupsPanel()
    panel.show()
    try:
        panel.set_source(db.path, BackupSource("folder", folders["Photos"], source, "Photos", "Photos", "Source"),
                         can_compare=True)
        wait_for(lambda: panel.files.table.rowCount() == 1)
        assert panel.status.text().startswith("Full match")
        assert panel.locations.table.rowCount() == 2
        assert panel.files.table.item(0, 2).text() == "First/a.txt"
        panel.locations.table.selectRow(1)
        wait_for(lambda: panel.files.table.item(0, 2).text() == "Second/a.txt")
        assert panel.locations.path_label.text() == "Second"
    finally:
        panel.close()
        db.close()


def test_panel_hides_stale_results_and_offers_compare(app, copies):
    db, engine, source, _, folders, _ = copies
    panel = BackupsPanel()
    panel.show()
    try:
        selected = BackupSource("folder", folders["Project"], source, "Project", "Project", "Source")
        panel.set_source(db.path, selected, can_compare=True)
        wait_for(lambda: panel.drives.rowCount() == 2)
        engine.invalidate_all("A new scan was applied.")
        panel.set_source(db.path, selected, can_compare=True, refresh=True)
        wait_for(lambda: panel.status.text() == "Outdated")
        assert panel.drives.rowCount() == 0
        assert "Compare saved hashes" in panel.note.text()
        assert panel.compare_button.isEnabled()
        panel.set_analysis_running(True)
        assert not panel.compare_button.isEnabled()
        assert panel.cancel_button.isEnabled()
    finally:
        panel.close()


def test_browser_actions_open_panel_and_navigate_to_an_offline_file(app, copies, tmp_path, monkeypatch):
    db, engine, source, files, folders, target = copies
    settings = tmp_path / "settings.ini"
    monkeypatch.setattr("jvvv.app.QSettings", lambda *_: QSettings(str(settings), QSettings.Format.IniFormat))
    window = MainWindow()
    window.db = db
    window.catalogue_path = db.path
    window.backup_engine = engine
    window.resize(1240, 860)
    window._set_catalogue_open(True)
    window.show()
    try:
        window.refresh_volumes()
        folder = CatalogueItemRef("folder", folders["Project"], source, "Project")
        # Search and the Browser use the same item menu.
        menu = window.build_catalogue_item_context_menu(folder, include_catalogue_location=True)
        next(action for action in menu.actions() if action.text() == "View backups").trigger()
        assert window.backups_button.isChecked()
        assert window.tabs.currentWidget() is window.browser_tab
        wait_for(lambda: window.backups_panel.files.table.rowCount() == 3)
        window.backups_panel.files.open_button.click()
        wait_for(lambda: window.current_volume_id == target and window.selected_browser_item() is not None)
        assert window.selected_browser_item().relative_path == "Somewhere/renamed.txt"
        assert window.backups_panel.source.item_type == "file"
        wait_for(lambda: window.backups_panel.locations.table.rowCount() == 1)
        assert window.backups_panel.locations.path_label.text() == "Project/a.txt"

        window.show_drive_backups(source)
        assert window.backups_panel.source.item_id == folders[""]
        assert window.backups_panel.source.path == ""

        def open_backups_from_properties(dialog):
            next(button for button in dialog.action_buttons if button.text() == "View backups").click()

        monkeypatch.setattr(ItemPropertiesDialog, "exec", open_backups_from_properties)
        window.show_browser_item_properties("file", files["Project/a.txt"])
        assert window.backups_panel.source.item_id == files["Project/a.txt"]
    finally:
        window.db = None
        window.close()


def test_compare_button_runs_background_comparison_and_refreshes_panel(app, copies, tmp_path, monkeypatch):
    db, engine, source, _, _, _ = copies
    engine.invalidate_all("A new scan was applied.")
    settings = tmp_path / "settings.ini"
    monkeypatch.setattr("jvvv.app.QSettings", lambda *_: QSettings(str(settings), QSettings.Format.IniFormat))
    window = MainWindow()
    window.db = db
    window.catalogue_path = db.path
    window.backup_engine = engine
    window._set_catalogue_open(True)
    window.show()
    try:
        window.refresh_volumes()
        window.show_drive_backups(source)
        wait_for(lambda: window.backups_panel.status.text() == "Outdated")
        window.backups_panel.compare_button.click()
        wait_for(lambda: window.backup_analysis_worker is None and engine.state().status == "completed", timeout=15)
        wait_for(lambda: window.backups_panel.status.text().startswith("1/3 file contents found"))
        assert window.backups_panel.drives.rowCount() == 2
    finally:
        if window.backup_analysis_worker is not None:
            window.backup_analysis_worker.cancel()
            wait_for(lambda: window.backup_analysis_worker is None, timeout=15)
        window.db = None
        window.close()


def test_splitter_is_vertical_when_space_is_tight(app):
    splitter = BackupsSplitter(Qt.Orientation.Horizontal)
    splitter.resize(700, 500)
    splitter.show()
    try:
        assert splitter.orientation() == Qt.Orientation.Vertical
        splitter.resize(1000, 500)
        assert splitter.orientation() == Qt.Orientation.Horizontal
    finally:
        splitter.close()
