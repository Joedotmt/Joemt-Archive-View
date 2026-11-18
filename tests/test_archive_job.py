"""Archived contents, recovery and file protection at the filesystem boundary."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from threading import Barrier, Event, get_ident
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


def test_preview_input_reuses_immutable_blocks_and_spills_on_memory_pressure(tmp_path, monkeypatch):
    headroom = 1024
    monkeypatch.setattr(archive_job, "archive_memory_headroom", lambda: headroom)
    monkeypatch.setattr(archive_job, "MEMORY_RECHECK_BYTES", 4)
    cache = archive_job.MediaInput(tmp_path, ".mov", 10)
    try:
        first = b"first"
        cache.write(first)
        assert cache.blocks[0] is first
        mutable = bytearray(b"ab")
        cache.write(memoryview(mutable))
        mutable[:] = b"cd"
        headroom = 0
        cache.write(b"end")
        assert not cache.blocks and cache.stream is not None
        assert cache.materialize().read_bytes() == b"firstabend"
    finally:
        cache.close()
    assert not cache.path.exists()


def test_pressure_limits_read_ahead_and_releases_optional_data_while_waiting(tmp_path, monkeypatch):
    file = tmp_path / "project.dat"
    file.write_bytes(b"0123456789abcdef")
    entry = archive_job.ProjectEntry(file.name, file.stat(), False)
    release = Future()
    job = ArchiveJob(type("DB", (), {"hash_algorithm": "sha256"})(), destination_ready=release)
    monkeypatch.setattr(job, "_check", lambda: None)
    samples = iter((32, 0))
    monkeypatch.setattr(archive_job, "archive_memory_headroom", lambda: next(samples))
    monkeypatch.setattr(archive_job, "MEMORY_RECHECK_BYTES", 4)
    monkeypatch.setattr(archive_job, "CHUNK_SIZE", 4)
    job._read_ahead(tmp_path, [entry], tmp_path, None, lambda: False)
    assert job.prefixes[file.name].size == 4
    monkeypatch.setattr(archive_job, "archive_memory_headroom", lambda: -4)
    release.set_result(True)
    job._wait_for_destination()
    assert not job.prefixes


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


@pytest.mark.skipif(os.name != "nt", reason="Exercises protection of the Windows Q: drive")
def test_automatic_registration_leaves_a_protected_mount_read_only(tmp_path, project, index_volume, monkeypatch):
    _, destination = project
    db = create_catalogue(tmp_path / "Archive.jvvv")
    try:
        volume_id = index_volume(db, destination)
        before = db.get_volume(volume_id)
        capture = archive_job.capture_volume_snapshot

        def capture_protected(path):
            if Path(path) == Path("Q:/"):
                return replace(capture(destination), source_path="Q:\\", mount_root="Q:\\")
            return capture(path)

        monkeypatch.setattr(archive_job, "capture_volume_snapshot", capture_protected)
        archive_job.register_scanned_drive(db, before)
        assert not (destination / ".jvvv-drive.json").exists()
        assert db.get_volume(volume_id)["identity_kind"] == before["identity_kind"]
    finally:
        db.close()


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
    progress_formats = []
    set_format = window.scan_progress.setFormat

    def record_progress(text):
        if window.scan_progress.maximum() == 1000:
            progress_formats.append(text)
        set_format(text)

    monkeypatch.setattr(window.scan_progress, "setFormat", record_progress)
    other = tmp_path / "Another Drive"
    other.mkdir()
    unscanned = tmp_path / "Unscanned"
    unscanned.mkdir()
    shown_dialogs = []
    lookup_started = Event()
    release_lookup = Event()
    gui_thread = get_ident()
    list_snapshots = utils.list_connected_volume_snapshots

    def slow_drive_lookup():
        assert get_ident() != gui_thread, "Drive discovery ran on the UI thread"
        lookup_started.set()
        assert release_lookup.wait(8)
        return list_snapshots()

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
        assert str(destination) in dialog.destination.currentText()
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
        volume = window.db.get_volume(volume_id)
        window._connected_volume_paths[app_module.volume_location_key(volume)] = str(destination)
        window.refresh_volumes()
        assert window.archive_project_action.isEnabled()
        assert window.select_volume(other_id)
        monkeypatch.setattr(utils, "list_connected_volume_snapshots", slow_drive_lookup)
        if from_context:
            class ArchiveMenu(QMenu):
                def exec(self, _point):
                    assert all(action.text() != "Register Archive Drive…" for action in self.actions())
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
        queued = window.operations_queue.jobs[0]
        assert queued.state == "queued"
        assert window.operations_dialog is None
        assert window.catalogue_header.isAncestorOf(window.apply_queue_button)
        assert window.catalogue_header.isAncestorOf(window.queue_manager_button)
        assert window.queue_summary_label.text() == queued.title
        assert window.apply_queue_button.isEnabled()
        assert window.apply_queue_button.text() == "Apply"
        if from_context:
            window.queue_manager_button.click()
            manager = window.operations_dialog
            assert manager.apply_button.text() == "Apply"
            assert manager.apply_button.isEnabled()
            manager.apply_button.click()
        else:
            window.apply_queue_button.click()
        wait_for_queue(lookup_started.is_set)
        assert window.operations_queue.get(queued.id).state == "ready"
        assert window.scan_progress.format() == "Checking drive connection…"
        assert queued.title in window.operation_progress_label.text()
        if from_context:
            assert "Checking drive connection…" in manager.progress.text()
        assert window.select_volume(volume_id)
        assert window.current_volume_id == volume_id
        release_lookup.set()
        wait_for_queue(lambda: window.operations_queue.jobs[0].state in {"completed", "warning", "failed"})
        job = window.operations_queue.jobs[0]
        assert job.state == "completed", job.detail
        assert len(window.db.list_volumes()) == 3
        assert window.db.get_folder_by_path(volume_id, source.name) is not None
        assert not list(other.iterdir())
        assert any("Copying and hashing" in text for text in progress_formats)
        assert any("Reading back the destination" in text for text in progress_formats)
        assert window.scan_progress.value() == window.scan_progress.maximum()
        assert window.queue_summary_label.text() == "No pending changes"
        assert not window.apply_queue_button.isEnabled()
        window.apply_operations()
        assert window.operations_queue.jobs == [job]
        window.queue_manager_button.click()
        manager = window.operations_dialog
        assert manager.windowTitle() == "Queue Manager"
        assert manager.tables["Pending"].rowCount() == 0
        assert manager.tables["Completed"].rowCount() == 1
        assert not manager.apply_button.isEnabled()
    finally:
        release_lookup.set()
        window.close()
        app.processEvents()


def wait_for_queue(predicate):
    from time import monotonic
    from PySide6.QtTest import QTest

    end = monotonic() + 12
    while not predicate() and monotonic() < end:
        QTest.qWait(25)
    assert predicate(), "Timed out waiting for the operations queue."


@pytest.mark.parametrize("kind", ["scan", "archive", "pause one archive"])
def test_queue_runs_two_independent_jobs_and_publishes_both(tmp_path, project, index_volume, monkeypatch, kind):
    from jvvv.app import MainWindow
    from jvvv.operations_queue import OperationDisks

    source, destination = project
    second = tmp_path / "Second volume"
    second.mkdir()
    for root in (destination, second):
        (root / "before.dat").write_bytes(b"indexed earlier")
        if kind == "scan":
            (root / "Gone").mkdir()
            (root / "Gone" / "nested.dat").write_bytes(b"removed with its folder")
            (root / "gone.dat").write_bytes(b"removed from a folder that stays")

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    window = MainWindow()
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window._set_catalogue_open(True)
    parallel_io = Barrier(2, timeout=10)
    resume_retry = Event()

    def resolve_disks(cls, job, path):
        disk = (7, 1 if Path(path) == destination else 2)
        return OperationDisks(disk) if job.kind == "scan" else OperationDisks(disk, (7, 7), True, True)

    monkeypatch.setattr(OperationDisks, "resolve", classmethod(resolve_disks))
    try:
        ids = [index_volume(window.db, path) for path in (destination, second)]
        previous_ids = [
            window.db.list_files(volume_id, window.db.get_root_folder(volume_id)["id"])[0]["id"]
            for volume_id in ids
        ]
        if kind == "scan":
            original = VolumeScanner._hash_file

            def hash_file(self, path, *args, **kwargs):
                if path.name == "before.dat":
                    parallel_io.wait()
                return original(self, path, *args, **kwargs)

            monkeypatch.setattr(VolumeScanner, "_hash_file", hash_file)
            for volume_id, path in zip(ids, (destination, second)):
                (path / "Gone" / "nested.dat").unlink()
                (path / "Gone").rmdir()
                (path / "gone.dat").unlink()
                (path / "new.dat").write_bytes(b"new file")
                window.start_scan(volume_id)
        else:
            original = ArchiveJob._copy_file

            def copy_file(self, path, part, *args, **kwargs):
                if path.name == "empty.dat" and not resume_retry.is_set():
                    parallel_io.wait()
                    if kind == "pause one archive" and part.parent.parent == destination:
                        assert resume_retry.wait(10)
                return original(self, path, part, *args, **kwargs)

            monkeypatch.setattr(ArchiveJob, "_copy_file", copy_file)
            queue = window._get_operations_queue()
            for volume_id in ids:
                volume = window.db.get_volume(volume_id)
                queue.add("archive", volume, volume["name"], source=str(source))

        window.queue_manager_button.click()
        window.apply_queue_button.click()
        wait_for_queue(lambda: len(window.running_operations) == 2)
        assert window.operation_progress_label.text().startswith("2 jobs running")
        assert window.operations_dialog.tables["Pending"].cellWidget(1, 4) is not None
        if kind == "pause one archive":
            first, other = window.operations_queue.jobs
            window.pause_queue_job(first.id)
            resume_retry.set()
            wait_for_queue(lambda: window.operations_queue.get(first.id).state == "paused"
                           and window.operations_queue.get(other.id).state == "completed")
            assert pending_archive(window.db.path) is None
            window.apply_queue_button.click()
        wait_for_queue(lambda: all(job.state in {"completed", "warning", "failed"} for job in window.operations_queue.jobs))
        assert all(job.state == "completed" for job in window.operations_queue.jobs), window.operations_queue.jobs
        for volume_id, file_id in zip(ids, previous_ids):
            assert window.db.get_file(file_id)["name"] == "before.dat"
            if kind == "scan":
                assert window.db.get_volume(volume_id)["indexed_file_count"] == 2
                root = window.db.get_root_folder(volume_id)
                assert {row["name"] for row in window.db.list_files(volume_id, root["id"])} == {"before.dat", "new.dat"}
                assert window.db.get_folder_by_path(volume_id, "Gone") is None
                scan = window.db.list_scan_history(volume_id)[0]
                assert scan["status"] == "completed"
                assert (scan["files_removed"], scan["folders_removed"]) == (2, 1)
            else:
                assert window.db.get_folder_by_path(volume_id, source.name) is not None
    finally:
        resume_retry.set()
        window.close()
        app.processEvents()


@pytest.mark.parametrize("pause_first", [False, True])
def test_next_archive_reads_ahead_while_same_disk_verifies(tmp_path, project, index_volume, monkeypatch, pause_first):
    from jvvv.app import MainWindow
    from jvvv.operations_queue import OperationDisks

    first, destination = project
    second = tmp_path / "Next project"
    second.mkdir()
    Image.new("RGB", (16, 16), "blue").save(second / "first.png")
    payload = bytes(range(256)) * 2048
    (second / "large.dat").write_bytes(payload)
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    monkeypatch.setattr(OperationDisks, "resolve", classmethod(lambda cls, *_: cls((7, 2), (7, 1), True)))
    monkeypatch.setattr(archive_job, "archive_memory_headroom", lambda: 8192)
    monkeypatch.setattr(archive_job, "physical_disk_key", lambda path: (7, 2) if path == destination else (7, 1))
    # This image-only fixture does not require a system video encoder.
    monkeypatch.setattr("jvvv.archive_ui.prepare_operation_previews", lambda settings, source: (settings, None))
    release = Event()
    verifying = Event()
    original = archive_job.DestinationReader.digest
    buffered_sizes = {}
    original_copy = ArchiveJob._copy_file

    def copy_file(job, path, *args):
        if path.parent == second and path.name in job.prefixes:
            buffered_sizes[path.name] = job.prefixes[path.name].size
        return original_copy(job, path, *args)

    monkeypatch.setattr(ArchiveJob, "_copy_file", copy_file)

    def digest(reader, path, *args, **kwargs):
        if not verifying.is_set():
            verifying.set()
            assert release.wait(12)
        return original(reader, path, *args, **kwargs)

    monkeypatch.setattr(archive_job.DestinationReader, "digest", digest)
    window = MainWindow()
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window._set_catalogue_open(True)
    preview_root = tmp_path / "Previews"
    preview_root.mkdir()
    window.preview_settings = PreviewSettings(enabled=True, root_directory=str(preview_root))
    try:
        volume_id = index_volume(window.db, destination)
        volume = window.db.get_volume(volume_id)
        queue = window._get_operations_queue()
        a, b = [queue.add("archive", volume, "Test HDD", source=str(path)) for path in (first, second)]
        window.apply_operations()
        wait_for_queue(lambda: b.id in window.running_operations and
                       window.running_operations[b.id].message.startswith("Waiting for the previous archive"))
        assert verifying.is_set()
        assert not (destination / second.name).exists()
        assert not (window.db.path / f"archive-job-{b.id}.sqlite").exists()
        assert second.name in window.operation_progress_label.text()
        if pause_first:
            window.pause_queue_job(a.id)
        release.set()
        wait_for_queue(lambda: not window.running_operations)
        if pause_first:
            assert [job.state for job in queue.jobs] == ["paused", "paused"]
            assert not (destination / second.name).exists()
            window.apply_operations()
            wait_for_queue(lambda: all(job.state == "completed" for job in queue.jobs))
        assert [job.state for job in queue.jobs] == ["completed", "completed"]
        if not pause_first:
            assert sum(buffered_sizes.values()) == 8192
            assert 0 < buffered_sizes["large.dat"] < len(payload)
        assert (destination / second.name / "large.dat").read_bytes() == payload
        assert window.db.get_folder_by_path(volume_id, second.name)
        assert list(preview_root.rglob("*.jpg"))
    finally:
        release.set()
        window.close()
        app.processEvents()


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
        assert window.operations_dialog is None
        assert window.queue_summary_label.text() == "3 pending changes"
        assert window.apply_queue_button.text() == "Apply All"
        window.queue_manager_button.click()
        assert window.operations_dialog.apply_button.text() == "Apply All"
        assert not window.db.get_volume(destination_id)["last_scan_at"]
        offline.rename(hidden)
        window.apply_queue_button.click()
        wait_for_queue(lambda: queue.get(archive.id).state in {"completed", "warning", "failed"})
        assert queue.get(first.id).state == "waiting"
        assert queue.get(scan.id).state == "completed", queue.get(scan.id).detail
        assert queue.get(archive.id).state == "completed", queue.get(archive.id).detail
        assert (destination / source.name / "Project.jvvv-project.json").is_file()
        assert (destination / ".jvvv-drive.json").is_file()
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


@pytest.mark.parametrize("case", ["rescan", "archive", "initial scan", "moved", "duplicate", "previous relink"])
def test_queue_checks_known_rescan_directly_and_preserves_drive_discovery(tmp_path, monkeypatch, case):
    from jvvv.app import _probe_queue_drives
    from jvvv.drive_marker import MARKER_IDENTITY, write_drive_marker
    from jvvv.operations_queue import Operation, OperationDisks

    cached = tmp_path / "Cached drive"
    moved = tmp_path / "Reconnected drive"
    drive_id = str(uuid4())
    for path in (cached, moved):
        path.mkdir()
        write_drive_marker(path, drive_id)
    snapshot = VolumeSnapshot(str(cached), str(cached), "", "windows-volume-guid", "original",
                              identity_serial="1234", identity_filesystem="exFAT")
    relocated = replace(snapshot, source_path=str(moved), mount_root=str(moved))
    volume = {**snapshot.as_db_fields(), "uuid": drive_id, "source_path": str(cached),
              "identity_kind": MARKER_IDENTITY, "identity_token": drive_id,
              "last_scan_at": None if case == "initial scan" else "earlier"}
    job = Operation("job", "archive" if case == "archive" else "scan", drive_id, "AID-151",
                    state="relink" if case == "previous relink" else "ready")
    discovery_calls = []

    def discover():
        discovery_calls.append(True)
        if case in {"duplicate", "previous relink"}:
            return [snapshot, relocated]
        return [relocated] if case == "moved" else [snapshot]

    observed = replace(snapshot, identity_serial="4321") if case == "duplicate" else snapshot
    monkeypatch.setattr(utils, "list_connected_volume_snapshots", discover)
    monkeypatch.setattr("jvvv.app.capture_volume_snapshot", lambda _path: None if case == "moved" else observed)
    monkeypatch.setattr(OperationDisks, "resolve", classmethod(lambda cls, *_: cls((7, 1))))
    result = Future()
    _probe_queue_drives([(job, volume, str(cached))], result)
    check = result.result()[job.id]

    assert len(discovery_calls) == (0 if case == "rescan" else 1)
    if case in {"duplicate", "previous relink"}:
        assert (check.path, check.problem) == (None, "Relink Required")
    else:
        assert (check.path, check.problem) == (str(moved if case == "moved" else cached), "")


@pytest.mark.parametrize("has_marker", [False, True])
def test_first_scan_registers_drive_and_rejects_cloned_markers(
    tmp_path, project, index_volume, monkeypatch, has_marker,
):
    import shutil
    from PySide6.QtWidgets import QMessageBox
    from jvvv.app import MainWindow
    from jvvv.drive_marker import MARKER_IDENTITY, MARKER_NAME, read_drive_marker, write_drive_marker

    source, destination = project
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    monkeypatch.setattr(MainWindow, "_confirm_snapshot_before_close", lambda self: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: pytest.fail("Unexpected registration question"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    window = MainWindow()
    window.catalogue_path = tmp_path / "Archive.jvvv"
    window.db = create_catalogue(window.catalogue_path)
    window._set_catalogue_open(True)
    try:
        volume_id = index_volume(window.db, destination, scan=False)
        original_id = str(uuid4()) if has_marker else window.db.get_volume(volume_id)["uuid"]
        if has_marker:
            write_drive_marker(destination, original_id)
        window.start_scan(volume_id)
        assert (destination / MARKER_NAME).exists() == has_marker
        window.apply_queue_button.click()
        wait_for_queue(lambda: window.operations_queue.jobs[-1].state in {"completed", "warning", "failed"})
        job = window.operations_queue.jobs[-1]
        assert job.state == "completed", job.detail
        volume = window.db.get_volume(volume_id)
        assert volume["identity_kind"] == MARKER_IDENTITY
        assert read_drive_marker(destination) == volume["identity_token"] == original_id

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
