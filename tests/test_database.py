from __future__ import annotations

from datetime import date
import sqlite3

import pytest

from jvvv import database as database_module
from jvvv.database import (
    Database,
    InvalidCatalogueError,
    SCHEMA_VERSION,
    UnsupportedCatalogueError,
    count_rows,
    create_catalogue,
    open_catalogue,
    sqlite_file_uri,
)


def test_format_timestamp_returns_none_for_unrepresentable_os_timestamp(monkeypatch):
    class UnrepresentableDateTime:
        @staticmethod
        def fromtimestamp(value, tz):
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(database_module, "datetime", UnrepresentableDateTime)

    assert database_module.format_timestamp(123) is None


def test_database_initializes_schema(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        tables = {
            row["name"]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "volumes",
            "folders",
            "files",
            "file_media_metadata",
            "scan_history",
            "scan_errors",
        } <= tables
        assert "volume_register" in tables
        assert {
            "backup_analysis_runs",
            "backup_analysis_state",
            "backup_file_results",
            "backup_folder_results",
            "backup_volume_results",
            "backup_mirror_candidates",
        } <= tables
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert "file_preview_status" in tables
        assert {"files_fts", "folders_fts"} <= tables
        folder_indexes = {
            row["name"]
            for row in db.connection.execute("PRAGMA index_list(folders)")
        }
        file_indexes = {
            row["name"]
            for row in db.connection.execute("PRAGMA index_list(files)")
        }
        scan_history_indexes = {
            row["name"]
            for row in db.connection.execute("PRAGMA index_list(scan_history)")
        }
        scan_error_indexes = {
            row["name"]
            for row in db.connection.execute("PRAGMA index_list(scan_errors)")
        }
        assert "idx_folders_parent" in folder_indexes
        assert "idx_files_folder" in file_indexes
        assert "idx_scan_history_volume" in scan_history_indexes
        assert "idx_scan_errors_volume" in scan_error_indexes
        scan_history_columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(scan_history)")
        }
        assert {
            "files_added",
            "files_removed",
            "files_changed",
            "folders_added",
            "folders_removed",
            "bytes_before",
            "bytes_after",
            "files_hashed",
            "bytes_hashed",
            "hash_errors",
            "media_files",
            "media_metadata_collected",
        } <= scan_history_columns
        file_columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(files)")
        }
        assert {"content_hash", "content_hash_algorithm"} <= file_columns
        media_columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(file_media_metadata)")
        }
        assert {
            "file_id",
            "status",
            "media_kind",
            "source",
            "container",
            "duration_ms",
            "width",
            "height",
            "video_codecs",
            "audio_codecs",
            "sample_rate_hz",
            "channels",
            "bit_rate",
            "message",
            "probed_at",
        } <= media_columns
        volume_columns = {
            row["name"]: row
            for row in db.connection.execute("PRAGMA table_info(volumes)")
        }
        assert volume_columns["name"]["notnull"] == 0
        assert {
            "identity_kind",
            "identity_token",
            "identity_label",
            "identity_serial",
            "identity_filesystem",
            "source_relative_path",
        } <= set(volume_columns)
        folder_columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(folders)")
        }
        assert {
            "recursive_size_bytes",
            "recursive_file_count",
            "recursive_subfolder_count",
            "direct_file_count",
            "direct_subfolder_count",
            "stats_updated_at",
        } <= folder_columns
        register_columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(volume_register)")
        }
        assert {
            "drive_id",
            "status",
            "condition",
            "description",
            "connector",
            "date_added",
            "master_volume_id",
        } <= register_columns
    finally:
        db.close()
