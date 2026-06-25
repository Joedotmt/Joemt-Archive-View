from __future__ import annotations

import os

from jvvv.database import Database, count_rows
from jvvv.scanner import VolumeScanner


def make_tree(root):
    (root / "Docs").mkdir()
    (root / "Docs" / "report.txt").write_text("hello", encoding="utf-8")
    (root / "Docs" / "budget.csv").write_text("1,2,3", encoding="utf-8")
    (root / "Photos").mkdir()
    (root / "Photos" / "image.JPG").write_bytes(b"jpeg")


def test_scan_indexes_files_and_folders(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_seen == 3
        assert result.folders_seen == 3
        assert count_rows(db, "files") == 3
        assert count_rows(db, "folders") == 3

        volume = db.get_volume(volume_id)
        assert volume["indexed_file_count"] == 3
        assert volume["indexed_folder_count"] == 3
        assert volume["capacity_bytes"] > 0
        assert volume["last_scan_at"]

        docs = db.get_folder_by_path(volume_id, "Docs")
        assert docs is not None
        files = db.list_files(volume_id, docs["id"])
        assert {row["name"] for row in files} == {"budget.csv", "report.txt"}
        assert db.search(".jpg")[0]["name"] == "image.JPG"
    finally:
        db.close()


def test_rescan_removes_deleted_and_updates_changed_files(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        scanner = VolumeScanner(db)
        scanner.scan(volume_id)

        os.remove(source / "Docs" / "budget.csv")
        (source / "Docs" / "report.txt").write_text("changed content", encoding="utf-8")
        (source / "Docs" / "notes.md").write_text("new", encoding="utf-8")

        result = scanner.scan(volume_id, remove_deleted=True)
        assert result.status == "completed"
        assert count_rows(db, "files") == 3
        assert not db.search("budget.csv")

        report = [row for row in db.search("report.txt") if row["item_type"] == "file"][0]
        assert report["size_bytes"] == len("changed content")
        assert db.search("notes.md")
    finally:
        db.close()


def test_rescan_can_mark_deleted_files_missing(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        scanner = VolumeScanner(db)
        scanner.scan(volume_id)

        os.remove(source / "Docs" / "budget.csv")
        result = scanner.scan(volume_id, remove_deleted=False)

        assert result.status == "completed"
        matches = [row for row in db.search("budget.csv") if row["item_type"] == "file"]
        assert len(matches) == 1
        assert matches[0]["missing"] == 1
        volume = db.get_volume(volume_id)
        assert volume["indexed_file_count"] == 2
    finally:
        db.close()


def test_cancelled_scan_rolls_back_partial_catalogue(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)

        for index in range(50):
            (source / f"extra-{index}.txt").write_text(str(index), encoding="utf-8")

        calls = {"count": 0}

        def should_cancel() -> bool:
            calls["count"] += 1
            return calls["count"] > 5

        result = VolumeScanner(db, cancel_callback=should_cancel, batch_size=1).scan(volume_id)
        assert result.status == "cancelled"
        assert count_rows(db, "files") == 3
    finally:
        db.close()
