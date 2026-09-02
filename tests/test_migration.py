"""The v11 -> v12 catalogue upgrade: opens existing catalogues without losing anything."""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from legacy_v11 import (  # noqa: E402
    V11_DATA_TABLES,
    V11_SCHEMA_VERSION,
    create_v11_catalogue,
    snapshot,
)

from jvvv import database as database_module  # noqa: E402
from jvvv.catalogue_backup import (  # noqa: E402
    CatalogueBackupError,
    create_catalogue_backup,
    restore_catalogue_backup,
)
from jvvv.database import (  # noqa: E402
    SCAN_HISTORY_PREVIEW_COLUMNS,
    SCHEMA_VERSION,
    CatalogueError,
    Database,
    UnsupportedCatalogueError,
    open_catalogue,
)


def user_version(path: pathlib.Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def schema_of(path: pathlib.Path) -> dict[str, object]:
    """Table columns (name, type, notnull, default) and index names of a catalogue file."""

    connection = sqlite3.connect(path)
    try:
        tables = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        columns = {
            table: [
                (str(row[1]), str(row[2]), int(row[3]), row[4])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in tables
        }
        indexes = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        )
        return {"tables": tables, "columns": columns, "indexes": indexes}
    finally:
        connection.close()


def _strip_new_columns(rows: list[tuple], width: int) -> list[tuple]:
    return sorted((row[:width] for row in rows), key=repr)


def test_v11_fixture_really_is_schema_version_11(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")

    assert user_version(path) == V11_SCHEMA_VERSION == 11
    schema = schema_of(path)
    assert "file_preview_status" not in schema["tables"]
    assert "preview_mode" not in {column[0] for column in schema["columns"]["scan_history"]}
    before = snapshot(path)
    assert len(before["files"]) == 4
    assert len(before["scan_history"]) == 2


def test_opening_a_v11_catalogue_upgrades_it_in_place_and_keeps_every_row(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    before = snapshot(path)
    v11_scan_history_width = len(before["scan_history"][0])

    db = open_catalogue(path)
    try:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.validate_schema()
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(db.connection.execute("PRAGMA foreign_key_check")) == []

        # The upgraded catalogue is immediately usable through the normal API.
        assert sorted(row["id"] for row in db.list_volumes()) == [7, 12]
        assert {row["item_id"] for row in db.search("shared clip")} == {1001, 2001}
        history = {row["id"]: row for row in db.list_scan_history(7)}
        assert history[301]["status"] == "completed"
        for column, definition in SCAN_HISTORY_PREVIEW_COLUMNS:
            expected = 0 if "INTEGER" in definition else definition.split("DEFAULT ", 1)[1].strip("'")
            assert history[301][column] == expected, column
        assert db.count_preview_statuses() == {}
        with db.transaction():
            db.replace_file_preview_status(
                1001,
                {"media_kind": "video", "profile_id": "h264-1fps-240p-crf35-veryfast", "status": "missing"},
            )
        assert db.get_file_preview_status(1001)["status"] == "missing"
        with db.transaction():
            db.replace_file_preview_status(1001, None)
    finally:
        db.close()

    assert user_version(path) == SCHEMA_VERSION
    after = snapshot(path)
    for table in V11_DATA_TABLES:
        if table == "scan_history":
            assert _strip_new_columns(after[table], v11_scan_history_width) == before[table]
        else:
            assert after[table] == before[table], table
    assert after["sqlite_sequence"] == before["sqlite_sequence"]

    # The upgraded schema is exactly the schema a brand-new v12 catalogue gets.
    fresh = Database(tmp_path / "fresh.jvvv")
    fresh.close()
    upgraded_schema = schema_of(path)
    fresh_schema = schema_of(tmp_path / "fresh.jvvv")
    assert upgraded_schema["tables"] == fresh_schema["tables"]
    assert upgraded_schema["indexes"] == fresh_schema["indexes"]
    for table in fresh_schema["tables"]:
        assert upgraded_schema["columns"][table] == fresh_schema["columns"][table], table


def test_the_direct_database_constructor_also_upgrades(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    before = snapshot(path)

    db = Database(path)
    try:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "file_preview_status" in db._table_names()
    finally:
        db.close()
    assert snapshot(path)["files"] == before["files"]


def test_reopening_an_upgraded_catalogue_is_a_no_op(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    open_catalogue(path).close()
    first = schema_of(path)

    db = open_catalogue(path)
    try:
        db.upgrade_schema(SCHEMA_VERSION)  # explicit call on a current catalogue does nothing
    finally:
        db.close()
    assert schema_of(path) == first
    assert user_version(path) == SCHEMA_VERSION


def test_a_v11_catalogue_cannot_be_opened_read_only_before_the_upgrade(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    before = snapshot(path)

    with pytest.raises(UnsupportedCatalogueError, match="must be upgraded"):
        Database(path, initialize=False, create=False, read_only=True)

    assert user_version(path) == V11_SCHEMA_VERSION
    assert snapshot(path) == before
    # After a normal open the read-only connection works.
    open_catalogue(path).close()
    reader = Database(path, initialize=False, create=False, read_only=True)
    try:
        reader.validate_catalogue()
        assert reader.get_file_preview_status(1001) is None
    finally:
        reader.close()


def test_a_failed_upgrade_rolls_back_and_leaves_the_v11_catalogue_unchanged(tmp_path, monkeypatch):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    before = snapshot(path)
    before_schema = schema_of(path)
    broken = (
        *database_module.V11_TO_V12_UPGRADE_SQL,
        "CREATE TABLE file_preview_status (duplicate INTEGER)",  # fails: already created above
    )
    monkeypatch.setattr(database_module, "V11_TO_V12_UPGRADE_SQL", broken)

    with pytest.raises(CatalogueError, match="left unchanged"):
        open_catalogue(path)

    assert user_version(path) == V11_SCHEMA_VERSION
    assert schema_of(path)["tables"] == before_schema["tables"]
    assert schema_of(path)["columns"]["scan_history"] == before_schema["columns"]["scan_history"]
    assert snapshot(path) == before

    monkeypatch.setattr(database_module, "V11_TO_V12_UPGRADE_SQL", broken[:-1])
    db = open_catalogue(path)
    try:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()
    assert snapshot(path)["files"] == before["files"]


def test_other_retired_versions_are_still_rejected_without_modification(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 10")
    connection.commit()
    connection.close()
    before = snapshot(path)

    with pytest.raises(UnsupportedCatalogueError, match="retired schema version 10"):
        open_catalogue(path)

    assert user_version(path) == 10
    assert snapshot(path) == before


def test_upgraded_catalogue_backs_up_and_restores_losslessly(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    original = snapshot(path)
    open_catalogue(path).close()

    backup_path = tmp_path / "legacy.backup.zip"
    create_catalogue_backup(path, backup_path)
    restored_path = tmp_path / "restored.jvvv"
    restore_catalogue_backup(backup_path, restored_path)

    restored = snapshot(restored_path, tables=("volumes", "volume_register", "folders", "files", "file_media_metadata", "scan_errors"))
    for table in ("volumes", "volume_register", "folders", "files", "file_media_metadata", "scan_errors"):
        assert restored[table] == original[table], table
    assert user_version(restored_path) == SCHEMA_VERSION
    assert "file_preview_status" in schema_of(restored_path)["tables"]


def test_backing_up_an_unupgraded_v11_file_explains_how_to_upgrade(tmp_path):
    path = create_v11_catalogue(tmp_path / "legacy.jvvv")

    with pytest.raises(CatalogueBackupError, match="open the catalogue in JVVV once"):
        create_catalogue_backup(path, tmp_path / "legacy.backup.zip")

    assert user_version(path) == V11_SCHEMA_VERSION


def test_upgrade_tolerates_a_pre_existing_preview_table(tmp_path):
    """A v11 file that already has the v12 table (aborted build, manual repair) still upgrades."""

    from jvvv.database import FILE_PREVIEW_STATUS_TABLE_SQL

    path = create_v11_catalogue(tmp_path / "legacy.jvvv")
    connection = sqlite3.connect(path)
    connection.execute(FILE_PREVIEW_STATUS_TABLE_SQL)
    connection.commit()
    connection.close()
    assert user_version(path) == V11_SCHEMA_VERSION
    before = snapshot(path)

    db = open_catalogue(path)
    try:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.validate_schema()
    finally:
        db.close()
    assert snapshot(path)["files"] == before["files"]