def test_finishing_scans_keeps_only_compact_recent_history(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        for _ in range(105):
            scan_id = db.start_scan(volume_id)
            db.finish_scan(scan_id, "completed", 10, 2, 0)

        history = db.list_scan_history(volume_id, limit=200)
        assert len(history) == 100
        assert all(row["files_seen"] == 10 for row in history)
    finally:
        db.close()


def test_sqlite_uri_encodes_unc_server_as_part_of_path():
    class ResolvedUncPath:
        drive = r"\\192.168.1.100\archive"

        @staticmethod
        def as_uri():
            return "file://192.168.1.100/archive/Archive%20One.jvvv"

    class UncPath:
        @staticmethod
        def resolve(*, strict):
            assert strict is False
            return ResolvedUncPath()

    assert sqlite_file_uri(UncPath()) == (
        "file:////192.168.1.100/archive/Archive%20One.jvvv?mode=rw"
    )
    assert Database._uses_network_storage(UncPath()) is True


def test_read_only_connection_enables_query_only_mode(tmp_path):
    path = tmp_path / "catalogue.jvvv"
    created = Database(path)
    created.close()

    reader = Database(
        path,
        initialize=False,
        create=False,
        read_only=True,
    )
    try:
        assert reader.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.connection.execute("DELETE FROM volumes")
    finally:
        reader.close()


def test_network_storage_recognizes_mapped_windows_drive(monkeypatch):
    class ResolvedMappedPath:
        drive = "Z:"

    class MappedPath:
        @staticmethod
        def resolve(*, strict):
            assert strict is False
            return ResolvedMappedPath()

    checked_drives = []
    monkeypatch.setattr(
        database_module,
        "_windows_drive_is_remote",
        lambda drive: checked_drives.append(drive) or True,
    )

    assert Database._uses_network_storage(MappedPath()) is True
    assert checked_drives == ["Z:"]


def test_network_catalogue_preserves_existing_rollback_journal(monkeypatch, tmp_path):
    path = tmp_path / "network-catalogue.jvvv"
    created = Database(path)
    created.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    connection.commit()
    connection.close()
    assert Database._database_header_journal_mode(path) == "Rollback"

    monkeypatch.setattr(
        Database,
        "_uses_network_storage",
        staticmethod(lambda path: True),
    )

    db = Database(path, create=False)
    try:
        journal_mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "delete"
        locking_mode = db.connection.execute("PRAGMA locking_mode").fetchone()[0]
        assert locking_mode.lower() == "normal"
    finally:
        db.close()


def test_network_catalogue_rejects_wal_before_sqlite_access(monkeypatch, tmp_path):
    path = tmp_path / "wal-catalogue.jvvv"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL").fetchone()
    connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    assert Database._database_header_journal_mode(path) == "WAL"
    monkeypatch.setattr(
        Database,
        "_uses_network_storage",
        staticmethod(lambda path: True),
    )

    with pytest.raises(database_module.CatalogueError, match="still in WAL mode"):
        Database(path, initialize=False, create=False)


def test_network_catalogue_open_skips_full_integrity_scan(monkeypatch, tmp_path):
    path = tmp_path / "network-catalogue.jvvv"
    db = Database(path)
    db.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    connection.close()

    statements = []
    sqlite_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        traced_connection = sqlite_connect(*args, **kwargs)
        traced_connection.set_trace_callback(statements.append)
        return traced_connection

    monkeypatch.setattr(
        Database,
        "_uses_network_storage",
        staticmethod(lambda path: True),
    )
    monkeypatch.setattr(database_module.sqlite3, "connect", traced_connect)

    db = open_catalogue(path)
    db.close()

    assert not any("quick_check" in statement.lower() for statement in statements)


def test_large_local_catalogue_open_skips_full_integrity_scan(monkeypatch, tmp_path):
    path = tmp_path / "large-catalogue.jvvv"
    db = Database(path)
    db.close()

    statements = []
    sqlite_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        traced_connection = sqlite_connect(*args, **kwargs)
        traced_connection.set_trace_callback(statements.append)
        return traced_connection

    monkeypatch.setattr(database_module, "AUTOMATIC_INTEGRITY_CHECK_MAX_BYTES", 1)
    monkeypatch.setattr(database_module.sqlite3, "connect", traced_connect)

    db = open_catalogue(path)
    db.close()

    assert not any("quick_check" in statement.lower() for statement in statements)


def test_sqlite_failure_reports_connection_stage_and_error_code(monkeypatch, tmp_path):
    class FailingConnection:
        row_factory = None
        closed = False
        statements = []

        def execute(self, statement):
            self.statements.append(statement)
            if statement == "PRAGMA synchronous = NORMAL":
                error = sqlite3.OperationalError("disk I/O error")
                error.sqlite_errorname = "unknown"
                error.sqlite_errorcode = 8714
                raise error
            return self

        @staticmethod
        def fetchone():
            return (None,)

        def close(self):
            self.closed = True

    path = tmp_path / "network-catalogue.jvvv"
    path.touch()
    connection = FailingConnection()
    monkeypatch.setattr(
        Database,
        "_uses_network_storage",
        staticmethod(lambda path: True),
    )
    monkeypatch.setattr(database_module.sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(database_module.CatalogueError) as error_info:
        Database(path, initialize=False, create=False)

    details = error_info.value.diagnostic_details
    assert "Operation: setting SQLite synchronous mode to NORMAL" in details
    assert "Network storage detected: Yes" in details
    assert "Journal mode in file header: Unknown" in details
    assert "Requested journal mode: Preserve rollback mode" in details
    assert "SQLite error name: SQLITE_IOERR_IN_PAGE" in details
    assert "SQLite error code: 8714" in details
    assert connection.statements == [
        "PRAGMA foreign_keys = ON",
        "PRAGMA busy_timeout = 2000",
        "PRAGMA synchronous = NORMAL",
    ]
    assert connection.closed is True


def test_reads_remain_available_during_writer_transaction(tmp_path):
    path = tmp_path / "catalogue.sqlite3"
    scanned_at = "2026-06-25T12:00:00.000000+0000"
    writer = Database(path)
    reader: Database | None = None
    try:
        assert writer.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        volume_id = writer.create_volume("Archive", str(tmp_path))
        with writer.transaction():
            root_id = writer.ensure_folder(
                volume_id=volume_id,
                parent_id=None,
                name="Archive",
                relative_path="",
                scanned_at=scanned_at,
            )
            writer.ensure_folder(
                volume_id=volume_id,
                parent_id=root_id,
                name="Existing",
                relative_path="Existing",
                scanned_at=scanned_at,
            )

        reader = Database(path, initialize=False, create=False, busy_timeout_ms=50)
        writer.connection.execute("BEGIN EXCLUSIVE")
        try:
            writer.ensure_folder(
                volume_id=volume_id,
                parent_id=root_id,
                name="Scanning",
                relative_path="Scanning",
                scanned_at="2026-06-25T13:00:00.000000+0000",
            )

            children = reader.list_child_folders(volume_id, root_id)
        finally:
            writer.connection.rollback()

        assert [row["name"] for row in children] == ["Existing"]
    finally:
        if reader is not None:
            reader.close()
        writer.close()


def test_create_catalogue_appends_extension_and_reopens(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    try:
        assert db.path == tmp_path / "Archive.jvvv"
        assert db.path.exists()
        db.create_volume("Archive", str(tmp_path))
    finally:
        db.close()

    db = open_catalogue(tmp_path / "Archive.jvvv")
    try:
        assert count_rows(db, "volumes") == 1
    finally:
        db.close()


def test_open_catalogue_rejects_invalid_file(tmp_path):
    path = tmp_path / "broken.jvvv"
    path.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(InvalidCatalogueError):
        open_catalogue(path)


def test_volume_crud(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        volume = db.get_volume(volume_id)
        assert volume is not None
        assert volume["name"] == "Archive"
        assert volume["source_path"] == str(tmp_path)
        assert volume["identity_kind"] == ""
        assert volume["identity_token"] == ""
        assert volume["drive_id"] == "AID-001"
        assert volume["register_status"] == "Archive"
        assert volume["date_added"] == date.today().isoformat()

        db.update_volume(
            volume_id,
            "Renamed",
            str(tmp_path / "other"),
            {"drive_id": "AID-042", "status": "Maintenance", "condition": "Good"},
        )
        volume = db.get_volume(volume_id)
        assert volume["name"] == "Renamed"
        assert volume["source_path"] == str(tmp_path / "other")
        assert volume["drive_id"] == "AID-042"
        assert volume["register_status"] == "Maintenance"
        assert volume["condition"] == "Good"

        db.delete_volume(volume_id)
        assert db.get_volume(volume_id) is None
        assert count_rows(db, "volumes") == 0
        assert count_rows(db, "volume_register") == 0
    finally:
        db.close()


def test_delete_volume_removes_indexed_records_for_only_that_volume(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        scanned_at = "2026-06-25T12:00:00.000000+0000"
        deleted_volume_id = db.create_volume("Delete Me", str(tmp_path / "deleted"))
        kept_volume_id = db.create_volume("Keep Me", str(tmp_path / "kept"))

        for volume_id, name in [(deleted_volume_id, "deleted"), (kept_volume_id, "kept")]:
            with db.transaction() as conn:
                root_id = db.ensure_folder(
                    volume_id=volume_id,
                    parent_id=None,
                    name=name,
                    relative_path="",
                    scanned_at=scanned_at,
                )
                child_id = db.ensure_folder(
                    volume_id=volume_id,
                    parent_id=root_id,
                    name="child",
                    relative_path="child",
                    scanned_at=scanned_at,
                )
                db.upsert_file(
                    volume_id=volume_id,
                    folder_id=child_id,
                    name="file.txt",
                    relative_path="child/file.txt",
                    extension="txt",
                    size_bytes=123,
                    modified_at=None,
                    scanned_at=scanned_at,
                )
                scan_id = conn.execute(
                    """
                    INSERT INTO scan_history (volume_id, started_at, status)
                    VALUES (?, ?, 'completed')
                    """,
                    (volume_id, scanned_at),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO scan_errors (scan_id, volume_id, path, message, created_at)
                    VALUES (?, ?, 'child/file.txt', 'problem', ?)
                    """,
                    (scan_id, volume_id, scanned_at),
                )

        db.delete_volume(deleted_volume_id)

        assert db.get_volume(deleted_volume_id) is None
        assert db.get_volume(kept_volume_id) is not None
        for table in ("folders", "files", "scan_history", "scan_errors"):
            assert db.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE volume_id = ?",
                (deleted_volume_id,),
            ).fetchone()[0] == 0
            assert db.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE volume_id = ?",
                (kept_volume_id,),
            ).fetchone()[0] > 0
    finally:
        db.close()


def test_catalogue_info_summarizes_storage_and_index_counts(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        scanned_at = "2026-06-25T12:00:00.000000+0000"
        later_scan = "2026-06-25T13:00:00.000000+0000"
        first_volume_id = db.create_volume("First", str(tmp_path / "first"))
        second_volume_id = db.create_volume("Second", str(tmp_path / "second"))

        db.update_volume_storage(first_volume_id, 1000, 600, 400, scanned_at)
        db.update_volume_storage(second_volume_id, 2000, 800, 1200, later_scan)

        first_root_id = db.ensure_folder(
            volume_id=first_volume_id,
            parent_id=None,
            name="First",
            relative_path="",
            scanned_at=scanned_at,
        )
        child_id = db.ensure_folder(
            volume_id=first_volume_id,
            parent_id=first_root_id,
            name="child",
            relative_path="child",
            scanned_at=scanned_at,
        )
        old_folder_id = db.ensure_folder(
            volume_id=first_volume_id,
            parent_id=first_root_id,
            name="old",
            relative_path="old",
            scanned_at=scanned_at,
        )
        db.ensure_folder(
            volume_id=second_volume_id,
            parent_id=None,
            name="Second",
            relative_path="",
            scanned_at=later_scan,
        )

        db.upsert_file(
            volume_id=first_volume_id,
            folder_id=child_id,
            name="one.bin",
            relative_path="child/one.bin",
            extension="bin",
            size_bytes=100,
            modified_at=None,
            scanned_at=scanned_at,
        )
        db.upsert_file(
            volume_id=first_volume_id,
            folder_id=child_id,
            name="two.bin",
            relative_path="child/two.bin",
            extension="bin",
            size_bytes=150,
            modified_at=None,
            scanned_at=scanned_at,
        )
        missing_file_id = db.upsert_file(
            volume_id=first_volume_id,
            folder_id=old_folder_id,
            name="gone.bin",
            relative_path="old/gone.bin",
            extension="bin",
            size_bytes=25,
            modified_at=None,
            scanned_at=scanned_at,
        )
        db.connection.execute("UPDATE folders SET missing = 1 WHERE id = ?", (old_folder_id,))
        db.connection.execute("UPDATE files SET missing = 1 WHERE id = ?", (missing_file_id,))

        scan_id = db.start_scan(first_volume_id)
        db.finish_scan(scan_id, "completed", 2, 3, 0)

        info = db.get_catalogue_info()

        assert info["volume_count"] == 2
        assert info["total_capacity_bytes"] == 3000
        assert info["total_used_bytes"] == 1400
        assert info["total_free_bytes"] == 1600
        assert info["indexed_size_bytes"] == 250
        assert info["file_count"] == 2
        assert info["folder_count"] == 3
        assert info["missing_file_count"] == 1
        assert info["missing_folder_count"] == 1
        assert info["scan_count"] == 1
        assert info["latest_scan_at"] == later_scan
    finally:
        db.close()


def test_volume_location_identity_can_be_updated_and_cleared(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        db.update_volume_location(
            volume_id,
            str(tmp_path / "Drive"),
            {
                "identity_kind": "windows-volume-guid",
                "identity_token": "\\\\?\\volume{abc}\\",
                "identity_label": "Archive",
                "identity_serial": "1234ABCD",
                "identity_filesystem": "NTFS",
                "source_relative_path": "Archive/Subfolder",
            },
        )

        volume = db.get_volume(volume_id)
        assert volume["source_path"] == str(tmp_path / "Drive")
        assert volume["identity_kind"] == "windows-volume-guid"
        assert volume["identity_token"] == "\\\\?\\volume{abc}\\"
        assert volume["identity_label"] == "Archive"
        assert volume["identity_serial"] == "1234ABCD"
        assert volume["identity_filesystem"] == "NTFS"
        assert volume["source_relative_path"] == "Archive/Subfolder"

        db.update_volume_location(volume_id, str(tmp_path / "Other"), None)
        volume = db.get_volume(volume_id)
        assert volume["source_path"] == str(tmp_path / "Other")
        assert volume["identity_kind"] == ""
        assert volume["identity_token"] == ""
    finally:
        db.close()


def test_duplicate_volume_names_are_rejected(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        db.create_volume("Archive", str(tmp_path))
        with pytest.raises(sqlite3.IntegrityError):
            db.create_volume("archive", str(tmp_path))
    finally:
        db.close()


def test_volume_name_is_optional_and_drive_id_allows_custom_text(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        first_id = db.create_volume("", str(tmp_path), {"drive_id": "Shelf B / Client Archive"})
        second_id = db.create_volume("", str(tmp_path), {"drive_id": "2026-offsite-copy"})

        first = db.get_volume(first_id)
        second = db.get_volume(second_id)
        assert first["name"] is None
        assert first["drive_id"] == "Shelf B / Client Archive"
        assert second["name"] is None
        assert second["drive_id"] == "2026-offsite-copy"
    finally:
        db.close()


def test_next_drive_id_uses_highest_existing_aid_sequence(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        db.create_volume("First", str(tmp_path), {"drive_id": "AID-001"})
        db.create_volume("Custom", str(tmp_path), {"drive_id": "Shelf B"})
        db.create_volume("Large", str(tmp_path), {"drive_id": "AID-1250"})
        assert db.next_drive_id() == "AID-1251"

        volume_id = db.create_volume("Next", str(tmp_path))
        volume = db.get_volume(volume_id)
        assert volume["drive_id"] == "AID-1251"
    finally:
        db.close()


def test_mirror_relationships_are_validated_and_block_master_deletion(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        master_id = db.create_volume("Master", str(tmp_path), {"drive_id": "AID-001"})
        mirror_id = db.create_volume(
            "Mirror",
            str(tmp_path),
                {
                    "drive_id": "AID-002",
                    "is_mirror": True,
                    "master_volume_id": master_id,
                    "date_added": "2026-06-01",
                    "mirror_date": "2026-06-25",
                },
            )

        mirror = db.get_volume(mirror_id)
        assert mirror["is_mirror"] == 1
        assert mirror["master_volume_id"] == master_id
        assert mirror["master_drive_id"] == "AID-001"

        with pytest.raises(ValueError):
            db.upsert_volume_register(
                master_id,
                {"drive_id": "AID-001", "is_mirror": True, "master_volume_id": mirror_id},
            )

        with pytest.raises(Exception):
            db.delete_volume(master_id)

        db.upsert_volume_register(
            mirror_id,
            {"drive_id": "AID-002", "is_mirror": False, "status": "Archive", "condition": "Unknown"},
        )
        db.delete_volume(master_id)
        assert db.get_volume(master_id) is None
    finally:
        db.close()


@pytest.mark.parametrize("schema_version", range(1, SCHEMA_VERSION))
def test_retired_catalogue_versions_are_rejected_without_modification(
    tmp_path,
    schema_version,
):
    path = tmp_path / f"catalogue-v{schema_version}.jvvv"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('unchanged')")
    connection.execute(f"PRAGMA user_version = {schema_version}")
    connection.commit()
    connection.close()
    original_bytes = path.read_bytes()
    original_journal_mode = Database._database_header_journal_mode(path)

    with pytest.raises(
        UnsupportedCatalogueError,
        match=rf"retired schema version {schema_version}",
    ):
        open_catalogue(path)

    assert path.read_bytes() == original_bytes
    assert Database._database_header_journal_mode(path) == original_journal_mode


def test_future_catalogue_version_is_rejected_without_modification(tmp_path):
    schema_version = SCHEMA_VERSION + 1
    path = tmp_path / "future-catalogue.jvvv"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute(f"PRAGMA user_version = {schema_version}")
    connection.commit()
    connection.close()
    original_bytes = path.read_bytes()

    with pytest.raises(
        UnsupportedCatalogueError,
        match=rf"schema version {schema_version}.*only supports version {SCHEMA_VERSION}",
    ):
        open_catalogue(path)

    assert path.read_bytes() == original_bytes


def test_upsert_file_accepts_unsigned_64_bit_identity_values(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        scanned_at = "2026-06-25T12:00:00.000000+0000"
        volume_id = db.create_volume("Archive", str(tmp_path))
        folder_id = db.ensure_folder(
            volume_id=volume_id,
            parent_id=None,
            name="Archive",
            relative_path="",
            scanned_at=scanned_at,
        )
        identity_device = 2**63 + 7
        identity_inode = 2**63 + 99

        db.upsert_file(
            volume_id=volume_id,
            folder_id=folder_id,
            name="original.bin",
            relative_path="original.bin",
            extension="bin",
            size_bytes=1024,
            modified_at=None,
            scanned_at=scanned_at,
            identity_device=identity_device,
            identity_inode=identity_inode,
        )
        db.upsert_file(
            volume_id=volume_id,
            folder_id=folder_id,
            name="linked.bin",
            relative_path="linked.bin",
            extension="bin",
            size_bytes=1024,
            modified_at=None,
            scanned_at=scanned_at,
            identity_device=identity_device,
            identity_inode=identity_inode,
        )

        rows = list(db.connection.execute("SELECT identity_device, identity_inode FROM files"))
        assert {row["identity_device"] for row in rows} == {identity_device - 2**64}
        assert {row["identity_inode"] for row in rows} == {identity_inode - 2**64}

        db.rebuild_folder_statistics(volume_id, scanned_at)
        root = db.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == 1024
        assert root["recursive_file_count"] == 2
        assert root["direct_file_count"] == 2
    finally:
        db.close()


def test_upsert_file_validates_and_replaces_content_hash_fields(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        scanned_at = "2026-08-27T12:00:00.000000+0000"
        volume_id = db.create_volume("Archive", str(tmp_path))
        with db.transaction():
            folder_id = db.ensure_folder(
                volume_id=volume_id,
                parent_id=None,
                name="Archive",
                relative_path="",
                scanned_at=scanned_at,
            )
            file_id = db.upsert_file(
                volume_id=volume_id,
                folder_id=folder_id,
                name="hashed.bin",
                relative_path="hashed.bin",
                extension="bin",
                size_bytes=32,
                modified_at=None,
                scanned_at=scanned_at,
                content_hash=bytes(range(32)),
                content_hash_algorithm=" SHA256 ",
            )

        row = db.get_file(file_id)
        assert row["content_hash"] == bytes(range(32))
        assert row["content_hash_algorithm"] == "sha256"

        with pytest.raises(ValueError, match="stored together"):
            db.upsert_file(
                volume_id=volume_id,
                folder_id=folder_id,
                name="missing-algorithm.bin",
                relative_path="missing-algorithm.bin",
                extension="bin",
                size_bytes=1,
                modified_at=None,
                scanned_at=scanned_at,
                content_hash=bytes(range(32)),
            )
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            db.upsert_file(
                volume_id=volume_id,
                folder_id=folder_id,
                name="short.bin",
                relative_path="short.bin",
                extension="bin",
                size_bytes=1,
                modified_at=None,
                scanned_at=scanned_at,
                content_hash=b"short",
                content_hash_algorithm="sha256",
            )

        with db.transaction():
            same_file_id = db.upsert_file(
                volume_id=volume_id,
                folder_id=folder_id,
                name="hashed.bin",
                relative_path="hashed.bin",
                extension="bin",
                size_bytes=32,
                modified_at=None,
                scanned_at=scanned_at,
            )
        row = db.get_file(same_file_id)
        assert same_file_id == file_id
        assert row["content_hash"] is None
        assert row["content_hash_algorithm"] is None
    finally:
        db.close()


def test_finish_scan_stores_hash_and_media_statistics(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        scan_id = db.start_scan(volume_id)
        db.finish_scan(
            scan_id,
            "completed",
            4,
            2,
            1,
            files_hashed=3,
            bytes_hashed=4096,
            hash_errors=1,
            media_files=2,
            media_metadata_collected=1,
        )

        row = db.list_scan_history(volume_id)[0]
        assert row["files_hashed"] == 3
        assert row["bytes_hashed"] == 4096
        assert row["hash_errors"] == 1
        assert row["media_files"] == 2
        assert row["media_metadata_collected"] == 1
    finally:
        db.close()


def test_file_media_metadata_api_replaces_and_clears_one_to_one_record(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        scanned_at = "2026-08-27T12:00:00.000000+0000"
        volume_id = db.create_volume("Archive", str(tmp_path))
        with db.transaction():
            folder_id = db.ensure_folder(
                volume_id=volume_id,
                parent_id=None,
                name="Archive",
                relative_path="",
                scanned_at=scanned_at,
            )
            file_id = db.upsert_file(
                volume_id=volume_id,
                folder_id=folder_id,
                name="clip.mp4",
                relative_path="clip.mp4",
                extension="mp4",
                size_bytes=100,
                modified_at=None,
                scanned_at=scanned_at,
            )
            db.replace_file_media_metadata(
                file_id,
                {
                    "status": "available",
                    "media_kind": "video",
                    "source": "ffprobe",
                    "container": "mov,mp4",
                    "duration_ms": 12_345,
                    "width": 1920,
                    "height": 1080,
                    "video_codecs": "h264",
                    "audio_codecs": "aac",
                    "sample_rate_hz": 48_000,
                    "channels": 2,
                    "bit_rate": 8_000_000,
                    "message": "",
                    "probed_at": scanned_at,
                },
            )

        row = db.get_file_media_metadata(file_id)
        assert row["status"] == "available"
        assert row["media_kind"] == "video"
        assert row["duration_ms"] == 12_345
        assert (row["width"], row["height"]) == (1920, 1080)
        assert row["video_codecs"] == "h264"
        assert row["audio_codecs"] == "aac"

        with db.transaction():
            db.replace_file_media_metadata(file_id, None)
        assert db.get_file_media_metadata(file_id) is None
    finally:
        db.close()


def _catalogue_with_media_files(db, tmp_path):
    """Create one volume with an image, a video, and a text file; return their IDs."""

    scanned_at = "2026-09-01T12:00:00.000000+0000"
    volume_id = db.create_volume("Archive", str(tmp_path))
    ids = {}
    with db.transaction():
        folder_id = db.ensure_folder(
            volume_id=volume_id,
            parent_id=None,
            name="Archive",
            relative_path="",
            scanned_at=scanned_at,
        )
        for name, extension, digest in (
            ("photo.jpg", "jpg", bytes([1]) * 32),
            ("clip.mp4", "mp4", bytes([2]) * 32),
            ("notes.txt", "txt", bytes([3]) * 32),
            ("unhashed.png", "png", None),
        ):
            ids[name] = db.upsert_file(
                volume_id=volume_id,
                folder_id=folder_id,
                name=name,
                relative_path=name,
                extension=extension,
                size_bytes=10,
                modified_at=None,
                scanned_at=scanned_at,
                content_hash=digest,
                content_hash_algorithm="sha256" if digest is not None else None,
            )
    return volume_id, ids


def test_file_preview_status_api_stores_validates_and_clears_records(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id, ids = _catalogue_with_media_files(db, tmp_path)
        digest = bytes([1]) * 32
        with db.transaction():
            db.replace_file_preview_status(
                ids["photo.jpg"],
                {
                    "media_kind": "image",
                    "profile_id": "jpeg-max1600-q82",
                    "status": "available",
                    "source_hash": digest,
                    "preview_size": 2**40 + 5,
                    "preview_width": 1600,
                    "preview_height": 1067,
                    "generated_at": "2026-09-01T12:00:01.000000+0000",
                },
            )
            db.replace_file_preview_status(
                ids["clip.mp4"],
                {
                    "media_kind": "video",
                    "profile_id": "h264-1fps-240p-crf35-veryfast",
                    "status": "failed",
                    "source_hash": bytes([2]) * 32,
                    "error_stage": "ffmpeg-exit",
                    "error_message": "FFmpeg exited with code 1.",
                },
            )

        photo = db.get_file_preview_status(ids["photo.jpg"])
        assert photo["status"] == "available"
        assert photo["media_kind"] == "image"
        assert photo["profile_id"] == "jpeg-max1600-q82"
        assert bytes(photo["source_hash"]) == digest
        assert photo["preview_size"] == 2**40 + 5  # 64-bit byte counts survive
        assert (photo["preview_width"], photo["preview_height"]) == (1600, 1067)
        assert photo["error_message"] == ""
        assert photo["updated_at"]

        clip = db.get_file_preview_status(ids["clip.mp4"])
        assert clip["status"] == "failed"
        assert clip["error_stage"] == "ffmpeg-exit"
        assert clip["preview_size"] is None

        assert db.count_preview_statuses(volume_id) == {"available": 1, "failed": 1}
        assert db.count_preview_statuses() == {"available": 1, "failed": 1}

        failures = db.list_preview_failures(volume_id)
        assert [row["relative_path"] for row in failures] == ["clip.mp4"]
        assert failures[0]["error_message"] == "FFmpeg exited with code 1."
        assert bytes(failures[0]["content_hash"]) == bytes([2]) * 32
        assert db.list_preview_failures(volume_id, limit=0) == []

        properties = db.get_item_properties("file", ids["clip.mp4"])
        assert properties["preview_status"] == "failed"
        assert properties["preview_error_stage"] == "ffmpeg-exit"
        assert properties["preview_profile_id"] == "h264-1fps-240p-crf35-veryfast"
        assert db.get_item_properties("file", ids["notes.txt"])["preview_status"] is None

        # Replacing overwrites the single row per file; None removes it.
        with db.transaction():
            db.replace_file_preview_status(
                ids["clip.mp4"],
                {
                    "media_kind": "video",
                    "profile_id": "h264-1fps-240p-crf35-veryfast",
                    "status": "available",
                    "preview_size": 10,
                },
            )
        assert db.get_file_preview_status(ids["clip.mp4"])["status"] == "available"
        assert db.list_preview_failures(volume_id) == []
        with db.transaction():
            db.replace_file_preview_status(ids["clip.mp4"], None)
        assert db.get_file_preview_status(ids["clip.mp4"]) is None

        # Invalid values are rejected before anything is stored.
        for bad in (
            {"media_kind": "image", "profile_id": "p", "status": "sometimes"},
            {"media_kind": "audio", "profile_id": "p", "status": "available"},
            {"media_kind": "image", "profile_id": "", "status": "available"},
            {"media_kind": "image", "profile_id": "p", "status": "available", "source_hash": b"short"},
        ):
            with pytest.raises(ValueError):
                db.replace_file_preview_status(ids["photo.jpg"], bad)
        assert db.get_file_preview_status(ids["photo.jpg"]) is None
    finally:
        db.close()


def test_file_preview_status_rows_follow_their_file(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id, ids = _catalogue_with_media_files(db, tmp_path)
        with db.transaction():
            db.replace_file_preview_status(
                ids["photo.jpg"],
                {"media_kind": "image", "profile_id": "p", "status": "missing"},
            )
        db.delete_volume(volume_id)
        assert count_rows(db, "file_preview_status") == 0
    finally:
        db.close()


def test_content_hash_lookup_reports_only_requested_extensions(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        _catalogue_with_media_files(db, tmp_path)
        inserted = db.prepare_content_hash_lookup({".JPG", "png", ""})
        assert inserted == 1  # the PNG has no hash; the JPG is matched case-insensitively
        assert db.content_hash_referenced(bytes([1]) * 32)
        assert not db.content_hash_referenced(bytes([2]) * 32)  # mp4 was not requested
        assert not db.content_hash_referenced(bytes([3]) * 32)

        assert db.prepare_content_hash_lookup(["mp4", "txt"]) == 2
        assert db.content_hash_referenced(bytes([2]) * 32)
        assert not db.content_hash_referenced(bytes([1]) * 32)

        assert db.prepare_content_hash_lookup([]) == 0
        assert not db.content_hash_referenced(bytes([2]) * 32)
        db.drop_content_hash_lookup()
        db.drop_content_hash_lookup()  # idempotent
    finally:
        db.close()


def test_content_hash_lookup_works_on_a_read_only_connection(tmp_path):
    path = tmp_path / "catalogue.sqlite3"
    writer = Database(path)
    try:
        _catalogue_with_media_files(writer, tmp_path)
    finally:
        writer.close()

    reader = Database(path, initialize=False, create=False, read_only=True)
    try:
        # The Preview Cache manager opens the catalogue read-only; the lookup
        # lives in a TEMP table, which query_only would otherwise refuse.
        assert reader.prepare_content_hash_lookup(["jpg", "mp4"]) == 2
        assert reader.content_hash_referenced(bytes([1]) * 32)
        assert reader.content_hash_referenced(bytes([2]) * 32)
        assert not reader.content_hash_referenced(bytes([3]) * 32)
        reader.drop_content_hash_lookup()
        # The connection is still read-only for the main database afterwards.
        assert reader.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.connection.execute("DELETE FROM files")
    finally:
        reader.close()


def test_finish_scan_persists_preview_summary_and_rejects_unknown_modes(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        scan_id = db.start_scan(volume_id)
        db.finish_scan(
            scan_id,
            "completed",
            3,
            1,
            0,
            "Catalogue indexing succeeded, but 1 offline preview was not created.",
            preview_summary={
                "mode": "enabled",
                "image_generated": 2,
                "image_reused": 1,
                "image_failed": 1,
                "video_generated": 0,
                "video_reused": 4,
                "video_failed": 0,
                "storage_skipped": 7,
                "bytes_written": 2**41,
                "message": "Preview storage became unavailable: disk full",
            },
        )
        row = db.list_scan_history(volume_id)[0]
        assert row["preview_mode"] == "enabled"
        assert row["image_previews_generated"] == 2
        assert row["image_previews_reused"] == 1
        assert row["image_previews_failed"] == 1
        assert row["video_previews_reused"] == 4
        assert row["previews_storage_skipped"] == 7
        assert row["preview_bytes_written"] == 2**41
        assert row["preview_message"].startswith("Preview storage became unavailable")

        default_scan = db.start_scan(volume_id)
        db.finish_scan(default_scan, "completed", 0, 0, 0)
        default_row = db.list_scan_history(volume_id)[0]
        assert default_row["preview_mode"] == "disabled"
        assert default_row["preview_bytes_written"] == 0

        rejected_scan = db.start_scan(volume_id)
        with pytest.raises(ValueError):
            db.finish_scan(rejected_scan, "completed", 0, 0, 0, preview_summary={"mode": "maybe"})
    finally:
        db.close()
