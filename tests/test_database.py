from __future__ import annotations

from datetime import date
import sqlite3

import pytest

from jvvv.database import (
    Database,
    InvalidCatalogueError,
    count_rows,
    create_catalogue,
    open_catalogue,
)


def test_database_initializes_schema(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        tables = {
            row["name"]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"volumes", "folders", "files", "scan_history", "scan_errors"} <= tables
        assert "volume_register" in tables
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 5
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


def test_version_1_catalogue_migrates_folder_stats_as_unknown(tmp_path):
    path = tmp_path / "catalogue.jvvv"
    db = Database(path, initialize=False)
    try:
        with db.transaction() as conn:
            db._apply_migration_1()
            conn.execute("PRAGMA user_version = 1")
            now = "2026-06-25T12:00:00.000000+0000"
            volume_id = conn.execute(
                """
                INSERT INTO volumes (name, source_path, created_at, updated_at)
                VALUES ('Archive', ?, ?, ?)
                """,
                (str(tmp_path), now, now),
            ).lastrowid
            folder_id = db.ensure_folder(
                volume_id=volume_id,
                parent_id=None,
                name="Archive",
                relative_path="",
                scanned_at="2026-06-25T12:00:00.000000+0000",
            )
            conn.execute(
                """
                INSERT INTO files (
                    volume_id, folder_id, name, relative_path, extension,
                    size_bytes, modified_at, missing, scanned_at
                )
                VALUES (?, ?, 'file.txt', 'file.txt', 'txt', 123, NULL, 0, ?)
                """,
                (volume_id, folder_id, "2026-06-25T12:00:00.000000+0000"),
            )
    finally:
        db.close()

    migrated = open_catalogue(path)
    try:
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 5
        root = migrated.get_root_folder(volume_id)
        assert root is not None
        assert root["recursive_size_bytes"] is None
        assert root["recursive_file_count"] is None

        migrated.rebuild_folder_statistics(volume_id)
        root = migrated.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == 123
        assert root["recursive_file_count"] == 1
        assert root["direct_file_count"] == 1
    finally:
        migrated.close()


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
