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
        assert docs["recursive_size_bytes"] == len("hello") + len("1,2,3")
        assert docs["recursive_file_count"] == 2
        assert docs["recursive_subfolder_count"] == 0
        assert docs["direct_file_count"] == 2
        assert docs["direct_subfolder_count"] == 0

        root = db.get_root_folder(volume_id)
        assert root is not None
        assert root["recursive_size_bytes"] == len("hello") + len("1,2,3") + len(b"jpeg")
        assert root["recursive_file_count"] == 3
        assert root["recursive_subfolder_count"] == 2
        assert root["direct_file_count"] == 0
        assert root["direct_subfolder_count"] == 2
        assert root["stats_updated_at"] == volume["last_scan_at"]

        files = db.list_files(volume_id, docs["id"])
        assert {row["name"] for row in files} == {"budget.csv", "report.txt"}
        assert db.search(".jpg")[0]["name"] == "image.JPG"
    finally:
        db.close()


def test_scan_reports_folder_statistics_progress(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    progress_events = []
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(
            db,
            stats_progress_callback=lambda files, folders, message, done, total: progress_events.append(
                (files, folders, message, done, total)
            ),
        ).scan(volume_id)

        assert result.status == "completed"
        assert progress_events[0] == (3, 3, "Preparing folder statistics", 0, 3)
        assert (3, 3, "Calculating folder statistics", 3, 3) in progress_events
        assert progress_events[-1] == (3, 3, "Folder statistics updated", 3, 3)
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

        docs = db.get_folder_by_path(volume_id, "Docs")
        assert docs["recursive_size_bytes"] == len("changed content") + len("new")
        assert docs["recursive_file_count"] == 2
        root = db.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == len("changed content") + len("new") + len(b"jpeg")
        assert root["recursive_file_count"] == 3
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
        docs = db.get_folder_by_path(volume_id, "Docs")
        assert docs["recursive_size_bytes"] == len("hello")
        assert docs["recursive_file_count"] == 1
        assert docs["direct_file_count"] == 1
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
        root = db.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == len("hello") + len("1,2,3") + len(b"jpeg")
        assert root["recursive_file_count"] == 3
    finally:
        db.close()


def test_folder_statistics_can_rebuild_without_source_drive(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)

        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE folders
                SET recursive_size_bytes = NULL,
                    recursive_file_count = NULL,
                    recursive_subfolder_count = NULL,
                    direct_file_count = NULL,
                    direct_subfolder_count = NULL,
                    stats_updated_at = NULL
                WHERE volume_id = ?
                """,
                (volume_id,),
            )

        source.rename(tmp_path / "drive-disconnected")
        updated = db.rebuild_folder_statistics(volume_id)

        assert updated == 3
        root = db.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == len("hello") + len("1,2,3") + len(b"jpeg")
        assert root["recursive_file_count"] == 3
        assert root["recursive_subfolder_count"] == 2
        assert root["stats_updated_at"]
    finally:
        db.close()


def test_scan_does_not_follow_symlinked_content(tmp_path):
    if not hasattr(os, "symlink"):
        return

    source = tmp_path / "drive"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (source / "real.txt").write_bytes(b"real")
    (outside / "outside.txt").write_bytes(b"outside")

    try:
        os.symlink(outside, source / "outside-link", target_is_directory=True)
        os.symlink(outside / "outside.txt", source / "outside-file-link.txt")
    except (OSError, NotImplementedError):
        return

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_seen == 1
        assert db.search("outside") == []
        root = db.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == len(b"real")
        assert root["recursive_file_count"] == 1
        assert root["recursive_subfolder_count"] == 0
    finally:
        db.close()


def test_hardlinked_file_size_is_counted_once_per_folder_tree(tmp_path):
    if not hasattr(os, "link"):
        return

    source = tmp_path / "drive"
    source.mkdir()
    original = source / "original.bin"
    linked = source / "linked.bin"
    original.write_bytes(b"payload")
    try:
        os.link(original, linked)
    except OSError:
        return

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_seen == 2
        root = db.get_root_folder(volume_id)
        assert root["recursive_size_bytes"] == len(b"payload")
        assert root["recursive_file_count"] == 2
        assert root["direct_file_count"] == 2
    finally:
        db.close()
