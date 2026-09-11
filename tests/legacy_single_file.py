"""LEGACY test fixture: build a single-file schema-12 catalogue as older JVVV builds wrote it.

Frozen copy of the pre-directory-format schema, kept only so the conversion in
:mod:`jvvv.legacy` can be tested.  Delete with the jvvv.legacy package.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jvvv.backup_analysis import ANALYSIS_SCHEMA_SQL

LEGACY_SCHEMA_VERSION = 12
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"

LEGACY_CATALOGUE_SCHEMA_SQL: tuple[str, ...] = (
    "\n    CREATE TABLE volumes (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        name TEXT UNIQUE COLLATE NOCASE,\n        source_path TEXT NOT NULL,\n        capacity_bytes INTEGER NOT NULL DEFAULT 0,\n        used_bytes INTEGER NOT NULL DEFAULT 0,\n        free_bytes INTEGER NOT NULL DEFAULT 0,\n        indexed_file_count INTEGER NOT NULL DEFAULT 0,\n        indexed_folder_count INTEGER NOT NULL DEFAULT 0,\n        last_scan_at TEXT,\n        created_at TEXT NOT NULL,\n        updated_at TEXT NOT NULL,\n        identity_kind TEXT NOT NULL DEFAULT '',\n        identity_token TEXT NOT NULL DEFAULT '',\n        identity_label TEXT NOT NULL DEFAULT '',\n        identity_serial TEXT NOT NULL DEFAULT '',\n        identity_filesystem TEXT NOT NULL DEFAULT '',\n        source_relative_path TEXT NOT NULL DEFAULT ''\n    )\n    ",
    '\n    CREATE TABLE folders (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,\n        parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,\n        name TEXT NOT NULL,\n        relative_path TEXT NOT NULL,\n        missing INTEGER NOT NULL DEFAULT 0,\n        scanned_at TEXT,\n        modified_at TEXT,\n        recursive_size_bytes INTEGER,\n        recursive_file_count INTEGER,\n        recursive_subfolder_count INTEGER,\n        direct_file_count INTEGER,\n        direct_subfolder_count INTEGER,\n        stats_updated_at TEXT,\n        UNIQUE(volume_id, relative_path)\n    )\n    ',
    "\n    CREATE TABLE files (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,\n        folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,\n        name TEXT NOT NULL,\n        relative_path TEXT NOT NULL,\n        extension TEXT NOT NULL DEFAULT '',\n        size_bytes INTEGER NOT NULL DEFAULT 0,\n        modified_at TEXT,\n        missing INTEGER NOT NULL DEFAULT 0,\n        scanned_at TEXT,\n        identity_device INTEGER,\n        identity_inode INTEGER,\n        content_hash BLOB,\n        content_hash_algorithm TEXT,\n        UNIQUE(volume_id, relative_path)\n    )\n    ",
    "\n    CREATE TABLE file_media_metadata (\n        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,\n        status TEXT NOT NULL,\n        media_kind TEXT NOT NULL DEFAULT '',\n        source TEXT NOT NULL DEFAULT '',\n        container TEXT,\n        duration_ms INTEGER,\n        width INTEGER,\n        height INTEGER,\n        video_codecs TEXT,\n        audio_codecs TEXT,\n        sample_rate_hz INTEGER,\n        channels INTEGER,\n        bit_rate INTEGER,\n        message TEXT NOT NULL DEFAULT '',\n        probed_at TEXT\n    )\n    ",
    "\n    CREATE TABLE scan_history (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,\n        started_at TEXT NOT NULL,\n        finished_at TEXT,\n        status TEXT NOT NULL,\n        files_seen INTEGER NOT NULL DEFAULT 0,\n        folders_seen INTEGER NOT NULL DEFAULT 0,\n        errors_count INTEGER NOT NULL DEFAULT 0,\n        message TEXT,\n        files_added INTEGER,\n        files_removed INTEGER,\n        files_changed INTEGER,\n        folders_added INTEGER,\n        folders_removed INTEGER,\n        bytes_before INTEGER,\n        bytes_after INTEGER,\n        files_hashed INTEGER NOT NULL DEFAULT 0,\n        bytes_hashed INTEGER NOT NULL DEFAULT 0,\n        hash_errors INTEGER NOT NULL DEFAULT 0,\n        media_files INTEGER NOT NULL DEFAULT 0,\n        media_metadata_collected INTEGER NOT NULL DEFAULT 0,\n        preview_mode TEXT NOT NULL DEFAULT 'disabled',\n        image_previews_generated INTEGER NOT NULL DEFAULT 0,\n        image_previews_reused INTEGER NOT NULL DEFAULT 0,\n        image_previews_failed INTEGER NOT NULL DEFAULT 0,\n        video_previews_generated INTEGER NOT NULL DEFAULT 0,\n        video_previews_reused INTEGER NOT NULL DEFAULT 0,\n        video_previews_failed INTEGER NOT NULL DEFAULT 0,\n        previews_storage_skipped INTEGER NOT NULL DEFAULT 0,\n        preview_bytes_written INTEGER NOT NULL DEFAULT 0,\n        preview_message TEXT NOT NULL DEFAULT ''\n    )\n    ",
    "\n    CREATE TABLE IF NOT EXISTS file_preview_status (\n        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,\n        media_kind TEXT NOT NULL,\n        profile_id TEXT NOT NULL,\n        status TEXT NOT NULL,\n        source_hash BLOB,\n        preview_size INTEGER,\n        preview_width INTEGER,\n        preview_height INTEGER,\n        preview_duration_ms INTEGER,\n        generated_at TEXT,\n        error_stage TEXT,\n        error_message TEXT NOT NULL DEFAULT '',\n        updated_at TEXT NOT NULL\n    )\n    ",
    '\n    CREATE TABLE scan_errors (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        scan_id INTEGER REFERENCES scan_history(id) ON DELETE CASCADE,\n        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,\n        path TEXT NOT NULL,\n        message TEXT NOT NULL,\n        created_at TEXT NOT NULL\n    )\n    ',
    "\n    CREATE TABLE volume_register (\n        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,\n        drive_id TEXT UNIQUE COLLATE NOCASE,\n        is_mirror INTEGER NOT NULL DEFAULT 0,\n        status TEXT NOT NULL DEFAULT 'Archive',\n        condition TEXT NOT NULL DEFAULT 'Unknown',\n        description TEXT NOT NULL DEFAULT '',\n        earliest_content_date TEXT,\n        latest_content_date TEXT,\n        connector TEXT NOT NULL DEFAULT 'Unknown',\n        date_added TEXT NOT NULL,\n        retired_date TEXT,\n        mirror_date TEXT,\n        master_volume_id INTEGER REFERENCES volumes(id) ON DELETE RESTRICT,\n        created_at TEXT NOT NULL,\n        updated_at TEXT NOT NULL,\n        CHECK (is_mirror IN (0, 1))\n    )\n    ",
    'CREATE INDEX idx_folders_volume_parent ON folders(volume_id, parent_id)',
    'CREATE INDEX idx_folders_name ON folders(name COLLATE NOCASE)',
    'CREATE INDEX idx_folders_path ON folders(relative_path COLLATE NOCASE)',
    'CREATE INDEX idx_folders_volume_stats_size ON folders(volume_id, recursive_size_bytes)',
    'CREATE INDEX idx_folders_parent ON folders(parent_id)',
    'CREATE INDEX idx_files_volume_folder ON files(volume_id, folder_id)',
    'CREATE INDEX idx_files_name ON files(name COLLATE NOCASE)',
    'CREATE INDEX idx_files_extension ON files(extension COLLATE NOCASE)',
    'CREATE INDEX idx_files_path ON files(relative_path COLLATE NOCASE)',
    'CREATE INDEX idx_files_identity ON files(volume_id, identity_device, identity_inode)',
    'CREATE INDEX idx_files_folder ON files(folder_id)',
    'CREATE INDEX idx_scan_history_volume ON scan_history(volume_id)',
    'CREATE INDEX idx_scan_errors_scan ON scan_errors(scan_id)',
    'CREATE INDEX idx_scan_errors_volume ON scan_errors(volume_id)',
    'CREATE INDEX IF NOT EXISTS idx_file_preview_status_status ON file_preview_status(status)',
    'CREATE INDEX idx_volume_register_status ON volume_register(status COLLATE NOCASE)',
    'CREATE INDEX idx_volume_register_condition ON volume_register(condition COLLATE NOCASE)',
    'CREATE INDEX idx_volume_register_connector ON volume_register(connector COLLATE NOCASE)',
    'CREATE INDEX idx_volume_register_master ON volume_register(master_volume_id)',
    'CREATE INDEX idx_volumes_identity ON volumes(identity_kind, identity_token)',
)

LEGACY_SEARCH_INDEX_SQL: tuple[str, ...] = (
    "\n            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(\n                name,\n                relative_path,\n                extension,\n                content='files',\n                content_rowid='id',\n                tokenize='trigram',\n                detail='column'\n            )\n            ",
    "\n            CREATE VIRTUAL TABLE IF NOT EXISTS folders_fts USING fts5(\n                name,\n                relative_path,\n                content='folders',\n                content_rowid='id',\n                tokenize='trigram',\n                detail='column'\n            )\n            ",
    '\n            CREATE TRIGGER IF NOT EXISTS files_fts_insert\n            AFTER INSERT ON files BEGIN\n                INSERT INTO files_fts(rowid, name, relative_path, extension)\n                VALUES (new.id, new.name, new.relative_path, new.extension);\n            END\n            ',
    "\n            CREATE TRIGGER IF NOT EXISTS files_fts_delete\n            AFTER DELETE ON files BEGIN\n                INSERT INTO files_fts(files_fts, rowid, name, relative_path, extension)\n                VALUES ('delete', old.id, old.name, old.relative_path, old.extension);\n            END\n            ",
    "\n            CREATE TRIGGER IF NOT EXISTS files_fts_update\n            AFTER UPDATE OF name, relative_path, extension ON files\n            WHEN old.name IS NOT new.name\n              OR old.relative_path IS NOT new.relative_path\n              OR old.extension IS NOT new.extension\n            BEGIN\n                INSERT INTO files_fts(files_fts, rowid, name, relative_path, extension)\n                VALUES ('delete', old.id, old.name, old.relative_path, old.extension);\n                INSERT INTO files_fts(rowid, name, relative_path, extension)\n                VALUES (new.id, new.name, new.relative_path, new.extension);\n            END\n            ",
    '\n            CREATE TRIGGER IF NOT EXISTS folders_fts_insert\n            AFTER INSERT ON folders BEGIN\n                INSERT INTO folders_fts(rowid, name, relative_path)\n                VALUES (new.id, new.name, new.relative_path);\n            END\n            ',
    "\n            CREATE TRIGGER IF NOT EXISTS folders_fts_delete\n            AFTER DELETE ON folders BEGIN\n                INSERT INTO folders_fts(folders_fts, rowid, name, relative_path)\n                VALUES ('delete', old.id, old.name, old.relative_path);\n            END\n            ",
    "\n            CREATE TRIGGER IF NOT EXISTS folders_fts_update\n            AFTER UPDATE OF name, relative_path ON folders\n            WHEN old.name IS NOT new.name\n              OR old.relative_path IS NOT new.relative_path\n            BEGIN\n                INSERT INTO folders_fts(folders_fts, rowid, name, relative_path)\n                VALUES ('delete', old.id, old.name, old.relative_path);\n                INSERT INTO folders_fts(rowid, name, relative_path)\n                VALUES (new.id, new.name, new.relative_path);\n            END\n            ",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def create_legacy_catalogue(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    """Create an empty schema-12 single-file catalogue and return an open connection."""

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("BEGIN")
    for statement in LEGACY_CATALOGUE_SCHEMA_SQL:
        connection.execute(statement)
    for statement in ANALYSIS_SCHEMA_SQL:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO backup_analysis_state (id, active_run_id, forced_stale, stale_reason, updated_at)"
        " VALUES (1, NULL, 0, '', ?)",
        (utc_now(),),
    )
    for statement in LEGACY_SEARCH_INDEX_SQL:
        connection.execute(statement)
    connection.execute("INSERT INTO files_fts(files_fts) VALUES ('rebuild')")
    connection.execute("INSERT INTO folders_fts(folders_fts) VALUES ('rebuild')")
    connection.execute(f"PRAGMA user_version = {LEGACY_SCHEMA_VERSION}")
    connection.commit()
    return connection


def add_legacy_volume(
    connection: sqlite3.Connection,
    *,
    name: str,
    drive_id: str,
    files: dict[str, tuple[int, str | None]],
    source_path: str = "",
    scan_errors: int = 0,
) -> int:
    """Insert a volume with a root folder, one sub-folder per path segment, files, and a scan."""

    now = utc_now()
    cursor = connection.execute(
        """
        INSERT INTO volumes (name, source_path, created_at, updated_at, indexed_file_count, indexed_folder_count, last_scan_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, source_path, now, now, len(files), 0, now),
    )
    volume_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO volume_register (volume_id, drive_id, is_mirror, status, condition, description,
            connector, date_added, created_at, updated_at)
        VALUES (?, ?, 0, 'Archive', 'Unknown', '', 'Unknown', ?, ?, ?)
        """,
        (volume_id, drive_id, now[:10], now, now),
    )
    root = connection.execute(
        "INSERT INTO folders (volume_id, parent_id, name, relative_path, scanned_at) VALUES (?, NULL, 'root', '', ?)",
        (volume_id, now),
    ).lastrowid
    folder_ids: dict[str, int] = {"": int(root)}
    for relative_path, (size, modified_at) in sorted(files.items()):
        parts = relative_path.split("/")
        parent = folder_ids[""]
        for depth in range(len(parts) - 1):
            folder_path = "/".join(parts[: depth + 1])
            if folder_path not in folder_ids:
                folder_ids[folder_path] = int(
                    connection.execute(
                        "INSERT INTO folders (volume_id, parent_id, name, relative_path, scanned_at) VALUES (?, ?, ?, ?, ?)",
                        (volume_id, parent, parts[depth], folder_path, now),
                    ).lastrowid
                )
            parent = folder_ids[folder_path]
        extension = parts[-1].rsplit(".", 1)[1].lower() if "." in parts[-1] else ""
        connection.execute(
            """
            INSERT INTO files (volume_id, folder_id, name, relative_path, extension, size_bytes, modified_at, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (volume_id, parent, parts[-1], relative_path, extension, size, modified_at, now),
        )
    connection.execute(
        "UPDATE volumes SET indexed_folder_count = ? WHERE id = ?",
        (len(folder_ids), volume_id),
    )
    scan_id = connection.execute(
        """
        INSERT INTO scan_history (volume_id, started_at, finished_at, status, files_seen, folders_seen, errors_count)
        VALUES (?, ?, ?, 'completed', ?, ?, ?)
        """,
        (volume_id, now, now, len(files), len(folder_ids), scan_errors),
    ).lastrowid
    for index in range(scan_errors):
        connection.execute(
            "INSERT INTO scan_errors (scan_id, volume_id, path, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (scan_id, volume_id, f"locked-{index}", "Access denied", now),
        )
    connection.commit()
    return volume_id
