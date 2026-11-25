"""The catalogue folder format: registry + volume files (truth) and the rebuildable index."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from jvvv import volume_store
from jvvv.backup_analysis import BackupAnalysisEngine
from jvvv.catalogue_format import (
    CATALOGUE_DB_NAME,
    INDEX_DB_NAME,
    VOLUMES_DIR_NAME,
    CatalogueLayout,
    open_registry_db,
    read_registry,
    read_volume_meta_from_path,
    registry_meta,
)
from jvvv.database import (
    Database,
    InvalidCatalogueError,
    count_rows,
    create_catalogue,
    locate_catalogue,
    open_catalogue,
)
from jvvv.scanner import VolumeScanner

SCANNED_AT = "2026-01-01T00:00:00.000000+0000"


def populate(db: Database, volume_id: int, prefix: str, count: int, *, errors: int = 1) -> None:
    scan_id = db.start_scan(volume_id)
    with db.transaction():
        root = db.ensure_folder(volume_id, None, "root", "", SCANNED_AT)
        photos = db.ensure_folder(volume_id, root, "Photos", "Photos", SCANNED_AT)
        nested = db.ensure_folder(volume_id, photos, "2026", "Photos/2026", SCANNED_AT)
        for index in range(count):
            folder = nested if index % 2 else photos
            relative = f"{'Photos/2026' if index % 2 else 'Photos'}/{prefix}{index:04d}.jpg"
            file_id = db.upsert_file(
                volume_id,
                folder,
                f"{prefix}{index:04d}.jpg",
                relative,
                "jpg",
                1000 + index,
                SCANNED_AT,
                SCANNED_AT,
                content_hash=bytes([index % 256]) * 32,
                content_hash_algorithm="sha256",
            )
            if index % 5 == 0:
                db.replace_file_media_metadata(
                    file_id,
                    {"status": "complete", "media_kind": "image", "width": 640, "height": 480},
                )
            if index % 9 == 0:
                db.replace_file_preview_status(
                    file_id,
                    {
                        "media_kind": "image",
                        "profile_id": "jpeg-max1600-q82",
                        "status": "available",
                        "source_hash": bytes([index % 256]) * 32,
                        "preview_size": 123,
                    },
                )
        for index in range(errors):
            db.add_scan_error(scan_id, volume_id, f"Photos/locked-{index}", "Access denied")
        db.finalize_scan_items(volume_id, SCANNED_AT)
        db.rebuild_folder_statistics(volume_id, stats_updated_at=SCANNED_AT)
        db.refresh_volume_counts(volume_id, SCANNED_AT)
        db.update_volume_content_dates_from_index(volume_id)
    db.finish_scan(scan_id, "completed", count, 3, errors)


def volume_counts(db: Database, volume_id: int) -> tuple[int, ...]:
    connection = db.connection
    return (
        connection.execute("SELECT COUNT(*) FROM folders WHERE volume_id = ?", (volume_id,)).fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM files WHERE volume_id = ?", (volume_id,)).fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM file_media_metadata m JOIN files f ON f.id = m.file_id WHERE f.volume_id = ?",
            (volume_id,),
        ).fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM file_preview_status p JOIN files f ON f.id = p.file_id WHERE f.volume_id = ?",
            (volume_id,),
        ).fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM scan_history WHERE volume_id = ?", (volume_id,)).fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM scan_errors WHERE volume_id = ?", (volume_id,)).fetchone()[0],
    )


def test_create_catalogue_builds_the_folder_layout(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    try:
        root = tmp_path / "Archive.jvvv"
        assert db.path == root
        assert sorted(entry.name for entry in root.iterdir() if not entry.name.startswith(INDEX_DB_NAME + "-")) == [
            CATALOGUE_DB_NAME,
            INDEX_DB_NAME,
            VOLUMES_DIR_NAME,
        ]
        registry = open_registry_db(root / CATALOGUE_DB_NAME, read_only=True)
        try:
            meta = registry_meta(registry)
            assert meta["format"] == "jvvv-catalogue-directory"
            assert read_registry(registry) == []
        finally:
            registry.close()
        assert db.catalogue_uuid() == meta["catalogue_uuid"]
    finally:
        db.close()


def test_locate_catalogue_accepts_the_folder_or_a_file_inside_it(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    db.close()
    root = tmp_path / "Archive.jvvv"
    assert locate_catalogue(root) == root
    assert locate_catalogue(root / CATALOGUE_DB_NAME) == root
    assert locate_catalogue(root / INDEX_DB_NAME) == root
    assert locate_catalogue(tmp_path / "Archive") == root


def test_every_write_lands_in_the_volume_file_before_the_index_commits(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    try:
        volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
        path = db.volume_file_path(volume_id)
        assert path.is_file()
        assert path.parent == db.layout.volumes_dir
        meta = read_volume_meta_from_path(path)
        assert meta.revision == 1
        assert meta.uuid == db.get_volume(volume_id)["uuid"]

        populate(db, volume_id, "a", 40)
        meta = read_volume_meta_from_path(path)
        state = db.index_state(volume_id)
        assert state["revision"] == meta.revision
        assert state["content_digest"] == meta.content_digest
        assert state["history_digest"] == meta.history_digest
        # The digests describe the file's own content and can be recomputed from it.
        assert volume_store.volume_file_digests(path) == (meta.content_digest, meta.history_digest)
        db.verify_volume_file(volume_id)

        connection = sqlite3.connect(path)
        try:
            row = connection.execute("SELECT name, drive_id, indexed_file_count FROM volume").fetchone()
            assert row == ("Drive A", "AID-001", 40)
            assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 40
            assert connection.execute("SELECT COUNT(*) FROM scan_errors").fetchone()[0] == 1
            # Local ids are dense and ordered by path.
            assert [r[0] for r in connection.execute("SELECT id FROM folders ORDER BY id")] == [1, 2, 3]
            assert connection.execute("SELECT relative_path FROM folders WHERE id = 1").fetchone()[0] == ""
        finally:
            connection.close()
    finally:
        db.close()


def test_metadata_edits_bump_the_revision_but_keep_the_content_digest(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    try:
        volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
        populate(db, volume_id, "a", 10)
        path = db.volume_file_path(volume_id)
        before = read_volume_meta_from_path(path)

        db.update_volume(volume_id, "Drive A (renamed)", str(tmp_path), {"drive_id": "AID-009"})
        after = read_volume_meta_from_path(path)
        assert after.revision == before.revision + 1
        assert after.content_digest == before.content_digest
        assert after.history_digest == before.history_digest
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("SELECT name, drive_id FROM volume").fetchone() == (
                "Drive A (renamed)",
                "AID-009",
            )
        finally:
            connection.close()

        scan_id = db.start_scan(volume_id)
        db.finish_scan(scan_id, "failed", 0, 0, 0, "Not connected")
        history = read_volume_meta_from_path(path)
        assert history.content_digest == after.content_digest
        assert history.history_digest != after.history_digest
        db.verify_volume_file(volume_id)
    finally:
        db.close()


def test_reopening_a_clean_catalogue_imports_nothing(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 12)
    db.close()

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        assert db.last_reconcile is not None
        assert not db.last_reconcile.changed
    finally:
        db.close()


def test_a_deleted_index_is_rebuilt_from_the_volume_files(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    first = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    second = db.create_volume("Drive B", str(tmp_path), {"drive_id": "AID-002"})
    populate(db, first, "a", 33, errors=2)
    populate(db, second, "b", 7)
    expected_first = volume_counts(db, first)
    expected_second = volume_counts(db, second)
    BackupAnalysisEngine(db).analyse()
    assert BackupAnalysisEngine(db).state().status == "completed"
    db.close()
    (tmp_path / "Archive.jvvv" / INDEX_DB_NAME).unlink()

    progress = []
    db = open_catalogue(tmp_path / "Archive.jvvv", progress_callback=lambda *args: progress.append(args))
    try:
        report = db.last_reconcile
        assert report.rebuilt
        assert sorted(volume_id for volume_id, _ in report.imported) == [first, second]
        assert volume_counts(db, first) == expected_first
        assert volume_counts(db, second) == expected_second
        assert {row["name"] for row in db.list_volumes()} == {"Drive A", "Drive B"}
        assert db.get_volume(first)["drive_id"] == "AID-001"
        assert db.get_volume(first)["indexed_file_count"] == 33
        assert [row["name"] for row in db.search("a0001")] == ["a0001.jpg"]
        # Evidence is derived from the index and is invalidated by a rebuild.
        assert BackupAnalysisEngine(db).state().status == "not_analyzed"
        assert any("Rebuilding" in message for *_, message in progress)
        for volume_id in (first, second):
            db.verify_volume_file(volume_id)
    finally:
        db.close()


def test_a_volume_file_changed_by_another_tool_is_reimported_on_open(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 5)
    path = db.volume_file_path(volume_id)
    db.close()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE volume SET name = 'Renamed elsewhere'")
    connection.execute("DELETE FROM files WHERE id = 1")
    connection.execute(
        "UPDATE volume_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision'"
    )
    connection.commit()
    connection.close()

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        assert [volume_id for volume_id, _ in db.last_reconcile.imported] == [volume_id]
        assert db.get_volume(volume_id)["name"] == "Renamed elsewhere"
        assert count_rows(db, "files") == 4
    finally:
        db.close()


def test_a_missing_registered_volume_file_refuses_to_open(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 3)
    path = db.volume_file_path(volume_id)
    db.close()
    path.unlink()

    with pytest.raises(InvalidCatalogueError, match="files are missing"):
        open_catalogue(tmp_path / "Archive.jvvv")


def test_unregistered_volume_files_and_temp_files_are_reported_and_cleaned(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 3)
    layout = db.layout
    db.close()
    orphan = layout.volumes_dir / "11111111-1111-4111-8111-111111111111.jvvvol"
    orphan.write_bytes(b"not really a volume")
    temp = layout.volumes_dir / "whatever.jvvvol.tmp-abcdef"
    temp.write_bytes(b"partial export")

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        report = db.last_reconcile
        assert report.orphan_files == [orphan]
        assert report.cleaned_temp_files == 1
        assert not temp.exists()
        assert orphan.exists()
        assert count_rows(db, "volumes") == 1
    finally:
        db.close()


def test_deleting_a_volume_removes_its_file_and_registry_entry(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    try:
        first = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
        second = db.create_volume("Drive B", str(tmp_path), {"drive_id": "AID-002"})
        populate(db, first, "a", 4)
        populate(db, second, "b", 4)
        first_path = db.volume_file_path(first)
        second_path = db.volume_file_path(second)

        db.delete_volume(first)

        assert not first_path.exists()
        assert not db.layout.volume_tombstone(first_path.stem).exists()
        assert second_path.exists()
        assert [entry.volume_id for entry in db.registry_entries()] == [second]
        assert db.index_state(first) is None
        assert count_rows(db, "files") == 4
        # Ids are never reused: the next volume continues the catalogue-wide sequence.
        third = db.create_volume("Drive C", str(tmp_path), {"drive_id": "AID-003"})
        assert third > second
    finally:
        db.close()


def test_an_interrupted_deletion_is_completed_on_the_next_open(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    first = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    second = db.create_volume("Drive B", str(tmp_path), {"drive_id": "AID-002"})
    populate(db, first, "a", 4)
    populate(db, second, "b", 4)
    first_path = db.volume_file_path(first)
    first_uuid = db.get_volume(first)["uuid"]
    db.close()
    # Simulate a crash after the tombstone rename but before the registry/index caught up.
    os.replace(first_path, CatalogueLayout(tmp_path / "Archive.jvvv").volume_tombstone(first_uuid))

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        assert db.last_reconcile.completed_deletions == [first_uuid]
        assert [row["name"] for row in db.list_volumes()] == ["Drive B"]
        assert not first_path.exists()
        assert not db.layout.volume_tombstone(first_uuid).exists()
        assert [entry.volume_id for entry in db.registry_entries()] == [second]
    finally:
        db.close()


def test_a_volume_file_newer_than_the_index_wins_after_a_crash_between_the_two_commits(tmp_path):
    """Truth is written first: if the index commit never happened, the file is re-imported."""

    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 6)
    path = db.volume_file_path(volume_id)
    index_path = db.index_path
    db.close()

    # Roll the *index* back to an older state of the volume while the file keeps the newer one.
    connection = sqlite3.connect(index_path)
    connection.execute("DELETE FROM files WHERE volume_id = ? AND name = 'a0000.jpg'", (volume_id,))
    connection.execute("UPDATE index_state SET revision = revision - 1 WHERE volume_id = ?", (volume_id,))
    connection.commit()
    connection.close()

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        assert [item for item, _ in db.last_reconcile.imported] == [volume_id]
        assert count_rows(db, "files") == 6
    finally:
        db.close()


def test_the_scanner_skips_the_catalogue_folder_inside_the_scanned_drive(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    (source / "user.bin").write_bytes(b"user data")
    (source / "Photos").mkdir()
    (source / "Photos" / "a.jpg").write_bytes(b"jpeg")
    db = Database(source / "catalogue.jvvv")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)
        assert result.status == "completed"
        names = sorted(row["relative_path"] for row in db.connection.execute("SELECT relative_path FROM files"))
        assert names == ["Photos/a.jpg", "user.bin"]
        folders = sorted(row["relative_path"] for row in db.connection.execute("SELECT relative_path FROM folders"))
        assert folders == ["", "Photos"]
    finally:
        db.close()


def test_read_only_connections_do_not_touch_truth_files(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 3)
    path = db.volume_file_path(volume_id)
    db.close()
    before = path.read_bytes()

    reader = Database(tmp_path / "Archive.jvvv", initialize=False, create=False, read_only=True)
    try:
        reader.validate_catalogue()
        assert count_rows(reader, "files") == 3
        with pytest.raises(Exception):
            reader.update_volume(volume_id, "x", "", None)
    finally:
        reader.close()
    assert path.read_bytes() == before


def test_export_volume_regenerates_a_lost_volume_file_from_the_index(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    try:
        volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
        populate(db, volume_id, "a", 8)
        path = db.volume_file_path(volume_id)
        original = read_volume_meta_from_path(path)
        path.unlink()

        result = db.export_volume(volume_id)

        assert path.is_file()
        assert result.meta.content_digest == original.content_digest
        assert result.meta.history_digest == original.history_digest
        assert result.counts.files == 8
        db.verify_volume_file(volume_id)
    finally:
        db.close()


def test_mirror_links_survive_an_index_rebuild_regardless_of_import_order(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    master = db.create_volume("Master", str(tmp_path), {"drive_id": "AID-001"})
    mirror = db.create_volume(
        "Mirror",
        str(tmp_path),
        {"drive_id": "AID-002", "is_mirror": True, "master_volume_id": master},
    )
    populate(db, master, "m", 3)
    populate(db, mirror, "r", 3)
    # Force the mirror to be imported before its master by giving it the lower id order.
    assert master < mirror
    db.close()
    (tmp_path / "Archive.jvvv" / INDEX_DB_NAME).unlink()

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        row = db.get_volume(mirror)
        assert row["is_mirror"] == 1
        assert row["master_volume_id"] == master
        assert row["master_name"] == "Master"
    finally:
        db.close()


def test_canonical_serialisation_is_deterministic_and_round_trips(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 20)
    path = db.volume_file_path(volume_id)
    db.close()

    first = volume_store.volume_file_digests(path)
    second = volume_store.volume_file_digests(path)
    assert first == second

    connection = volume_store.open_volume_file(path, read_only=True)
    try:
        chunks = list(volume_store.iter_canonical_bytes(connection, volume_store.CONTENT_TABLES))
        tables = list(volume_store.read_canonical_tables(chunks, volume_store.CONTENT_TABLES))
    finally:
        connection.close()
    names = [name for name, _, _ in tables]
    assert names == ["folders", "files", "file_media_metadata", "file_preview_status"]
    files = next(rows for name, _, rows in tables if name == "files")
    assert len(files) == 20
    hashes = [row[list(volume_store.FILE_COLUMNS).index("content_hash")] for row in files]
    assert all(isinstance(value, bytes) and len(value) == 32 for value in hashes)


def test_canonical_reader_is_independent_of_chunk_boundaries_and_stays_linear(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    volume_id = db.create_volume("Drive A", str(tmp_path), {"drive_id": "AID-001"})
    populate(db, volume_id, "a", 400)
    path = db.volume_file_path(volume_id)
    db.close()

    connection = volume_store.open_volume_file(path, read_only=True)
    try:
        stream = b"".join(volume_store.iter_canonical_bytes(connection, volume_store.CONTENT_TABLES))
    finally:
        connection.close()
    expected = list(volume_store.read_canonical_tables([stream], volume_store.CONTENT_TABLES))

    def rechunk(data: bytes, size: int) -> list[bytes]:
        return [data[index : index + size] for index in range(0, len(data), size)]

    for size in (1, 7, 64, 1000, len(stream) // 3 or 1):
        assert list(volume_store.read_canonical_tables(rechunk(stream, size), volume_store.CONTENT_TABLES)) == expected

    # One large chunk with tens of thousands of lines must split without quadratic copying.
    import time

    lines = b"".join(b'["%d","x"]\n' % index for index in range(200_000))
    started = time.perf_counter()
    assert sum(1 for _ in volume_store.iter_lines([lines])) == 200_000
    assert time.perf_counter() - started < 2.0
