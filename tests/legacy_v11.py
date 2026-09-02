"""Build a representative *schema version 11* catalogue for upgrade tests.

The DDL below is the v11 catalogue schema exactly as shipped before the
offline-preview work (git commit ``eb3ee6a``), plus the unchanged backup-analysis
and FTS structures.  It is kept verbatim on purpose: the upgrade test must start
from what real v11 files look like, not from today's schema minus a diff.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jvvv.backup_analysis import ANALYSIS_SCHEMA_SQL

V11_SCHEMA_VERSION = 11

V11_CATALOGUE_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE volumes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE COLLATE NOCASE,
        source_path TEXT NOT NULL,
        capacity_bytes INTEGER NOT NULL DEFAULT 0,
        used_bytes INTEGER NOT NULL DEFAULT 0,
        free_bytes INTEGER NOT NULL DEFAULT 0,
        indexed_file_count INTEGER NOT NULL DEFAULT 0,
        indexed_folder_count INTEGER NOT NULL DEFAULT 0,
        last_scan_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        identity_kind TEXT NOT NULL DEFAULT '',
        identity_token TEXT NOT NULL DEFAULT '',
        identity_label TEXT NOT NULL DEFAULT '',
        identity_serial TEXT NOT NULL DEFAULT '',
        identity_filesystem TEXT NOT NULL DEFAULT '',
        source_relative_path TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
        parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        missing INTEGER NOT NULL DEFAULT 0,
        scanned_at TEXT,
        modified_at TEXT,
        recursive_size_bytes INTEGER,
        recursive_file_count INTEGER,
        recursive_subfolder_count INTEGER,
        direct_file_count INTEGER,
        direct_subfolder_count INTEGER,
        stats_updated_at TEXT,
        UNIQUE(volume_id, relative_path)
    )
    """,
    """
    CREATE TABLE files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
        folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        extension TEXT NOT NULL DEFAULT '',
        size_bytes INTEGER NOT NULL DEFAULT 0,
        modified_at TEXT,
        missing INTEGER NOT NULL DEFAULT 0,
        scanned_at TEXT,
        identity_device INTEGER,
        identity_inode INTEGER,
        content_hash BLOB,
        content_hash_algorithm TEXT,
        UNIQUE(volume_id, relative_path)
    )
    """,
    """
    CREATE TABLE file_media_metadata (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        media_kind TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        container TEXT,
        duration_ms INTEGER,
        width INTEGER,
        height INTEGER,
        video_codecs TEXT,
        audio_codecs TEXT,
        sample_rate_hz INTEGER,
        channels INTEGER,
        bit_rate INTEGER,
        message TEXT NOT NULL DEFAULT '',
        probed_at TEXT
    )
    """,
    """
    CREATE TABLE scan_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        files_seen INTEGER NOT NULL DEFAULT 0,
        folders_seen INTEGER NOT NULL DEFAULT 0,
        errors_count INTEGER NOT NULL DEFAULT 0,
        message TEXT,
        files_added INTEGER,
        files_removed INTEGER,
        files_changed INTEGER,
        folders_added INTEGER,
        folders_removed INTEGER,
        bytes_before INTEGER,
        bytes_after INTEGER,
        files_hashed INTEGER NOT NULL DEFAULT 0,
        bytes_hashed INTEGER NOT NULL DEFAULT 0,
        hash_errors INTEGER NOT NULL DEFAULT 0,
        media_files INTEGER NOT NULL DEFAULT 0,
        media_metadata_collected INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE scan_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER REFERENCES scan_history(id) ON DELETE CASCADE,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE volume_register (
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
        drive_id TEXT UNIQUE COLLATE NOCASE,
        is_mirror INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'Archive',
        condition TEXT NOT NULL DEFAULT 'Unknown',
        description TEXT NOT NULL DEFAULT '',
        earliest_content_date TEXT,
        latest_content_date TEXT,
        connector TEXT NOT NULL DEFAULT 'Unknown',
        date_added TEXT NOT NULL,
        retired_date TEXT,
        mirror_date TEXT,
        master_volume_id INTEGER REFERENCES volumes(id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (is_mirror IN (0, 1))
    )
    """,
    "CREATE INDEX idx_folders_volume_parent ON folders(volume_id, parent_id)",
    "CREATE INDEX idx_folders_name ON folders(name COLLATE NOCASE)",
    "CREATE INDEX idx_folders_path ON folders(relative_path COLLATE NOCASE)",
    "CREATE INDEX idx_folders_volume_stats_size ON folders(volume_id, recursive_size_bytes)",
    "CREATE INDEX idx_folders_parent ON folders(parent_id)",
    "CREATE INDEX idx_files_volume_folder ON files(volume_id, folder_id)",
    "CREATE INDEX idx_files_name ON files(name COLLATE NOCASE)",
    "CREATE INDEX idx_files_extension ON files(extension COLLATE NOCASE)",
    "CREATE INDEX idx_files_path ON files(relative_path COLLATE NOCASE)",
    "CREATE INDEX idx_files_identity ON files(volume_id, identity_device, identity_inode)",
    "CREATE INDEX idx_files_folder ON files(folder_id)",
    "CREATE INDEX idx_scan_history_volume ON scan_history(volume_id)",
    "CREATE INDEX idx_scan_errors_scan ON scan_errors(scan_id)",
    "CREATE INDEX idx_scan_errors_volume ON scan_errors(volume_id)",
    "CREATE INDEX idx_volume_register_status ON volume_register(status COLLATE NOCASE)",
    "CREATE INDEX idx_volume_register_condition ON volume_register(condition COLLATE NOCASE)",
    "CREATE INDEX idx_volume_register_connector ON volume_register(connector COLLATE NOCASE)",
    "CREATE INDEX idx_volume_register_master ON volume_register(master_volume_id)",
    "CREATE INDEX idx_volumes_identity ON volumes(identity_kind, identity_token)",
)

