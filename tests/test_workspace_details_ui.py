"""Selected-volume information and filters scoped to the file browser."""

import os
from threading import Event, get_ident

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QSettings, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTreeWidgetItem

from jvvv.app import (
    BackupDisplay, BrowserItem, CatalogueItemRef, MainWindow, StandardTableModel, TableColumn,
    VolumeConnections, volume_location_key,
)
from jvvv.database import create_catalogue
from jvvv.list_ui import ListTableView, ListTableWidget, ListTreeWidget
from jvvv.utils import VolumeSnapshot, format_size
from test_catalogue_directory import populate


@pytest.fixture
def window(monkeypatch, tmp_path):
    application = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("jvvv.app.QSettings", lambda *args: settings)
    monkeypatch.setattr("jvvv.app.list_connected_volume_snapshots", lambda: [])
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    instance = MainWindow()
    path = tmp_path / "Archive.jvvv"
    db = create_catalogue(path)
    instance._open_catalogue_in_window(db, path, None, volume_connections=VolumeConnections([], {}))
    yield instance
    instance.close()
    application.processEvents()


def populated_volume(window, tmp_path):
    volume_id = window.db.create_volume(
        "Photo Archive", str(tmp_path / "Original Drive"),
        {"drive_id": "AID-001", "connector": "USB-C", "condition": "Good"},
    )
    populate(window.db, volume_id, "photo", 8, errors=0)
    window.db.update_volume_storage(volume_id, 3 * 1024**4, 2 * 1024**4, 1024**4)
    return window.db.get_volume(volume_id)


def test_sorting_volumes_keeps_the_selected_volume_and_its_details(window, tmp_path):
    window.volume_connection_timer.stop()
    volume = populated_volume(window, tmp_path)
    other = window.db.create_volume(
        "Video Archive", str(tmp_path / "Video Drive"), {"drive_id": "AID-002"}
    )
    window.refresh_volumes()
    table = window.volume_table
    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert window.select_volume(volume["id"])
    table.sortByColumn(0, Qt.SortOrder.DescendingOrder)

    assert [item.id for item in window.volume_model.items] == [other, volume["id"]]
    assert table.selectionModel().selectedRows()[0].row() == 1
    assert window.selected_volume_id() == window.current_volume_id == volume["id"]
    assert window.detail_labels["name"].text() == "Photo Archive"

    window.show()
    QApplication.processEvents()
    blank = QPoint(10, table.viewport().height() - 5)
    assert not table.indexAt(blank).isValid()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=blank)
    assert window.selected_volume_id() is None
    assert window.current_volume_id is None
    assert window.detail_labels["name"].text() == "-"


@pytest.mark.parametrize("widget_type", [ListTableView, ListTableWidget, ListTreeWidget])
def test_lists_scroll_past_the_last_row_and_clear_selection_in_blank_space(widget_type):
    app = QApplication.instance() or QApplication([])
    labels = [f"Item {row}" for row in range(40)]
    if widget_type is ListTableView:
        view = widget_type()
        model = StandardTableModel([TableColumn("Name", str)], view)
        view.setModel(model)
        model.set_items(labels)
    elif widget_type is ListTableWidget:
        view = widget_type(["Name"])
        for label in labels:
            view.append_text_row([label])
    else:
        view = widget_type()
        view.addTopLevelItems([QTreeWidgetItem([label]) for label in labels])

    try:
        view.resize(300, 200)
        view.show()
        app.processEvents()
        scrollbar = view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        last = view.model().index(len(labels) - 1, 0)
        last_rect = view.visualRect(last)
        assert 0 <= last_rect.top() < last_rect.bottom() <= view.viewport().height() // 2 + 1
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=last_rect.center())
        assert view.currentIndex() == last
        assert view.selectionModel().hasSelection()
        blank = QPoint(last_rect.center().x(), view.viewport().height() - 5)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=blank)
        assert not view.currentIndex().isValid()
        assert not view.selectionModel().hasSelection()
    finally:
        view.close()


def test_folder_tree_keeps_trailing_scroll_space_after_navigation_and_row_changes():
    app = QApplication.instance() or QApplication([])
    tree = ListTreeWidget()
    tree.setUniformRowHeights(True)
    root = QTreeWidgetItem(tree, ["Volume"])
    folders = [QTreeWidgetItem(root, [f"Folder {row:02d}"]) for row in range(40)]
    root.setExpanded(True)
    try:
        tree.resize(300, 200)
        tree.show()
        app.processEvents()

        tree.setCurrentItem(folders[-1])
        tree.scrollToBottom()
        assert 0 <= tree.visualItemRect(folders[-1]).bottom() <= tree.viewport().height() // 2

        scrollbar = tree.verticalScrollBar()
        position = scrollbar.value()
        tree.setCurrentItem(folders[-2])
        assert scrollbar.value() == position

        root.setSizeHint(0, QSize(100, 24))
        tree.scrollToBottom()
        assert 0 <= tree.visualItemRect(folders[-1]).bottom() <= tree.viewport().height() // 2
    finally:
        tree.close()


