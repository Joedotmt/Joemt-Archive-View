"""Catalogue-wide algorithm choice, persistence, and full scan integration."""
import sqlite3

import pytest

from jvvv.backup_analysis import BackupAnalysisEngine
from jvvv.catalogue_format import CATALOGUE_DB_NAME, INDEX_DB_NAME
from jvvv.content_hash import HASH_LENGTHS, new_hasher
from jvvv.database import CatalogueError, Database, open_catalogue
from jvvv.preview_cache import PreviewCache
from jvvv.preview_config import ImagePreviewProfile, VideoPreviewProfile
from jvvv.preview_service import inspect_preview_file
from jvvv.snapshot_repository import SnapshotManifest, SnapshotRepository
from test_preview_scanner import make_scanner, make_service, write_test_image

ALGORITHMS = tuple(HASH_LENGTHS)


def digest_for(algorithm, data):
    hasher = new_hasher(algorithm)
    hasher.update(data)
    return hasher.digest()


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_choice_locks_on_first_volume_and_survives_delete_reopen_restore(tmp_path, algorithm):
    path = tmp_path / "archive.jvvv"
    db = Database(path)
    try:
        assert db.hash_algorithm == "sha256"
        assert not db.hash_algorithm_locked
        db.set_catalogue_meta("hash_algorithm", algorithm)
        assert db.hash_algorithm == algorithm
        volume_id = db.create_volume("Drive", str(tmp_path))
        assert db.hash_algorithm_locked
        other = next(a for a in ALGORITHMS if a != algorithm)
        with pytest.raises(CatalogueError, match="permanently locked"):
            db.set_hash_algorithm(other)
        with pytest.raises(CatalogueError, match="cannot be changed manually"):
            db.set_catalogue_meta("hash_algorithm_locked", "0")
        db.delete_volume(volume_id)
        with pytest.raises(CatalogueError, match="permanently locked"):
            db.set_catalogue_meta("hash_algorithm", other)
    finally:
        db.close()
    db = open_catalogue(path)
    try:
        assert db.hash_algorithm == algorithm
        assert db.hash_algorithm_locked
    finally:
        db.close()
    repository = SnapshotRepository.create(tmp_path / "history.jvvvrepo")
    snapshot = repository.create_snapshot(path).manifest
    restored = tmp_path / "restored.jvvv"
    repository.restore_snapshot(snapshot.snapshot_id, restored)
    db = open_catalogue(restored)
    try:
        assert db.hash_algorithm == algorithm
        assert db.hash_algorithm_locked
        with pytest.raises(CatalogueError, match="permanently locked"):
            db.set_hash_algorithm(other)
    finally:
        db.close()


def test_empty_catalogue_refreshes_choice_from_another_connection(tmp_path):
    first = Database(tmp_path / "archive.jvvv")
    second = open_catalogue(first.path)
    try:
        assert first.hash_algorithm == "sha256"
        second.set_hash_algorithm("xxh3_128")
        assert first.hash_algorithm == "xxh3_128"
        second.set_hash_algorithm("blake3")
        second.create_volume("Drive", str(tmp_path))
        assert first.hash_algorithm == "blake3"
        with pytest.raises(CatalogueError):
            first.set_hash_algorithm("sha256")
    finally:
        second.close()
        first.close()


def test_legacy_catalogue_defaults_to_locked_sha256(tmp_path):
    path = tmp_path / "legacy.jvvv"
    db = Database(path)
    db.create_volume("Drive", str(tmp_path))
    db.close()
    with sqlite3.connect(path / CATALOGUE_DB_NAME) as connection:
        connection.execute("DELETE FROM catalogue_meta WHERE key IN ('hash_algorithm', 'hash_algorithm_locked')")
        connection.execute("UPDATE catalogue_meta SET value = '1' WHERE key = 'format_version'")
        connection.execute("PRAGMA user_version = 1")
    db = open_catalogue(path)
    try:
        assert db.hash_algorithm == "sha256"
        assert db.hash_algorithm_locked
        with pytest.raises(CatalogueError):
            db.set_hash_algorithm("blake3")
    finally:
        db.close()


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_scan_preview_backup_and_snapshot_use_catalogue_algorithm(tmp_path, algorithm):
    path = tmp_path / "archive.jvvv"
    source = tmp_path / "drive"
    source.mkdir()
    image = source / "photo.png"
    write_test_image(image, 32, 24, "png")
    expected = digest_for(algorithm, image.read_bytes())
    db = Database(path)
    try:
        db.set_hash_algorithm(algorithm)
        first = db.create_volume("Original", str(source))
        second = db.create_volume("Backup", str(source))
        service, cache = make_service(tmp_path)
        result = make_scanner(db, service).scan(first)
        assert result.files_hashed == 1
        assert result.preview["image_generated"] == 1
        result = make_scanner(db, service).scan(second)
        assert result.preview["image_reused"] == 1
        row = db.connection.execute("SELECT * FROM files WHERE volume_id = ?", (first,)).fetchone()
        assert row["content_hash_algorithm"] == algorithm
        assert row["content_hash"] == expected
        assert len(expected) == HASH_LENGTHS[algorithm]
        preview = cache.preview_path("image", expected)
        assert preview.is_file()
        info = inspect_preview_file(service.settings, "image", expected, algorithm)
        assert info.path == preview
        assert info.exists and info.valid
        statuses = db.connection.execute("SELECT source_hash FROM file_preview_status").fetchall()
        assert len(statuses) == 2
        assert all(status[0] == expected for status in statuses)
        db.prepare_content_hash_lookup(["png"])
        assert db.content_hash_referenced(expected, algorithm)
        assert not db.content_hash_referenced(expected, next(a for a in ALGORITHMS if a != algorithm))
        db.drop_content_hash_lookup()
        db.connection.commit()
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        run_id = engine.state().active_run_id
        assert engine.locations("file", row["id"], second, run_id)
    finally:
        db.close()
    (path / INDEX_DB_NAME).unlink()
    db = open_catalogue(path)
    try:
        assert db.hash_algorithm == algorithm
        assert db.connection.execute("SELECT content_hash FROM files LIMIT 1").fetchone()[0] == expected
    finally:
        db.close()
    repository = SnapshotRepository.create(tmp_path / "history.jvvvrepo")
    snapshot = repository.create_snapshot(path).manifest
    restored = tmp_path / "restored.jvvv"
    repository.restore_snapshot(snapshot.snapshot_id, restored)
    db = open_catalogue(restored)
    try:
        assert db.hash_algorithm == algorithm
        assert db.hash_algorithm_locked
        assert db.connection.execute("SELECT content_hash FROM files LIMIT 1").fetchone()[0] == expected
    finally:
        db.close()


