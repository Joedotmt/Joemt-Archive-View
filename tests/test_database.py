from __future__ import annotations

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
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 1
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

        db.update_volume(volume_id, "Renamed", str(tmp_path / "other"))
        volume = db.get_volume(volume_id)
        assert volume["name"] == "Renamed"
        assert volume["source_path"] == str(tmp_path / "other")

        db.delete_volume(volume_id)
        assert db.get_volume(volume_id) is None
        assert count_rows(db, "volumes") == 0
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