V11_SEARCH_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
        name,
        relative_path,
        extension,
        content='files',
        content_rowid='id',
        tokenize='trigram',
        detail='column'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS folders_fts USING fts5(
        name,
        relative_path,
        content='folders',
        content_rowid='id',
        tokenize='trigram',
        detail='column'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS files_fts_insert
    AFTER INSERT ON files BEGIN
        INSERT INTO files_fts(rowid, name, relative_path, extension)
        VALUES (new.id, new.name, new.relative_path, new.extension);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS files_fts_delete
    AFTER DELETE ON files BEGIN
        INSERT INTO files_fts(files_fts, rowid, name, relative_path, extension)
        VALUES ('delete', old.id, old.name, old.relative_path, old.extension);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS files_fts_update
    AFTER UPDATE OF name, relative_path, extension ON files
    WHEN old.name IS NOT new.name
      OR old.relative_path IS NOT new.relative_path
      OR old.extension IS NOT new.extension
    BEGIN
        INSERT INTO files_fts(files_fts, rowid, name, relative_path, extension)
        VALUES ('delete', old.id, old.name, old.relative_path, old.extension);
        INSERT INTO files_fts(rowid, name, relative_path, extension)
        VALUES (new.id, new.name, new.relative_path, new.extension);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS folders_fts_insert
    AFTER INSERT ON folders BEGIN
        INSERT INTO folders_fts(rowid, name, relative_path)
        VALUES (new.id, new.name, new.relative_path);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS folders_fts_delete
    AFTER DELETE ON folders BEGIN
        INSERT INTO folders_fts(folders_fts, rowid, name, relative_path)
        VALUES ('delete', old.id, old.name, old.relative_path);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS folders_fts_update
    AFTER UPDATE OF name, relative_path ON folders
    WHEN old.name IS NOT new.name
      OR old.relative_path IS NOT new.relative_path
    BEGIN
        INSERT INTO folders_fts(folders_fts, rowid, name, relative_path)
        VALUES ('delete', old.id, old.name, old.relative_path);
        INSERT INTO folders_fts(rowid, name, relative_path)
        VALUES (new.id, new.name, new.relative_path);
    END
    """,
)

# Tables whose rows the upgrade must carry over unchanged (in v11 column order).
V11_DATA_TABLES: tuple[str, ...] = (
    "volumes",
    "volume_register",
    "folders",
    "files",
    "file_media_metadata",
    "scan_history",
    "scan_errors",
    "backup_analysis_runs",
    "backup_analysis_state",
    "backup_analysis_volume_snapshots",
    "backup_file_results",
    "backup_folder_results",
    "backup_folder_drive_matches",
    "backup_volume_results",
    "backup_mirror_candidates",
    "backup_analysis_invalidations",
)

SHARED_HASH = bytes(range(32))
UNIQUE_HASH = bytes(reversed(range(32)))


def create_v11_catalogue(path: Path) -> Path:
    """Write a populated schema-version-11 catalogue to ``path`` and return it."""

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN")
        for statement in V11_CATALOGUE_SCHEMA_SQL:
            connection.execute(statement)
        for statement in ANALYSIS_SCHEMA_SQL:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO backup_analysis_state (
                id, active_run_id, forced_stale, stale_reason, updated_at
            ) VALUES (1, NULL, 0, '', '2026-08-20T10:00:00.000000+0000')
            """
        )
        for statement in V11_SEARCH_SCHEMA_SQL:
            connection.execute(statement)

        connection.executemany(
            """
            INSERT INTO volumes (
                id, name, source_path, identity_kind, identity_token, identity_label,
                identity_serial, identity_filesystem, source_relative_path,
                capacity_bytes, used_bytes, free_bytes, indexed_file_count,
                indexed_folder_count, last_scan_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (7, "Primary Ω archive", r"E:\Archive Root", "windows-volume-guid",
                 r"\\?\volume{11111111-2222-3333-4444-555555555555}\ ", "ARCHIVE_Ω", "A1B2C3D4",
                 "NTFS", "", 8_000_000_000_000, 5_000_000_000_000, 3_000_000_000_000, 3, 3,
                 "2026-08-20T10:00:00.000000+0000", "2026-01-01T00:00:00.000000+0000",
                 "2026-08-20T10:00:00.000000+0000"),
                (12, None, r"F:\ ", "", "", "", "", "", "Sub/folder", 0, 0, 0, 1, 1, None,
                 "2026-02-02T00:00:00.000000+0000", "2026-08-22T01:00:00.000000+0000"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO volume_register (
                volume_id, drive_id, is_mirror, status, condition, description,
                earliest_content_date, latest_content_date, connector, date_added,
                retired_date, mirror_date, master_volume_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (7, "AID-007", 0, "Archive", "Good", "Main archive Ω", "2001-02-03", "2026-04-05",
                 "USB-C", "2026-01-01", None, None, None, "2026-01-01T00:00:00.000000+0000",
                 "2026-08-20T10:00:00.000000+0000"),
                (12, "AID-012", 1, "Retired", "Poor", "", None, None, "Network", "2026-02-02",
                 "2026-08-01", "2026-03-03", 7, "2026-02-02T00:00:00.000000+0000",
                 "2026-08-22T01:00:00.000000+0000"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO folders (
                id, volume_id, parent_id, name, relative_path, missing, scanned_at,
                modified_at, recursive_size_bytes, recursive_file_count,
                recursive_subfolder_count, direct_file_count, direct_subfolder_count,
                stats_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (101, 7, None, "Primary Ω archive", "", 0, "scan-a", None, 434, 3, 2, 0, 2, "scan-a"),
                (102, 7, 101, "Prøjects", "Prøjects", 0, "scan-a", "2026-01-01T00:00:00.000000+0000",
                 101, 2, 0, 2, 0, "scan-a"),
                (103, 7, 101, "Gone", "Gone", 1, "old-scan", None, 333, 1, 0, 1, 0, "old-scan"),
                (201, 12, None, "AID-012", "", 0, "scan-b", None, 101, 1, 0, 1, 0, "scan-b"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO files (
                id, volume_id, folder_id, name, relative_path, extension, size_bytes,
                modified_at, missing, scanned_at, identity_device, identity_inode,
                content_hash, content_hash_algorithm
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1001, 7, 102, "shared clip.mov", "Prøjects/shared clip.mov", "mov", 101,
                 "2026-04-05T06:07:08.000009+0000", 0, "scan-a", 44, 55, SHARED_HASH, "sha256"),
                (1002, 7, 102, "résumé 💾.txt", "Prøjects/résumé 💾.txt", "txt", 0, None, 0,
                 "scan-a", None, None, None, None),
                (1003, 7, 103, "missing.bin", "Gone/missing.bin", "bin", 333,
                 "2001-02-03T04:05:06.000007+0000", 1, "old-scan", -9, 123, UNIQUE_HASH, "sha256"),
                (2001, 12, 201, "shared clip.mov", "shared clip.mov", "mov", 101,
                 "2026-04-05T06:07:08.000009+0000", 0, "scan-b", 66, 77, SHARED_HASH, "sha256"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO file_media_metadata (
                file_id, status, media_kind, source, container, duration_ms, width, height,
                video_codecs, audio_codecs, sample_rate_hz, channels, bit_rate, message,
                probed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1001, "complete", "video", "ffprobe", "mov,mp4", 12_345, 3840, 2160, "h264",
                 "aac", 48_000, 6, 9_999_999, "", "2026-08-20T10:00:00.000001+0000"),
                (1002, "unavailable", "", "extension", None, None, None, None, None, None,
                 None, None, None, "Not media Ω", None),
            ],
        )
        connection.executemany(
            """
            INSERT INTO scan_history (
                id, volume_id, started_at, finished_at, status, files_seen, folders_seen,
                errors_count, message, files_added, files_removed, files_changed,
                folders_added, folders_removed, bytes_before, bytes_after, files_hashed,
                bytes_hashed, hash_errors, media_files, media_metadata_collected
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (301, 7, "2026-08-20T09:00:00.000000+0000", "2026-08-20T10:00:00.000000+0000",
                 "completed", 3, 3, 1, "Completed with one unreadable path", 2, 1, 1, 2, 1, 444,
                 434, 2, 202, 1, 1, 1),
                (305, 12, "2026-08-22T01:00:00.000000+0000", None, "cancelled", 1, 1, 1, None,
                 None, None, None, None, None, None, None, 1, 101, 0, 1, 1),
            ],
        )
        connection.executemany(
            """
            INSERT INTO scan_errors (id, scan_id, volume_id, path, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (401, 301, 7, r"System Volume Information\secret", "[WinError 5] Access denied",
                 "2026-08-20T09:30:00.000000+0000"),
                (409, None, 12, "Prøjects/💥", "Detached diagnostic\nwith details",
                 "2026-08-22T01:01:00.000000+0000"),
            ],
        )
        connection.execute(
            """
            INSERT INTO backup_analysis_runs (
                id, started_at, completed_at, status, rules_version, source_signature,
                files_analyzed, folders_analyzed, likely_files, message
            ) VALUES (900, 'analysis-start', 'analysis-end', 'completed', 1, 'signature',
                      4, 3, 1, 'old rules')
            """
        )
        connection.execute(
            """
            UPDATE backup_analysis_state
            SET active_run_id = 900, forced_stale = 1, stale_reason = 'deliberately stale',
                updated_at = 'old-state'
            WHERE id = 1
            """
        )
        connection.execute(
            """
            INSERT INTO backup_file_results (run_id, file_id, volume_id, status,
                other_volume_ids, evidence_text)
            VALUES (900, 1001, 7, 'likely', '[12]', 'old evidence')
            """
        )
        for table, sequence in {
            "volumes": 50,
            "folders": 500,
            "files": 5_000,
            "scan_history": 600,
            "scan_errors": 700,
            "backup_analysis_runs": 950,
        }.items():
            connection.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                (sequence, table),
            )
        connection.execute(f"PRAGMA user_version = {V11_SCHEMA_VERSION}")
        connection.commit()
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()
    return path


def snapshot(path: Path, tables: tuple[str, ...] = V11_DATA_TABLES) -> dict[str, list[tuple]]:
    """Every row of every listed table, ordered by rowid, as plain tuples."""

    connection = sqlite3.connect(path)
    try:
        result: dict[str, list[tuple]] = {}
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            result[table] = sorted(
                (tuple(row) for row in connection.execute(f'SELECT {", ".join(columns)} FROM "{table}"')),
                key=repr,
            )
        result["sqlite_sequence"] = sorted(
            tuple(row) for row in connection.execute("SELECT name, seq FROM sqlite_sequence")
        )
        return result
    finally:
        connection.close()
