from __future__ import annotations

import hashlib
import json
import sqlite3
import warnings
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_LZMA, ZipFile

import pytest

import jvvv.catalogue_backup as catalogue_backup
from jvvv.backup_analysis import BackupAnalysisEngine
from jvvv.catalogue_backup import (
    BackupInspection,
    BackupResult,
    CatalogueBackupError,
    InvalidBackupError,
    RestoreResult,
    UnsupportedBackupError,
    create_catalogue_backup,
    restore_catalogue_backup,
    validate_catalogue_backup,
)
from jvvv.database import Database, SCHEMA_VERSION


ARCHIVE_MEMBERS = {"manifest.json", "source.sqlite"}
V2_ARCHIVE_MEMBERS = ARCHIVE_MEMBERS | {"analysis.sqlite"}
SHARED_HASH = bytes(range(32))
UNIQUE_HASH = bytes(reversed(range(32)))
SOURCE_TABLES = {
    "volumes": (
        "id",
        "name",
        "source_path",
        "identity_kind",
        "identity_token",
        "identity_label",
        "identity_serial",
        "identity_filesystem",
        "source_relative_path",
        "capacity_bytes",
        "used_bytes",
        "free_bytes",
        "indexed_file_count",
        "indexed_folder_count",
        "last_scan_at",
        "created_at",
        "updated_at",
    ),
    "volume_register": (
        "volume_id",
        "drive_id",
        "is_mirror",
        "status",
        "condition",
        "description",
        "earliest_content_date",
        "latest_content_date",
        "connector",
        "date_added",
        "retired_date",
        "mirror_date",
        "master_volume_id",
        "created_at",
        "updated_at",
    ),
    "folders": (
        "id",
        "volume_id",
        "parent_id",
        "name",
        "relative_path",
        "missing",
        "scanned_at",
        "modified_at",
        "recursive_size_bytes",
        "recursive_file_count",
        "recursive_subfolder_count",
        "direct_file_count",
        "direct_subfolder_count",
        "stats_updated_at",
    ),
    "files": (
        "id",
        "volume_id",
        "folder_id",
        "name",
        "relative_path",
        "extension",
        "size_bytes",
        "modified_at",
        "missing",
        "scanned_at",
        "identity_device",
        "identity_inode",
        "content_hash",
        "content_hash_algorithm",
    ),
    "file_media_metadata": (
        "file_id",
        "status",
        "media_kind",
        "source",
        "container",
        "duration_ms",
        "width",
        "height",
        "video_codecs",
        "audio_codecs",
        "sample_rate_hz",
        "channels",
        "bit_rate",
        "message",
        "probed_at",
    ),
    "scan_history": (
        "id",
        "volume_id",
        "started_at",
        "finished_at",
        "status",
        "files_seen",
        "folders_seen",
        "errors_count",
        "message",
        "files_added",
        "files_removed",
        "files_changed",
        "folders_added",
        "folders_removed",
        "bytes_before",
        "bytes_after",
        "files_hashed",
        "bytes_hashed",
        "hash_errors",
        "media_files",
        "media_metadata_collected",
    ),
    "scan_errors": (
        "id",
        "scan_id",
        "volume_id",
        "path",
        "message",
        "created_at",
    ),
}
PRIMARY_KEYS = {
    "volumes": "id",
    "volume_register": "volume_id",
    "folders": "id",
    "files": "id",
    "file_media_metadata": "file_id",
    "scan_history": "id",
    "scan_errors": "id",
}
AUTOINCREMENT_TABLES = (
    "volumes",
    "folders",
    "files",
    "scan_history",
    "scan_errors",
    "backup_analysis_runs",
    "backup_analysis_invalidations",
)
EXPECTED_ROW_COUNTS = {
    "volumes": 2,
    "volume_register": 2,
    "folders": 4,
    "files": 4,
    "file_media_metadata": 2,
    "scan_history": 2,
    "scan_errors": 2,
}


