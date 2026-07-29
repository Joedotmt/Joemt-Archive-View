from __future__ import annotations

from jvvv.database import Database
from jvvv.scanner import VolumeScanner


def test_search_across_multiple_volumes(tmp_path):
    source_a = tmp_path / "drive-a"
    source_b = tmp_path / "drive-b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "Invoices").mkdir()
    (source_a / "Invoices" / "january.pdf").write_bytes(b"pdf")
    (source_b / "Music").mkdir()
    (source_b / "Music" / "january.mp3").write_bytes(b"mp3")

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_a = db.create_volume("Archive A", str(source_a))
        volume_b = db.create_volume("Archive B", str(source_b))
        scanner = VolumeScanner(db)
        scanner.scan(volume_a)
        scanner.scan(volume_b)

        names = {(row["volume_name"], row["name"]) for row in db.search("january")}
        assert names == {("Archive A", "january.pdf"), ("Archive B", "january.mp3")}
        drive_ids = {(row["volume_name"], row["drive_id"]) for row in db.search("january")}
        assert drive_ids == {("Archive A", "AID-001"), ("Archive B", "AID-002")}

        pdf_results = db.search(".pdf")
        assert len(pdf_results) == 1
        assert pdf_results[0]["name"] == "january.pdf"
        assert pdf_results[0]["drive_id"] == "AID-001"

        folder_results = [row for row in db.search("Invoices") if row["item_type"] == "folder"]
        assert len(folder_results) == 1
        assert folder_results[0]["relative_path"] == "Invoices"
        assert folder_results[0]["size_bytes"] == len(b"pdf")
        assert folder_results[0]["drive_id"] == "AID-001"
    finally:
        db.close()


def test_search_returns_matching_root_folder(tmp_path):
    source = tmp_path / "Hal Far Site 301118"
    source.mkdir()
    (source / "report.txt").write_text("report")

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Site archive", str(source))
        VolumeScanner(db).scan(volume_id)

        folder_results = [
            row
            for row in db.search("Hal Far Site 301118")
            if row["item_type"] == "folder"
        ]

        assert len(folder_results) == 1
        assert folder_results[0]["name"] == "Hal Far Site 301118"
        assert folder_results[0]["relative_path"] == ""
    finally:
        db.close()


def test_search_omits_descendants_matching_only_by_path_by_default(tmp_path):
    source = tmp_path / "drive"
    target = source / "Hal Far Site 301118"
    target.mkdir(parents=True)
    for index in range(10):
        (target / f"file-{index:02}.txt").write_text("content")

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Site archive", str(source))
        VolumeScanner(db).scan(volume_id)

        results = db.search("Hal Far Site 301118")

        assert len(results) == 1
        assert results[0]["item_type"] == "folder"
        assert results[0]["name"] == "Hal Far Site 301118"

        path_results = db.search(
            "Hal Far Site 301118",
            limit=3,
            include_paths=True,
        )
        assert len(path_results) == 3
        assert path_results[0]["item_type"] == "folder"
        assert path_results[0]["name"] == "Hal Far Site 301118"
    finally:
        db.close()


def test_search_returns_more_than_previous_result_limit(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        scanned_at = "2026-06-25T12:00:00.000000+0000"
        with db.transaction():
            root_id = db.ensure_folder(
                volume_id=volume_id,
                parent_id=None,
                name="Archive",
                relative_path="",
                scanned_at=scanned_at,
            )
            for index in range(650):
                name = f"common-result-{index:04d}.txt"
                db.upsert_file(
                    volume_id=volume_id,
                    folder_id=root_id,
                    name=name,
                    relative_path=name,
                    extension="txt",
                    size_bytes=index,
                    modified_at=None,
                    scanned_at=scanned_at,
                )

        results = db.search("common-result")

        assert len(results) == 650
    finally:
        db.close()


def test_search_index_tracks_file_updates_and_deletes(tmp_path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Archive", str(tmp_path))
        scanned_at = "2026-06-25T12:00:00.000000+0000"
        with db.transaction():
            root_id = db.ensure_folder(
                volume_id=volume_id,
                parent_id=None,
                name="Archive",
                relative_path="",
                scanned_at=scanned_at,
            )
            file_id = db.upsert_file(
                volume_id=volume_id,
                folder_id=root_id,
                name="oldneedle.bin",
                relative_path="folder/item.bin",
                extension="bin",
                size_bytes=1,
                modified_at=None,
                scanned_at=scanned_at,
            )

        assert [row["item_id"] for row in db.search("oldneedle")] == [file_id]

        with db.transaction():
            updated_id = db.upsert_file(
                volume_id=volume_id,
                folder_id=root_id,
                name="newneedle.bin",
                relative_path="folder/item.bin",
                extension="bin",
                size_bytes=2,
                modified_at=None,
                scanned_at=scanned_at,
            )

        assert updated_id == file_id
        assert db.search("oldneedle") == []
        assert [row["item_id"] for row in db.search("newneedle")] == [file_id]

        with db.transaction() as conn:
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

        assert db.search("newneedle") == []
    finally:
        db.close()
