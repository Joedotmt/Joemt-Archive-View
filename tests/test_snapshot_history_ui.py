"""Snapshot selection, repository availability, and the open-time locate prompt."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMessageBox

from jvvv.app import MainWindow, RelinkRepositoryDialog, SnapshotHistoryDialog
from jvvv.database import create_catalogue, open_catalogue
from jvvv.snapshot_repository import SnapshotRepository, repository_link_settings


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("jvvv.app.QSettings", lambda *args: settings)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    monkeypatch.setattr("jvvv.app.list_connected_volume_snapshots", lambda: [])
    instance = MainWindow()
    yield instance
    instance.close()
    app.processEvents()


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "Archive.jvvv"
    db = create_catalogue(path)
    db.create_volume("Photos", str(tmp_path), {"drive_id": "AID-001"})
    db.create_volume("Videos", str(tmp_path), {"drive_id": "AID-002"})
    db.close()
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    first = repository.create_snapshot(path, label="First").manifest
    repository.create_snapshot(path, label="Latest")
    db = open_catalogue(path)
    for key, value in repository_link_settings(path, repository.path).items():
        db.set_catalogue_meta(key, value)
    db.close()
    return path, repository, first


def open_in_window(window, path):
    window._open_catalogue_in_window(
        open_catalogue(path, check_same_thread=False), path, None, connected_volume_snapshots=[]
    )


def answer_unavailable(monkeypatch, choice="Continue Without Repository"):
    prompts = []

    def execute(box):
        assert box.windowTitle() == "Snapshot Repository Unavailable"
        prompts.append(box.informativeText())
        next(button for button in box.buttons() if button.text() == choice).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", execute)
    return prompts


def test_snapshot_actions_follow_selection_and_volumes_are_for_inspection(window, archive, monkeypatch):
    path, repository, first = archive
    open_in_window(window, path)
    dialog = SnapshotHistoryDialog(window)
    try:
        dialog.refresh()
        assert dialog.windowTitle() == "Snapshot Manager"
        assert dialog.relink_button.isHidden()
        assert dialog.repository_status_label.text() == "Repository available"
        assert dialog.snapshots_group.isAncestorOf(dialog.create_button)
        assert dialog.snapshots_group.isAncestorOf(dialog.delete_button)
        assert dialog.repository_group.isAncestorOf(dialog.repository_tools_button)
        dialog.snapshot_table.selectRow(1)
        assert dialog.selected_manifest().snapshot_id == first.snapshot_id
        assert "First" in dialog.selected_snapshot_label.text()
        assert "First" in dialog.volume_context_label.text()
        assert dialog.volume_table.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
        assert dialog.volume_table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
        assert [dialog.volume_table.item(row, 1).text() for row in range(2)] == ["Photos", "Videos"]
        calls = []
        monkeypatch.setattr(window, "delete_snapshot", lambda *args: calls.append(("snapshot", args)))
        monkeypatch.setattr(window, "verify_repository", lambda *args: calls.append(("verify", args)))
        monkeypatch.setattr(window, "prune_repository", lambda *args: calls.append(("prune", args)))
        dialog.delete_button.click()
        dialog.verify_action.trigger()
        dialog.prune_action.trigger()
        assert calls == [
            ("snapshot", (repository.path, first)),
            ("verify", (repository.path,)),
            ("prune", (repository.path,)),
        ]
        dialog.refresh()
        assert dialog.selected_manifest().snapshot_id == first.snapshot_id
        assert dialog.volume_table.rowCount() == 2
    finally:
        dialog.close()


def test_first_snapshot_state_has_no_relink_action_or_open_time_warning(window, tmp_path, monkeypatch):
    path = tmp_path / "New.jvvv"
    create_catalogue(path).close()
    monkeypatch.setattr(QMessageBox, "exec", lambda box: pytest.fail("No repository has gone missing"))
    open_in_window(window, path)
    QApplication.instance().processEvents()
    window.show_snapshot_history()
    dialog = window.snapshot_history_dialog
    assert dialog.repository_status_label.text() == "No repository yet"
    assert dialog.relink_button.isHidden()
    assert dialog.repository_tools_button.isHidden()
    assert dialog.create_button.text() == "Create First Snapshot…"
    assert dialog.create_button.isEnabled()
    assert not dialog.delete_button.isEnabled()
    assert dialog.delete_button.isHidden()
    assert dialog.volumes_group.isHidden()
    calls = []
    monkeypatch.setattr(window, "create_snapshot_from_menu", lambda: calls.append(True))
    dialog.create_button.click()
    assert calls == [True]


def test_missing_repository_prompts_after_async_open_and_only_once(window, archive, monkeypatch):
    path, repository, _ = archive
    repository.path.rename(repository.path.with_name("Moved.jvvvrepo"))
    prompts = answer_unavailable(monkeypatch)
    window.open_catalogue_path(path)
    deadline = time.monotonic() + 15
    while (not prompts or window._catalogue_open_in_progress()) and time.monotonic() < deadline:
        QApplication.instance().processEvents()
        time.sleep(0.01)
    assert len(prompts) == 1
    assert str(repository.path) in prompts[0]
    assert window.db is not None
    assert window.stack.currentWidget() is window.catalogue_page
    window.show_snapshot_history()
    window.snapshot_history_dialog.refresh()
    assert len(prompts) == 1
    assert not window.snapshot_history_dialog.relink_button.isHidden()
    assert not window.snapshot_history_dialog.create_button.isEnabled()
    assert window.snapshot_history_dialog.volumes_group.isHidden()


def test_opening_history_can_locate_a_repository_that_moved_since_catalogue_open(window, archive, monkeypatch):
    path, repository, _ = archive
    open_in_window(window, path)
    QApplication.instance().processEvents()
    moved = repository.path.with_name("Moved.jvvvrepo")
    repository.path.rename(moved)
    prompts = answer_unavailable(monkeypatch, "Locate Repository…")
    monkeypatch.setattr(RelinkRepositoryDialog, "choose", classmethod(lambda cls, *args, **kwargs: moved))
    window.show_snapshot_history()
    assert len(prompts) == 1
    assert window.repository_link().path == moved.resolve()
    dialog = window.snapshot_history_dialog
    assert dialog.repository_status_label.text() == "Repository available"
    assert dialog.relink_button.isHidden()
    assert dialog.snapshot_table.rowCount() == 2


def test_check_again_recognizes_a_reconnected_repository_without_relinking(window, archive):
    path, repository, _ = archive
    open_in_window(window, path)
    QApplication.instance().processEvents()
    moved = repository.path.with_name("Disconnected.jvvvrepo")
    repository.path.rename(moved)
    dialog = SnapshotHistoryDialog(window)
    try:
        dialog.refresh()
        assert not dialog.relink_button.isHidden()
        assert dialog.repository_tools_button.isHidden()
        moved.rename(repository.path)
        dialog.refresh_button.click()
        assert dialog.relink_button.isHidden()
        assert dialog.snapshot_table.rowCount() == 2
    finally:
        dialog.close()


def test_queued_repository_prompt_is_ignored_after_catalogue_closes(window, archive, monkeypatch):
    path, repository, _ = archive
    repository.path.rename(repository.path.with_name("Moved.jvvvrepo"))
    monkeypatch.setattr(QMessageBox, "exec", lambda box: pytest.fail("Closed catalogue must not prompt"))
    open_in_window(window, path)
    assert window.close_catalogue()
    QApplication.instance().processEvents()


def test_repository_actions_disable_while_work_is_running(window, archive, monkeypatch):
    path, _, _ = archive
    open_in_window(window, path)
    window.show_snapshot_history()
    dialog = window.snapshot_history_dialog
    monkeypatch.setattr(window, "_catalogue_job_running", lambda: True)
    window._set_catalogue_busy(True)
    for action in (dialog.create_button, dialog.delete_button,
                   dialog.verify_action, dialog.prune_action, dialog.refresh_button):
        assert not action.isEnabled()
    monkeypatch.setattr(window, "_catalogue_job_running", lambda: False)
    window._set_catalogue_busy(False)
    assert dialog.create_button.isEnabled()
    assert dialog.delete_button.isEnabled()