def test_selected_volume_shows_saved_connector_storage_and_counts_while_offline(window, tmp_path, monkeypatch):
    volume = populated_volume(window, tmp_path)
    monkeypatch.setattr(window, "current_source_path_for_volume", lambda _volume: None)
    window.show_volume_details(volume)
    labels = window.detail_labels
    assert labels["drive_id"].text() == "AID-001"
    assert labels["name"].text() == "Photo Archive"
    assert labels["connector"].text() == "USB-C"
    assert labels["connection"].text() == "Offline"
    assert labels["register_status"].text() == "Archive"
    assert labels["condition"].text() == "Good"
    assert labels["capacity"].text() == format_size(3 * 1024**4)
    assert labels["used"].text() == format_size(2 * 1024**4)
    assert labels["free"].text() == format_size(1024**4)
    assert labels["files"].text() == "8"
    assert labels["folders"].text() == "3"
    assert labels["last_scan"].text() != "-"
    assert labels["path"].text() == volume["source_path"]
    assert labels["path"].toolTip() == volume["source_path"]
    assert window.detail_full.value() == 67


def test_selected_volume_uses_connected_location_and_clears_stale_information(window, tmp_path, monkeypatch):
    volume = populated_volume(window, tmp_path)
    connected_path = str(tmp_path / "Connected Drive")
    monkeypatch.setattr(window, "current_source_path_for_volume", lambda _volume: connected_path)
    window.show_volume_details(volume)
    assert window.detail_labels["connection"].text() == "Connected"
    assert window.detail_labels["path"].text() == connected_path
    assert window.detail_labels["path"].toolTip() == connected_path
    window.show_volume_details(None)
    assert all(label.text() == "-" for label in window.detail_labels.values())
    assert all(not label.toolTip() for label in window.detail_labels.values())
    assert window.detail_full.value() == 0


def test_browsing_uses_saved_data_while_a_drive_probe_is_blocked(window, tmp_path, monkeypatch):
    window.volume_connection_timer.stop()
    volume = populated_volume(window, tmp_path)
    mounted = tmp_path / "Mounted Drive"
    mounted.mkdir()
    snapshot = VolumeSnapshot(str(mounted), str(mounted), "", "test-device", "drive-1")
    window.db.update_volume_location(volume["id"], volume["source_path"], snapshot.as_db_fields())

    gui_thread = get_ident()
    started = Event()
    release = Event()
    probes = []

    def slow_probe():
        assert get_ident() != gui_thread
        probes.append(True)
        started.set()
        assert release.wait(5)
        return [snapshot]

    monkeypatch.setattr("jvvv.app.list_connected_volume_snapshots", slow_probe)
    monkeypatch.setattr(
        "jvvv.app.resolve_volume_source_path",
        lambda *_: pytest.fail("Browsing should use cached connection information"),
    )
    window.check_connected_volumes()
    try:
        assert started.wait(5)
        window.check_connected_volumes()
        assert len(probes) == 1
        window.refresh_volumes()
        assert window.select_volume(volume["id"])
        assert window.detail_labels["connection"].text() == "Offline"
        assert window.folder_tree.topLevelItemCount() == 1

        window.select_folder_path("Photos")
        folder_id = window.current_folder_id
        assert window.current_path_label.text() == "Photos"
    finally:
        release.set()

    _, result = window._volume_connection_probe
    result.result(timeout=5)
    window.check_connected_volumes()
    window.volume_connection_refresh_timer.stop()
    window.refresh_after_connected_volumes_changed()
    assert window.detail_labels["connection"].text() == "Connected"
    assert window.detail_labels["path"].text() == str(mounted)
    assert window.current_folder_id == folder_id
    assert window.current_path_label.text() == "Photos"
    assert window.offline_label.text() == ""


