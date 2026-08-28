from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping
import zipfile

from . import __version__ as APPLICATION_VERSION
from .backup_analysis import ANALYSIS_SCHEMA_SQL, BackupAnalysisEngine, RULES_VERSION
from .database import (
    CATALOGUE_EXTENSION,
    REQUIRED_COLUMNS,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    Database,
    catalogue_path_with_extension,
)


BACKUP_FORMAT = "jvvv-semantic-backup"
BACKUP_FORMAT_VERSION = 1
PAYLOAD_FORMAT_VERSION = 1
FOLDER_AGGREGATE_ALGORITHM_VERSION = 1
VOLUME_COUNT_ALGORITHM_VERSION = 1
PAYLOAD_APPLICATION_ID = 0x4A565642  # "JVVB"
MANIFEST_PATH = "manifest.json"
PAYLOAD_PATH = "source.sqlite"
BACKUP_FILE_FILTER = "JVVV Catalogue Backups (*.zip)"
MAX_MANIFEST_BYTES = 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


class CatalogueBackupError(Exception):
    """Base class for catalogue backup and restore failures."""


class InvalidBackupError(CatalogueBackupError):
    """The selected archive is damaged or is not a JVVV backup."""


class UnsupportedBackupError(CatalogueBackupError):
    """The archive is valid but requires a newer JVVV backup reader."""


class BackupCancelled(CatalogueBackupError):
    """The current backup or restore operation was cancelled."""


@dataclass(frozen=True)
class BackupProgress:
    phase: str
    completed: int
    total: int
    message: str


@dataclass(frozen=True)
class BackupResult:
    source_path: Path
    backup_path: Path
    original_size: int
    backup_size: int
    payload_size: int
    savings_bytes: int
    savings_percent: float
    table_rows: dict[str, int]


@dataclass(frozen=True)
class RestoreResult:
    backup_path: Path
    catalogue_path: Path
    backup_size: int
    catalogue_size: int
    regenerated_components: tuple[str, ...]


@dataclass(frozen=True)
class BackupInspection:
    backup_path: Path
    manifest: dict[str, Any]
    archive_size: int
    payload_size: int
    table_rows: dict[str, int]


ProgressCallback = Callable[[BackupProgress], None]
CancelCallback = Callable[[], bool]


AUTHORITATIVE_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
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
        "content_hash_id",
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
    "content_blobs": ("id", "digest"),
    "catalogue_sequences": ("name", "seq"),
    "folder_state_exceptions": (
        "folder_id",
        "recursive_size_bytes",
        "recursive_file_count",
        "recursive_subfolder_count",
        "direct_file_count",
        "direct_subfolder_count",
        "stats_updated_at",
    ),
    "volume_count_exceptions": (
        "volume_id",
        "indexed_file_count",
        "indexed_folder_count",
    ),
}

SOURCE_TABLES = (
    "volumes",
    "volume_register",
    "folders",
    "files",
    "file_media_metadata",
    "scan_history",
    "scan_errors",
)

AUTOINCREMENT_SOURCE_TABLES = (
    "volumes",
    "folders",
    "files",
    "scan_history",
    "scan_errors",
)

ANALYSIS_AUTOINCREMENT_TABLES = (
    "backup_analysis_runs",
    "backup_analysis_invalidations",
)

ANALYSIS_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "backup_analysis_runs": (
        "id",
        "started_at",
        "completed_at",
        "status",
        "rules_version",
        "source_signature",
        "files_analyzed",
        "folders_analyzed",
        "likely_files",
        "possible_files",
        "ambiguous_files",
        "excluded_files",
        "single_files",
        "message",
    ),
    "backup_analysis_state": (
        "id",
        "active_run_id",
        "forced_stale",
        "stale_reason",
        "updated_at",
    ),
    "backup_analysis_volume_snapshots": (
        "run_id",
        "volume_id",
        "drive_id",
        "last_scan_at",
        "indexed_file_count",
        "indexed_folder_count",
    ),
    "backup_file_results": (
        "run_id",
        "file_id",
        "volume_id",
        "status",
        "other_volume_ids",
        "evidence_text",
        "strong_volume_ids",
        "possible_volume_ids",
        "verified_volume_ids",
    ),
    "backup_folder_results": (
        "run_id",
        "folder_id",
        "volume_id",
        "status",
        "other_volume_ids",
        "evidence_text",
        "best_target_volume_id",
        "matched_files",
        "total_files",
        "matched_bytes",
        "total_bytes",
        "best_coverage_files_percent",
        "best_coverage_bytes_percent",
        "scattered",
    ),
    "backup_folder_drive_matches": (
        "run_id",
        "folder_id",
        "target_volume_id",
        "status",
        "matched_files",
        "total_files",
        "matched_bytes",
        "total_bytes",
        "evidence_text",
    ),
    "backup_volume_results": (
        "run_id",
        "volume_id",
        "status",
        "health_status",
        "coverage_eligible",
        "total_files",
        "total_bytes",
        "coverage_files",
        "coverage_bytes",
        "likely_files",
        "likely_bytes",
        "possible_files",
        "possible_bytes",
        "ambiguous_files",
        "ambiguous_bytes",
        "excluded_files",
        "excluded_bytes",
        "single_files",
        "single_bytes",
        "likely_files_percent",
        "likely_bytes_percent",
        "latest_scan_status",
        "latest_scan_errors",
    ),
    "backup_mirror_candidates": (
        "run_id",
        "source_volume_id",
        "target_volume_id",
        "source_coverage_percent",
        "target_coverage_percent",
        "matched_files",
        "complete_structure",
        "evidence_text",
        "manual_mirror_link",
    ),
    "backup_analysis_invalidations": (
        "id",
        "volume_id",
        "reason",
        "created_at",
    ),
}

FTS_TRIGGERS = (
    "files_fts_insert",
    "files_fts_delete",
    "files_fts_update",
    "folders_fts_insert",
    "folders_fts_delete",
    "folders_fts_update",
)

FTS_SHADOW_TABLES = {
    f"{prefix}_{suffix}"
    for prefix in ("files_fts", "folders_fts")
    for suffix in ("data", "idx", "content", "docsize", "config")
}

KNOWN_INDEXES = {
    "idx_backup_file_results_item",
    "idx_backup_folder_matches_item",
    "idx_backup_folder_results_item",
    "idx_backup_volume_results_volume",
    "idx_files_extension",
    "idx_files_folder",
    "idx_files_identity",
    "idx_files_name",
    "idx_files_path",
    "idx_files_volume_folder",
    "idx_folders_name",
    "idx_folders_parent",
    "idx_folders_path",
    "idx_folders_volume_parent",
    "idx_folders_volume_stats_size",
    "idx_scan_errors_scan",
    "idx_scan_errors_volume",
    "idx_scan_history_volume",
    "idx_volume_register_condition",
    "idx_volume_register_connector",
    "idx_volume_register_master",
    "idx_volume_register_status",
    "idx_volumes_identity",
}