def test_preview_names_separate_algorithms_and_preserve_legacy_sha(tmp_path):
    paths = []
    for algorithm in ALGORITHMS:
        cache = PreviewCache(tmp_path, ImagePreviewProfile(), VideoPreviewProfile(), hash_algorithm=algorithm)
        digest = b"a" * HASH_LENGTHS[algorithm]
        path = cache.preview_path("image", digest)
        cache.ensure_parent(path)
        path.write_bytes(b"preview")
        paths.append(path)
        assert PreviewCache.parse_content_preview_name(path.name)[0] == algorithm
    assert len(set(paths)) == 3
    assert paths[0].name == (b"a" * 32).hex() + ".jpg"
    cache = PreviewCache(tmp_path, ImagePreviewProfile(), VideoPreviewProfile())
    entries = list(cache.iter_previews("image", cache.profile_id("image")))
    assert {entry.hash_algorithm for entry in entries} == set(ALGORITHMS)


def test_legacy_snapshot_defaults_to_sha256(tmp_path):
    db = Database(tmp_path / "archive.jvvv")
    db.close()
    repository = SnapshotRepository.create(tmp_path / "history.jvvvrepo")
    manifest = repository.create_snapshot(tmp_path / "archive.jvvv").manifest
    values = manifest.to_json()
    values["format_version"] = 1
    values["catalogue"].pop("hash_algorithm")
    values["catalogue"].pop("hash_algorithm_locked")
    legacy = SnapshotManifest.from_json(values)
    assert legacy.hash_algorithm == "sha256"


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_backend_rejects_mixed_algorithms_and_invalid_digest_sizes(tmp_path, algorithm):
    db = Database(tmp_path / "archive.jvvv")
    try:
        db.set_hash_algorithm(algorithm)
        volume = db.create_volume("Drive", str(tmp_path))
        with db.transaction():
            folder = db.ensure_folder(volume_id=volume, parent_id=None, name="Drive", relative_path="", scanned_at="2026-10-07")
            arguments = dict(volume_id=volume, folder_id=folder, name="file", relative_path="file", extension="", size_bytes=1, modified_at=None, scanned_at="2026-10-07")
            other = next(a for a in ALGORITHMS if a != algorithm)
            with pytest.raises(ValueError, match="must match the catalogue"):
                db.upsert_file(**arguments, content_hash=b"a" * HASH_LENGTHS[other], content_hash_algorithm=other)
            with pytest.raises(ValueError, match="exactly"):
                db.upsert_file(**arguments, content_hash=b"short", content_hash_algorithm=algorithm)
        with pytest.raises(ValueError, match="Unsupported"):
            db.set_hash_algorithm("unknown")
    finally:
        db.close()


def test_read_only_catalogue_cannot_change_algorithm(tmp_path):
    path = tmp_path / "archive.jvvv"
    db = Database(path)
    db.close()
    db = Database(path, initialize=False, create=False, read_only=True)
    try:
        with pytest.raises(CatalogueError, match="read-only"):
            db.set_hash_algorithm("blake3")
    finally:
        db.close()


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_hash_stream_matches_library_digest(algorithm):
    import hashlib
    import blake3
    import xxhash

    constructors = {"sha256": hashlib.sha256, "blake3": blake3.blake3, "xxh3_128": xxhash.xxh3_128}
    data = bytes(range(256)) * 100
    hasher = new_hasher(algorithm)
    for start in range(0, len(data), 113):
        hasher.update(data[start:start + 113])
    assert hasher.digest() == constructors[algorithm](data).digest()


def test_saved_hash_comparison_uses_algorithm_and_checks_digest_length():
    from jvvv.backup_analysis import _hash_key

    digest = b"a" * 32
    assert _hash_key("SHA256", digest) == _hash_key("sha256", digest)
    assert _hash_key("sha256", digest) != _hash_key("blake3", digest)
    assert _hash_key("xxh3_128", digest) is None
    assert _hash_key("sha256", None) is None