def _create_representative_catalogue(path: Path) -> Path:
    db = Database(path)
    try:
        with db.transaction(immediate=True) as connection:
            connection.executemany(
                """
                INSERT INTO volumes (
                    id, name, source_path, identity_kind, identity_token,
                    identity_label, identity_serial, identity_filesystem,
                    source_relative_path, capacity_bytes, used_bytes, free_bytes,
                    indexed_file_count, indexed_folder_count, last_scan_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        7,
                        "Primary Ω archive",
                        r"E:\Archive Root",
                        "windows-volume",
                        "volume:{11111111-2222-3333-4444-555555555555}",
                        "ARCHIVE_Ω",
                        "SER-0007",
                        "NTFS",
                        "Media/2026",
                        8_000_000_000_000,
                        4_500_000_000_000,
                        3_500_000_000_000,
                        999,
                        888,
                        "2026-08-20T10:11:12.123456+0000",
                        "2024-01-02T03:04:05.000006+0000",
                        "2026-08-21T12:13:14.000015+0000",
                    ),
                    (
                        12,
                        None,
                        r"\\server\offsite\mirror",
                        "unc",
                        "server/offsite",
                        "",
                        "",
                        "zfs",
                        "",
                        9_000_000_000_000,
                        111,
                        8_999_999_999_889,
                        777,
                        666,
                        "2026-08-22T01:02:03.000004+0000",
                        "2025-02-03T04:05:06.000007+0000",
                        "2026-08-23T05:06:07.000008+0000",
                    ),
                ],
            )
            connection.executemany(
                """
                INSERT INTO volume_register (
                    volume_id, drive_id, is_mirror, status, condition,
                    description, earliest_content_date, latest_content_date,
                    connector, date_added, retired_date, mirror_date,
                    master_volume_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        7,
                        "AID-007",
                        0,
                        "In Use",
                        "Good",
                        "Line one\nLine two — Ω \x00 tail",
                        "1999-12-31",
                        "2026-08-20",
                        "USB-C",
                        "2024-01-02",
                        None,
                        None,
                        None,
                        "2024-01-02T03:04:05.000006+0000",
                        "2026-08-20T10:11:12.123456+0000",
                    ),
                    (
                        12,
                        "AID-012",
                        1,
                        "Archive",
                        "Fair",
                        "Off-site mirror",
                        None,
                        None,
                        "Network",
                        "2025-02-03",
                        None,
                        "2026-03-04",
                        7,
                        "2025-02-03T04:05:06.000007+0000",
                        "2026-08-23T05:06:07.000008+0000",
                    ),
                ],
            )
            connection.executemany(
                """
                INSERT INTO folders (
                    id, volume_id, parent_id, name, relative_path, missing,
                    scanned_at, modified_at, recursive_size_bytes,
                    recursive_file_count, recursive_subfolder_count,
                    direct_file_count, direct_subfolder_count, stats_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (101, 7, None, "Primary Ω archive", "", 0, "scan-a", None, 91_001, 91, 92, 93, 94, "bad-stats-a"),
                    (102, 7, 101, "Prøjects", "Prøjects", 0, "scan-a", "2026-01-01T00:00:00.000000+0000", 92_001, 81, 82, 83, 84, "bad-stats-b"),
                    (103, 7, 101, "Gone", "Gone", 1, "old-scan", None, 93_001, 71, 72, 73, 74, "bad-stats-c"),
                    (201, 12, None, "AID-012", "", 0, "scan-b", None, 94_001, 61, 62, 63, 64, "bad-stats-d"),
                ],
            )
            connection.executemany(
                """
                INSERT INTO files (
                    id, volume_id, folder_id, name, relative_path, extension,
                    size_bytes, modified_at, missing, scanned_at,
                    identity_device, identity_inode, content_hash,
                    content_hash_algorithm
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1001, 7, 102, "shared clip.mov", "Prøjects/shared clip.mov", ".mov", 101, "2026-04-05T06:07:08.000009+0000", 0, "scan-a", 44, 55, SHARED_HASH, "sha256"),
                    (1002, 7, 102, "résumé 💾.txt", "Prøjects/résumé 💾.txt", ".txt", 0, None, 0, "scan-a", None, None, None, None),
                    (1003, 7, 103, "missing.bin", "Gone/missing.bin", ".bin", 333, "2001-02-03T04:05:06.000007+0000", 1, "old-scan", -9, 123, UNIQUE_HASH, "sha256"),
                    (2001, 12, 201, "shared clip.mov", "shared clip.mov", ".mov", 101, "2026-04-05T06:07:08.000009+0000", 0, "scan-b", 66, 77, SHARED_HASH, "sha256"),
                ],
            )
            connection.executemany(
                """
                INSERT INTO file_media_metadata (
                    file_id, status, media_kind, source, container,
                    duration_ms, width, height, video_codecs, audio_codecs,
                    sample_rate_hz, channels, bit_rate, message, probed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1001, "complete", "video", "ffprobe", "mov,mp4", 12_345, 3840, 2160, "h264,hevc", "aac", 48_000, 6, 9_999_999, "", "2026-08-20T10:00:00.000001+0000"),
                    (1002, "unavailable", "", "extension", None, None, None, None, None, None, None, None, None, "Not media; user-visible diagnostic Ω", None),
                ],
            )
            connection.executemany(
                """
                INSERT INTO scan_history (
                    id, volume_id, started_at, finished_at, status,
                    files_seen, folders_seen, errors_count, message,
                    files_added, files_removed, files_changed, folders_added,
                    folders_removed, bytes_before, bytes_after, files_hashed,
                    bytes_hashed, hash_errors, media_files,
                    media_metadata_collected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (301, 7, "2026-08-20T09:00:00.000000+0000", "2026-08-20T10:00:00.000000+0000", "completed", 3, 3, 1, "Completed with one unreadable path", 2, 1, 1, 2, 1, 444, 434, 2, 202, 1, 1, 1),
                    (305, 12, "2026-08-22T01:00:00.000000+0000", None, "cancelled", 1, 1, 1, None, None, None, None, None, None, None, None, 1, 101, 0, 1, 1),
                ],
            )
            connection.executemany(
                """
                INSERT INTO scan_errors (
                    id, scan_id, volume_id, path, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (401, 301, 7, r"System Volume Information\secret", "[WinError 5] Access denied", "2026-08-20T09:30:00.000000+0000"),
                    (409, None, 12, "Prøjects/💥", "Detached diagnostic\nwith details", "2026-08-22T01:01:00.000000+0000"),
                ],
            )

            # These rows deliberately represent bulky, stale, reproducible state.
            # A semantic backup must not carry them into the payload.
            connection.execute(
                """
                INSERT INTO backup_analysis_runs (
                    id, started_at, completed_at, status, rules_version,
                    source_signature, files_analyzed, folders_analyzed,
                    likely_files, message
                ) VALUES (900, 'analysis-start', 'analysis-end', 'completed',
                          1, 'stale-signature', 4000000, 300000, 123, 'old rules')
                """
            )
            connection.execute(
                """
                UPDATE backup_analysis_state
                SET active_run_id = 900, forced_stale = 1,
                    stale_reason = 'deliberately stale', updated_at = 'old-state'
                WHERE id = 1
                """
            )
            connection.execute(
                """
                INSERT INTO backup_analysis_volume_snapshots
                    (run_id, volume_id, drive_id, last_scan_at,
                     indexed_file_count, indexed_folder_count)
                VALUES (900, 7, 'AID-007', 'old-scan', 999, 999)
                """
            )
            connection.execute(
                """
                INSERT INTO backup_file_results
                    (run_id, file_id, volume_id, status, other_volume_ids,
                     evidence_text)
                VALUES (900, 1001, 7, 'likely', '[12]', 'old evidence')
                """
            )
            connection.execute(
                """
                INSERT INTO backup_folder_results
                    (run_id, folder_id, volume_id, status, other_volume_ids,
                     evidence_text)
                VALUES (900, 102, 7, 'possible', '[12]', 'old evidence')
                """
            )
            connection.execute(
                """
                INSERT INTO backup_folder_drive_matches
                    (run_id, folder_id, target_volume_id, status, evidence_text)
                VALUES (900, 102, 12, 'possible', 'old evidence')
                """
            )
            connection.execute(
                """
                INSERT INTO backup_volume_results
                    (run_id, volume_id, status, health_status)
                VALUES (900, 7, 'complete', 'healthy')
                """
            )
            connection.execute(
                """
                INSERT INTO backup_mirror_candidates
                    (run_id, source_volume_id, target_volume_id, evidence_text)
                VALUES (900, 7, 12, 'old evidence')
                """
            )
            connection.execute(
                """
                INSERT INTO backup_analysis_invalidations
                    (id, volume_id, reason, created_at)
                VALUES (901, 7, 'old invalidation', 'old-time')
                """
            )

            for table, sequence in {
                "volumes": 50,
                "folders": 500,
                "files": 5_000,
                "scan_history": 600,
                "scan_errors": 700,
                "backup_analysis_runs": 950,
                "backup_analysis_invalidations": 960,
            }.items():
                connection.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                    (sequence, table),
                )
    finally:
        db.close()
    return path


@pytest.fixture
def representative_catalogue(tmp_path: Path) -> Path:
    return _create_representative_catalogue(tmp_path / "representative.jvvv")


def _semantic_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: list(
                connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} "
                    f"ORDER BY {PRIMARY_KEYS[table]}"
                )
            )
            for table, columns in SOURCE_TABLES.items()
        }
    finally:
        connection.close()


def _sequences(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        placeholders = ",".join("?" for _ in AUTOINCREMENT_TABLES)
        return {
            str(name): int(sequence)
            for name, sequence in connection.execute(
                f"SELECT name, seq FROM sqlite_sequence "
                f"WHERE name IN ({placeholders}) ORDER BY name",
                AUTOINCREMENT_TABLES,
            )
        }
    finally:
        connection.close()


def _read_archive(path: Path) -> tuple[dict[str, object], bytes]:
    with ZipFile(path) as archive:
        return json.loads(archive.read("manifest.json")), archive.read("source.sqlite")


def _write_archive(path: Path, members: list[tuple[str, bytes]]) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(path, "w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
            for name, data in members:
                archive.writestr(name, data)


def _write_mixed_archive(
    path: Path,
    members: list[tuple[str, bytes, int]],
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(path, "w", allowZip64=True) as archive:
            for name, data, compression in members:
                archive.writestr(name, data, compress_type=compression)


def _component(manifest: dict[str, object], path: str) -> dict[str, object]:
    components = manifest["components"]
    assert isinstance(components, list)
    component = next(
        item
        for item in components
        if isinstance(item, dict) and item.get("path") == path
    )
    return component


def _manifest_with_payload_integrity(
    manifest: dict[str, object], payload: bytes
) -> dict[str, object]:
    changed = json.loads(json.dumps(manifest))
    components = changed["components"]
    assert isinstance(components, list) and len(components) == 1
    payload_description = components[0]
    assert isinstance(payload_description, dict)
    payload_description["size"] = len(payload)
    payload_description["sha256"] = hashlib.sha256(payload).hexdigest()
    return changed


def _clear_backup_analysis(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("UPDATE backup_analysis_state SET active_run_id = NULL")
        connection.execute("DELETE FROM backup_analysis_runs")
        connection.execute("DELETE FROM backup_analysis_invalidations")
        connection.commit()
    finally:
        connection.close()


def _analysis_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    order_by = {
        "backup_analysis_runs": "id",
        "backup_analysis_state": "id",
        "backup_analysis_volume_snapshots": "run_id, volume_id",
        "backup_file_results": "run_id, file_id",
        "backup_folder_results": "run_id, folder_id",
        "backup_folder_drive_matches": "run_id, folder_id, target_volume_id",
        "backup_volume_results": "run_id, volume_id",
        "backup_mirror_candidates": "run_id, source_volume_id, target_volume_id",
        "backup_analysis_invalidations": "id",
    }
    connection = sqlite3.connect(path)
    try:
        return {
            table: list(connection.execute(f"SELECT * FROM {table} ORDER BY {ordering}"))
            for table, ordering in order_by.items()
        }
    finally:
        connection.close()


def test_backup_roundtrip_is_semantically_lossless_and_regenerates_derived_data(
    representative_catalogue: Path,
    tmp_path: Path,
):
    backup_path = tmp_path / "representative.zip"
    restored_path = tmp_path / "restored.jvvv"
    expected_source = _semantic_snapshot(representative_catalogue)
    expected_sequences = _sequences(representative_catalogue)
    original_size = representative_catalogue.stat().st_size

    backup = create_catalogue_backup(representative_catalogue, backup_path)

    assert isinstance(backup, BackupResult)
    assert backup.source_path == representative_catalogue
    assert backup.backup_path == backup_path
    assert backup.original_size == original_size
    assert backup.backup_size == backup_path.stat().st_size
    assert backup.payload_size > 0
    assert backup.backup_size < backup.original_size
    assert backup.savings_bytes == backup.original_size - backup.backup_size
    assert backup.savings_percent == pytest.approx(
        (backup.savings_bytes * 100.0) / backup.original_size
    )
    assert EXPECTED_ROW_COUNTS.items() <= backup.table_rows.items()
    with ZipFile(backup_path) as archive:
        stale_manifest = json.loads(archive.read("manifest.json"))
        assert set(archive.namelist()) == ARCHIVE_MEMBERS
    assert stale_manifest["format_version"] == 1
    assert stale_manifest["reconstruction"]["backup_analysis"]["storage"] == "stored"

    restored = restore_catalogue_backup(backup_path, restored_path)

    assert isinstance(restored, RestoreResult)
    assert restored.backup_path == backup_path
    assert restored.catalogue_path == restored_path
    assert restored.backup_size == backup_path.stat().st_size
    assert restored.catalogue_size == restored_path.stat().st_size
    assert {"search indexes", "folder aggregates", "volume counts"} <= set(
        restored.regenerated_components
    )
    assert "backup evidence" not in restored.regenerated_components
    assert _semantic_snapshot(restored_path) == expected_source
    assert _sequences(restored_path) == expected_sequences

    db = Database(restored_path, create=False)
    try:
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(db.connection.execute("PRAGMA foreign_key_check")) == []
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        # Noncanonical generated values are irreducible exceptions: silently
        # normalizing them would not be a lossless restore.
        volume_counts = {
            row["id"]: (row["indexed_file_count"], row["indexed_folder_count"])
            for row in db.connection.execute(
                "SELECT id, indexed_file_count, indexed_folder_count FROM volumes"
            )
        }
        assert volume_counts == {7: (999, 888), 12: (777, 666)}
        folder_stats = db.connection.execute(
            """
            SELECT recursive_size_bytes, recursive_file_count,
                   recursive_subfolder_count, direct_file_count,
                   direct_subfolder_count, stats_updated_at
            FROM folders WHERE id = 101
            """
        ).fetchone()
        assert tuple(folder_stats) == (91_001, 91, 92, 93, 94, "bad-stats-a")

        search_ids = {(row["item_type"], row["item_id"]) for row in db.search("shared")}
        assert {("file", 1001), ("file", 2001)} <= search_ids

        # Stale/old-rule evidence cannot be reproduced exactly from the current
        # engine, so it is retained instead of being silently normalized.
        engine = BackupAnalysisEngine(db)
        state = engine.state()
        assert state.status == "outdated"
        assert state.active_run_id == 900
        assert state.stale_reason == "The matching rules changed after this analysis."
        assert db.connection.execute(
            "SELECT stale_reason FROM backup_analysis_state WHERE id = 1"
        ).fetchone()[0] == "deliberately stale"
        assert db.connection.execute(
            "SELECT evidence_text FROM backup_file_results WHERE run_id = 900"
        ).fetchone()[0] == "old evidence"
    finally:
        db.close()


def test_archive_manifest_payload_and_hash_deduplication_are_compact(
    representative_catalogue: Path,
    tmp_path: Path,
):
    backup_path = tmp_path / "catalogue.zip"
    _clear_backup_analysis(representative_catalogue)
    result = create_catalogue_backup(representative_catalogue, backup_path)

    with ZipFile(backup_path) as archive:
        infos = archive.infolist()
        assert {info.filename for info in infos} == ARCHIVE_MEMBERS
        assert len(infos) == 2
        assert all(info.compress_type == ZIP_DEFLATED for info in infos)
        payload_info = archive.getinfo("source.sqlite")
        assert payload_info.extract_version >= 45
        assert payload_info.file_size == result.payload_size
        assert payload_info.compress_size < payload_info.file_size
        manifest = json.loads(archive.read("manifest.json"))
        payload = archive.read("source.sqlite")

    assert manifest["format"] == "jvvv-semantic-backup"
    assert manifest["format_version"] == 1
    assert manifest["reconstruction"]["backup_analysis"]["storage"] == "none"
    assert manifest["catalogue_schema_version"] == SCHEMA_VERSION
    components = manifest["components"]
    assert isinstance(components, list) and len(components) == 1
    component = components[0]
    assert component == {
        **component,
        "path": "source.sqlite",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert set(component["tables"]) == set(SOURCE_TABLES) | {
        "catalogue_sequences",
        "content_blobs",
        "folder_state_exceptions",
        "volume_count_exceptions",
    }
    assert set(component["schema"]) == set(component["tables"])

    payload_path = tmp_path / "inspected-source.sqlite"
    payload_path.write_bytes(payload)
    connection = sqlite3.connect(payload_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(SOURCE_TABLES) | {"content_blobs"} <= tables
        assert not any(name.startswith(("files_fts", "folders_fts")) for name in tables)
        assert not any(name.startswith("backup_") for name in tables)
        assert connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 2
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
        persistent_indexes = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'
                """
            )
        }
        assert "content_blobs_digest_copy_index" not in persistent_indexes

        folder_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(folders)")
        }
        assert {
            "recursive_size_bytes",
            "recursive_file_count",
            "recursive_subfolder_count",
            "direct_file_count",
            "direct_subfolder_count",
            "stats_updated_at",
        }.isdisjoint(folder_columns)
        volume_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(volumes)")
        }
        assert {"indexed_file_count", "indexed_folder_count"}.isdisjoint(
            volume_columns
        )
    finally:
        connection.close()