PAYLOAD_SCHEMA_SQL = (
    """
    CREATE TABLE volumes (
        id INTEGER PRIMARY KEY,
        name TEXT,
        source_path TEXT NOT NULL,
        identity_kind TEXT NOT NULL,
        identity_token TEXT NOT NULL,
        identity_label TEXT NOT NULL,
        identity_serial TEXT NOT NULL,
        identity_filesystem TEXT NOT NULL,
        source_relative_path TEXT NOT NULL,
        capacity_bytes INTEGER NOT NULL,
        used_bytes INTEGER NOT NULL,
        free_bytes INTEGER NOT NULL,
        last_scan_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE volume_register (
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id),
        drive_id TEXT,
        is_mirror INTEGER NOT NULL,
        status TEXT NOT NULL,
        condition TEXT NOT NULL,
        description TEXT NOT NULL,
        earliest_content_date TEXT,
        latest_content_date TEXT,
        connector TEXT NOT NULL,
        date_added TEXT NOT NULL,
        retired_date TEXT,
        mirror_date TEXT,
        master_volume_id INTEGER REFERENCES volumes(id) DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE folders (
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL REFERENCES volumes(id),
        parent_id INTEGER REFERENCES folders(id) DEFERRABLE INITIALLY DEFERRED,
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        missing INTEGER NOT NULL,
        scanned_at TEXT,
        modified_at TEXT
    )
    """,
    "CREATE TABLE content_blobs (id INTEGER PRIMARY KEY, digest BLOB NOT NULL)",
    """
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL REFERENCES volumes(id),
        folder_id INTEGER REFERENCES folders(id) DEFERRABLE INITIALLY DEFERRED,
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        extension TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        modified_at TEXT,
        missing INTEGER NOT NULL,
        scanned_at TEXT,
        identity_device INTEGER,
        identity_inode INTEGER,
        content_hash_id INTEGER REFERENCES content_blobs(id),
        content_hash_algorithm TEXT
    )
    """,
    """
    CREATE TABLE file_media_metadata (
        file_id INTEGER PRIMARY KEY REFERENCES files(id),
        status TEXT NOT NULL,
        media_kind TEXT NOT NULL,
        source TEXT NOT NULL,
        container TEXT,
        duration_ms INTEGER,
        width INTEGER,
        height INTEGER,
        video_codecs TEXT,
        audio_codecs TEXT,
        sample_rate_hz INTEGER,
        channels INTEGER,
        bit_rate INTEGER,
        message TEXT NOT NULL,
        probed_at TEXT
    )
    """,
    """
    CREATE TABLE scan_history (
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL REFERENCES volumes(id),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        files_seen INTEGER NOT NULL,
        folders_seen INTEGER NOT NULL,
        errors_count INTEGER NOT NULL,
        message TEXT,
        files_added INTEGER,
        files_removed INTEGER,
        files_changed INTEGER,
        folders_added INTEGER,
        folders_removed INTEGER,
        bytes_before INTEGER,
        bytes_after INTEGER,
        files_hashed INTEGER NOT NULL,
        bytes_hashed INTEGER NOT NULL,
        hash_errors INTEGER NOT NULL,
        media_files INTEGER NOT NULL,
        media_metadata_collected INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE scan_errors (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER REFERENCES scan_history(id) DEFERRABLE INITIALLY DEFERRED,
        volume_id INTEGER NOT NULL REFERENCES volumes(id),
        path TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE TABLE catalogue_sequences (name TEXT PRIMARY KEY, seq INTEGER NOT NULL) WITHOUT ROWID",
    """
    CREATE TABLE folder_state_exceptions (
        folder_id INTEGER PRIMARY KEY REFERENCES folders(id),
        recursive_size_bytes INTEGER,
        recursive_file_count INTEGER,
        recursive_subfolder_count INTEGER,
        direct_file_count INTEGER,
        direct_subfolder_count INTEGER,
        stats_updated_at TEXT
    )
    """,
    """
    CREATE TABLE volume_count_exceptions (
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id),
        indexed_file_count INTEGER NOT NULL,
        indexed_folder_count INTEGER NOT NULL
    )
    """,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _emit(
    callback: ProgressCallback | None,
    phase: str,
    completed: int,
    total: int,
    message: str,
) -> None:
    if callback is not None:
        callback(BackupProgress(phase, int(completed), int(total), message))


def _check_cancel(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback is not None and cancel_callback():
        raise BackupCancelled("The catalogue backup operation was cancelled.")


def _sqlite_cancel_handler(cancel_callback: CancelCallback | None) -> Callable[[], int]:
    return lambda: 1 if cancel_callback is not None and cancel_callback() else 0


def _quoted_columns(columns: Iterable[str], *, prefix: str = "") -> str:
    return ", ".join(f'{prefix}"{column}"' for column in columns)


def _source_uri(path: Path) -> str:
    resolved = path.resolve(strict=True)
    uri = resolved.as_uri()
    if resolved.drive.startswith("\\\\"):
        uri = f"file:////{uri[len('file://'):]}"
    return f"{uri}?mode=ro"


def _attach_catalogue_read_only(
    connection: sqlite3.Connection,
    source_path: Path,
) -> None:
    try:
        connection.execute("ATTACH DATABASE ? AS original", (_source_uri(source_path),))
        return
    except sqlite3.OperationalError as first_error:
        # A cleanly closed WAL-header database on genuinely read-only storage
        # can still make SQLite request a transient SHM file. Immutable mode is
        # safe only when there are no WAL/SHM sidecars whose committed pages it
        # could ignore. Live catalogues take the normal mode=ro path above.
        wal_path = Path(f"{source_path}-wal")
        shm_path = Path(f"{source_path}-shm")
        if wal_path.exists() or shm_path.exists():
            raise first_error
        try:
            connection.execute(
                "ATTACH DATABASE ? AS original",
                (f"{_source_uri(source_path)}&immutable=1",),
            )
        except sqlite3.OperationalError:
            raise first_error


def _schema_columns(connection: sqlite3.Connection, database: str, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA {database}.table_info("{table}")')
    }


def _validate_catalogue_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA original.user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise UnsupportedBackupError(
            f"The catalogue uses schema version {version}; this JVVV version supports "
            f"up to version {SCHEMA_VERSION}."
        )
    if version != SCHEMA_VERSION:
        raise CatalogueBackupError(
            f"The catalogue must be upgraded to schema version {SCHEMA_VERSION} before "
            "it can be backed up. Open it in JVVV, then try again."
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM original.sqlite_schema WHERE type = 'table'"
        )
    }
    missing_tables = REQUIRED_TABLES - tables
    if missing_tables:
        raise CatalogueBackupError(
            "The catalogue is missing required tables: " + ", ".join(sorted(missing_tables))
        )
    known_tables = REQUIRED_TABLES | FTS_SHADOW_TABLES | {"sqlite_sequence"}
    unknown_tables = {
        table
        for table in tables - known_tables
        if not table.startswith("sqlite_")
    }
    if unknown_tables:
        raise CatalogueBackupError(
            "This catalogue contains unrecognized persistent tables that JVVV cannot "
            "safely classify for semantic backup: " + ", ".join(sorted(unknown_tables))
        )
    missing_columns: list[str] = []
    extra_columns: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        actual_columns = _schema_columns(connection, "original", table)
        for column in sorted(required - actual_columns):
            missing_columns.append(f"{table}.{column}")
        for column in sorted(actual_columns - required):
            extra_columns.append(f"{table}.{column}")
    if missing_columns:
        raise CatalogueBackupError(
            "The catalogue is missing required fields: " + ", ".join(missing_columns)
        )
    if extra_columns:
        raise CatalogueBackupError(
            "This catalogue contains unrecognized fields that cannot be discarded "
            "losslessly: " + ", ".join(extra_columns)
        )

    unknown_views = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM original.sqlite_schema WHERE type = 'view' ORDER BY name"
        )
    ]
    if unknown_views:
        raise CatalogueBackupError(
            "This catalogue contains unrecognized views: " + ", ".join(unknown_views)
        )
    trigger_names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM original.sqlite_schema WHERE type = 'trigger'"
        )
    }
    unknown_triggers = trigger_names - set(FTS_TRIGGERS)
    if unknown_triggers:
        raise CatalogueBackupError(
            "This catalogue contains unrecognized triggers that cannot be discarded "
            "losslessly: " + ", ".join(sorted(unknown_triggers))
        )
    index_names = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM original.sqlite_schema
            WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'
            """
        )
    }
    unknown_indexes = index_names - KNOWN_INDEXES
    if unknown_indexes:
        raise CatalogueBackupError(
            "This catalogue contains unrecognized indexes whose constraints cannot be "
            "discarded losslessly: " + ", ".join(sorted(unknown_indexes))
        )


def _check_sqlite_integrity(
    connection: sqlite3.Connection,
    database: str,
    error_type: type[CatalogueBackupError],
) -> None:
    rows = list(connection.execute(f"PRAGMA {database}.integrity_check(1)"))
    if len(rows) != 1 or str(rows[0][0]).casefold() != "ok":
        detail = str(rows[0][0]) if rows else "no integrity result"
        raise error_type(f"SQLite integrity validation failed: {detail}")
    foreign_key_row = connection.execute(
        f"PRAGMA {database}.foreign_key_check"
    ).fetchone()
    if foreign_key_row is not None:
        raise error_type(
            "The catalogue contains an invalid relationship and cannot be restored safely."
        )


