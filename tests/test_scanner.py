from __future__ import annotations

import hashlib
import os
import wave
from types import SimpleNamespace

from jvvv.database import Database, count_rows
from jvvv.media_metadata import MediaMetadata
from jvvv.scanner import FileChangedDuringHashError, VolumeScanner


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


def test_scan_reviews_and_applies_detected_changes(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        previews = []
        scanner = VolumeScanner(
            db,
            preview_callback=lambda changes: previews.append(changes) or True,
        )
        scanner.scan(volume_id)

        os.remove(source / "Docs" / "budget.csv")
        (source / "Docs" / "report.txt").write_text("changed content", encoding="utf-8")
        (source / "Docs" / "notes.md").write_text("new", encoding="utf-8")

        result = scanner.scan(volume_id)
        assert result.status == "completed"
        assert len(previews) == 1
        assert result.changes == previews[0]
        assert result.changes.files_added == 1
        assert result.changes.files_removed == 1
        assert result.changes.files_changed == 1
        assert result.changes.bytes_before == len("hello") + len("1,2,3") + len(b"jpeg")
        assert result.changes.bytes_after == len("changed content") + len("new") + len(b"jpeg")
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


def test_declining_scan_changes_leaves_catalogue_unchanged(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)
        before = db.get_volume(volume_id)["last_scan_at"]

        os.remove(source / "Docs" / "budget.csv")
        previews = []
        result = VolumeScanner(
            db,
            preview_callback=lambda changes: previews.append(changes) or False,
        ).scan(volume_id)

        assert result.status == "discarded"
        assert len(previews) == 1
        assert result.changes.files_removed == 1
        matches = [row for row in db.search("budget.csv") if row["item_type"] == "file"]
        assert len(matches) == 1
        assert matches[0]["missing"] == 0
        docs = db.get_folder_by_path(volume_id, "Docs")
        assert docs["recursive_size_bytes"] == len("hello") + len("1,2,3")
        assert docs["recursive_file_count"] == 2
        assert docs["direct_file_count"] == 2
        volume = db.get_volume(volume_id)
        assert volume["indexed_file_count"] == 3
        assert volume["last_scan_at"] == before
        history = db.list_scan_history(volume_id)
        assert history[0]["status"] == "discarded"
        assert history[0]["files_removed"] == 1
    finally:
        db.close()


def test_unchanged_scan_applies_without_requesting_review(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    make_tree(source)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)

        reviews = []
        result = VolumeScanner(
            db,
            preview_callback=lambda changes: reviews.append(changes) or True,
        ).scan(volume_id)

        assert result.status == "completed"
        assert result.changes is not None
        assert not result.changes.has_changes
        assert reviews == []
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


def test_scan_stores_sha256_for_normal_empty_and_equal_content_files(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    payload = b"same bytes under different names"
    (source / "first.bin").write_bytes(payload)
    (source / "renamed.dat").write_bytes(payload)
    (source / "empty.bin").write_bytes(b"")

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_hashed == 3
        assert result.bytes_hashed == len(payload) * 2
        assert result.hash_errors == 0

        rows = {
            row["name"]: row
            for row in db.connection.execute(
                "SELECT name, content_hash, content_hash_algorithm FROM files"
            )
        }
        expected = hashlib.sha256(payload).digest()
        assert rows["first.bin"]["content_hash"] == expected
        assert rows["renamed.dat"]["content_hash"] == expected
        assert rows["empty.bin"]["content_hash"] == hashlib.sha256(b"").digest()
        assert {row["content_hash_algorithm"] for row in rows.values()} == {"sha256"}

        history = db.list_scan_history(volume_id)[0]
        assert history["files_hashed"] == 3
        assert history["bytes_hashed"] == len(payload) * 2
        assert history["hash_errors"] == 0
    finally:
        db.close()


def test_rescan_detects_changed_content_with_same_size_and_mtime(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    file_path = source / "fixed.bin"
    file_path.write_bytes(b"first payload")

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)
        original_stat = file_path.stat()
        original_hash = db.connection.execute(
            "SELECT content_hash FROM files WHERE volume_id = ? AND relative_path = 'fixed.bin'",
            (volume_id,),
        ).fetchone()["content_hash"]

        file_path.write_bytes(b"other payload")
        os.utime(
            file_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        reviews = []
        result = VolumeScanner(
            db,
            preview_callback=lambda changes: reviews.append(changes) or True,
        ).scan(volume_id)

        assert result.status == "completed"
        assert result.changes is not None
        assert result.changes.files_changed == 1
        assert len(reviews) == 1
        new_hash = db.connection.execute(
            "SELECT content_hash FROM files WHERE volume_id = ? AND relative_path = 'fixed.bin'",
            (volume_id,),
        ).fetchone()["content_hash"]
        assert new_hash == hashlib.sha256(b"other payload").digest()
        assert new_hash != original_hash
    finally:
        db.close()


def test_rescan_ignores_touched_mtime_when_content_hash_is_unchanged(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    file_path = source / "touched.bin"
    payload = b"content did not change"
    file_path.write_bytes(payload)

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)
        original_stat = file_path.stat()
        os.utime(
            file_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 2_000_000_000),
        )

        reviews = []
        result = VolumeScanner(
            db,
            preview_callback=lambda changes: reviews.append(changes) or True,
        ).scan(volume_id)

        assert result.status == "completed"
        assert result.changes is not None
        assert result.changes.files_changed == 0
        assert not result.changes.has_changes
        assert reviews == []
        row = db.connection.execute(
            "SELECT content_hash, content_hash_algorithm FROM files WHERE volume_id = ?",
            (volume_id,),
        ).fetchone()
        assert row["content_hash"] == hashlib.sha256(payload).digest()
        assert row["content_hash_algorithm"] == "sha256"
    finally:
        db.close()


def test_cancellation_during_hashing_rolls_back_to_previous_hash(tmp_path, monkeypatch):
    source = tmp_path / "drive"
    source.mkdir()
    file_path = source / "large.bin"
    file_path.write_bytes(b"old content")

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)
        previous_hash = db.connection.execute(
            "SELECT content_hash FROM files WHERE volume_id = ?",
            (volume_id,),
        ).fetchone()["content_hash"]
        file_path.write_bytes(b"new content")

        monkeypatch.setattr("jvvv.scanner.HASH_READ_SIZE", 1)
        scanner = None

        def cancel_after_first_chunk() -> bool:
            return scanner is not None and scanner.bytes_hashed >= 1

        scanner = VolumeScanner(db, cancel_callback=cancel_after_first_chunk)
        result = scanner.scan(volume_id)

        assert result.status == "cancelled"
        assert result.bytes_hashed == 1
        row = db.connection.execute(
            "SELECT content_hash, content_hash_algorithm FROM files WHERE volume_id = ?",
            (volume_id,),
        ).fetchone()
        assert row["content_hash"] == previous_hash
        assert row["content_hash_algorithm"] == "sha256"
        assert db.list_scan_history(volume_id)[0]["status"] == "cancelled"
    finally:
        db.close()


def test_hash_read_failure_indexes_metadata_without_a_stale_hash(tmp_path, monkeypatch):
    source = tmp_path / "drive"
    source.mkdir()
    (source / "locked.bin").write_bytes(b"metadata is still available")

    def fail_hash(*args, **kwargs):
        raise PermissionError("content read denied")

    monkeypatch.setattr(VolumeScanner, "_hash_file", fail_hash)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_seen == 1
        assert result.files_hashed == 0
        assert result.hash_errors == 1
        assert result.errors_count == 1
        row = db.connection.execute(
            "SELECT size_bytes, content_hash, content_hash_algorithm FROM files WHERE volume_id = ?",
            (volume_id,),
        ).fetchone()
        assert row["size_bytes"] == len(b"metadata is still available")
        assert row["content_hash"] is None
        assert row["content_hash_algorithm"] is None
        error = db.list_scan_errors(volume_id)[0]
        assert "SHA-256 hash unavailable" in error["message"]
        assert "content read denied" in error["message"]
        history = db.list_scan_history(volume_id)[0]
        assert history["hash_errors"] == 1
    finally:
        db.close()


def test_hash_retries_once_with_a_fresh_stat_after_concurrent_change(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "drive"
    source.mkdir()
    file_path = source / "changing.bin"
    payload = b"stable on the retry"
    file_path.write_bytes(payload)
    original_hash_file = VolumeScanner._hash_file
    attempts = 0

    def change_once(scanner, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileChangedDuringHashError("changed during first attempt")
        return original_hash_file(scanner, *args, **kwargs)

    monkeypatch.setattr(VolumeScanner, "_hash_file", change_once)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert attempts == 2
        assert result.files_seen == 1
        assert result.files_hashed == 1
        assert result.hash_errors == 0
        assert result.errors_count == 0
        row = db.connection.execute("SELECT content_hash FROM files").fetchone()
        assert row["content_hash"] == hashlib.sha256(payload).digest()
    finally:
        db.close()


def test_repeatedly_changing_file_is_skipped_as_an_incomplete_scan_area(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "drive"
    source.mkdir()
    (source / "changing.bin").write_bytes(b"never stable")

    def always_change(*_args, **_kwargs):
        raise FileChangedDuringHashError("changed again")

    monkeypatch.setattr(VolumeScanner, "_hash_file", always_change)
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_seen == 0
        assert result.files_hashed == 0
        assert result.hash_errors == 0
        assert result.errors_count == 1
        assert db.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        error = db.list_scan_errors(volume_id)[0]
        assert "File skipped because it changed or disappeared" in error["message"]
        assert "SHA-256 hash unavailable" not in error["message"]
    finally:
        db.close()


def test_rescan_hash_failure_requires_review_and_decline_keeps_old_hash(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "drive"
    source.mkdir()
    file_path = source / "asset.bin"
    file_path.write_bytes(b"stable content")
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        VolumeScanner(db).scan(volume_id)
        old_hash = db.get_file(
            db.connection.execute("SELECT id FROM files").fetchone()["id"]
        )["content_hash"]

        monkeypatch.setattr(
            VolumeScanner,
            "_hash_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                PermissionError("drive read failed")
            ),
        )
        reviews = []
        result = VolumeScanner(
            db,
            preview_callback=lambda changes: reviews.append(changes) or False,
        ).scan(volume_id)

        assert result.status == "discarded"
        assert len(reviews) == 1
        assert reviews[0].hash_errors == 1
        assert reviews[0].has_changes
        row = db.connection.execute("SELECT content_hash FROM files").fetchone()
        assert row["content_hash"] == old_hash
    finally:
        db.close()


def test_scan_excludes_the_active_catalogue_and_sqlite_sidecars(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    (source / "user.bin").write_bytes(b"user data")
    db = Database(source / "catalogue.jvvv")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.status == "completed"
        assert result.files_seen == 1
        assert [row["name"] for row in db.connection.execute("SELECT name FROM files")] == [
            "user.bin"
        ]
    finally:
        db.close()


def test_scan_persists_builtin_wave_media_details(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    audio_path = source / "tone.wav"
    with wave.open(str(audio_path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00\x00\x00" * 800)

    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db).scan(volume_id)

        assert result.media_files == 1
        assert result.media_metadata_collected == 1
        file_id = db.connection.execute("SELECT id FROM files").fetchone()["id"]
        media = db.get_file_media_metadata(file_id)
        assert media["status"] == "complete"
        assert media["source"] == "python-wave"
        assert media["duration_ms"] == 100
        assert media["audio_codecs"] == "pcm_s16le"
        assert media["sample_rate_hz"] == 8000
        assert media["channels"] == 2
        assert media["bit_rate"] == 256000
    finally:
        db.close()


def test_unchanged_media_keeps_previous_details_when_latest_probe_fails(tmp_path):
    source = tmp_path / "drive"
    source.mkdir()
    media_path = source / "clip.mp4"
    media_path.write_bytes(b"unchanged video content")
    complete = MediaMetadata(
        status="complete",
        media_kind="video",
        source="ffprobe",
        container_format="mov,mp4",
        duration_ms=2500,
        width=1920,
        height=1080,
        video_codecs=("h264",),
        bit_rate=7_500_000,
    )
    unavailable = MediaMetadata(
        status="unavailable",
        media_kind="video",
        source="ffprobe",
        message="ffprobe was temporarily unavailable.",
    )
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        volume_id = db.create_volume("Drive", str(source))
        first = VolumeScanner(
            db,
            media_extractor=SimpleNamespace(inspect=lambda *_args, **_kwargs: complete),
        ).scan(volume_id)
        assert first.status == "completed"
        file_id = db.connection.execute("SELECT id FROM files").fetchone()["id"]
        first_media = db.get_file_media_metadata(file_id)
        first_probed_at = first_media["probed_at"]

        second = VolumeScanner(
            db,
            media_extractor=SimpleNamespace(inspect=lambda *_args, **_kwargs: unavailable),
        ).scan(volume_id)
        retained = db.get_file_media_metadata(file_id)

        assert second.status == "completed"
        assert second.media_files == 1
        assert second.media_metadata_collected == 0
        assert retained["status"] == "partial"
        assert retained["duration_ms"] == 2500
        assert (retained["width"], retained["height"]) == (1920, 1080)
        assert retained["video_codecs"] == "h264"
        assert retained["bit_rate"] == 7_500_000
        assert retained["probed_at"] == first_probed_at
        assert "Previously collected details were retained" in retained["message"]
        assert "temporarily unavailable" in retained["message"]

        media_path.write_bytes(b"different video content")
        third = VolumeScanner(
            db,
            media_extractor=SimpleNamespace(inspect=lambda *_args, **_kwargs: unavailable),
        ).scan(volume_id)
        replaced = db.get_file_media_metadata(file_id)

        assert third.status == "completed"
        assert replaced["status"] == "unavailable"
        assert replaced["duration_ms"] is None
        assert replaced["width"] is None
        assert "Previously collected" not in replaced["message"]
    finally:
        db.close()
