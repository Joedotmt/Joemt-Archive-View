"""Archived contents, recovery and file protection at the filesystem boundary."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

from PIL import Image
import pytest
from PySide6.QtWidgets import QApplication

from jvvv import archive_job, scanner, utils
from jvvv.archive_job import ArchiveJob, ArchivePaused, pending_archive
from jvvv.database import create_catalogue, open_catalogue
from jvvv.preview_config import PreviewSettings
from jvvv.scanner import VolumeScanner
from jvvv.utils import VolumeSnapshot


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "Project"
    destination = tmp_path / "Archive HDD"
    source.mkdir()
    destination.mkdir()
    (source / "nested").mkdir()
    (source / "empty").mkdir()
    (source / "empty.dat").touch()
    (source / "nested" / "source.dat").write_bytes(b"original" * 821 + b"last")
    return source, destination


@pytest.fixture
def index_volume(monkeypatch):
    """Model separate mounted drive roots with disposable folders."""
    snapshots = {}
    actual_snapshot = utils.capture_volume_snapshot

    def capture(path):
        return snapshots.get(Path(path)) or actual_snapshot(path)

    monkeypatch.setattr(utils, "list_connected_volume_snapshots", lambda: list(snapshots.values()))
    monkeypatch.setattr(utils, "capture_volume_snapshot", capture)
    monkeypatch.setattr(scanner, "capture_volume_snapshot", capture)
    monkeypatch.setattr(archive_job, "capture_volume_snapshot", capture)

    def index(db, path, *, scan=True, relative=""):
        snapshot = VolumeSnapshot(str(path), str(path), relative, "windows-volume-guid", path.name)
        snapshots[path] = snapshot
        volume_id = db.create_volume(path.name, str(path), location=None if scan else snapshot.as_db_fields())
        if scan:
            assert VolumeScanner(db).scan(volume_id).status == "completed"
        return volume_id

    return index


def test_verified_copy_keeps_source_and_catalogues_destination(tmp_path, project, index_volume):
    source, destination = project
    original = source / "nested" / "source.dat"
    before = original.stat()
    contents = original.read_bytes()
    db = create_catalogue(tmp_path / "Archive.jvvv")
    try:
        existing_file = destination / "existing.dat"
        existing_file.write_bytes(b"existing file")
        volume_id = index_volume(db, destination)
        volume = db.get_volume(volume_id)
        root = db.get_root_folder(volume_id)
        indexed_file = db.list_files(volume_id, root["id"])[0]
        existing_file.write_bytes(b"modified after the initial scan")
        result = ArchiveJob(db).run(source, volume_id)
        assert result.volume_id == volume_id
        assert result.path == destination / source.name
        assert result.files == 2
        assert result.bytes_copied == len(contents)
        assert (result.path / "nested/source.dat").read_bytes() == contents
        assert (result.path / "empty").is_dir()
        assert (result.path / "empty.dat").stat().st_size == 0
        assert original.read_bytes() == contents
        assert original.stat().st_mtime_ns == before.st_mtime_ns
        marker = result.path / "Project.jvvv-project.json"
        marker_data = json.loads(marker.read_bytes())
        assert marker_data["project_name"] == source.name
        assert marker_data["archived_by"] == "JVVV"
        assert not (source / marker.name).exists()
        project_folder = db.get_folder_by_path(volume_id, "Project")
        assert project_folder["parent_id"] == root["id"]
        top_level_files = {row["name"]: row for row in db.list_files(volume_id, project_folder["id"])}
        assert bytes(top_level_files[marker.name]["content_hash"]) == hashlib.sha256(marker.read_bytes()).digest()
        folder = db.get_folder_by_path(result.volume_id, "Project/nested")
        row = db.list_files(result.volume_id, folder["id"])[0]
        assert bytes(row["content_hash"]) == hashlib.sha256(contents).digest()
        assert row["identity_inode"] == (result.path / "nested/source.dat").stat().st_ino
        assert len(db.list_volumes()) == 1
        assert db.get_volume(volume_id)["last_scan_at"] == volume["last_scan_at"]
        assert db.get_volume(volume_id)["indexed_file_count"] == 4
        assert db.get_file(indexed_file["id"]) == indexed_file
        assert pending_archive(db.path) is None
        assert not list(destination.glob("*.partial"))
    finally:
        db.close()


def test_unscanned_volume_and_indexed_subfolder_are_ineligible(tmp_path, project, index_volume):
    source, destination = project
    folder = tmp_path / "Folder volume"
    folder.mkdir()
    db = create_catalogue(tmp_path / "Archive.jvvv")
    try:
        unscanned = index_volume(db, destination, scan=False)
        folder_volume = index_volume(db, folder, relative="Some folder")
        for volume_id in (unscanned, folder_volume, 999):
            with pytest.raises(OSError, match="initial volume scan"):
                ArchiveJob(db).run(source, volume_id)
        assert not list(destination.iterdir())
        assert not list(folder.iterdir())
        assert pending_archive(db.path) is None
    finally:
        db.close()


def test_unattended_scan_keeps_previous_index_when_a_file_cannot_be_read(
    tmp_path, project, index_volume, monkeypatch,
):
    _, destination = project
    existing = destination / "original.dat"
    existing.write_bytes(b"the indexed file")
    db = create_catalogue(tmp_path / "Archive.jvvv")
    try:
        volume_id = index_volume(db, destination)
        old_scan = db.get_volume(volume_id)["last_scan_at"]
        root = db.get_root_folder(volume_id)
        indexed = db.list_files(volume_id, root["id"])[0]
        existing.unlink()
        (destination / "unreadable.dat").write_bytes(b"an inaccessible file")

        def deny_read(*_args, **_kwargs):
            raise PermissionError("File could not be read")

        monkeypatch.setattr(VolumeScanner, "_hash_file", deny_read)
        result = VolumeScanner(db, require_complete=True).scan(volume_id)
        assert result.status == "discarded"
        assert "unreadable.dat" in result.message
        assert db.get_volume(volume_id)["last_scan_at"] == old_scan
        assert db.get_file(indexed["id"]) == indexed
        assert db.get_volume(volume_id)["indexed_file_count"] == 1
    finally:
        db.close()


def test_resume_rejects_wrong_volume_and_repairs_corrupt_staged_file(tmp_path, project, index_volume, monkeypatch):
    source, destination = project
    pause = Event()
    catalogue = tmp_path / "Archive.jvvv"
    db = create_catalogue(catalogue)

    def progress(label, *_):
        if "Reading back" in label:
            pause.set()

    try:
        volume_id = index_volume(db, destination)
        with pytest.raises(ArchivePaused):
            ArchiveJob(db, cancelled=pause.is_set, progress=progress).run(source, volume_id)
        assert not (destination / source.name).exists()
        assert not db.get_folder_by_path(volume_id, source.name)
        assert VolumeScanner(db).scan(volume_id).status == "completed"
        assert not db.list_child_folders(volume_id, db.get_root_folder(volume_id)["id"])
    finally:
        db.close()

    wrong = tmp_path / "Wrong folder"
    wrong.mkdir()
    db = open_catalogue(catalogue)
    try:
        wrong_id = index_volume(db, wrong)
        saved = pending_archive(db.path)
        stage = destination / f".jvvv-archive-{saved['id']}.partial" / "project"
        damaged = stage / "nested/source.dat"
        original = (source / "nested/source.dat").read_bytes()
        damaged.write_bytes(b"x" * len(original))
        with pytest.raises(OSError, match="original destination"):
            ArchiveJob(db).run(source, wrong_id)
        assert not list(wrong.iterdir())
        with pytest.raises(OSError, match="verification failed"):
            ArchiveJob(db).run(source, volume_id)
        assert not db.get_folder_by_path(volume_id, source.name)

        # The drive can reconnect at a different mount path with the same identity.
        remount = tmp_path / "Reconnected drive"
        capture = archive_job.capture_volume_snapshot
        remounted = replace(capture(destination), source_path=str(remount), mount_root=str(remount))
        destination.rename(remount)
        monkeypatch.setattr(utils, "list_connected_volume_snapshots", lambda: [remounted])
        monkeypatch.setattr(archive_job, "capture_volume_snapshot",
                            lambda path: remounted if path == remount else capture(path))
        recorded_paths = []
        copy = ArchiveJob._copy_file

        def record_copy(self, path, *args):
            recorded_paths.append(path)
            return copy(self, path, *args)

        monkeypatch.setattr(ArchiveJob, "_copy_file", record_copy)
        result = ArchiveJob(db).run(source, volume_id)
        assert recorded_paths == [source / "nested/source.dat"]
        assert result.path.parent == remount
        assert Path(db.get_volume(volume_id)["source_path"]) == remount
        assert (result.path / "nested/source.dat").read_bytes() == original
        assert len(db.list_volumes()) == 2
    finally:
        db.close()


def test_retry_after_publication_registers_once(tmp_path, project, index_volume, monkeypatch):
    source, destination = project
    db = create_catalogue(tmp_path / "Archive.jvvv")
    register = ArchiveJob._register

    def interrupt_after_commit(self, *args):
        register(self, *args)
        raise OSError("Interrupted before recording completion")

    try:
        volume_id = index_volume(db, destination)
        with monkeypatch.context() as context:
            context.setattr(ArchiveJob, "_register", interrupt_after_commit)
            with pytest.raises(OSError, match="Interrupted"):
                ArchiveJob(db).run(source, volume_id)
        assert (destination / source.name).is_dir()
        assert pending_archive(db.path) is not None
        assert len(db.list_volumes()) == 1
        published = db.get_folder_by_path(volume_id, source.name)
        result = ArchiveJob(db).run(source, volume_id)
        assert result.volume_id == db.list_volumes()[0]["id"]
        assert db.get_folder_by_path(volume_id, source.name)["id"] == published["id"]
        assert db.get_volume(volume_id)["indexed_file_count"] == 3
        assert len(db.list_volumes()) == 1
    finally:
        db.close()


def test_queued_archive_journals_are_independent_and_completed_jobs_recover(tmp_path, project, index_volume):
    source, destination = project
    another = tmp_path / "Second Project"
    another.mkdir()
    (another / "file.dat").write_bytes(b"another project")
    db = create_catalogue(tmp_path / "Archive.jvvv")
    first_id, second_id = uuid4().hex, uuid4().hex
    stop = Event()

    def progress(phase, *_):
        if phase.startswith("Reading back"):
            stop.set()

    try:
        volume_id = index_volume(db, destination)
        with pytest.raises(ArchivePaused):
            ArchiveJob(db, cancelled=stop.is_set, progress=progress).run(source, volume_id, job_id=first_id)
        ArchiveJob(db).run(another, volume_id, job_id=second_id)
        first = ArchiveJob(db).run(source, volume_id, job_id=first_id)
        source.rename(tmp_path / "Source renamed after archiving")
        assert ArchiveJob(db).run(source, volume_id, job_id=first_id) == first
        assert db.get_folder_by_path(volume_id, another.name) is not None
        assert db.get_folder_by_path(volume_id, source.name) is not None
    finally:
        db.close()


def test_resume_detects_source_edit_with_preserved_size_and_time(tmp_path, project, index_volume):
    source, destination = project
    db = create_catalogue(tmp_path / "Archive.jvvv")
    pause = Event()
    original = source / "nested/source.dat"
    stamp = original.stat()

    def progress(label, *_):
        if label.startswith("Reading back"):
            pause.set()

    try:
        volume_id = index_volume(db, destination)
        with pytest.raises(ArchivePaused):
            ArchiveJob(db, cancelled=pause.is_set, progress=progress).run(source, volume_id)
        original.write_bytes(b"x" * stamp.st_size)
        os.utime(original, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with pytest.raises(OSError, match="source changed"):
            ArchiveJob(db).run(source, volume_id)
        assert not db.get_folder_by_path(volume_id, source.name)
        assert not (destination / source.name).exists()
    finally:
        db.close()


def test_disconnected_destination_leaves_a_resumable_job(tmp_path, project, index_volume, monkeypatch):
    source, destination = project
    db = create_catalogue(tmp_path / "Archive.jvvv")

    def disconnect_on_verify(label, *_):
        if label.startswith("Reading back"):
            original = archive_job._checked_folder

            def checked(path):
                if path == destination:
                    raise OSError("The destination is disconnected")
                return original(path)

            context.setattr(archive_job, "_checked_folder", checked)

    try:
        volume_id = index_volume(db, destination)
        with monkeypatch.context() as context:
            with pytest.raises(OSError, match="disconnected"):
                ArchiveJob(db, progress=disconnect_on_verify).run(source, volume_id)
        assert not (destination / source.name).exists()
        assert pending_archive(db.path) is not None
        assert not db.get_folder_by_path(volume_id, source.name)
        result = ArchiveJob(db).run(source, volume_id)
        assert result.files == 2
        assert pending_archive(db.path) is None
    finally:
        db.close()


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows read leases and directory notifications")
def test_source_lease_blocks_existing_file_edits_and_new_file_aborts(tmp_path, project, index_volume):
    source, destination = project
    db = create_catalogue(tmp_path / "Archive.jvvv")
    original = source / "nested/source.dat"

    def try_edit(label, *_):
        if label.startswith("Copying"):
            with pytest.raises(PermissionError):
                original.write_bytes(b"must not overwrite")
            with pytest.raises(PermissionError):
                original.rename(source / "renamed.dat")
            with pytest.raises(PermissionError):
                original.unlink()
            (source / "new.dat").write_bytes(b"added by another application")

    try:
        volume_id = index_volume(db, destination)
        with pytest.raises(OSError, match="source project changed"):
            ArchiveJob(db, progress=try_edit).run(source, volume_id)
        assert not db.get_folder_by_path(volume_id, source.name)
        assert not (destination / source.name).exists()
        assert original.read_bytes().startswith(b"original")
        original.write_bytes(b"the lease was released")
    finally:
        db.close()


@pytest.mark.skipif(os.name != "nt", reason="Tests the protected volume's Windows drive identity")
def test_rejects_another_mount_of_protected_drive(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "alias"
    source.mkdir()
    destination.mkdir()

    def snapshot(path):
        return VolumeSnapshot(str(path), str(path), "", "windows-volume-guid", "protected-guid")

    monkeypatch.setattr(archive_job, "capture_volume_snapshot", snapshot)
    with pytest.raises(OSError, match="protected Q:"):
        archive_job.check_archive_write_locations(source, destination)
    assert not list(destination.iterdir())


def test_preview_worker_overlaps_next_copy_and_reuses_matching_output(tmp_path, project, index_volume, monkeypatch):
    source, destination = project
    image_path = source / "a-first.png"
    Image.new("RGB", (64, 32), "green").save(image_path)
    (source / "z-duplicate.png").write_bytes(image_path.read_bytes())
    started_next_copy = Event()
    inspected_first = Event()
    actual_copy = ArchiveJob._copy_file
    actual_inspect = ArchiveJob._inspect_media

    def copy(self, path, *args):
        if path.name == "empty.dat":
            assert inspected_first.wait(5), "The previous preview was never scheduled"
            started_next_copy.set()
        return actual_copy(self, path, *args)

    def inspect(self, entry, *args):
        if entry.relative == image_path.name:
            inspected_first.set()
            assert started_next_copy.wait(5), "The next file was not copied during preview work"
        return actual_inspect(self, entry, *args)

    monkeypatch.setattr(ArchiveJob, "_copy_file", copy)
    monkeypatch.setattr(ArchiveJob, "_inspect_media", inspect)
    app = QApplication.instance() or QApplication([])
    db = create_catalogue(tmp_path / "Archive.jvvv")
    preview_root = tmp_path / "previews"
    preview_root.mkdir()
    try:
        volume_id = index_volume(db, destination)
        settings = PreviewSettings(enabled=True, root_directory=str(preview_root))
        result = ArchiveJob(db, preview_settings=settings).run(source, volume_id)
        assert result.preview_failures == 0
        root = db.get_folder_by_path(result.volume_id, source.name)
        rows = [row for row in db.list_files(result.volume_id, root["id"]) if row["name"].endswith(".png")]
        assert len(rows) == 2
        assert all(db.get_file_preview_status(row["id"])["status"] == "available" for row in rows)
        assert len(list(preview_root.rglob("*.jpg"))) == 1
    finally:
        db.close()


@pytest.mark.parametrize("from_context", [False, True])
def test_archive_action_uses_only_existing_scanned_volumes(tmp_path, project, index_volume, monkeypatch, from_context):
    from PySide6.QtWidgets import QMenu
    import jvvv.app as app_module
    from jvvv.app import MainWindow
    from jvvv.archive_ui import ArchiveDialog

    source, destination = project
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    window = MainWindow()
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window._set_catalogue_open(True)
    other = tmp_path / "Another Drive"
    other.mkdir()
    unscanned = tmp_path / "Unscanned"
    unscanned.mkdir()
    shown_dialogs = []

    def execute(dialog):
        shown_dialogs.append(dialog)
        choices = [dialog.destination.itemData(index)[0] for index in range(dialog.destination.count())]
        if from_context:
            assert choices == [volume_id]
            assert not dialog.destination.isEnabled()
        else:
            assert set(choices) == {volume_id, other_id}
            assert dialog.destination.isEnabled()
            assert not dialog.start.isEnabled()
            dialog.destination.setCurrentIndex(choices.index(volume_id))
        dialog.source.setText(str(source))
        dialog.start.click()
        return dialog.result()

    monkeypatch.setattr(ArchiveDialog, "exec", execute)
    try:
        assert not window.archive_project_action.isEnabled()
        index_volume(window.db, unscanned, scan=False)
        window.refresh_volumes()
        assert not window.archive_project_action.isEnabled()
        volume_id = index_volume(window.db, destination)
        other_id = index_volume(window.db, other)
        window.refresh_volumes()
        assert window.archive_project_action.isEnabled()
        assert window.select_volume(other_id)
        if from_context:
            class ArchiveMenu(QMenu):
                def exec(self, _point):
                    action = next(action for action in self.actions() if action.text() == "Archive Project…")
                    assert action.isEnabled()
                    action.trigger()

            monkeypatch.setattr(app_module, "QMenu", ArchiveMenu)
            window.show()
            app.processEvents()
            row = next(row for row, item in enumerate(window.volume_model.items) if item.id == volume_id)
            point = window.volume_table.visualRect(window.volume_model.index(row, 0)).center()
            window.show_volume_context_menu(point)
        else:
            window.archive_project_action.trigger()
        assert len(shown_dialogs) == 1
        assert not (destination / source.name).exists()
        assert window.operations_queue.jobs[0].state == "queued"
        window.operations_dialog.apply.click()
        wait_for_queue(lambda: window.operations_queue.jobs[0].state in {"completed", "warning", "failed"})
        job = window.operations_queue.jobs[0]
        assert job.state == "completed", job.detail
        assert len(window.db.list_volumes()) == 3
        assert window.db.get_folder_by_path(volume_id, source.name) is not None
        assert not list(other.iterdir())
    finally:
        window.close()
        app.processEvents()


def wait_for_queue(predicate):
    from time import monotonic
    from PySide6.QtTest import QTest

    end = monotonic() + 12
    while not predicate() and monotonic() < end:
        QTest.qWait(25)
    assert predicate(), "Timed out waiting for the operations queue."


def test_queued_scans_and_dependent_archive_continue_past_disconnected_drive(
    tmp_path, project, index_volume, monkeypatch,
):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from jvvv.app import MainWindow
    from jvvv.archive_ui import ArchiveDialog

    source, destination = project
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    monkeypatch.setattr(MainWindow, "edit_volume_by_id", lambda *_: pytest.fail("Unexpected edit dialog"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: pytest.fail("Unexpected root picker"))
    monkeypatch.setattr(QMessageBox, "critical", lambda *_: pytest.fail("Unexpected modal error"))
    window = MainWindow()
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window._set_catalogue_open(True)
    offline = tmp_path / "Offline Drive"
    offline.mkdir()
    hidden = tmp_path / "Unplugged"

    def choose_archive(dialog):
        dialog.source.setText(str(source))
        dialog.start.click()
        return dialog.result()

    monkeypatch.setattr(ArchiveDialog, "exec", choose_archive)
    try:
        offline_id = index_volume(window.db, offline, scan=False)
        destination_id = index_volume(window.db, destination, scan=False)
        window.start_scan(offline_id)
        window.start_scan(destination_id)
        window.archive_project(destination_id)
        queue = window.operations_queue
        first, scan, archive = queue.jobs
        assert archive.requires == scan.id
        assert all(job.state == "queued" for job in queue.jobs)
        assert not window.db.get_volume(destination_id)["last_scan_at"]
        offline.rename(hidden)
        window.apply_operations()
        wait_for_queue(lambda: queue.get(archive.id).state in {"completed", "warning", "failed"})
        assert queue.get(first.id).state == "waiting"
        assert queue.get(scan.id).state == "completed", queue.get(scan.id).detail
        assert queue.get(archive.id).state == "completed", queue.get(archive.id).detail
        assert (destination / source.name / "Project.jvvv-project.json").is_file()
        hidden.rename(offline)
        window._pump_operations()
        wait_for_queue(lambda: queue.get(first.id).state == "completed")
        window.start_scan(destination_id)
        rescan = queue.jobs[-1]
        assert rescan.id != scan.id
        window.preview_settings = PreviewSettings(enabled=True, root_directory=str(tmp_path / "Missing Preview Store"))
        window.apply_operations()
        wait_for_queue(lambda: queue.get(rescan.id).state in {"completed", "warning", "failed"})
        assert queue.get(rescan.id).state == "warning", queue.get(rescan.id).detail
        assert "Offline previews skipped" in queue.get(rescan.id).detail
    finally:
        window.close()
        app.processEvents()


def test_register_archive_drive_survives_changed_windows_identity_and_rejects_clones(
    tmp_path, project, index_volume, monkeypatch,
):
    import shutil
    from PySide6.QtWidgets import QMessageBox
    import jvvv.app as app_module
    from jvvv.app import MainWindow
    from jvvv.drive_marker import MARKER_IDENTITY, MARKER_NAME, read_drive_marker

    source, destination = project
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    monkeypatch.setattr(app_module, "capture_volume_snapshot", utils.capture_volume_snapshot)
    window = MainWindow()
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window._set_catalogue_open(True)
    try:
        volume_id = index_volume(window.db, destination)
        assert not (destination / MARKER_NAME).exists()
        window.register_archive_drive(volume_id)
        volume = window.db.get_volume(volume_id)
        assert volume["identity_kind"] == MARKER_IDENTITY
        assert read_drive_marker(destination) == volume["uuid"]

        reconnect = tmp_path / "Drive with new letter and GUID"
        clone = tmp_path / "Clone"
        destination.rename(reconnect)
        clone.mkdir()
        original_capture = utils.capture_volume_snapshot
        changed = VolumeSnapshot(str(reconnect), str(reconnect), "", "windows-volume-guid", "new-native-id")
        duplicate = VolumeSnapshot(str(clone), str(clone), "", "windows-volume-guid", "clone-native-id")
        mounts = [changed]

        def capture(path):
            return changed if path == reconnect else original_capture(path)

        monkeypatch.setattr(utils, "list_connected_volume_snapshots", lambda: mounts)
        monkeypatch.setattr(scanner, "capture_volume_snapshot", capture)
        monkeypatch.setattr(archive_job, "capture_volume_snapshot", capture)
        assert utils.resolve_volume_source_path(volume) == str(reconnect)
        assert VolumeScanner(window.db, require_complete=True).scan(volume_id).status == "completed"
        volume = window.db.get_volume(volume_id)
        assert volume["indexed_file_count"] == 0
        assert volume["identity_kind"] == MARKER_IDENTITY

        shutil.copyfile(reconnect / MARKER_NAME, clone / MARKER_NAME)
        mounts.append(duplicate)
        assert utils.ConnectedVolumeResolver().resolve_details(volume) == (None, "Relink Required")
        with pytest.raises(OSError, match="Connect the selected"):
            ArchiveJob(window.db).run(source, volume_id, job_id=uuid4().hex)
        assert not (clone / source.name).exists()
        mounts.remove(duplicate)
        assert ArchiveJob(window.db).run(source, volume_id, job_id=uuid4().hex).path.parent == reconnect
    finally:
        window.close()
        app.processEvents()