@pytest.mark.parametrize("moved", [False, True])
def test_open_file_location_checks_identity_without_blocking_browsing(window, tmp_path, monkeypatch, moved):
    window.volume_connection_timer.stop()
    volume = populated_volume(window, tmp_path)
    mounted = tmp_path / "Mounted Drive"
    destination = tmp_path / "Reconnected Drive" if moved else mounted
    relative_path = "Photos/report.txt"
    file_path = destination / relative_path
    file_path.parent.mkdir(parents=True)
    file_path.write_text("indexed file")
    snapshot = VolumeSnapshot(str(mounted), str(mounted), "", "test-device", "drive-1")
    window.db.update_volume_location(volume["id"], volume["source_path"], snapshot.as_db_fields())
    volume = window.db.get_volume(volume["id"])
    window._connected_volume_paths[volume_location_key(volume)] = str(mounted)
    window.refresh_volumes()
    assert window.select_volume(volume["id"])

    gui_thread = get_ident()
    started = Event()
    release = Event()
    probes = []
    opened = []

    def capture(path):
        assert get_ident() != gui_thread
        assert path == str(mounted)
        probes.append("cached drive")
        started.set()
        assert release.wait(5)
        if moved:
            return VolumeSnapshot(str(mounted), str(mounted), "", "test-device", "other-drive")
        return snapshot

    def resolve(_volume):
        assert get_ident() != gui_thread
        assert moved
        probes.append("find moved drive")
        return str(destination)

    def open_in_file_manager(path, *, reveal):
        assert get_ident() != gui_thread
        opened.append((path, reveal))

    monkeypatch.setattr("jvvv.app.capture_volume_snapshot", capture)
    monkeypatch.setattr("jvvv.app.resolve_volume_source_path", resolve)
    monkeypatch.setattr("jvvv.app.open_in_file_manager", open_in_file_manager)
    target = CatalogueItemRef("file", 1, volume["id"], relative_path)
    menu = window.build_catalogue_item_context_menu(target)
    action = next(action for action in menu.actions() if action.text() == "Open File Location")
    assert action.isEnabled()
    try:
        action.trigger()
        assert started.wait(5)
        action.trigger()
        window.select_folder_path("Photos")
        assert window.current_path_label.text() == "Photos"
        assert opened == []
    finally:
        release.set()

    for _ in range(100):
        if not window._file_open_requests:
            break
        QTest.qWait(25)
    assert not window._file_open_requests
    assert opened == [(file_path, True)]
    assert probes == (["cached drive", "find moved drive"] if moved else ["cached drive"])


def test_unscanned_volume_does_not_report_unknown_capacity_as_measured_zero(window, tmp_path, monkeypatch):
    volume_id = window.db.create_volume("New Drive", str(tmp_path))
    monkeypatch.setattr(window, "current_source_path_for_volume", lambda _volume: None)
    window.show_volume_details(window.db.get_volume(volume_id))
    assert window.detail_labels["connector"].text() == "Unknown"
    assert [window.detail_labels[key].text() for key in ("capacity", "used", "free")] == ["-", "-", "-"]


def test_copy_filter_is_attached_to_file_list_and_still_filters_without_hiding_parent_navigation(window):
    assert window.browser_splitter.widget(1) is window.browser_file_pane
    assert window.browser_file_pane.isAncestorOf(window.file_table)
    assert window.browser_file_pane.isAncestorOf(window.browser_status_strip)
    assert window.browser_file_toolbar.isAncestorOf(window.browser_backup_filter_combo)
    assert not window.browser_file_pane.isAncestorOf(window.folder_tree)
    assert "Filter files by copy status" in {
        label.text() for label in window.browser_file_toolbar.findChildren(QLabel)
    }
    assert "How matching works" not in {button.text() for button in window.findChildren(QPushButton)}
    assert window.browser_backup_filter_combo.currentData() == "all"
    parent = BrowserItem("folder", 0, "..", "", "Folder", is_parent_entry=True)
    verified = BrowserItem(
        "file", 1, "Copy.jpg", "Copy.jpg", "Image",
        backup=BackupDisplay("verified", "Hash verified", "Matching content hash", 4),
    )
    unconfirmed = BrowserItem(
        "file", 2, "Only.jpg", "Only.jpg", "Image",
        backup=BackupDisplay("none", "No copy found", "No other copy", 0),
    )
    window.current_directory_items = [parent, verified, unconfirmed]
    window.apply_browser_backup_filter()
    assert set(item.name for item in window.browser_model.items) == {"..", "Copy.jpg", "Only.jpg"}
    window.browser_backup_filter_combo.setCurrentIndex(window.browser_backup_filter_combo.findData("verified"))
    assert set(item.name for item in window.browser_model.items) == {"..", "Copy.jpg"}
    window.browser_backup_filter_combo.setCurrentIndex(0)
    assert len(window.browser_model.items) == 3


def test_selected_volume_panel_fits_its_contents_at_larger_zoom(window, tmp_path, monkeypatch):
    volume = populated_volume(window, tmp_path)
    monkeypatch.setattr(window, "current_source_path_for_volume", lambda _volume: None)
    window.show_volume_details(volume)
    window.set_ui_zoom(1.5)
    window.resize(1280, 960)
    window.show()
    for _ in range(5):
        QApplication.instance().processEvents()
    assert window.detail_full.geometry().bottom() < window.details_box.height()
    assert all(label.geometry().bottom() < window.details_box.height()
               for label in window.detail_labels.values())
    assert window.file_table.height() > 150
