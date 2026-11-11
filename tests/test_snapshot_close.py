"""Snapshot reminders and the close flow, including real Qt worker completion."""

import shutil

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from jvvv.app import MainWindow, _RepositoryWorker
from jvvv.database import create_catalogue, open_catalogue
from jvvv.snapshot_repository import SnapshotCancelled, SnapshotRepository, default_repository_path


@pytest.fixture
def window(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window.db.create_volume("Drive", str(tmp_path), {"drive_id": "AID-001"})
    window._set_catalogue_open(True)
    yield window
    # Close normally to release catalogue locks, without another user decision.
    monkeypatch.setattr(window, "_confirm_snapshot_before_close", lambda: True)
    window.close()
    application.processEvents()


def answer_next_prompt(monkeypatch, choice, seen=None):
    def execute(box):
        assert box.windowTitle() == "Create Snapshot Before Closing?"
        if seen is not None:
            seen.append(box.informativeText())
        assert box.defaultButton().text() == "Create Snapshot && Close"
        assert box.escapeButton().text() == "Cancel"
        next(button for button in box.buttons() if button.text() == choice).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", execute)


def test_close_without_snapshot_keeps_local_changes(window, monkeypatch):
    path = window.catalogue_path
    answer_next_prompt(monkeypatch, "Close Without Snapshot")
    assert window.close_catalogue()
    assert window.db is None
    assert not default_repository_path(path).exists()
    db = open_catalogue(path)
    try:
        assert db.list_volumes()[0]["name"] == "Drive"
    finally:
        db.close()


def test_cancel_aborts_exit_and_keeps_catalogue_open(window, monkeypatch):
    original = window.db
    answer_next_prompt(monkeypatch, "Cancel")
    assert not window.close()
    assert window.db is original


@pytest.mark.parametrize("action", ["catalogue", "exit"])
def test_snapshot_and_close_waits_for_worker_and_links_repository(window, monkeypatch, action):
    path = window.catalogue_path
    answer_next_prompt(monkeypatch, "Create Snapshot && Close")
    assert window.close_catalogue() if action == "catalogue" else window.close()
    assert window.db is None
    assert window.repository_worker is None
    assert window.repository_thread is None
    repository = SnapshotRepository(default_repository_path(path))
    assert len(repository.list_snapshots()) == 1
    assert not repository.has_changes(path)
    db = open_catalogue(path)
    try:
        assert db.catalogue_meta()["snapshot_repository_uuid"]
    finally:
        db.close()


def test_unchanged_catalogue_closes_without_prompt_after_reopening(window, monkeypatch):
    path = window.catalogue_path
    repository = SnapshotRepository.create(default_repository_path(path))
    repository.create_snapshot(path)
    window.db.close()
    window.db = open_catalogue(path)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("Unexpected prompt"))
    assert window.close_catalogue()


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
def test_snapshot_failure_or_cancellation_aborts_close(window, monkeypatch, outcome):
    original = window.db
    answer_next_prompt(monkeypatch, "Create Snapshot && Close")
    errors = []
    monkeypatch.setattr(window, "on_repository_failed", lambda exc, details: errors.append(str(exc)))

    def operation(**kwargs):
        if outcome == "cancel":
            raise SnapshotCancelled()
        raise OSError("Repository is full")

    monkeypatch.setattr(
        "jvvv.app.snapshot_worker",
        lambda *args: _RepositoryWorker(operation, description="snapshot"),
    )
    assert not window.close_catalogue()
    assert window.db is original
    assert window.repository_thread is None
    assert errors == (["Repository is full"] if outcome == "failure" else [])


def test_cancel_button_waits_for_worker_and_rejects_reentrant_close(window, monkeypatch):
    answer_next_prompt(monkeypatch, "Create Snapshot && Close")
    original = window.db
    reentrant = []

    def operation(*, progress_callback, cancel_callback):
        import time

        # Wait for the UI's cancellation request, with a timeout to bound a failure.
        deadline = time.monotonic() + 5
        while not cancel_callback() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancel_callback()
        raise SnapshotCancelled()

    monkeypatch.setattr(
        "jvvv.app.snapshot_worker",
        lambda *args: _RepositoryWorker(operation, description="snapshot"),
    )

    def cancel():
        reentrant.append(window.close_catalogue())
        dialog = window.findChild(QProgressDialog)
        assert dialog is not None
        # Clicking the actual button emits canceled(), unlike programmatic cancel().
        from PySide6.QtWidgets import QPushButton

        dialog.findChild(QPushButton).click()

    QTimer.singleShot(0, cancel)
    assert not window.close_catalogue()
    assert reentrant == [False]
    assert window.db is original
    assert window.repository_worker is None


def test_offline_repository_explains_unknown_status(window, monkeypatch):
    path = window.catalogue_path
    repository = SnapshotRepository.create(default_repository_path(path))
    repository.create_snapshot(path)
    assert window.record_repository_link(repository.path)
    shutil.move(str(repository.path), str(repository.path.with_name("Moved.jvvvrepo")))
    seen = []
    answer_next_prompt(monkeypatch, "Cancel", seen)
    assert not window.close_catalogue()
    assert "offline" in seen[0]
    assert "could not be checked" in seen[0]


def test_cancelling_repository_selection_keeps_catalogue_open(window, monkeypatch):
    answer_next_prompt(monkeypatch, "Create Snapshot && Close")
    monkeypatch.setattr(window, "ensure_repository_for_snapshot", lambda: None)
    assert not window.close_catalogue()
    assert window.db is not None


def test_returning_home_before_creating_another_catalogue_keeps_snapshot_flow(window, monkeypatch, tmp_path):
    original = window.catalogue_path
    target = tmp_path / "New.jvvv"
    answer_next_prompt(monkeypatch, "Create Snapshot && Close")
    monkeypatch.setattr(window, "_choose_new_catalogue_path", lambda: target)
    open_in_window = window._open_catalogue_in_window
    monkeypatch.setattr(
        window,
        "_open_catalogue_in_window",
        lambda db, path, lock: open_in_window(db, path, lock, connected_volume_snapshots=[]),
    )
    window.new_catalogue()  # Creating another catalogue is unavailable in the workspace.
    assert window.catalogue_path == original
    assert window.close_catalogue()
    assert window.stack.currentWidget() is window.welcome_page
    window.new_catalogue()
    assert window.catalogue_path == target
    assert window.db is not None
    assert len(SnapshotRepository(default_repository_path(original)).list_snapshots()) == 1


def test_failed_repository_link_keeps_catalogue_open(window, monkeypatch):
    original = window.db
    answer_next_prompt(monkeypatch, "Create Snapshot && Close")
    monkeypatch.setattr(window, "record_repository_link", lambda path: False)
    assert not window.close_catalogue()
    assert window.db is original


def test_unknown_snapshot_status_still_allows_closing(window, monkeypatch):
    repository = SnapshotRepository.create(default_repository_path(window.catalogue_path))
    repository.create_snapshot(window.catalogue_path)

    def unavailable(*args):
        raise OSError("Unavailable")

    monkeypatch.setattr(SnapshotRepository, "has_changes", unavailable)
    seen = []
    answer_next_prompt(monkeypatch, "Close Without Snapshot", seen)
    assert window.close_catalogue()
    assert "could not be checked" in seen[0]
    assert "Unavailable" in seen[0]
