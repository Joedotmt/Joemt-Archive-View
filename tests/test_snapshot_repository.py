"""Snapshot repository: content-addressed version history of a catalogue folder."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jvvv.backup_analysis import BackupAnalysisEngine
from jvvv.database import count_rows, create_catalogue, open_catalogue
from jvvv.snapshot_repository import (
    InvalidRepositoryError,
    SnapshotCancelled,
    SnapshotError,
    SnapshotRepository,
    default_repository_path,
    repository_path_for_catalogue,
    repository_setting_value,
)
from jvvv.volume_store import iter_blob_bytes

from test_catalogue_directory import populate, volume_counts


def build_catalogue(tmp_path: Path, *, files_a: int = 30, files_b: int = 8):
    db = create_catalogue(tmp_path / "Archive")
    first = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    second = db.create_volume("Drive B", str(tmp_path), {"drive_id": "AID-002"})
    populate(db, first, "a", files_a)
    populate(db, second, "b", files_b)
    db.close()
    return tmp_path / "Archive.jvvv", first, second


def test_default_repository_sits_beside_the_catalogue(tmp_path):
    assert default_repository_path(tmp_path / "Archive.jvvv") == tmp_path / "Archive.jvvvrepo"
    assert default_repository_path(tmp_path / "Archive") == tmp_path / "Archive.jvvvrepo"
    stored = repository_setting_value(tmp_path / "Archive.jvvv", tmp_path / "Backups" / "Archive.jvvvrepo")
    assert stored == str(Path("Backups") / "Archive.jvvvrepo")
    assert repository_path_for_catalogue(tmp_path / "Archive.jvvv", stored) == (
        tmp_path / "Backups" / "Archive.jvvvrepo"
    ).resolve()
    assert repository_path_for_catalogue(tmp_path / "Archive.jvvv", None) == tmp_path / "Archive.jvvvrepo"


def test_first_snapshot_writes_every_volume_and_unchanged_volumes_are_free(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")

    progress = []
    result = repository.create_snapshot(catalogue, label="first", progress_callback=progress.append)

    assert result.volumes_written == 2
    assert result.volumes_reused == 0
    assert result.blobs_added == 4  # content + history per volume
    assert result.bytes_written > 0
    assert result.manifest.volume_count == 2
    assert result.manifest.file_count == 38
    assert {record.drive_id for record in result.manifest.volumes} == {"AID-001", "AID-002"}
    assert progress and progress[-1].phase == "manifest"
    manifest_path = repository.snapshot_path(result.manifest.snapshot_id)
    assert manifest_path.is_file()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["format"] == "jvvv-snapshot"
    assert data["totals"] == {"volumes": 2, "files": 38, "folders": 6}
    for record in result.manifest.volumes:
        assert repository.blob_path(record.content_blob).is_file()
        assert repository.blob_path(record.history_blob).is_file()

    again = repository.create_snapshot(catalogue, label="again")
    assert again.volumes_written == 0
    assert again.volumes_reused == 2
    assert again.blobs_added == 0
    assert again.bytes_written == 0
    assert again.manifest.blob_digests == result.manifest.blob_digests
    assert repository.meta()["catalogue_name"] == "Archive"


def test_only_the_rescanned_volume_costs_a_new_blob_and_the_delta_names_it(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    baseline = repository.create_snapshot(catalogue)

    db = open_catalogue(catalogue)
    populate(db, second, "c", 9)
    db.update_volume(first, "Drive A renamed", str(tmp_path), None)
    db.close()

    result = repository.create_snapshot(catalogue, label="rescan B")
    assert result.volumes_written == 1
    assert result.volumes_reused == 1
    assert result.blobs_added == 2
    delta = SnapshotRepository.delta(result.manifest, baseline.manifest)
    assert [record.drive_id for record in delta.content_changed] == ["AID-002"]
    assert [record.drive_id for record in delta.metadata_changed] == ["AID-001"]
    assert delta.added == [] and delta.removed == []
    assert next(record for record in result.manifest.volumes if record.drive_id == "AID-001").name == "Drive A renamed"

    statistics = repository.statistics()
    assert statistics.snapshot_count == 2
    assert statistics.blob_count == 6
    assert statistics.unreferenced_blobs == 0


def test_restore_rebuilds_an_identical_catalogue_and_its_index(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    db = open_catalogue(catalogue)
    expected = {first: volume_counts(db, first), second: volume_counts(db, second)}
    db.close()
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(catalogue, label="keep")

    progress = []
    result = repository.restore_snapshot(
        snapshot.manifest.snapshot_id,
        tmp_path / "Restored",
        progress_callback=progress.append,
    )
    assert result.catalogue_path == tmp_path / "Restored.jvvv"
    assert result.volumes_restored == 2 and result.files_restored == 38
    assert {item.phase for item in progress} >= {"prepare", "volumes", "index", "finalize"}

    restored = open_catalogue(tmp_path / "Restored.jvvv")
    try:
        assert not restored.last_reconcile.changed
        assert {row["id"]: row["name"] for row in restored.list_volumes()} == {
            first: "Drive A",
            second: "Drive B",
        }
        assert volume_counts(restored, first) == expected[first]
        assert volume_counts(restored, second) == expected[second]
        assert restored.get_volume(first)["drive_id"] == "AID-001"
        assert [row["name"] for row in restored.search("b0003")] == ["b0003.jpg"]
        assert restored.catalogue_uuid() == snapshot.manifest.catalogue_uuid
        for volume_id in (first, second):
            restored.verify_volume_file(volume_id)
        assert BackupAnalysisEngine(restored).state().status == "not_analyzed"
    finally:
        restored.close()

    # A snapshot of the restored catalogue reproduces exactly the same blobs.
    second_repository = SnapshotRepository.create(tmp_path / "Restored.jvvvrepo")
    again = second_repository.create_snapshot(tmp_path / "Restored.jvvv")
    assert again.manifest.blob_digests == snapshot.manifest.blob_digests


def test_restore_requires_explicit_overwrite_and_replaces_atomically(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(catalogue)

    with pytest.raises(FileExistsError):
        repository.restore_snapshot(snapshot.manifest.snapshot_id, catalogue)

    db = open_catalogue(catalogue)
    populate(db, first, "z", 3)
    db.close()
    result = repository.restore_snapshot(snapshot.manifest.snapshot_id, catalogue, overwrite=True)
    assert result.catalogue_path == catalogue
    assert not any(entry.name.startswith("Archive.jvvv.") for entry in tmp_path.iterdir())
    db = open_catalogue(catalogue)
    try:
        assert count_rows(db, "files") == 38
    finally:
        db.close()


def test_restore_a_single_volume_into_the_live_catalogue(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(catalogue, label="before")
    second_uuid = next(record.uuid for record in snapshot.manifest.volumes if record.volume_id == second)

    db = open_catalogue(catalogue)
    # populate() reuses one scanned_at, so the earlier 8 files of drive B survive: 30 + 8 + 20.
    populate(db, second, "c", 20)
    assert count_rows(db, "files") == 58
    db.close()

    result = repository.restore_volume(snapshot.manifest.snapshot_id, second_uuid, catalogue)
    assert result.volume_id == second and not result.registered

    db = open_catalogue(catalogue)
    try:
        assert [item for item, _ in db.last_reconcile.imported] == [second]
        assert count_rows(db, "files") == 38
        assert db.get_volume(second)["indexed_file_count"] == 8
        db.verify_volume_file(second)
    finally:
        db.close()


def test_restore_a_deleted_volume_registers_it_again(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(catalogue)
    first_uuid = next(record.uuid for record in snapshot.manifest.volumes if record.volume_id == first)

    db = open_catalogue(catalogue)
    db.delete_volume(first)
    db.close()

    result = repository.restore_volume(snapshot.manifest.snapshot_id, first_uuid, catalogue)
    assert result.registered and result.volume_id == first

    db = open_catalogue(catalogue)
    try:
        assert sorted(row["name"] for row in db.list_volumes()) == ["Drive A", "Drive B"]
        assert db.get_volume(first)["uuid"] == first_uuid
        assert count_rows(db, "files") == 38
    finally:
        db.close()


def test_delete_prune_and_verify(tmp_path):
    catalogue, first, second = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    old = repository.create_snapshot(catalogue, label="old")
    db = open_catalogue(catalogue)
    populate(db, second, "c", 4)
    db.close()
    new = repository.create_snapshot(catalogue, label="new")
    assert repository.statistics().blob_count == 6

    repository.delete_snapshot(old.manifest.snapshot_id)
    assert repository.snapshot_ids() == [new.manifest.snapshot_id]
    statistics = repository.statistics()
    assert statistics.unreferenced_blobs == 2

    pruned = repository.prune()
    assert pruned.removed_blobs == 2 and pruned.kept_blobs == 4 and pruned.bytes_freed > 0
    assert repository.verify().ok

    # Corrupt one blob and drop another: verify reports both precisely.
    record = new.manifest.volumes[0]
    corrupt = repository.blob_path(record.content_blob)
    corrupt.write_bytes(corrupt.read_bytes()[:-3] + b"xyz")
    repository.blob_path(record.history_blob).unlink()
    report = repository.verify()
    assert not report.ok
    assert any(entry.startswith(record.content_blob) for entry in report.corrupt_blobs)
    assert (new.manifest.snapshot_id, record.history_blob) in report.missing_blobs
    with pytest.raises(InvalidRepositoryError, match="missing"):
        repository.restore_snapshot(new.manifest.snapshot_id, tmp_path / "Never")
    with pytest.raises(InvalidRepositoryError):
        repository.delete_snapshot("does-not-exist")


def test_a_repository_belongs_to_one_catalogue(tmp_path):
    catalogue, *_ = build_catalogue(tmp_path)
    other = create_catalogue(tmp_path / "Other")
    other.create_volume("Other drive", str(tmp_path), {"drive_id": "AID-001"})
    other.close()
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    repository.create_snapshot(catalogue)

    with pytest.raises(SnapshotError, match="different catalogue"):
        repository.create_snapshot(tmp_path / "Other.jvvv")


def test_snapshot_can_be_cancelled_and_leaves_no_partial_manifest(tmp_path):
    catalogue, *_ = build_catalogue(tmp_path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    calls = []

    def cancel() -> bool:
        calls.append(True)
        return len(calls) > 1

    with pytest.raises(SnapshotCancelled):
        repository.create_snapshot(catalogue, cancel_callback=cancel)
    assert repository.snapshot_ids() == []
    assert not any(entry.name.startswith(".staging-") for entry in repository.blobs_dir.iterdir())


def test_blobs_are_named_by_the_digest_of_their_uncompressed_stream(tmp_path):
    catalogue, *_ = build_catalogue(tmp_path, files_a=3, files_b=1)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(catalogue)
    import hashlib

    for record in snapshot.manifest.volumes:
        path = repository.blob_path(record.content_blob)
        digest = hashlib.sha256(b"".join(iter_blob_bytes(path))).hexdigest()
        assert digest == record.content_blob
        first_line = b"".join(iter_blob_bytes(path)).split(b"\n", 1)[0]
        assert json.loads(first_line)["format"] == "jvvv-canonical-rows"


def test_open_rejects_folders_that_are_not_repositories(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "file.txt").write_text("x")
    with pytest.raises(InvalidRepositoryError):
        SnapshotRepository.open(tmp_path / "plain")
    with pytest.raises(InvalidRepositoryError):
        SnapshotRepository.create(tmp_path / "plain")
    created = SnapshotRepository.create(tmp_path / "fresh.jvvvrepo")
    assert created.exists() and created.snapshot_ids() == []
    assert SnapshotRepository.open_or_create(tmp_path / "fresh.jvvvrepo").path == created.path


def test_repository_link_follows_the_repository_when_it_moves(tmp_path):
    import shutil
    from jvvv.snapshot_repository import (
        LINK_LINKED,
        LINK_MISSING,
        LINK_UNLINKED,
        LINK_WRONG,
        inspect_relink_target,
        locate_repository_candidates,
        repository_link_settings,
        resolve_repository_link,
    )

    # Nested one level so the nearby search (parent and grandparent) stays inside
    # this test's temporary directory.
    work = tmp_path / "work"
    catalogue, first, second = build_catalogue(work, files_a=3, files_b=1)
    db = open_catalogue(catalogue)
    try:
        meta = db.catalogue_meta()
        # Nothing yet: the first snapshot would create the default sibling.
        link = resolve_repository_link(catalogue, meta)
        assert link.status == LINK_UNLINKED
        assert link.recorded_path == work / "Archive.jvvvrepo"

        repository = SnapshotRepository.create(link.recorded_path)
        repository.create_snapshot(catalogue, label="one")
        for key, value in repository_link_settings(catalogue, repository.path).items():
            db.set_catalogue_meta(key, value)
        meta = db.catalogue_meta()
        link = resolve_repository_link(catalogue, meta)
        assert link.status == LINK_LINKED and link.path == repository.path
        assert link.repository_uuid == repository.meta()["repository_uuid"]

        # Move the repository: the catalogue reports it offline at the last known location.
        moved = work / "Backups" / "Archive moved.jvvvrepo"
        moved.parent.mkdir()
        shutil.move(str(repository.path), str(moved))
        link = resolve_repository_link(catalogue, meta)
        assert link.status == LINK_MISSING
        assert link.recorded_path == work / "Archive.jvvvrepo"

        # A repository of another catalogue nearby is never offered or accepted.
        other = create_catalogue(work / "Other")
        other.create_volume("Other drive", str(tmp_path), {"drive_id": "AID-001"})
        other.close()
        other_repository = SnapshotRepository.create(work / "Other.jvvvrepo")
        other_repository.create_snapshot(work / "Other.jvvv")
        candidates = locate_repository_candidates(
            catalogue,
            repository_uuid=link.repository_uuid,
            catalogue_uuid=meta["catalogue_uuid"],
        )
        assert [candidate.path for candidate in candidates] == [moved]
        assert candidates[0].same_repository and candidates[0].snapshot_count == 1
        with pytest.raises(SnapshotError, match="different catalogue"):
            inspect_relink_target(
                other_repository.path,
                repository_uuid=link.repository_uuid,
                catalogue_uuid=meta["catalogue_uuid"],
            )
        with pytest.raises(InvalidRepositoryError):
            inspect_relink_target(work, repository_uuid="", catalogue_uuid=meta["catalogue_uuid"])

        # Relink: the settings now point at the moved folder and it is linked again.
        for key, value in repository_link_settings(catalogue, moved).items():
            db.set_catalogue_meta(key, value)
        link = resolve_repository_link(catalogue, db.catalogue_meta())
        assert link.status == LINK_LINKED and link.path == moved.resolve()

        # Another catalogue's repository dropped onto the recorded path is reported as wrong.
        shutil.move(str(other_repository.path), str(moved.parent / "x.jvvvrepo"))
        shutil.move(str(moved), str(work / "Archive.jvvvrepo"))
        shutil.move(str(moved.parent / "x.jvvvrepo"), str(moved))
        link = resolve_repository_link(catalogue, db.catalogue_meta())
        assert link.status == LINK_WRONG
        assert "different catalogue" in link.detail
    finally:
        db.close()


def test_restored_catalogue_is_linked_to_the_repository_it_came_from(tmp_path):
    from jvvv.snapshot_repository import LINK_LINKED, resolve_repository_link

    catalogue, *_ = build_catalogue(tmp_path, files_a=2, files_b=1)
    repository = SnapshotRepository.create(tmp_path / "Backups" / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(catalogue)
    result = repository.restore_snapshot(snapshot.manifest.snapshot_id, tmp_path / "Elsewhere" / "Restored")

    restored = open_catalogue(result.catalogue_path)
    try:
        link = resolve_repository_link(result.catalogue_path, restored.catalogue_meta())
        assert link.status == LINK_LINKED
        assert link.path == repository.path.resolve()
        assert link.repository_uuid == repository.meta()["repository_uuid"]
    finally:
        restored.close()
