"""LEGACY: converting a single-file schema-12 catalogue into a catalogue folder.

Delete this module together with the jvvv.legacy package once every catalogue
has been converted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jvvv.backup_analysis import BackupAnalysisEngine
from jvvv.database import (
    InvalidCatalogueError,
    LegacyCatalogueError,
    SCHEMA_VERSION,
    UnsupportedCatalogueError,
    count_rows,
    open_catalogue,
)
from jvvv.legacy import (
    inspect_legacy_catalogue,
    is_legacy_catalogue_file,
    legacy_archive_path,
    migrate_legacy_catalogue,
)
from jvvv.snapshot_repository import SnapshotRepository

from legacy_single_file import add_legacy_volume, create_legacy_catalogue

MODIFIED_AT = "2025-06-01T10:00:00.000000+0000"


def build_legacy(tmp_path: Path, *, wal: bool = True) -> tuple[Path, dict[str, int]]:
    path = tmp_path / "Archive.jvvv"
    connection = create_legacy_catalogue(path, wal=wal)
    ids = {}
    ids["first"] = add_legacy_volume(
        connection,
        name="First drive",
        drive_id="AID-001",
        files={
            "Projects/Alpha/report.pdf": (2000, MODIFIED_AT),
            "Projects/Alpha/notes.txt": (300, MODIFIED_AT),
            "Projects/Beta/clip.mp4": (900000, MODIFIED_AT),
            "readme.txt": (10, None),
        },
        scan_errors=2,
    )
    ids["second"] = add_legacy_volume(
        connection,
        name="Second drive",
        drive_id="AID-002",
        files={
            "Projects/Alpha/report.pdf": (2000, MODIFIED_AT),
            "Projects/Alpha/notes.txt": (300, MODIFIED_AT),
            "Other/photo.jpg": (5000, MODIFIED_AT),
        },
    )
    connection.close()
    return path, ids


def test_fixture_really_is_a_schema_12_single_file_catalogue(tmp_path):
    path, _ = build_legacy(tmp_path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        columns = {row[1] for row in connection.execute("PRAGMA table_info(volumes)")}
        assert "uuid" not in columns
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "index_state" not in tables
    finally:
        connection.close()
    assert is_legacy_catalogue_file(path)
    info = inspect_legacy_catalogue(path)
    assert (info.volume_count, info.file_count, info.folder_count) == (2, 7, 8)
    assert info.wal_mode


def test_opening_a_legacy_file_reports_it_as_legacy(tmp_path):
    path, _ = build_legacy(tmp_path)
    with pytest.raises(LegacyCatalogueError) as error_info:
        open_catalogue(path)
    assert error_info.value.legacy_path == path


def test_migration_produces_a_verified_catalogue_folder_and_keeps_the_original(tmp_path):
    path, ids = build_legacy(tmp_path)
    # Populate Backup Evidence in the legacy file so the migration must carry it over.
    legacy = sqlite3.connect(path)
    legacy.close()
    from jvvv.database import Database  # noqa: F401  (import kept local: not used for legacy files)

    progress = []
    result = migrate_legacy_catalogue(path, progress_callback=progress.append)

    assert result.catalogue_path == path
    assert result.legacy_path == legacy_archive_path(path)
    assert result.legacy_path.is_file()
    assert path.is_dir()
    assert (result.volume_count, result.file_count, result.folder_count) == (2, 7, 8)
    phases = [item.phase for item in progress]
    for phase in ("validate", "copy", "upgrade", "registry", "export", "verify", "finalize"):
        assert phase in phases
    assert not Path(f"{path}-wal").exists() and not Path(f"{path}-shm").exists()
    assert not any(entry.name.startswith("Archive.jvvv.migrating") for entry in tmp_path.iterdir())

    db = open_catalogue(path)
    try:
        assert not db.last_reconcile.changed
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        volumes = {row["id"]: row for row in db.list_volumes()}
        # Integer ids, names and register data are preserved.
        assert volumes[ids["first"]]["name"] == "First drive"
        assert volumes[ids["first"]]["drive_id"] == "AID-001"
        assert volumes[ids["second"]]["drive_id"] == "AID-002"
        assert all(len(row["uuid"]) == 36 for row in volumes.values())
        assert len({row["uuid"] for row in volumes.values()}) == 2
        assert count_rows(db, "files") == 7
        assert count_rows(db, "folders") == 8
        assert count_rows(db, "scan_errors") == 2
        assert count_rows(db, "scan_history") == 2
        assert [row["name"] for row in db.search("report")] == ["report.pdf", "report.pdf"]
        assert [entry.volume_id for entry in db.registry_entries()] == sorted(ids.values())
        for volume_id in ids.values():
            db.verify_volume_file(volume_id)
        assert len(list(db.layout.iter_volume_files())) == 2
    finally:
        db.close()

    # The archived original is untouched apart from its folded WAL.
    connection = sqlite3.connect(result.legacy_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 7
    finally:
        connection.close()


def test_migration_preserves_backup_evidence_results(tmp_path):
    path, ids = build_legacy(tmp_path)
    # Run the evidence engine against the legacy file through a plain connection wrapper.
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row

    class LegacyDatabase:
        def __init__(self, conn):
            self.connection = conn

        def transaction(self, *, immediate: bool = False):
            from contextlib import contextmanager

            @contextmanager
            def manager():
                if self.connection.in_transaction:
                    yield self.connection
                    return
                self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                try:
                    yield self.connection
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
                    raise

            return manager()

    summary = BackupAnalysisEngine(LegacyDatabase(connection)).analyse()
    assert summary.status == "completed"
    connection.close()

    migrate_legacy_catalogue(path)

    db = open_catalogue(path)
    try:
        engine = BackupAnalysisEngine(db)
        state = engine.state()
        assert state.status == "completed"
        assert not state.is_stale
        report = engine.file_status(
            db.connection.execute(
                "SELECT id FROM files WHERE volume_id = ? AND name = 'report.pdf'", (ids["first"],)
            ).fetchone()[0]
        )
        assert report.status == "likely"
    finally:
        db.close()


def test_migrated_catalogue_can_be_snapshotted_and_restored(tmp_path):
    path, ids = build_legacy(tmp_path)
    migrate_legacy_catalogue(path)
    repository = SnapshotRepository.create(tmp_path / "Archive.jvvvrepo")
    snapshot = repository.create_snapshot(path, label="after conversion")
    assert snapshot.volumes_written == 2
    restored = repository.restore_snapshot(snapshot.manifest.snapshot_id, tmp_path / "Copy")
    db = open_catalogue(restored.catalogue_path)
    try:
        assert count_rows(db, "files") == 7
        assert {row["drive_id"] for row in db.list_volumes()} == {"AID-001", "AID-002"}
    finally:
        db.close()


def test_migration_refuses_other_schema_versions_and_leaves_them_alone(tmp_path):
    path = tmp_path / "old.jvvv"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("PRAGMA user_version = 11")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(UnsupportedCatalogueError, match="schema version 11"):
        migrate_legacy_catalogue(path)
    assert path.read_bytes() == before
    assert not legacy_archive_path(path).exists()

    not_sqlite = tmp_path / "text.jvvv"
    not_sqlite.write_text("hello")
    with pytest.raises(InvalidCatalogueError):
        migrate_legacy_catalogue(not_sqlite)


def test_migration_refuses_to_overwrite_an_existing_archive_copy(tmp_path):
    path, _ = build_legacy(tmp_path)
    legacy_archive_path(path).write_bytes(b"older archive")
    with pytest.raises(FileExistsError):
        migrate_legacy_catalogue(path)
    assert path.is_file()


def test_migration_of_a_rollback_journal_file_works_too(tmp_path):
    path, ids = build_legacy(tmp_path, wal=False)
    result = migrate_legacy_catalogue(path)
    assert result.volume_count == 2
    db = open_catalogue(path)
    try:
        assert count_rows(db, "files") == 7
    finally:
        db.close()
