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