def _canonical_folder_statistics(
    connection: sqlite3.Connection,
    volume_id: int,
) -> dict[int, tuple[int, int, int, int, int]]:
    """Calculate the same folder aggregates as Database.rebuild_folder_statistics."""
    folder_rows = list(
        connection.execute(
            """
            SELECT id, parent_id, relative_path
            FROM original.folders
            WHERE volume_id = ? AND missing = 0
            """,
            (volume_id,),
        )
    )
    stats: dict[int, dict[str, int]] = {}
    depth_by_id: dict[int, int] = {}
    parent_by_id: dict[int, int | None] = {}
    children_by_parent: dict[int, list[int]] = {}
    for row in folder_rows:
        folder_id = int(row["id"])
        relative_path = str(row["relative_path"] or "")
        parent_id = int(row["parent_id"]) if row["parent_id"] is not None else None
        stats[folder_id] = {
            "direct_size": 0,
            "direct_file_count": 0,
            "direct_subfolder_count": 0,
            "recursive_size": 0,
            "recursive_file_count": 0,
            "recursive_subfolder_count": 0,
        }
        depth_by_id[folder_id] = 0 if not relative_path else relative_path.count("/") + 1
        parent_by_id[folder_id] = parent_id
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(folder_id)

    for folder_id, children in children_by_parent.items():
        if folder_id in stats:
            stats[folder_id]["direct_subfolder_count"] = len(children)

    for row in connection.execute(
        """
        SELECT folder_id, COUNT(*) AS direct_file_count,
               COALESCE(SUM(size_bytes), 0) AS direct_size
        FROM original.files
        WHERE volume_id = ? AND missing = 0 AND folder_id IS NOT NULL
        GROUP BY folder_id
        """,
        (volume_id,),
    ):
        folder_id = int(row["folder_id"])
        if folder_id in stats:
            stats[folder_id]["direct_size"] = int(row["direct_size"] or 0)
            stats[folder_id]["direct_file_count"] = int(row["direct_file_count"] or 0)

    for folder_id in sorted(depth_by_id, key=depth_by_id.get, reverse=True):
        folder_stats = stats[folder_id]
        recursive_size = folder_stats["direct_size"]
        recursive_file_count = folder_stats["direct_file_count"]
        recursive_subfolder_count = folder_stats["direct_subfolder_count"]
        for child_id in children_by_parent.get(folder_id, ()):
            child_stats = stats.get(child_id)
            if child_stats is None:
                continue
            recursive_size += child_stats["recursive_size"]
            recursive_file_count += child_stats["recursive_file_count"]
            recursive_subfolder_count += child_stats["recursive_subfolder_count"]
        folder_stats["recursive_size"] = recursive_size
        folder_stats["recursive_file_count"] = recursive_file_count
        folder_stats["recursive_subfolder_count"] = recursive_subfolder_count

    duplicate_rows = connection.execute(
        """
        WITH duplicate_identities AS (
            SELECT identity_device, identity_inode, MAX(size_bytes) AS size_bytes
            FROM original.files
            WHERE volume_id = ? AND missing = 0 AND folder_id IS NOT NULL
              AND identity_device IS NOT NULL AND identity_inode IS NOT NULL
            GROUP BY identity_device, identity_inode
            HAVING COUNT(*) > 1
        )
        SELECT f.identity_device, f.identity_inode, f.folder_id, d.size_bytes
        FROM original.files f
        JOIN duplicate_identities d
          ON d.identity_device = f.identity_device
         AND d.identity_inode = f.identity_inode
        WHERE f.volume_id = ? AND f.missing = 0 AND f.folder_id IS NOT NULL
        ORDER BY f.identity_device, f.identity_inode
        """,
        (volume_id, volume_id),
    )
    current_identity: tuple[int, int] | None = None
    current_size = 0
    ancestor_counts: dict[int, int] = {}

    def apply_identity_group() -> None:
        if current_identity is None:
            return
        for folder_id, count in ancestor_counts.items():
            if count > 1 and folder_id in stats:
                stats[folder_id]["recursive_size"] -= (count - 1) * current_size

    for row in duplicate_rows:
        identity = (int(row["identity_device"]), int(row["identity_inode"]))
        if identity != current_identity:
            apply_identity_group()
            current_identity = identity
            current_size = int(row["size_bytes"] or 0)
            ancestor_counts = {}
        current = int(row["folder_id"])
        visited: set[int] = set()
        while current in stats and current not in visited:
            visited.add(current)
            ancestor_counts[current] = ancestor_counts.get(current, 0) + 1
            parent = parent_by_id.get(current)
            if parent is None:
                break
            current = parent
    apply_identity_group()

    return {
        folder_id: (
            values["recursive_size"],
            values["recursive_file_count"],
            values["recursive_subfolder_count"],
            values["direct_file_count"],
            values["direct_subfolder_count"],
        )
        for folder_id, values in stats.items()
    }


