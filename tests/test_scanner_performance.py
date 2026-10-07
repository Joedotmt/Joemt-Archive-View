"""Regression coverage for scan work reuse without skipping content verification."""
from jvvv.content_hash import new_hasher
import os
from types import SimpleNamespace

import pytest

from jvvv.database import Database
from jvvv.media_metadata import MediaMetadata
from jvvv.scanner import VolumeScanner


@pytest.mark.parametrize("algorithm", ["sha256", "blake3", "xxh3_128"])
def test_complete_media_is_reused_only_after_full_hash_verification(tmp_path, algorithm):
    source = tmp_path / "drive"
    source.mkdir()
    path = source / "clip.mp4"
    path.write_bytes(b"first content")
    calls = []

    def inspect(path, **kwargs):
        calls.append(path)
        return MediaMetadata(status="complete", media_kind="video", duration_ms=1000)

    db = Database(tmp_path / "catalogue.jvvv")
    try:
        db.set_hash_algorithm(algorithm)
        volume_id = db.create_volume("Drive", str(source))
        scanner = VolumeScanner(db, media_extractor=SimpleNamespace(inspect=inspect))
        first = scanner.scan(volume_id)
        file_id = db.connection.execute("SELECT id FROM files").fetchone()[0]
        before = dict(db.get_file_media_metadata(file_id))
        second = scanner.scan(volume_id)
        assert len(calls) == 1
        assert second.media_metadata_reused == 1
        assert second.media_metadata_collected == 1
        assert first.files_hashed == second.files_hashed == 1
        assert first.bytes_hashed == second.bytes_hashed == len(b"first content")
        assert dict(db.get_file_media_metadata(file_id)) == before
        assert set(second.phase_seconds) == {"hashing", "media", "indexing", "previews", "total"}
        assert all(value >= 0 for value in second.phase_seconds.values())

        previous_stat = path.stat()
        path.write_bytes(b"other content")
        os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        third = scanner.scan(volume_id)
        assert len(calls) == 2
        assert third.media_metadata_reused == 0
        assert third.changes.files_changed == 1
        row = db.connection.execute("SELECT content_hash, identity_inode FROM files").fetchone()
        hasher = new_hasher(algorithm)
        hasher.update(b"other content")
        assert row["content_hash"] == hasher.digest()
        assert row["identity_inode"] == path.stat().st_ino
    finally:
        db.close()


@pytest.mark.parametrize("old_status", ["partial", "failed", "unavailable"])
def test_incomplete_media_details_are_retried(tmp_path, old_status):
    source = tmp_path / "drive"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"same bytes")
    results = iter([
        MediaMetadata(status=old_status, media_kind="video"),
        MediaMetadata(status="complete", media_kind="video", duration_ms=5000),
    ])
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id = db.create_volume("Drive", str(source))
        scanner = VolumeScanner(db, media_extractor=SimpleNamespace(inspect=lambda *a, **k: next(results)))
        scanner.scan(volume_id)
        result = scanner.scan(volume_id)
        assert result.media_metadata_reused == 0
        row = db.connection.execute("SELECT * FROM file_media_metadata").fetchone()
        assert row["status"] == "complete"
        assert row["duration_ms"] == 5000
    finally:
        db.close()


def test_many_small_files_do_not_flood_progress_notifications(tmp_path, monkeypatch):
    source = tmp_path / "drive"
    source.mkdir()
    for index in range(80):
        (source / str(index)).mkdir()
        (source / str(index) / "file.txt").write_bytes(b"contents")
    # A scan completed within one update interval should still force phase
    # transitions, but not queue one UI event for every directory.
    monkeypatch.setattr("jvvv.scanner.time", SimpleNamespace(monotonic=lambda: 1.0))
    events = []
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id = db.create_volume("Drive", str(source))
        result = VolumeScanner(db, progress_callback=lambda *args: events.append(args), batch_size=1).scan(volume_id)
        assert result.files_seen == result.files_hashed == 80
        assert len(events) <= 4
        assert any("Preparing folder sizes" in event[2] for event in events)
    finally:
        db.close()