def test_current_backup_evidence_uses_exact_lzma_accelerator_and_restores(
    representative_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_backup_analysis(representative_catalogue)
    db = Database(representative_catalogue, create=False)
    try:
        assert BackupAnalysisEngine(db).analyse().status == "completed"
    finally:
        db.close()
    expected_analysis = _analysis_snapshot(representative_catalogue)
    expected_sequences = _sequences(representative_catalogue)
    expected_analysis_sequences = {
        name: sequence
        for name, sequence in expected_sequences.items()
        if name in {"backup_analysis_runs", "backup_analysis_invalidations"}
    }
    backup_path = tmp_path / "current-analysis.zip"
    restored_path = tmp_path / "current-analysis.jvvv"

    result = create_catalogue_backup(representative_catalogue, backup_path)
    with ZipFile(backup_path) as archive:
        assert set(archive.namelist()) == V2_ARCHIVE_MEMBERS
        assert archive.getinfo("source.sqlite").compress_type == ZIP_DEFLATED
        analysis_info = archive.getinfo("analysis.sqlite")
        assert analysis_info.compress_type == ZIP_LZMA
        assert analysis_info.compress_size < analysis_info.file_size
        manifest = json.loads(archive.read("manifest.json"))
        payload = archive.read("source.sqlite")
        analysis_payload = archive.read("analysis.sqlite")

    assert result.backup_size < result.original_size
    assert manifest["format_version"] == 2
    analysis_reconstruction = manifest["reconstruction"]["backup_analysis"]
    assert analysis_reconstruction["storage"] == "accelerator"
    assert analysis_reconstruction["component"] == "analysis.sqlite"
    assert analysis_reconstruction["requested"] is True
    assert analysis_reconstruction["source_was_stale"] is False
    assert {item["path"] for item in manifest["components"]} == {
        "source.sqlite",
        "analysis.sqlite",
    }
    analysis_component = _component(manifest, "analysis.sqlite")
    assert analysis_component["size"] == len(analysis_payload)
    assert analysis_component["sha256"] == hashlib.sha256(analysis_payload).hexdigest()
    expected_counts = {
        table: len(rows)
        for table, rows in expected_analysis.items()
    }
    expected_counts["analysis_sequences"] = len(expected_analysis_sequences)
    assert analysis_component["tables"] == expected_counts
    assert set(analysis_component["schema"]) == set(expected_analysis) | {
        "analysis_sequences"
    }

    payload_path = tmp_path / "current-source.sqlite"
    payload_path.write_bytes(payload)
    connection = sqlite3.connect(payload_path)
    try:
        payload_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert not any(name.startswith("backup_") for name in payload_tables)
    finally:
        connection.close()

    analysis_path = tmp_path / "current-analysis.sqlite"
    analysis_path.write_bytes(analysis_payload)
    connection = sqlite3.connect(analysis_path)
    try:
        analysis_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        assert analysis_tables == set(expected_analysis) | {"analysis_sequences"}
        for table, columns in catalogue_backup.ANALYSIS_TABLE_COLUMNS.items():
            actual_columns = tuple(
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            assert actual_columns == columns
            assert analysis_component["schema"][table] == list(columns)
            assert connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0] == expected_counts[table]
        assert analysis_component["schema"]["analysis_sequences"] == ["name", "seq"]
        assert dict(
            connection.execute(
                "SELECT name, seq FROM analysis_sequences ORDER BY name"
            )
        ) == expected_analysis_sequences
        assert list(
            connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'
                """
            )
        ) == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()

    inspection = validate_catalogue_backup(backup_path)
    assert inspection.manifest["format_version"] == 2
    assert inspection.analysis_payload_size == len(analysis_payload)

    def unexpected_reanalysis(*_args, **_kwargs):
        raise AssertionError("accelerated restore recomputed backup evidence")

    monkeypatch.setattr(BackupAnalysisEngine, "analyse", unexpected_reanalysis)
    restore_catalogue_backup(backup_path, restored_path)

    assert _analysis_snapshot(restored_path) == expected_analysis
    assert _sequences(restored_path) == expected_sequences


def _create_current_analysis_backup(source: Path, backup: Path) -> None:
    _clear_backup_analysis(source)
    db = Database(source, create=False)
    try:
        assert BackupAnalysisEngine(db).analyse().status == "completed"
    finally:
        db.close()
    create_catalogue_backup(source, backup)


def test_v1_no_analysis_archive_remains_validate_and_restore_compatible(
    representative_catalogue: Path,
    tmp_path: Path,
):
    _clear_backup_analysis(representative_catalogue)
    expected = _semantic_snapshot(representative_catalogue)
    backup_path = tmp_path / "legacy-v1.zip"
    restored_path = tmp_path / "legacy-v1.jvvv"

    create_catalogue_backup(representative_catalogue, backup_path)

    with ZipFile(backup_path) as archive:
        assert set(archive.namelist()) == ARCHIVE_MEMBERS
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format_version"] == 1
    assert manifest["reconstruction"]["backup_analysis"]["storage"] == "none"
    assert validate_catalogue_backup(backup_path).manifest["format_version"] == 1
    restore_catalogue_backup(backup_path, restored_path)
    assert _semantic_snapshot(restored_path) == expected


def test_tampered_v2_analysis_accelerator_is_rejected_atomically(
    representative_catalogue: Path,
    tmp_path: Path,
):
    valid_path = tmp_path / "accelerated-valid.zip"
    tampered_path = tmp_path / "accelerated-tampered.zip"
    target = tmp_path / "existing.jvvv"
    _create_current_analysis_backup(representative_catalogue, valid_path)
    with ZipFile(valid_path) as archive:
        manifest = archive.read("manifest.json")
        source_payload = archive.read("source.sqlite")
        analysis_payload = bytearray(archive.read("analysis.sqlite"))
    analysis_payload[len(analysis_payload) // 2] ^= 0xFF
    _write_mixed_archive(
        tampered_path,
        [
            ("source.sqlite", source_payload, ZIP_DEFLATED),
            ("analysis.sqlite", bytes(analysis_payload), ZIP_LZMA),
            ("manifest.json", manifest, ZIP_DEFLATED),
        ],
    )
    target.write_bytes(b"existing catalogue bytes")
    names_before = {item.name for item in tmp_path.iterdir()}

    with pytest.raises(InvalidBackupError, match="(?i)(checksum|integrity|hash)"):
        validate_catalogue_backup(tampered_path)
    with pytest.raises(InvalidBackupError):
        restore_catalogue_backup(tampered_path, target, overwrite=True)

    assert target.read_bytes() == b"existing catalogue bytes"
    assert {item.name for item in tmp_path.iterdir()} == names_before


def test_restore_uses_captured_backup_size_after_validated_archive_is_removed(
    representative_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    backup_path = tmp_path / "remove-after-validation.zip"
    restored_path = tmp_path / "remove-after-validation.jvvv"
    _create_current_analysis_backup(representative_catalogue, backup_path)
    expected_backup_size = backup_path.stat().st_size
    validate_to_payload = catalogue_backup._validate_archive_to_payload

    def validate_then_remove(*args, **kwargs):
        inspection = validate_to_payload(*args, **kwargs)
        backup_path.unlink()
        return inspection

    monkeypatch.setattr(
        catalogue_backup,
        "_validate_archive_to_payload",
        validate_then_remove,
    )

    result = restore_catalogue_backup(backup_path, restored_path)

    assert not backup_path.exists()
    assert result.backup_size == expected_backup_size
    assert restored_path.is_file()


@pytest.mark.parametrize(
    "archive_shape",
    ["missing", "extra", "duplicate", "unsafe", "wrong-compression"],
)
def test_v2_analysis_member_shape_and_compression_are_strictly_validated(
    archive_shape: str,
    representative_catalogue: Path,
    tmp_path: Path,
):
    valid_path = tmp_path / "accelerated-valid.zip"
    malformed_path = tmp_path / f"accelerated-{archive_shape}.zip"
    _create_current_analysis_backup(representative_catalogue, valid_path)
    with ZipFile(valid_path) as archive:
        manifest = archive.read("manifest.json")
        source_payload = archive.read("source.sqlite")
        analysis_payload = archive.read("analysis.sqlite")

    members = [
        ("source.sqlite", source_payload, ZIP_DEFLATED),
        ("analysis.sqlite", analysis_payload, ZIP_LZMA),
        ("manifest.json", manifest, ZIP_DEFLATED),
    ]
    if archive_shape == "missing":
        members = [member for member in members if member[0] != "analysis.sqlite"]
    elif archive_shape == "extra":
        members.append(("unexpected.bin", b"unexpected", ZIP_DEFLATED))
    elif archive_shape == "duplicate":
        members.append(("analysis.sqlite", analysis_payload, ZIP_LZMA))
    elif archive_shape == "unsafe":
        members.append(("../analysis.sqlite", analysis_payload, ZIP_LZMA))
    else:
        members[1] = ("analysis.sqlite", analysis_payload, ZIP_DEFLATED)
    _write_mixed_archive(malformed_path, members)

    with pytest.raises(
        InvalidBackupError,
        match="(?i)(member|component|compression|duplicate|missing|unexpected|unsafe)",
    ):
        validate_catalogue_backup(malformed_path)


def test_canonical_aggregates_are_omitted_and_rebuilt_exactly(
    representative_catalogue: Path,
    tmp_path: Path,
):
    _clear_backup_analysis(representative_catalogue)
    db = Database(representative_catalogue, create=False)
    try:
        for volume in db.connection.execute(
            "SELECT id, last_scan_at FROM volumes ORDER BY id"
        ).fetchall():
            db.rebuild_folder_statistics(
                int(volume["id"]),
                stats_updated_at=volume["last_scan_at"],
            )
        with db.transaction() as connection:
            connection.execute(
                """
                UPDATE volumes
                SET indexed_file_count = (
                        SELECT COUNT(*) FROM files
                        WHERE files.volume_id = volumes.id AND files.missing = 0
                    ),
                    indexed_folder_count = (
                        SELECT COUNT(*) FROM folders
                        WHERE folders.volume_id = volumes.id AND folders.missing = 0
                    )
                """
            )
    finally:
        db.close()
    expected = _semantic_snapshot(representative_catalogue)
    backup_path = tmp_path / "canonical.zip"
    restored_path = tmp_path / "canonical.jvvv"

    result = create_catalogue_backup(representative_catalogue, backup_path)

    assert result.table_rows["folder_state_exceptions"] == 0
    assert result.table_rows["volume_count_exceptions"] == 0
    restored = restore_catalogue_backup(backup_path, restored_path)
    assert {"folder aggregates", "volume counts"} <= set(
        restored.regenerated_components
    )
    assert _semantic_snapshot(restored_path) == expected


def test_validate_returns_checked_manifest_and_sizes(
    representative_catalogue: Path,
    tmp_path: Path,
):
    backup_path = tmp_path / "catalogue.zip"
    result = create_catalogue_backup(representative_catalogue, backup_path)

    inspection = validate_catalogue_backup(backup_path)

    assert isinstance(inspection, BackupInspection)
    assert inspection.backup_path == backup_path
    assert inspection.manifest["format"] == "jvvv-semantic-backup"
    assert inspection.manifest["format_version"] == 1
    assert inspection.archive_size == backup_path.stat().st_size
    assert inspection.payload_size == result.payload_size
    assert EXPECTED_ROW_COUNTS.items() <= inspection.table_rows.items()


def test_create_stream_verifies_without_extracting_and_revalidating_its_archive(
    representative_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Creation must not repeat the full public validation/restore read path."""
    backup_path = tmp_path / "stream-verified.zip"
    full_validator = catalogue_backup._validate_archive_to_payload

    def unexpected_full_validation(*_args, **_kwargs):
        raise AssertionError(
            "create_catalogue_backup extracted and fully revalidated its own archive"
        )

    monkeypatch.setattr(
        catalogue_backup,
        "_validate_archive_to_payload",
        unexpected_full_validation,
    )
    create_catalogue_backup(representative_catalogue, backup_path)

    # The separately invoked public validator must retain the complete path.
    monkeypatch.setattr(
        catalogue_backup,
        "_validate_archive_to_payload",
        full_validator,
    )
    inspection = validate_catalogue_backup(backup_path)
    assert inspection.backup_path == backup_path
    assert inspection.payload_size > 0


def test_create_validates_final_payload_without_scanning_omitted_source_indexes(
    representative_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Derived-index state must not force a full scan of the source database."""
    source = Database(representative_catalogue, create=False)
    try:
        source.connection.execute("INSERT INTO files_fts(files_fts) VALUES('delete-all')")
        source.connection.execute("INSERT INTO folders_fts(folders_fts) VALUES('delete-all')")
        source.connection.commit()
        assert source.search("shared") == []
    finally:
        source.close()

    integrity_check = catalogue_backup._check_sqlite_integrity
    checked_databases: list[str] = []

    def record_payload_checks(connection, database, error_type):
        if database == "original":
            raise AssertionError("creation performed a full source integrity scan")
        checked_databases.append(database)
        return integrity_check(connection, database, error_type)

    monkeypatch.setattr(
        catalogue_backup,
        "_check_sqlite_integrity",
        record_payload_checks,
    )
    backup_path = tmp_path / "derived-index.zip"
    restored_path = tmp_path / "derived-index.jvvv"

    create_catalogue_backup(representative_catalogue, backup_path)

    assert "main" in checked_databases
    restore_catalogue_backup(backup_path, restored_path)
    restored = Database(restored_path, create=False)
    try:
        search_ids = {
            (row["item_type"], row["item_id"])
            for row in restored.search("shared")
        }
        assert {("file", 1001), ("file", 2001)} <= search_ids
    finally:
        restored.close()


def test_payload_tampering_is_rejected_and_failed_restore_leaves_target_untouched(
    representative_catalogue: Path,
    tmp_path: Path,
):
    valid_path = tmp_path / "valid.zip"
    tampered_path = tmp_path / "tampered.zip"
    target = tmp_path / "existing.jvvv"
    create_catalogue_backup(representative_catalogue, valid_path)
    manifest, payload = _read_archive(valid_path)
    damaged = bytearray(payload)
    damaged[len(damaged) // 2] ^= 0xFF
    _write_archive(
        tampered_path,
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("source.sqlite", bytes(damaged)),
        ],
    )
    target.write_bytes(b"existing catalogue bytes")
    names_before = {item.name for item in tmp_path.iterdir()}

    with pytest.raises(InvalidBackupError, match="(?i)(checksum|integrity|hash)"):
        validate_catalogue_backup(tampered_path)
    with pytest.raises(InvalidBackupError):
        restore_catalogue_backup(tampered_path, target, overwrite=True)

    assert target.read_bytes() == b"existing catalogue bytes"
    assert {item.name for item in tmp_path.iterdir()} == names_before


def test_checksum_valid_but_unrestorable_payload_is_atomic(
    representative_catalogue: Path,
    tmp_path: Path,
):
    valid_path = tmp_path / "valid.zip"
    broken_path = tmp_path / "broken.zip"
    payload_path = tmp_path / "broken-source.sqlite"
    target = tmp_path / "existing.jvvv"
    create_catalogue_backup(representative_catalogue, valid_path)
    manifest, payload = _read_archive(valid_path)
    payload_path.write_bytes(payload)
    connection = sqlite3.connect(payload_path)
    try:
        # The payload schema itself permits this, but the normal catalogue's
        # case-insensitive unique volume-name constraint does not. This reaches
        # reconstruction after checksum and payload-schema validation.
        connection.execute(
            "UPDATE volumes SET name = 'PRIMARY Ω ARCHIVE' WHERE id = 12"
        )
        connection.commit()
    finally:
        connection.close()
    broken_payload = payload_path.read_bytes()
    changed_manifest = _manifest_with_payload_integrity(manifest, broken_payload)
    _write_archive(
        broken_path,
        [
            ("manifest.json", json.dumps(changed_manifest).encode("utf-8")),
            ("source.sqlite", broken_payload),
        ],
    )
    payload_path.unlink()
    target.write_bytes(b"original target")
    names_before = {item.name for item in tmp_path.iterdir()}

    validate_catalogue_backup(broken_path)
    with pytest.raises(InvalidBackupError):
        restore_catalogue_backup(broken_path, target, overwrite=True)

    assert target.read_bytes() == b"original target"
    assert {item.name for item in tmp_path.iterdir()} == names_before


@pytest.mark.parametrize(
    ("mutation", "component"),
    [
        ("UPDATE files SET modified_at = '' WHERE id = 1002", "file"),
        ("UPDATE scan_errors SET id = 410 WHERE id = 409", "scan_errors"),
    ],
)
def test_restore_validation_compares_nulls_and_source_ids_exactly(
    mutation: str,
    component: str,
    representative_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_backup_analysis(representative_catalogue)
    backup_path = tmp_path / f"exact-{component}.zip"
    target = tmp_path / f"exact-{component}.jvvv"
    create_catalogue_backup(representative_catalogue, backup_path)
    copy_source = catalogue_backup._copy_payload_to_catalogue

    def copy_then_mutate(db, *args, **kwargs):
        result = copy_source(db, *args, **kwargs)
        db.connection.execute(mutation)
        db.connection.commit()
        return result

    monkeypatch.setattr(
        catalogue_backup,
        "_copy_payload_to_catalogue",
        copy_then_mutate,
    )

    with pytest.raises(CatalogueBackupError, match=component):
        restore_catalogue_backup(backup_path, target)

    assert not target.exists()
    assert not list(tmp_path.glob("*.restoring"))


def test_unsupported_backup_version_is_distinct_from_invalid_backup(
    representative_catalogue: Path,
    tmp_path: Path,
):
    valid_path = tmp_path / "valid.zip"
    unsupported_path = tmp_path / "future.zip"
    create_catalogue_backup(representative_catalogue, valid_path)
    manifest, payload = _read_archive(valid_path)
    manifest["format_version"] = 999
    _write_archive(
        unsupported_path,
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("source.sqlite", payload),
        ],
    )

    with pytest.raises(UnsupportedBackupError, match="(?i)(version|newer|unsupported)"):
        validate_catalogue_backup(unsupported_path)


@pytest.mark.parametrize("archive_shape", ["missing", "extra", "duplicate"])
def test_noncanonical_archive_member_sets_are_rejected(
    archive_shape: str,
    representative_catalogue: Path,
    tmp_path: Path,
):
    valid_path = tmp_path / "valid.zip"
    malformed_path = tmp_path / f"{archive_shape}.zip"
    create_catalogue_backup(representative_catalogue, valid_path)
    manifest, payload = _read_archive(valid_path)
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    members = [("manifest.json", manifest_bytes), ("source.sqlite", payload)]
    if archive_shape == "missing":
        members.pop()
    elif archive_shape == "extra":
        members.append(("notes.txt", b"unexpected"))
    else:
        members.append(("manifest.json", manifest_bytes))
    _write_archive(malformed_path, members)

    with pytest.raises(
        InvalidBackupError,
        match="(?i)(member|archive|component|duplicate|missing|unexpected)",
    ):
        validate_catalogue_backup(malformed_path)


def test_create_and_restore_require_explicit_overwrite(
    representative_catalogue: Path,
    tmp_path: Path,
):
    backup_path = tmp_path / "catalogue.zip"
    backup_path.write_bytes(b"do not replace")

    with pytest.raises(FileExistsError):
        create_catalogue_backup(representative_catalogue, backup_path)
    assert backup_path.read_bytes() == b"do not replace"

    create_catalogue_backup(representative_catalogue, backup_path, overwrite=True)
    validate_catalogue_backup(backup_path)

    target = tmp_path / "catalogue.jvvv"
    target.write_bytes(b"do not replace target")
    with pytest.raises(FileExistsError):
        restore_catalogue_backup(backup_path, target)
    assert target.read_bytes() == b"do not replace target"

    restored = restore_catalogue_backup(backup_path, target, overwrite=True)
    assert restored.catalogue_path == target
    assert _semantic_snapshot(target) == _semantic_snapshot(representative_catalogue)


def test_restore_refuses_destination_with_sqlite_recovery_sidecar(
    representative_catalogue: Path,
    tmp_path: Path,
):
    backup = tmp_path / "catalogue.zip"
    target = tmp_path / "existing.jvvv"
    sidecar = Path(f"{target}-wal")
    create_catalogue_backup(representative_catalogue, backup)
    target.write_bytes(b"existing target")
    sidecar.write_bytes(b"existing recovery state")

    with pytest.raises(CatalogueBackupError, match="(?i)(recovery|open|unclean)"):
        restore_catalogue_backup(backup, target, overwrite=True)

    assert target.read_bytes() == b"existing target"
    assert sidecar.read_bytes() == b"existing recovery state"


def test_live_wal_backup_uses_one_committed_sqlite_snapshot(tmp_path: Path):
    source = tmp_path / "live.jvvv"
    backup = tmp_path / "live.zip"
    restored = tmp_path / "live-restored.jvvv"
    writer = Database(source)
    try:
        first_id = writer.create_volume("Before snapshot", str(tmp_path / "before"))
        late_id: int | None = None

        def mutate_after_snapshot(progress):
            nonlocal late_id
            if (
                late_id is None
                and progress.phase == "copy_source"
                and progress.completed >= 1
            ):
                late_id = writer.create_volume(
                    "Committed after snapshot",
                    str(tmp_path / "late"),
                )

        result = create_catalogue_backup(
            source,
            backup,
            progress_callback=mutate_after_snapshot,
        )
        assert late_id is not None
        assert result.table_rows["volumes"] == 1
        assert {row["id"] for row in writer.list_volumes()} == {first_id, late_id}
    finally:
        writer.close()

    restore_catalogue_backup(backup, restored)
    reader = Database(restored, create=False)
    try:
        assert [(row["id"], row["name"]) for row in reader.list_volumes()] == [
            (first_id, "Before snapshot")
        ]
    finally:
        reader.close()


@pytest.mark.parametrize("extension_kind", ["table", "column", "trigger"])
def test_unknown_persistent_schema_is_rejected_instead_of_silently_discarded(
    extension_kind: str,
    representative_catalogue: Path,
    tmp_path: Path,
):
    connection = sqlite3.connect(representative_catalogue)
    try:
        if extension_kind == "table":
            connection.execute("CREATE TABLE plugin_notes (id INTEGER, note TEXT)")
        elif extension_kind == "column":
            connection.execute("ALTER TABLE volumes ADD COLUMN plugin_rating INTEGER")
        else:
            connection.execute(
                """
                CREATE TRIGGER plugin_volume_trigger AFTER UPDATE ON volumes
                BEGIN SELECT 1; END
                """
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(CatalogueBackupError, match="(?i)(unrecognized|lossless)"):
        create_catalogue_backup(
            representative_catalogue,
            tmp_path / f"unsupported-{extension_kind}.zip",
        )