def _store_derived_exceptions(
    connection: sqlite3.Connection,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> None:
    connection.execute(
        """
        INSERT INTO volume_count_exceptions (
            volume_id, indexed_file_count, indexed_folder_count
        )
        SELECT v.id, v.indexed_file_count, v.indexed_folder_count
        FROM original.volumes v
        WHERE v.indexed_file_count != (
                SELECT COUNT(*) FROM original.files f
                WHERE f.volume_id = v.id AND f.missing = 0
              )
           OR v.indexed_folder_count != (
                SELECT COUNT(*) FROM original.folders fo
                WHERE fo.volume_id = v.id AND fo.missing = 0
              )
        ORDER BY v.id
        """
    )
    volumes = list(
        connection.execute(
            "SELECT id, last_scan_at FROM original.volumes ORDER BY id"
        )
    )
    for index, volume in enumerate(volumes, start=1):
        _check_cancel(cancel_callback)
        volume_id = int(volume["id"])
        canonical = _canonical_folder_statistics(connection, volume_id)
        expected_time = volume["last_scan_at"]
        exceptions: list[tuple[Any, ...]] = []
        for row in connection.execute(
            """
            SELECT id, missing, recursive_size_bytes, recursive_file_count,
                   recursive_subfolder_count, direct_file_count,
                   direct_subfolder_count, stats_updated_at
            FROM original.folders WHERE volume_id = ? ORDER BY id
            """,
            (volume_id,),
        ):
            original = (
                row["recursive_size_bytes"],
                row["recursive_file_count"],
                row["recursive_subfolder_count"],
                row["direct_file_count"],
                row["direct_subfolder_count"],
                row["stats_updated_at"],
            )
            if bool(row["missing"]):
                expected = (None, None, None, None, None, None)
            elif expected_time is None:
                # There is no catalogue-owned timestamp from which the visible
                # provenance value can be reconstructed exactly.
                expected = None
            else:
                values = canonical.get(int(row["id"]))
                expected = (*values, expected_time) if values is not None else None
            if expected is None or original != expected:
                exceptions.append((int(row["id"]), *original))
        if exceptions:
            connection.executemany(
                """
                INSERT INTO folder_state_exceptions (
                    folder_id, recursive_size_bytes, recursive_file_count,
                    recursive_subfolder_count, direct_file_count,
                    direct_subfolder_count, stats_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                exceptions,
            )
        _emit(
            progress_callback,
            "classify_derived",
            index,
            len(volumes),
            "Verifying regenerable folder statistics…",
        )


def _analysis_reconstruction_metadata(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    state_row = connection.execute(
        "SELECT * FROM original.backup_analysis_state WHERE id = 1"
    ).fetchone()
    if state_row is None or state_row["active_run_id"] is None:
        return {
            "storage": "none",
            "requested": False,
            "source_was_stale": False,
            "source_rules_version": None,
            "source_status": "not_analyzed",
        }
    run_row = connection.execute(
        "SELECT * FROM original.backup_analysis_runs WHERE id = ?",
        (state_row["active_run_id"],),
    ).fetchone()
    if run_row is None:
        return {
            "storage": "stored",
            "requested": True,
            "source_was_stale": True,
            "source_rules_version": None,
            "source_status": "invalid",
        }

    snapshot = [
        [
            int(row["id"]),
            row["last_scan_at"],
            int(row["indexed_file_count"] or 0),
            int(row["indexed_folder_count"] or 0),
        ]
        for row in connection.execute(
            """
            SELECT id, last_scan_at, indexed_file_count, indexed_folder_count
            FROM original.volumes ORDER BY id
            """
        )
    ]
    signature = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    current = (
        not bool(state_row["forced_stale"])
        and int(run_row["rules_version"] or 0) == RULES_VERSION
        and str(run_row["source_signature"] or "") == signature
        and str(run_row["status"] or "") == "completed"
    )
    metadata: dict[str, Any] = {
        "storage": "regenerate" if current else "stored",
        "requested": True,
        "source_was_stale": not current,
        "source_rules_version": int(run_row["rules_version"] or 0),
        "source_status": str(run_row["status"] or ""),
    }
    if current:
        metadata["source_run"] = {
            column: run_row[column]
            for column in ANALYSIS_TABLE_COLUMNS["backup_analysis_runs"]
        }
        metadata["source_state"] = {
            column: state_row[column]
            for column in ANALYSIS_TABLE_COLUMNS["backup_analysis_state"]
        }
        sequence_rows = connection.execute(
            """
            SELECT name, seq FROM original.sqlite_sequence
            WHERE name IN ('backup_analysis_runs', 'backup_analysis_invalidations')
            ORDER BY name
            """
        )
        metadata["source_sequences"] = {
            str(row["name"]): int(row["seq"])
            for row in sequence_rows
        }
    return metadata


def _create_and_copy_stored_analysis(
    connection: sqlite3.Connection,
    table_rows: dict[str, int],
) -> None:
    for statement in ANALYSIS_SCHEMA_SQL:
        if statement.lstrip().casefold().startswith("create table"):
            connection.execute(statement)
    for table, columns in ANALYSIS_TABLE_COLUMNS.items():
        column_sql = _quoted_columns(columns)
        connection.execute(
            f'INSERT INTO "{table}" ({column_sql}) '
            f'SELECT {column_sql} FROM original."{table}"'
        )
        table_rows[table] = int(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        )


def _create_payload(
    source_path: Path,
    payload_path: Path,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> tuple[dict[str, int], dict[str, Any]]:
    # uri=True also enables URI filenames for ATTACH, allowing the source to
    # be opened explicitly read-only while this main payload remains writable.
    connection = sqlite3.connect(payload_path, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA temp_store = FILE")
    connection.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    try:
        _attach_catalogue_read_only(connection, source_path)
        connection.execute("BEGIN")
        _emit(progress_callback, "validate_source", 0, 0, "Checking catalogue integrity…")
        _validate_catalogue_schema(connection)
        _check_sqlite_integrity(connection, "original", CatalogueBackupError)
        _check_cancel(cancel_callback)

        for statement in PAYLOAD_SCHEMA_SQL:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {PAYLOAD_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {PAYLOAD_FORMAT_VERSION}")

        table_rows = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM original."{table}"').fetchone()[0]
            )
            for table in SOURCE_TABLES
        }
        analysis_reconstruction = _analysis_reconstruction_metadata(connection)

        copy_steps = len(SOURCE_TABLES) + 1
        completed = 0

        for table in ("volumes", "folders"):
            columns = AUTHORITATIVE_TABLE_COLUMNS[table]
            column_sql = _quoted_columns(columns)
            connection.execute(
                f'INSERT INTO "{table}" ({column_sql}) '
                f'SELECT {column_sql} FROM original."{table}" ORDER BY id'
            )
            completed += 1
            _emit(
                progress_callback,
                "copy_source",
                completed,
                copy_steps,
                f"Saving irreducible {table.replace('_', ' ')}…",
            )
            _check_cancel(cancel_callback)

        _emit(
            progress_callback,
            "deduplicate_hashes",
            completed,
            copy_steps,
            "Deduplicating saved content hashes…",
        )
        connection.execute(
            """
            INSERT INTO content_blobs (digest)
            SELECT content_hash
            FROM original.files
            WHERE content_hash IS NOT NULL
            GROUP BY content_hash
            ORDER BY content_hash
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX content_blobs_digest_copy_index ON content_blobs(digest)"
        )
        table_rows["content_blobs"] = int(
            connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
        )
        completed += 1
        _emit(
            progress_callback,
            "deduplicate_hashes",
            completed,
            copy_steps,
            "Deduplicating saved content hashes…",
        )

        file_columns = AUTHORITATIVE_TABLE_COLUMNS["files"]
        _emit(
            progress_callback,
            "copy_source",
            completed,
            copy_steps,
            "Saving file records…",
        )
        connection.execute(
            f"""
            INSERT INTO files ({_quoted_columns(file_columns)})
            SELECT f.id, f.volume_id, f.folder_id, f.name, f.relative_path,
                   f.extension, f.size_bytes, f.modified_at, f.missing,
                   f.scanned_at, f.identity_device, f.identity_inode,
                   b.id, f.content_hash_algorithm
            FROM original.files f
            LEFT JOIN content_blobs b ON b.digest = f.content_hash
            ORDER BY f.id
            """
        )
        connection.execute("DROP INDEX content_blobs_digest_copy_index")
        completed += 1
        _emit(
            progress_callback,
            "copy_source",
            completed,
            copy_steps,
            "Saving file records…",
        )

        for table in (
            "volume_register",
            "file_media_metadata",
            "scan_history",
            "scan_errors",
        ):
            columns = AUTHORITATIVE_TABLE_COLUMNS[table]
            column_sql = _quoted_columns(columns)
            order_column = columns[0]
            connection.execute(
                f'INSERT INTO "{table}" ({column_sql}) '
                f'SELECT {column_sql} FROM original."{table}" ORDER BY "{order_column}"'
            )
            completed += 1
            _emit(
                progress_callback,
                "copy_source",
                completed,
                copy_steps,
                f"Saving irreducible {table.replace('_', ' ')}…",
            )
            _check_cancel(cancel_callback)

        sequence_table_exists = connection.execute(
            """
            SELECT 1 FROM original.sqlite_schema
            WHERE type = 'table' AND name = 'sqlite_sequence'
            """
        ).fetchone()
        if sequence_table_exists:
            sequence_names = list(AUTOINCREMENT_SOURCE_TABLES)
            if analysis_reconstruction["storage"] == "stored":
                sequence_names.extend(ANALYSIS_AUTOINCREMENT_TABLES)
            placeholders = ",".join("?" for _ in sequence_names)
            connection.execute(
                f"""
                INSERT INTO catalogue_sequences (name, seq)
                SELECT name, seq FROM original.sqlite_sequence
                WHERE name IN ({placeholders})
                ORDER BY name
                """,
                sequence_names,
            )
        table_rows["catalogue_sequences"] = int(
            connection.execute("SELECT COUNT(*) FROM catalogue_sequences").fetchone()[0]
        )
        _store_derived_exceptions(
            connection,
            progress_callback,
            cancel_callback,
        )
        table_rows["folder_state_exceptions"] = int(
            connection.execute("SELECT COUNT(*) FROM folder_state_exceptions").fetchone()[0]
        )
        table_rows["volume_count_exceptions"] = int(
            connection.execute("SELECT COUNT(*) FROM volume_count_exceptions").fetchone()[0]
        )
        if analysis_reconstruction["storage"] == "stored":
            _emit(
                progress_callback,
                "preserve_analysis",
                0,
                0,
                "Preserving non-regenerable stale backup evidence…",
            )
            _create_and_copy_stored_analysis(connection, table_rows)
        connection.commit()
        connection.execute("DETACH DATABASE original")
        connection.execute("PRAGMA foreign_keys = ON")
        _check_sqlite_integrity(connection, "main", CatalogueBackupError)
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue backup operation was cancelled.") from exc
        raise CatalogueBackupError(f"The catalogue backup could not be created: {exc}") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()

    _check_cancel(cancel_callback)
    _emit(progress_callback, "compact", 0, 0, "Compacting source data…")
    compact = sqlite3.connect(payload_path)
    compact.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    try:
        compact.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue backup operation was cancelled.") from exc
        raise CatalogueBackupError(f"The backup source could not be compacted: {exc}") from exc
    finally:
        compact.set_progress_handler(None, 0)
        compact.close()
    return table_rows, analysis_reconstruction


def _sha256_file(
    path: Path,
    cancel_callback: CancelCallback | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            _check_cancel(cancel_callback)
            chunk = source.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_for_payload(
    source_path: Path,
    payload_path: Path,
    payload_sha256: str,
    table_rows: Mapping[str, int],
    analysis_reconstruction: Mapping[str, Any],
) -> dict[str, Any]:
    all_schemas = {**AUTHORITATIVE_TABLE_COLUMNS, **ANALYSIS_TABLE_COLUMNS}
    schemas = {
        table: list(all_schemas[table])
        for table in table_rows
    }
    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": _utc_now(),
        "application_version": APPLICATION_VERSION,
        "minimum_application_version": APPLICATION_VERSION,
        "catalogue_schema_version": SCHEMA_VERSION,
        "source_catalogue_name": source_path.name,
        "source_catalogue_size": source_path.stat().st_size,
        "components": [
            {
                "path": PAYLOAD_PATH,
                "media_type": "application/vnd.sqlite3",
                "payload_format_version": PAYLOAD_FORMAT_VERSION,
                "size": payload_path.stat().st_size,
                "sha256": payload_sha256,
                "tables": {name: int(count) for name, count in table_rows.items()},
                "schema": schemas,
            }
        ],
        "reconstruction": {
            "search_indexes": "rebuild_from_files_and_folders",
            "folder_aggregates": "rebuild_from_hierarchy_and_files",
            "folder_aggregate_algorithm_version": FOLDER_AGGREGATE_ALGORITHM_VERSION,
            "volume_counts": "recount_non_missing_rows",
            "volume_count_algorithm_version": VOLUME_COUNT_ALGORITHM_VERSION,
            "search_schema_version": SCHEMA_VERSION,
            "backup_analysis": dict(analysis_reconstruction),
            "backup_analysis_rules_version": RULES_VERSION,
        },
        "omitted_derived": [
            "SQLite free pages and journals",
            "ordinary SQL indexes and triggers",
            "files_fts and folders_fts search indexes",
            "folder aggregate statistics",
            "volume indexed row counts",
            "current backup-analysis result tables (stale generations are retained)",
        ],
    }


def _write_archive(
    archive_path: Path,
    payload_path: Path,
    manifest: Mapping[str, Any],
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> None:
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_size = payload_path.stat().st_size
    written = 0
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        manifest_info = zipfile.ZipInfo(MANIFEST_PATH)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o600 << 16
        archive.writestr(manifest_info, manifest_bytes, compresslevel=9)

        payload_info = zipfile.ZipInfo(PAYLOAD_PATH)
        payload_info.compress_type = zipfile.ZIP_DEFLATED
        payload_info.external_attr = 0o600 << 16
        with payload_path.open("rb") as source, archive.open(
            payload_info,
            "w",
            force_zip64=True,
        ) as destination:
            while True:
                _check_cancel(cancel_callback)
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                destination.write(chunk)
                written += len(chunk)
                _emit(
                    progress_callback,
                    "compress",
                    written,
                    payload_size,
                    "Compressing semantic backup…",
                )


def create_catalogue_backup(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    overwrite: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> BackupResult:
    source = catalogue_path_with_extension(source_path).resolve(strict=False)
    target = Path(backup_path).expanduser()
    if target.suffix.casefold() != ".zip":
        target = Path(f"{target}.zip")
    target = target.resolve(strict=False)
    if not source.is_file():
        raise CatalogueBackupError(f"Catalogue file does not exist: {source}")
    if target.exists() and not overwrite:
        raise FileExistsError(f"Backup already exists: {target}")
    if source == target:
        raise CatalogueBackupError("The backup path must be different from the catalogue path.")

    target.parent.mkdir(parents=True, exist_ok=True)
    payload_fd, payload_name = tempfile.mkstemp(suffix=".sqlite", prefix="jvvv-backup-")
    os.close(payload_fd)
    payload = Path(payload_name)
    archive_fd, archive_name = tempfile.mkstemp(
        suffix=".creating",
        prefix=f"{target.name}.",
        dir=target.parent,
    )
    os.close(archive_fd)
    archive_temp = Path(archive_name)
    try:
        table_rows, analysis = _create_payload(
            source,
            payload,
            progress_callback,
            cancel_callback,
        )
        _emit(progress_callback, "checksum", 0, 0, "Calculating integrity checksum…")
        payload_sha256 = _sha256_file(payload, cancel_callback)
        manifest = _manifest_for_payload(
            source,
            payload,
            payload_sha256,
            table_rows,
            analysis,
        )
        _write_archive(
            archive_temp,
            payload,
            manifest,
            progress_callback,
            cancel_callback,
        )
        _check_cancel(cancel_callback)
        _validate_archive_to_payload(
            archive_temp,
            payload.with_suffix(".validated.sqlite"),
            cancel_callback=cancel_callback,
        )
        payload.with_suffix(".validated.sqlite").unlink(missing_ok=True)
        os.replace(archive_temp, target)
    except Exception:
        archive_temp.unlink(missing_ok=True)
        payload.with_suffix(".validated.sqlite").unlink(missing_ok=True)
        raise
    finally:
        payload.unlink(missing_ok=True)

    original_size = source.stat().st_size
    backup_size = target.stat().st_size
    savings = original_size - backup_size
    percent = (savings * 100.0 / original_size) if original_size else 0.0
    _emit(progress_callback, "complete", 1, 1, "Catalogue backup created.")
    return BackupResult(
        source,
        target,
        original_size,
        backup_size,
        int(manifest["components"][0]["size"]),
        savings,
        percent,
        dict(table_rows),
    )


def _safe_member_names(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise InvalidBackupError("The backup contains duplicate archive members.")
    expected = {MANIFEST_PATH, PAYLOAD_PATH}
    if set(names) != expected:
        missing = expected - set(names)
        extra = set(names) - expected
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected " + ", ".join(sorted(extra)))
        raise InvalidBackupError(
            "The backup component list is invalid" + (f" ({'; '.join(details)})" if details else "") + "."
        )
    for info in infos:
        path = PurePosixPath(info.filename)
        if info.is_dir() or path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            raise InvalidBackupError("The backup contains an unsafe archive member path.")
        if info.flag_bits & 0x1:
            raise InvalidBackupError("Encrypted catalogue backups are not supported.")
    return {info.filename: info for info in infos}


def _read_manifest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    if info.file_size > MAX_MANIFEST_BYTES:
        raise InvalidBackupError("The backup manifest is unreasonably large.")
    try:
        raw = archive.read(info)
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError, zipfile.BadZipFile) as exc:
        raise InvalidBackupError("The backup manifest is damaged or unreadable.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise InvalidBackupError("The selected ZIP is not a JVVV catalogue backup.")
    version = manifest.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidBackupError("The backup manifest has no valid format version.")
    if version > BACKUP_FORMAT_VERSION:
        raise UnsupportedBackupError(
            f"This backup uses format version {version}; this JVVV version supports "
            f"up to version {BACKUP_FORMAT_VERSION}."
        )
    if version != BACKUP_FORMAT_VERSION:
        raise UnsupportedBackupError(f"Backup format version {version} is not supported.")
    catalogue_version = manifest.get("catalogue_schema_version")
    if not isinstance(catalogue_version, int) or isinstance(catalogue_version, bool):
        raise InvalidBackupError("The backup has no valid catalogue schema version.")
    if catalogue_version > SCHEMA_VERSION:
        raise UnsupportedBackupError(
            f"The restored catalogue requires schema version {catalogue_version}; this "
            f"JVVV version supports up to version {SCHEMA_VERSION}."
        )
    components = manifest.get("components")
    if not isinstance(components, list) or len(components) != 1:
        raise InvalidBackupError("The backup manifest has an invalid component list.")
    component = components[0]
    if not isinstance(component, dict) or component.get("path") != PAYLOAD_PATH:
        raise InvalidBackupError("The backup source component is not declared correctly.")
    if component.get("payload_format_version") != PAYLOAD_FORMAT_VERSION:
        raise UnsupportedBackupError("The backup source payload version is not supported.")
    size = component.get("size")
    checksum = component.get("sha256")
    tables = component.get("tables")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or not isinstance(tables, dict)
    ):
        raise InvalidBackupError("The backup source component metadata is invalid.")
    try:
        bytes.fromhex(checksum)
    except ValueError as exc:
        raise InvalidBackupError("The backup source checksum is invalid.") from exc
    reconstruction = manifest.get("reconstruction")
    analysis = reconstruction.get("backup_analysis") if isinstance(reconstruction, dict) else None
    if not isinstance(reconstruction, dict) or (
        reconstruction.get("folder_aggregate_algorithm_version")
        != FOLDER_AGGREGATE_ALGORITHM_VERSION
        or reconstruction.get("volume_count_algorithm_version")
        != VOLUME_COUNT_ALGORITHM_VERSION
        or reconstruction.get("search_schema_version") != SCHEMA_VERSION
    ):
        raise UnsupportedBackupError(
            "The backup requires an unsupported catalogue reconstruction algorithm."
        )
    if not isinstance(analysis, dict) or analysis.get("storage") not in {
        "none",
        "regenerate",
        "stored",
    }:
        raise InvalidBackupError("The backup reconstruction instructions are invalid.")
    if analysis["storage"] == "regenerate":
        source_run = analysis.get("source_run")
        source_state = analysis.get("source_state")
        source_sequences = analysis.get("source_sequences")
        if (
            not isinstance(source_run, dict)
            or set(source_run) != set(ANALYSIS_TABLE_COLUMNS["backup_analysis_runs"])
            or not isinstance(source_state, dict)
            or set(source_state) != set(ANALYSIS_TABLE_COLUMNS["backup_analysis_state"])
            or not isinstance(source_sequences, dict)
            or set(source_sequences) - set(ANALYSIS_AUTOINCREMENT_TABLES)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in source_sequences.values()
            )
        ):
            raise InvalidBackupError(
                "The backup-analysis reconstruction identity is invalid."
            )
    expected_tables = set(AUTHORITATIVE_TABLE_COLUMNS)
    if analysis["storage"] == "stored":
        expected_tables.update(ANALYSIS_TABLE_COLUMNS)
    if set(tables) != expected_tables or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in tables.values()
    ):
        raise InvalidBackupError("The backup table inventory is invalid.")
    return manifest


def _validate_payload(
    payload_path: Path,
    manifest: Mapping[str, Any],
    cancel_callback: CancelCallback | None,
) -> None:
    component = manifest["components"][0]
    connection = sqlite3.connect(f"{payload_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        payload_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != PAYLOAD_APPLICATION_ID:
            raise InvalidBackupError("The backup source payload has an invalid application ID.")
        if payload_version > PAYLOAD_FORMAT_VERSION:
            raise UnsupportedBackupError(
                f"Backup payload version {payload_version} is newer than this JVVV version."
            )
        if payload_version != PAYLOAD_FORMAT_VERSION:
            raise UnsupportedBackupError(f"Backup payload version {payload_version} is not supported.")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_schemas = dict(AUTHORITATIVE_TABLE_COLUMNS)
        analysis = manifest["reconstruction"]["backup_analysis"]
        if analysis["storage"] == "stored":
            expected_schemas.update(ANALYSIS_TABLE_COLUMNS)
        if tables != set(expected_schemas):
            raise InvalidBackupError("The backup source payload has an unexpected table schema.")
        manifest_schema = component.get("schema")
        if not isinstance(manifest_schema, dict):
            raise InvalidBackupError("The backup manifest has no source schema declaration.")
        for table, expected_columns in expected_schemas.items():
            actual = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            if actual != expected_columns or manifest_schema.get(table) != list(expected_columns):
                raise InvalidBackupError(
                    f"The backup source table '{table}' does not match its declared schema."
                )
            actual_count = int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            if actual_count != int(component["tables"][table]):
                raise InvalidBackupError(
                    f"The backup source table '{table}' has an invalid row count."
                )
        _check_sqlite_integrity(connection, "main", InvalidBackupError)
    except sqlite3.OperationalError as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue restore operation was cancelled.") from exc
        raise InvalidBackupError(f"The backup source payload cannot be read: {exc}") from exc
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def _validate_archive_to_payload(
    backup_path: Path,
    payload_path: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> BackupInspection:
    _check_cancel(cancel_callback)
    _emit(progress_callback, "validate", 0, 0, "Validating backup archive…")
    try:
        with zipfile.ZipFile(backup_path, "r", allowZip64=True) as archive:
            infos = _safe_member_names(archive)
            manifest = _read_manifest(archive, infos[MANIFEST_PATH])
            component = manifest["components"][0]
            payload_info = infos[PAYLOAD_PATH]
            if payload_info.file_size != int(component["size"]):
                raise InvalidBackupError("The backup source size does not match its manifest.")
            digest = hashlib.sha256()
            copied = 0
            with archive.open(payload_info, "r") as source, payload_path.open("wb") as destination:
                while True:
                    _check_cancel(cancel_callback)
                    chunk = source.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    destination.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    if copied > int(component["size"]):
                        raise InvalidBackupError("The backup source exceeds its declared size.")
                    _emit(
                        progress_callback,
                        "validate",
                        copied,
                        int(component["size"]),
                        "Checking backup integrity…",
                    )
            if copied != int(component["size"]):
                raise InvalidBackupError("The backup source is truncated.")
            if digest.hexdigest() != str(component["sha256"]).casefold():
                raise InvalidBackupError("The backup source checksum does not match its manifest.")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        if isinstance(exc, CatalogueBackupError):
            raise
        raise InvalidBackupError("The selected file is not a readable JVVV backup ZIP.") from exc

    _validate_payload(payload_path, manifest, cancel_callback)
    return BackupInspection(
        backup_path,
        manifest,
        backup_path.stat().st_size,
        int(manifest["components"][0]["size"]),
        {name: int(value) for name, value in manifest["components"][0]["tables"].items()},
    )


def validate_catalogue_backup(
    backup_path: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> BackupInspection:
    path = Path(backup_path).expanduser().resolve(strict=False)
    if not path.is_file():
        raise InvalidBackupError(f"Backup file does not exist: {path}")
    with tempfile.TemporaryDirectory(prefix="jvvv-validate-") as temp_directory:
        return _validate_archive_to_payload(
            path,
            Path(temp_directory) / PAYLOAD_PATH,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )


def _copy_payload_to_catalogue(
    db: Database,
    payload_path: Path,
    manifest: Mapping[str, Any],
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> None:
    connection = db.connection
    connection.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    connection.execute(
        "ATTACH DATABASE ? AS source_backup",
        (str(payload_path.resolve()),),
    )
    try:
        with db.transaction(immediate=True) as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            for trigger in FTS_TRIGGERS:
                conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')

            volume_columns = AUTHORITATIVE_TABLE_COLUMNS["volumes"]
            conn.execute(
                f"""
                INSERT INTO volumes ({_quoted_columns(volume_columns)})
                SELECT {_quoted_columns(volume_columns)}
                FROM source_backup.volumes ORDER BY id
                """
            )
            _emit(progress_callback, "restore_source", 1, len(SOURCE_TABLES), "Restoring volumes…")

            register_columns = AUTHORITATIVE_TABLE_COLUMNS["volume_register"]
            conn.execute(
                f"""
                INSERT INTO volume_register ({_quoted_columns(register_columns)})
                SELECT {_quoted_columns(register_columns)}
                FROM source_backup.volume_register ORDER BY volume_id
                """
            )
            _emit(progress_callback, "restore_source", 2, len(SOURCE_TABLES), "Restoring catalogue metadata…")

            folder_columns = AUTHORITATIVE_TABLE_COLUMNS["folders"]
            conn.execute(
                f"""
                INSERT INTO folders ({_quoted_columns(folder_columns)})
                SELECT {_quoted_columns(folder_columns)}
                FROM source_backup.folders ORDER BY id
                """
            )
            _emit(progress_callback, "restore_source", 3, len(SOURCE_TABLES), "Restoring folder hierarchy…")

            destination_file_columns = (
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
            )
            _emit(
                progress_callback,
                "restore_source",
                3,
                len(SOURCE_TABLES),
                "Restoring file records and hashes…",
            )
            conn.execute(
                f"""
                INSERT INTO files ({_quoted_columns(destination_file_columns)})
                SELECT f.id, f.volume_id, f.folder_id, f.name, f.relative_path,
                       f.extension, f.size_bytes, f.modified_at, f.missing,
                       f.scanned_at, f.identity_device, f.identity_inode,
                       b.digest, f.content_hash_algorithm
                FROM source_backup.files f
                LEFT JOIN source_backup.content_blobs b ON b.id = f.content_hash_id
                ORDER BY f.id
                """
            )
            _emit(progress_callback, "restore_source", 4, len(SOURCE_TABLES), "Restoring file records and hashes…")

            for offset, table in enumerate(
                ("file_media_metadata", "scan_history", "scan_errors"),
                start=5,
            ):
                columns = AUTHORITATIVE_TABLE_COLUMNS[table]
                conn.execute(
                    f'INSERT INTO "{table}" ({_quoted_columns(columns)}) '
                    f'SELECT {_quoted_columns(columns)} FROM source_backup."{table}" '
                    f'ORDER BY "{columns[0]}"'
                )
                _emit(
                    progress_callback,
                    "restore_source",
                    offset,
                    len(SOURCE_TABLES),
                    f"Restoring {table.replace('_', ' ')}…",
                )

            analysis = manifest["reconstruction"]["backup_analysis"]
            if analysis["storage"] == "stored":
                conn.execute("DELETE FROM backup_analysis_state")
                analysis_order = (
                    "backup_analysis_runs",
                    "backup_analysis_volume_snapshots",
                    "backup_file_results",
                    "backup_folder_results",
                    "backup_folder_drive_matches",
                    "backup_volume_results",
                    "backup_mirror_candidates",
                    "backup_analysis_invalidations",
                    "backup_analysis_state",
                )
                for table in analysis_order:
                    columns = ANALYSIS_TABLE_COLUMNS[table]
                    conn.execute(
                        f'INSERT INTO "{table}" ({_quoted_columns(columns)}) '
                        f'SELECT {_quoted_columns(columns)} '
                        f'FROM source_backup."{table}"'
                    )
                _emit(
                    progress_callback,
                    "restore_source",
                    len(SOURCE_TABLES),
                    len(SOURCE_TABLES),
                    "Restoring preserved backup evidence…",
                )

            for row in conn.execute(
                "SELECT name, seq FROM source_backup.catalogue_sequences ORDER BY name"
            ):
                conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (row["name"],))
                conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                    (row["name"], row["seq"]),
                )
        _check_cancel(cancel_callback)
    except sqlite3.Error as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue restore operation was cancelled.") from exc
        raise InvalidBackupError(
            "The backup source data violates catalogue constraints and cannot be "
            f"restored safely: {exc}"
        ) from exc
    finally:
        connection.execute("DETACH DATABASE source_backup")


def _apply_derived_exceptions(db: Database, payload_path: Path) -> None:
    connection = db.connection
    connection.execute(
        "ATTACH DATABASE ? AS source_backup",
        (str(payload_path.resolve()),),
    )
    try:
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE folders
                SET recursive_size_bytes = (
                        SELECT recursive_size_bytes
                        FROM source_backup.folder_state_exceptions e
                        WHERE e.folder_id = folders.id
                    ),
                    recursive_file_count = (
                        SELECT recursive_file_count
                        FROM source_backup.folder_state_exceptions e
                        WHERE e.folder_id = folders.id
                    ),
                    recursive_subfolder_count = (
                        SELECT recursive_subfolder_count
                        FROM source_backup.folder_state_exceptions e
                        WHERE e.folder_id = folders.id
                    ),
                    direct_file_count = (
                        SELECT direct_file_count
                        FROM source_backup.folder_state_exceptions e
                        WHERE e.folder_id = folders.id
                    ),
                    direct_subfolder_count = (
                        SELECT direct_subfolder_count
                        FROM source_backup.folder_state_exceptions e
                        WHERE e.folder_id = folders.id
                    ),
                    stats_updated_at = (
                        SELECT stats_updated_at
                        FROM source_backup.folder_state_exceptions e
                        WHERE e.folder_id = folders.id
                    )
                WHERE id IN (
                    SELECT folder_id FROM source_backup.folder_state_exceptions
                )
                """
            )
            conn.execute(
                """
                UPDATE volumes
                SET indexed_file_count = (
                        SELECT indexed_file_count
                        FROM source_backup.volume_count_exceptions e
                        WHERE e.volume_id = volumes.id
                    ),
                    indexed_folder_count = (
                        SELECT indexed_folder_count
                        FROM source_backup.volume_count_exceptions e
                        WHERE e.volume_id = volumes.id
                    )
                WHERE id IN (
                    SELECT volume_id FROM source_backup.volume_count_exceptions
                )
                """
            )
    finally:
        connection.execute("DETACH DATABASE source_backup")


def _restore_regenerated_analysis_identity(
    db: Database,
    analysis: Mapping[str, Any],
    generated_run_id: int,
) -> None:
    source_run = analysis["source_run"]
    source_state = analysis["source_state"]
    source_run_id = int(source_run["id"])
    connection = db.connection
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if generated_run_id != source_run_id:
            for table in (
                "backup_analysis_volume_snapshots",
                "backup_file_results",
                "backup_folder_results",
                "backup_folder_drive_matches",
                "backup_volume_results",
                "backup_mirror_candidates",
            ):
                connection.execute(
                    f'UPDATE "{table}" SET run_id = ? WHERE run_id = ?',
                    (source_run_id, generated_run_id),
                )
            connection.execute(
                "UPDATE backup_analysis_runs SET id = ? WHERE id = ?",
                (source_run_id, generated_run_id),
            )

        run_columns = ANALYSIS_TABLE_COLUMNS["backup_analysis_runs"][1:]
        connection.execute(
            "UPDATE backup_analysis_runs SET "
            + ", ".join(f'"{column}" = ?' for column in run_columns)
            + " WHERE id = ?",
            tuple(source_run[column] for column in run_columns) + (source_run_id,),
        )
        state_columns = ANALYSIS_TABLE_COLUMNS["backup_analysis_state"][1:]
        connection.execute(
            "UPDATE backup_analysis_state SET "
            + ", ".join(f'"{column}" = ?' for column in state_columns)
            + " WHERE id = ?",
            tuple(source_state[column] for column in state_columns)
            + (int(source_state["id"]),),
        )
        for name, sequence in analysis.get("source_sequences", {}).items():
            connection.execute("DELETE FROM sqlite_sequence WHERE name = ?", (name,))
            connection.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (name, int(sequence)),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise CatalogueBackupError(
            "Regenerated backup evidence could not retain its original identity safely."
        )


def _regenerate_derived_data(
    db: Database,
    payload_path: Path,
    manifest: Mapping[str, Any],
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> tuple[str, ...]:
    volumes = list(
        db.connection.execute(
            "SELECT id, last_scan_at, updated_at FROM volumes ORDER BY id"
        )
    )
    total_volumes = len(volumes)
    for index, volume in enumerate(volumes, start=1):
        _check_cancel(cancel_callback)

        def folder_progress(completed: int, total: int, message: str) -> None:
            _check_cancel(cancel_callback)
            _emit(
                progress_callback,
                "rebuild_folder_aggregates",
                completed,
                total,
                f"{message} (volume {index}/{max(total_volumes, 1)})…",
            )

        db.rebuild_folder_statistics(
            int(volume["id"]),
            # A never-scanned legacy volume has no last_scan_at. Its saved
            # updated_at gives reconstruction a stable catalogue-owned time
            # instead of introducing the wall clock into derived state.
            stats_updated_at=volume["last_scan_at"] or volume["updated_at"],
            progress_callback=folder_progress,
        )

    with db.transaction() as conn:
        conn.execute(
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
    _apply_derived_exceptions(db, payload_path)
    _emit(progress_callback, "rebuild_search", 0, 0, "Rebuilding search indexes…")
    db.rebuild_search_indexes()
    regenerated = ["folder aggregates", "volume counts", "search indexes"]

    analysis = manifest.get("reconstruction", {}).get("backup_analysis", {})
    if isinstance(analysis, dict) and analysis.get("storage") == "regenerate":
        _emit(progress_callback, "rebuild_analysis", 0, 0, "Rebuilding backup evidence…")
        engine = BackupAnalysisEngine(db)

        def analysis_progress(value: Any) -> None:
            _emit(
                progress_callback,
                "rebuild_analysis",
                int(getattr(value, "completed", 0) or 0),
                int(getattr(value, "total", 0) or 0),
                str(getattr(value, "message", "") or "Rebuilding backup evidence…"),
            )

        summary = engine.analyse(
            progress_callback=analysis_progress,
            cancel_callback=cancel_callback,
        )
        if summary.status == "cancelled":
            raise BackupCancelled("The catalogue restore operation was cancelled.")
        if summary.status != "completed":
            raise CatalogueBackupError(
                f"Backup evidence could not be regenerated ({summary.status})."
            )
        if summary.run_id is None:
            raise CatalogueBackupError("Backup evidence regeneration returned no run ID.")
        _restore_regenerated_analysis_identity(db, analysis, int(summary.run_id))
        regenerated.append("backup evidence")
    return tuple(regenerated)


def _has_difference(connection: sqlite3.Connection, left: str, right: str) -> bool:
    return connection.execute(
        f"SELECT 1 FROM ({left} EXCEPT {right}) LIMIT 1"
    ).fetchone() is not None


def _validate_restored_semantics(
    db: Database,
    payload_path: Path,
    manifest: Mapping[str, Any],
    cancel_callback: CancelCallback | None,
) -> None:
    connection = db.connection
    connection.execute(
        "ATTACH DATABASE ? AS source_backup",
        (str(payload_path.resolve()),),
    )
    try:
        for table in (
            "volumes",
            "volume_register",
            "folders",
            "file_media_metadata",
            "scan_history",
            "scan_errors",
        ):
            columns = AUTHORITATIVE_TABLE_COLUMNS[table]
            restored = f'SELECT {_quoted_columns(columns)} FROM main."{table}"'
            source = f'SELECT {_quoted_columns(columns)} FROM source_backup."{table}"'
            if _has_difference(connection, restored, source) or _has_difference(
                connection, source, restored
            ):
                raise CatalogueBackupError(
                    f"Restored source validation failed for the '{table}' component."
                )

        restored_files = """
            SELECT f.id, f.volume_id, f.folder_id, f.name, f.relative_path,
                   f.extension, f.size_bytes, f.modified_at, f.missing,
                   f.scanned_at, f.identity_device, f.identity_inode,
                   f.content_hash, f.content_hash_algorithm
            FROM main.files f
        """
        source_files = """
            SELECT f.id, f.volume_id, f.folder_id, f.name, f.relative_path,
                   f.extension, f.size_bytes, f.modified_at, f.missing,
                   f.scanned_at, f.identity_device, f.identity_inode,
                   b.digest, f.content_hash_algorithm
            FROM source_backup.files f
            LEFT JOIN source_backup.content_blobs b ON b.id = f.content_hash_id
        """
        if _has_difference(connection, restored_files, source_files) or _has_difference(
            connection, source_files, restored_files
        ):
            raise CatalogueBackupError("Restored source validation failed for file records.")

        restored_folder_exceptions = """
            SELECT f.id, f.recursive_size_bytes, f.recursive_file_count,
                   f.recursive_subfolder_count, f.direct_file_count,
                   f.direct_subfolder_count, f.stats_updated_at
            FROM main.folders f
            JOIN source_backup.folder_state_exceptions e ON e.folder_id = f.id
        """
        source_folder_exceptions = """
            SELECT folder_id, recursive_size_bytes, recursive_file_count,
                   recursive_subfolder_count, direct_file_count,
                   direct_subfolder_count, stats_updated_at
            FROM source_backup.folder_state_exceptions
        """
        if _has_difference(
            connection,
            restored_folder_exceptions,
            source_folder_exceptions,
        ) or _has_difference(
            connection,
            source_folder_exceptions,
            restored_folder_exceptions,
        ):
            raise CatalogueBackupError(
                "Restored folder-statistic exception validation failed."
            )

        restored_volume_exceptions = """
            SELECT v.id, v.indexed_file_count, v.indexed_folder_count
            FROM main.volumes v
            JOIN source_backup.volume_count_exceptions e ON e.volume_id = v.id
        """
        source_volume_exceptions = """
            SELECT volume_id, indexed_file_count, indexed_folder_count
            FROM source_backup.volume_count_exceptions
        """
        if _has_difference(
            connection,
            restored_volume_exceptions,
            source_volume_exceptions,
        ) or _has_difference(
            connection,
            source_volume_exceptions,
            restored_volume_exceptions,
        ):
            raise CatalogueBackupError("Restored volume-count exception validation failed.")

        restored_sequences = """
            SELECT m.name, m.seq FROM main.sqlite_sequence m
            JOIN source_backup.catalogue_sequences s ON s.name = m.name
        """
        source_sequences = "SELECT name, seq FROM source_backup.catalogue_sequences"
        if _has_difference(connection, source_sequences, restored_sequences):
            raise CatalogueBackupError("Restored catalogue ID sequence validation failed.")

        analysis = manifest["reconstruction"]["backup_analysis"]
        if analysis["storage"] == "stored":
            for table, columns in ANALYSIS_TABLE_COLUMNS.items():
                restored = f'SELECT {_quoted_columns(columns)} FROM main."{table}"'
                source = f'SELECT {_quoted_columns(columns)} FROM source_backup."{table}"'
                if _has_difference(connection, restored, source) or _has_difference(
                    connection,
                    source,
                    restored,
                ):
                    raise CatalogueBackupError(
                        f"Preserved backup-evidence validation failed for '{table}'."
                    )
        elif analysis["storage"] == "regenerate":
            run_id = int(analysis["source_run"]["id"])
            run = connection.execute(
                "SELECT * FROM main.backup_analysis_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            state = connection.execute(
                "SELECT * FROM main.backup_analysis_state WHERE id = ?",
                (int(analysis["source_state"]["id"]),),
            ).fetchone()
            if run is None or state is None or any(
                run[column] != analysis["source_run"][column]
                for column in ANALYSIS_TABLE_COLUMNS["backup_analysis_runs"]
            ) or any(
                state[column] != analysis["source_state"][column]
                for column in ANALYSIS_TABLE_COLUMNS["backup_analysis_state"]
            ):
                raise CatalogueBackupError(
                    "Regenerated backup-evidence identity validation failed."
                )
            for name, expected_sequence in analysis["source_sequences"].items():
                row = connection.execute(
                    "SELECT seq FROM main.sqlite_sequence WHERE name = ?",
                    (name,),
                ).fetchone()
                if row is None or int(row["seq"]) != int(expected_sequence):
                    raise CatalogueBackupError(
                        "Regenerated backup-evidence sequence validation failed."
                    )
        _check_cancel(cancel_callback)
    except sqlite3.OperationalError as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue restore operation was cancelled.") from exc
        raise CatalogueBackupError(f"The restored catalogue could not be validated: {exc}") from exc
    finally:
        connection.execute("DETACH DATABASE source_backup")


def restore_catalogue_backup(
    backup_path: str | Path,
    catalogue_path: str | Path,
    *,
    overwrite: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> RestoreResult:
    backup = Path(backup_path).expanduser().resolve(strict=False)
    target = catalogue_path_with_extension(catalogue_path).resolve(strict=False)
    if not backup.is_file():
        raise InvalidBackupError(f"Backup file does not exist: {backup}")

    with tempfile.TemporaryDirectory(prefix="jvvv-restore-source-") as temp_directory:
        payload = Path(temp_directory) / PAYLOAD_PATH
        inspection = _validate_archive_to_payload(
            backup,
            payload,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        _check_cancel(cancel_callback)

        if target.exists() and not overwrite:
            raise FileExistsError(f"Catalogue already exists: {target}")
        destination_sidecars = [
            path
            for path in (Path(f"{target}-wal"), Path(f"{target}-shm"))
            if path.exists()
        ]
        if destination_sidecars:
            raise CatalogueBackupError(
                "The destination has SQLite recovery files and may still be open or "
                "uncleanly closed. Open and close that catalogue normally, or choose "
                "a different destination."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        output_fd, output_name = tempfile.mkstemp(
            prefix=f"{target.name}.",
            suffix=".restoring",
            dir=target.parent,
        )
        os.close(output_fd)
        output = Path(output_name)
        db: Database | None = None
        regenerated: tuple[str, ...] = ()
        try:
            db = Database(output)
            _copy_payload_to_catalogue(
                db,
                payload,
                inspection.manifest,
                progress_callback,
                cancel_callback,
            )
            regenerated = _regenerate_derived_data(
                db,
                payload,
                inspection.manifest,
                progress_callback,
                cancel_callback,
            )
            db.connection.set_progress_handler(
                _sqlite_cancel_handler(cancel_callback),
                10_000,
            )
            _emit(progress_callback, "validate_restored", 0, 0, "Validating restored catalogue…")
            _validate_restored_semantics(
                db,
                payload,
                inspection.manifest,
                cancel_callback,
            )
            _check_sqlite_integrity(db.connection, "main", CatalogueBackupError)
            db.connection.execute("INSERT INTO files_fts(files_fts, rank) VALUES('integrity-check', 1)")
            db.connection.execute("INSERT INTO folders_fts(folders_fts, rank) VALUES('integrity-check', 1)")
            db.connection.commit()
            db.connection.set_progress_handler(None, 0)
            db.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.connection.execute("PRAGMA journal_mode = DELETE").fetchone()
            db.close()
            db = None
            _check_cancel(cancel_callback)
            os.replace(output, target)
        except Exception:
            if db is not None:
                db.close()
            output.unlink(missing_ok=True)
            Path(f"{output}-wal").unlink(missing_ok=True)
            Path(f"{output}-shm").unlink(missing_ok=True)
            raise

    _emit(progress_callback, "complete", 1, 1, "Catalogue restored.")
    return RestoreResult(
        backup,
        target,
        backup.stat().st_size,
        target.stat().st_size,
        regenerated,
    )


__all__ = [
    "BACKUP_FILE_FILTER",
    "BACKUP_FORMAT",
    "BACKUP_FORMAT_VERSION",
    "BackupCancelled",
    "BackupInspection",
    "BackupProgress",
    "BackupResult",
    "CatalogueBackupError",
    "InvalidBackupError",
    "RestoreResult",
    "UnsupportedBackupError",
    "create_catalogue_backup",
    "restore_catalogue_backup",
    "validate_catalogue_backup",
]
