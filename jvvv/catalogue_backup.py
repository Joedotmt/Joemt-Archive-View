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
BACKUP_FORMAT_VERSION = 2
LEGACY_BACKUP_FORMAT_VERSION = 1
PAYLOAD_FORMAT_VERSION = 1
ANALYSIS_PAYLOAD_FORMAT_VERSION = 1
FOLDER_AGGREGATE_ALGORITHM_VERSION = 1
VOLUME_COUNT_ALGORITHM_VERSION = 1
PAYLOAD_APPLICATION_ID = 0x4A565642  # "JVVB"
ANALYSIS_PAYLOAD_APPLICATION_ID = 0x4A565641  # "JVVA"
MANIFEST_PATH = "manifest.json"
PAYLOAD_PATH = "source.sqlite"
ANALYSIS_PAYLOAD_PATH = "analysis.sqlite"
BACKUP_FILE_FILTER = "JVVV Catalogue Backups (*.zip)"
MAX_MANIFEST_BYTES = 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
ZIP_COMPRESSION_LEVEL = 6


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
    analysis_payload_size: int = 0


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

ANALYSIS_SEQUENCE_TABLE = "analysis_sequences"
ANALYSIS_ACCELERATOR_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    **ANALYSIS_TABLE_COLUMNS,
    ANALYSIS_SEQUENCE_TABLE: ("name", "seq"),
}

FTS_TRIGGERS = (
    "files_fts_insert",
    "files_fts_delete",
    "files_fts_update",
    "folders_fts_insert",
    "folders_fts_delete",
    "folders_fts_update",
)

# A restore is built in an unpublished temporary database.  Only these indexes
# materially help reconstruction; every other named index is cheaper to build
# once after the multi-million-row bulk load has finished.
RESTORE_WORK_INDEXES = frozenset(
    {
        "idx_files_identity",
        "idx_files_volume_folder",
        "idx_folders_volume_parent",
        "idx_scan_errors_volume",
        "idx_scan_history_volume",
    }
)

TABLE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "volumes": ("id",),
    "volume_register": ("volume_id",),
    "folders": ("id",),
    "file_media_metadata": ("file_id",),
    "scan_history": ("id",),
    "scan_errors": ("id",),
    "backup_analysis_runs": ("id",),
    "backup_analysis_state": ("id",),
    "backup_analysis_volume_snapshots": ("run_id", "volume_id"),
    "backup_file_results": ("run_id", "file_id"),
    "backup_folder_results": ("run_id", "folder_id"),
    "backup_folder_drive_matches": (
        "run_id",
        "folder_id",
        "target_volume_id",
    ),
    "backup_volume_results": ("run_id", "volume_id"),
    "backup_mirror_candidates": (
        "run_id",
        "source_volume_id",
        "target_volume_id",
    ),
    "backup_analysis_invalidations": ("id",),
}

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
            f"The catalogue uses schema version {version}; this JVVV build only supports "
            f"version {SCHEMA_VERSION}."
        )
    if version != SCHEMA_VERSION:
        raise CatalogueBackupError(
            f"The catalogue uses the retired schema version {version}. This JVVV build "
            f"only backs up the current schema version {SCHEMA_VERSION}."
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
) -> tuple[dict[int, tuple[int, int, int, int, int]], int, int]:
    """Return canonical folder aggregates and non-missing volume row counts."""
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

    indexed_file_count = 0
    for row in connection.execute(
        """
        SELECT folder_id, COUNT(*) AS direct_file_count,
               COALESCE(SUM(size_bytes), 0) AS direct_size
        FROM original.files
        WHERE volume_id = ? AND missing = 0
        GROUP BY folder_id
        """,
        (volume_id,),
    ):
        direct_file_count = int(row["direct_file_count"] or 0)
        indexed_file_count += direct_file_count
        if row["folder_id"] is None:
            continue
        folder_id = int(row["folder_id"])
        if folder_id in stats:
            stats[folder_id]["direct_size"] = int(row["direct_size"] or 0)
            stats[folder_id]["direct_file_count"] = direct_file_count

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

    canonical = {
        folder_id: (
            values["recursive_size"],
            values["recursive_file_count"],
            values["recursive_subfolder_count"],
            values["direct_file_count"],
            values["direct_subfolder_count"],
        )
        for folder_id, values in stats.items()
    }
    return canonical, indexed_file_count, len(folder_rows)


def _store_derived_exceptions(
    connection: sqlite3.Connection,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> tuple[int, int]:
    volumes = list(
        connection.execute(
            """
            SELECT id, last_scan_at, indexed_file_count, indexed_folder_count
            FROM original.volumes ORDER BY id
            """
        )
    )
    folder_exception_count = 0
    volume_exceptions: list[tuple[int, int, int]] = []
    for index, volume in enumerate(volumes, start=1):
        _check_cancel(cancel_callback)
        volume_id = int(volume["id"])
        canonical, indexed_file_count, indexed_folder_count = (
            _canonical_folder_statistics(connection, volume_id)
        )
        if (
            int(volume["indexed_file_count"]) != indexed_file_count
            or int(volume["indexed_folder_count"]) != indexed_folder_count
        ):
            volume_exceptions.append(
                (
                    volume_id,
                    int(volume["indexed_file_count"]),
                    int(volume["indexed_folder_count"]),
                )
            )
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
            folder_exception_count += len(exceptions)
        _emit(
            progress_callback,
            "classify_derived",
            index,
            len(volumes),
            "Verifying regenerable folder statistics…",
        )
    if volume_exceptions:
        connection.executemany(
            """
            INSERT INTO volume_count_exceptions (
                volume_id, indexed_file_count, indexed_folder_count
            ) VALUES (?, ?, ?)
            """,
            volume_exceptions,
        )
    return folder_exception_count, len(volume_exceptions)


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
        "storage": "accelerator" if current else "stored",
        "requested": True,
        "source_was_stale": not current,
        "source_rules_version": int(run_row["rules_version"] or 0),
        "source_status": str(run_row["status"] or ""),
    }
    if current:
        metadata["component"] = ANALYSIS_PAYLOAD_PATH
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
        changes_before = connection.total_changes
        connection.execute(
            f'INSERT INTO "{table}" ({column_sql}) '
            f'SELECT {column_sql} FROM original."{table}"'
        )
        table_rows[table] = connection.total_changes - changes_before


def _qualified_analysis_table_statement(statement: str, database: str) -> str:
    marker = "CREATE TABLE IF NOT EXISTS "
    if marker not in statement:
        raise CatalogueBackupError("The backup-analysis table schema is invalid.")
    return statement.replace(marker, f'{marker}"{database}".', 1)


def _create_and_copy_analysis_accelerator(
    connection: sqlite3.Connection,
    database: str,
) -> dict[str, int]:
    """Copy current analysis tables into a compact attached SQLite payload."""
    for statement in ANALYSIS_SCHEMA_SQL:
        if statement.lstrip().casefold().startswith("create table"):
            connection.execute(
                _qualified_analysis_table_statement(statement, database)
            )
    connection.execute(
        f"""
        CREATE TABLE "{database}"."{ANALYSIS_SEQUENCE_TABLE}" (
            name TEXT PRIMARY KEY,
            seq INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        f"PRAGMA {database}.application_id = {ANALYSIS_PAYLOAD_APPLICATION_ID}"
    )
    connection.execute(
        f"PRAGMA {database}.user_version = {ANALYSIS_PAYLOAD_FORMAT_VERSION}"
    )

    table_rows: dict[str, int] = {}
    for table, columns in ANALYSIS_TABLE_COLUMNS.items():
        column_sql = _quoted_columns(columns)
        order_sql = _quoted_columns(TABLE_PRIMARY_KEYS[table])
        changes_before = connection.total_changes
        connection.execute(
            f'INSERT INTO "{database}"."{table}" ({column_sql}) '
            f'SELECT {column_sql} FROM original."{table}" ORDER BY {order_sql}'
        )
        table_rows[table] = connection.total_changes - changes_before

    placeholders = ",".join("?" for _ in ANALYSIS_AUTOINCREMENT_TABLES)
    changes_before = connection.total_changes
    connection.execute(
        f"""
        INSERT INTO "{database}"."{ANALYSIS_SEQUENCE_TABLE}" (name, seq)
        SELECT name, seq FROM original.sqlite_sequence
        WHERE name IN ({placeholders})
        ORDER BY name
        """,
        ANALYSIS_AUTOINCREMENT_TABLES,
    )
    table_rows[ANALYSIS_SEQUENCE_TABLE] = (
        connection.total_changes - changes_before
    )
    return table_rows


def _compact_payload(
    payload_path: Path,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
    message: str,
) -> None:
    _check_cancel(cancel_callback)
    _emit(progress_callback, "compact", 0, 0, message)
    compact = sqlite3.connect(payload_path)
    compact.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    try:
        if int(compact.execute("PRAGMA freelist_count").fetchone()[0]) > 0:
            compact.execute("VACUUM")
    except sqlite3.DatabaseError as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue backup operation was cancelled.") from exc
        raise CatalogueBackupError(f"The backup source could not be compacted: {exc}") from exc
    finally:
        compact.set_progress_handler(None, 0)
        compact.close()


def _create_payload(
    source_path: Path,
    payload_path: Path,
    analysis_payload_path: Path,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> tuple[dict[str, int], dict[str, Any], dict[str, int] | None]:
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
        connection.execute(
            "ATTACH DATABASE ? AS analysis_payload",
            (str(analysis_payload_path.resolve()),),
        )
        connection.execute("PRAGMA analysis_payload.journal_mode = OFF")
        connection.execute("PRAGMA analysis_payload.synchronous = OFF")
        connection.execute("BEGIN")
        _emit(progress_callback, "validate_source", 0, 0, "Checking catalogue format…")
        _validate_catalogue_schema(connection)
        _check_cancel(cancel_callback)

        for statement in PAYLOAD_SCHEMA_SQL:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {PAYLOAD_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {PAYLOAD_FORMAT_VERSION}")

        table_rows: dict[str, int] = {}
        analysis_reconstruction = _analysis_reconstruction_metadata(connection)
        analysis_table_rows: dict[str, int] | None = None

        copy_steps = len(SOURCE_TABLES) + 1
        completed = 0

        for table in ("volumes", "folders"):
            columns = AUTHORITATIVE_TABLE_COLUMNS[table]
            column_sql = _quoted_columns(columns)
            changes_before = connection.total_changes
            connection.execute(
                f'INSERT INTO "{table}" ({column_sql}) '
                f'SELECT {column_sql} FROM original."{table}" ORDER BY id'
            )
            table_rows[table] = connection.total_changes - changes_before
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
            CREATE TEMP TABLE content_blob_lookup (
                id INTEGER PRIMARY KEY,
                digest BLOB NOT NULL UNIQUE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO temp.content_blob_lookup (digest)
            SELECT content_hash
            FROM original.files
            WHERE content_hash IS NOT NULL
            GROUP BY content_hash
            ORDER BY content_hash
            """
        )
        changes_before = connection.total_changes
        connection.execute(
            """
            INSERT INTO content_blobs (id, digest)
            SELECT id, digest FROM temp.content_blob_lookup ORDER BY id
            """
        )
        table_rows["content_blobs"] = connection.total_changes - changes_before
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
        changes_before = connection.total_changes
        connection.execute(
            f"""
            INSERT INTO files ({_quoted_columns(file_columns)})
            SELECT f.id, f.volume_id, f.folder_id, f.name, f.relative_path,
                   f.extension, f.size_bytes, f.modified_at, f.missing,
                   f.scanned_at, f.identity_device, f.identity_inode,
                   b.id, f.content_hash_algorithm
            FROM original.files f
            LEFT JOIN temp.content_blob_lookup b ON b.digest = f.content_hash
            ORDER BY f.id
            """
        )
        table_rows["files"] = connection.total_changes - changes_before
        connection.execute("DROP TABLE temp.content_blob_lookup")
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
            changes_before = connection.total_changes
            connection.execute(
                f'INSERT INTO "{table}" ({column_sql}) '
                f'SELECT {column_sql} FROM original."{table}" ORDER BY "{order_column}"'
            )
            table_rows[table] = connection.total_changes - changes_before
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
            changes_before = connection.total_changes
            connection.execute(
                f"""
                INSERT INTO catalogue_sequences (name, seq)
                SELECT name, seq FROM original.sqlite_sequence
                WHERE name IN ({placeholders})
                ORDER BY name
                """,
                sequence_names,
            )
            table_rows["catalogue_sequences"] = (
                connection.total_changes - changes_before
            )
        else:
            table_rows["catalogue_sequences"] = 0
        folder_exception_count, volume_exception_count = _store_derived_exceptions(
            connection,
            progress_callback,
            cancel_callback,
        )
        table_rows["folder_state_exceptions"] = folder_exception_count
        table_rows["volume_count_exceptions"] = volume_exception_count
        if analysis_reconstruction["storage"] == "stored":
            _emit(
                progress_callback,
                "preserve_analysis",
                0,
                0,
                "Preserving non-regenerable stale backup evidence…",
            )
            _create_and_copy_stored_analysis(connection, table_rows)
        elif analysis_reconstruction["storage"] == "accelerator":
            _emit(
                progress_callback,
                "preserve_analysis",
                0,
                0,
                "Saving current backup evidence accelerator…",
            )
            analysis_table_rows = _create_and_copy_analysis_accelerator(
                connection,
                "analysis_payload",
            )
        connection.commit()
        connection.execute("DETACH DATABASE analysis_payload")
        connection.execute("DETACH DATABASE original")
    except sqlite3.DatabaseError as exc:
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

    _compact_payload(
        payload_path,
        progress_callback,
        cancel_callback,
        "Compacting source data…",
    )
    if analysis_table_rows is not None:
        _compact_payload(
            analysis_payload_path,
            progress_callback,
            cancel_callback,
            "Compacting backup-evidence accelerator…",
        )
    return table_rows, analysis_reconstruction, analysis_table_rows


def _manifest_for_payload(
    source_path: Path,
    payload_path: Path,
    payload_sha256: str,
    table_rows: Mapping[str, int],
    analysis_reconstruction: Mapping[str, Any],
    *,
    analysis_payload_path: Path | None = None,
    analysis_payload_sha256: str | None = None,
    analysis_table_rows: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    all_schemas = {**AUTHORITATIVE_TABLE_COLUMNS, **ANALYSIS_TABLE_COLUMNS}
    schemas = {
        table: list(all_schemas[table])
        for table in table_rows
    }
    has_accelerator = analysis_table_rows is not None
    if has_accelerator != (
        analysis_payload_path is not None and analysis_payload_sha256 is not None
    ):
        raise CatalogueBackupError(
            "The backup-analysis accelerator metadata is incomplete."
        )
    components: list[dict[str, Any]] = [
        {
            "path": PAYLOAD_PATH,
            "media_type": "application/vnd.sqlite3",
            "payload_format_version": PAYLOAD_FORMAT_VERSION,
            "size": payload_path.stat().st_size,
            "sha256": payload_sha256,
            "tables": {name: int(count) for name, count in table_rows.items()},
            "schema": schemas,
        }
    ]
    if has_accelerator:
        assert analysis_payload_path is not None
        assert analysis_payload_sha256 is not None
        assert analysis_table_rows is not None
        components.append(
            {
                "path": ANALYSIS_PAYLOAD_PATH,
                "media_type": "application/vnd.sqlite3",
                "payload_format_version": ANALYSIS_PAYLOAD_FORMAT_VERSION,
                "size": analysis_payload_path.stat().st_size,
                "sha256": analysis_payload_sha256,
                "tables": {
                    name: int(count)
                    for name, count in analysis_table_rows.items()
                },
                "schema": {
                    table: list(ANALYSIS_ACCELERATOR_TABLE_COLUMNS[table])
                    for table in analysis_table_rows
                },
            }
        )

    omitted_derived = [
        "SQLite free pages and journals",
        "ordinary SQL indexes and triggers",
        "files_fts and folders_fts search indexes",
        "folder aggregate statistics",
        "volume indexed row counts",
    ]
    if has_accelerator or analysis_reconstruction.get("storage") == "stored":
        omitted_derived.append("backup-analysis indexes")
    else:
        omitted_derived.append(
            "current backup-analysis result tables (stale generations are retained)"
        )

    return {
        "format": BACKUP_FORMAT,
        "format_version": (
            BACKUP_FORMAT_VERSION
            if has_accelerator
            else LEGACY_BACKUP_FORMAT_VERSION
        ),
        "created_at": _utc_now(),
        "application_version": APPLICATION_VERSION,
        "minimum_application_version": APPLICATION_VERSION,
        "catalogue_schema_version": SCHEMA_VERSION,
        "source_catalogue_name": source_path.name,
        "source_catalogue_size": source_path.stat().st_size,
        "components": components,
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
        "omitted_derived": omitted_derived,
    }


def _write_archive(
    archive_path: Path,
    source_path: Path,
    payload_path: Path,
    table_rows: Mapping[str, int],
    analysis_reconstruction: Mapping[str, Any],
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
    *,
    analysis_payload_path: Path | None = None,
    analysis_table_rows: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    payload_size = payload_path.stat().st_size
    analysis_payload_size = (
        analysis_payload_path.stat().st_size
        if analysis_payload_path is not None
        else 0
    )
    total_size = payload_size + analysis_payload_size
    digest = hashlib.sha256()
    analysis_digest: Any | None = None
    written = 0
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=ZIP_COMPRESSION_LEVEL,
        allowZip64=True,
    ) as archive:
        with payload_path.open("rb") as source, archive.open(
            PAYLOAD_PATH,
            "w",
            force_zip64=True,
        ) as destination:
            while True:
                _check_cancel(cancel_callback)
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                destination.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                _emit(
                    progress_callback,
                    "compress",
                    written,
                    total_size,
                    "Compressing semantic backup…",
                )
        # Passing the member name above makes ZipFile apply its explicit
        # compression level on every supported Python version.  Permissions
        # are central-directory metadata and can be set after streaming.
        archive.getinfo(PAYLOAD_PATH).external_attr = 0o600 << 16

        if analysis_payload_path is not None:
            analysis_digest = hashlib.sha256()
            analysis_info = zipfile.ZipInfo(ANALYSIS_PAYLOAD_PATH)
            analysis_info.compress_type = zipfile.ZIP_LZMA
            analysis_info.external_attr = 0o600 << 16
            with analysis_payload_path.open("rb") as source, archive.open(
                analysis_info,
                "w",
                force_zip64=True,
            ) as destination:
                while True:
                    _check_cancel(cancel_callback)
                    chunk = source.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    destination.write(chunk)
                    analysis_digest.update(chunk)
                    written += len(chunk)
                    _emit(
                        progress_callback,
                        "compress",
                        written,
                        total_size,
                        "Compressing backup-evidence accelerator…",
                    )

        manifest = _manifest_for_payload(
            source_path,
            payload_path,
            digest.hexdigest(),
            table_rows,
            analysis_reconstruction,
            analysis_payload_path=analysis_payload_path,
            analysis_payload_sha256=(
                analysis_digest.hexdigest()
                if analysis_digest is not None
                else None
            ),
            analysis_table_rows=analysis_table_rows,
        )
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_info = zipfile.ZipInfo(MANIFEST_PATH)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o600 << 16
        archive.writestr(
            manifest_info,
            manifest_bytes,
            compresslevel=ZIP_COMPRESSION_LEVEL,
        )
    return manifest


def _manifest_components_by_path(
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(component["path"]): component
        for component in manifest["components"]
    }


def _validate_archive_member_set(
    infos: Mapping[str, zipfile.ZipInfo],
    manifest: Mapping[str, Any],
) -> None:
    expected = {MANIFEST_PATH, *_manifest_components_by_path(manifest)}
    actual = set(infos)
    if actual == expected:
        return
    missing = expected - actual
    extra = actual - expected
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(sorted(missing)))
    if extra:
        details.append("unexpected " + ", ".join(sorted(extra)))
    raise InvalidBackupError(
        "The backup component list is invalid"
        + (f" ({'; '.join(details)})" if details else "")
        + "."
    )


def _validate_archive_compression(
    infos: Mapping[str, zipfile.ZipInfo],
    manifest: Mapping[str, Any],
) -> None:
    if int(manifest["format_version"]) != BACKUP_FORMAT_VERSION:
        return
    if infos[PAYLOAD_PATH].compress_type != zipfile.ZIP_DEFLATED:
        raise InvalidBackupError(
            "The v2 catalogue source must use DEFLATE compression."
        )
    if infos[ANALYSIS_PAYLOAD_PATH].compress_type != zipfile.ZIP_LZMA:
        raise InvalidBackupError(
            "The backup-analysis accelerator must use LZMA compression."
        )


def _verify_created_archive(
    archive_path: Path,
    expected_manifest: Mapping[str, Any],
    cancel_callback: CancelCallback | None,
) -> None:
    """Stream-check a newly written ZIP without revalidating SQLite twice."""
    try:
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            infos = _safe_member_names(archive)
            manifest_info = infos.get(MANIFEST_PATH)
            if manifest_info is None:
                raise InvalidBackupError("The backup manifest is missing.")
            manifest = _read_manifest(archive, manifest_info)
            _validate_archive_member_set(infos, manifest)
            _validate_archive_compression(infos, manifest)
            if manifest != expected_manifest:
                raise CatalogueBackupError(
                    "The newly created backup manifest could not be verified."
                )
            for path, component in _manifest_components_by_path(manifest).items():
                payload_info = infos[path]
                expected_size = int(component["size"])
                if payload_info.file_size != expected_size:
                    raise CatalogueBackupError(
                        f"The newly created backup component '{path}' size could not be verified."
                    )
                digest = hashlib.sha256()
                checked = 0
                with archive.open(payload_info, "r") as source:
                    while True:
                        _check_cancel(cancel_callback)
                        chunk = source.read(COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        checked += len(chunk)
                        if checked > expected_size:
                            raise CatalogueBackupError(
                                f"The newly created backup component '{path}' exceeds its declared size."
                            )
                        digest.update(chunk)
                if checked != expected_size:
                    raise CatalogueBackupError(
                        f"The newly created backup component '{path}' is truncated."
                    )
                if digest.hexdigest() != str(component["sha256"]).casefold():
                    raise CatalogueBackupError(
                        f"The newly created backup component '{path}' checksum could not be verified."
                    )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise CatalogueBackupError(
            "The newly created catalogue backup ZIP could not be verified."
        ) from exc


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
    analysis_fd, analysis_name = tempfile.mkstemp(
        suffix=".sqlite",
        prefix="jvvv-analysis-backup-",
    )
    os.close(analysis_fd)
    analysis_payload = Path(analysis_name)
    archive_fd, archive_name = tempfile.mkstemp(
        suffix=".creating",
        prefix=f"{target.name}.",
        dir=target.parent,
    )
    os.close(archive_fd)
    archive_temp = Path(archive_name)
    try:
        table_rows, analysis, analysis_table_rows = _create_payload(
            source,
            payload,
            analysis_payload,
            progress_callback,
            cancel_callback,
        )
        _emit(
            progress_callback,
            "validate_payload",
            0,
            0,
            "Validating compact backup source…",
        )
        validation_manifest = _manifest_for_payload(
            source,
            payload,
            "0" * 64,
            table_rows,
            analysis,
            analysis_payload_path=(
                analysis_payload if analysis_table_rows is not None else None
            ),
            analysis_payload_sha256=(
                "0" * 64 if analysis_table_rows is not None else None
            ),
            analysis_table_rows=analysis_table_rows,
        )
        try:
            _validate_payload(payload, validation_manifest, cancel_callback)
            if analysis_table_rows is not None:
                _validate_analysis_payload(
                    analysis_payload,
                    validation_manifest,
                    cancel_callback,
                    payload,
                )
        except InvalidBackupError as exc:
            raise CatalogueBackupError(
                f"The generated backup source failed validation: {exc}"
            ) from exc

        manifest = _write_archive(
            archive_temp,
            source,
            payload,
            table_rows,
            analysis,
            progress_callback,
            cancel_callback,
            analysis_payload_path=(
                analysis_payload if analysis_table_rows is not None else None
            ),
            analysis_table_rows=analysis_table_rows,
        )
        _check_cancel(cancel_callback)
        _emit(progress_callback, "verify_archive", 0, 0, "Verifying backup archive…")
        _verify_created_archive(archive_temp, manifest, cancel_callback)
        os.replace(archive_temp, target)
    except Exception:
        archive_temp.unlink(missing_ok=True)
        raise
    finally:
        payload.unlink(missing_ok=True)
        analysis_payload.unlink(missing_ok=True)

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
    for info in infos:
        path = PurePosixPath(info.filename)
        if info.is_dir() or path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            raise InvalidBackupError("The backup contains an unsafe archive member path.")
        if info.flag_bits & 0x1:
            raise InvalidBackupError("Encrypted catalogue backups are not supported.")
    return {info.filename: info for info in infos}


def _validate_component_declaration(
    component: Any,
    path: str,
    payload_version: int,
) -> Mapping[str, Any]:
    if (
        not isinstance(component, dict)
        or component.get("path") != path
        or component.get("media_type") != "application/vnd.sqlite3"
    ):
        raise InvalidBackupError(
            f"The backup component '{path}' is not declared correctly."
        )
    if component.get("payload_format_version") != payload_version:
        raise UnsupportedBackupError(
            f"The backup component '{path}' payload version is not supported."
        )
    size = component.get("size")
    checksum = component.get("sha256")
    tables = component.get("tables")
    schema = component.get("schema")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or not isinstance(tables, dict)
        or not isinstance(schema, dict)
    ):
        raise InvalidBackupError(
            f"The backup component '{path}' metadata is invalid."
        )
    try:
        bytes.fromhex(checksum)
    except ValueError as exc:
        raise InvalidBackupError(
            f"The backup component '{path}' checksum is invalid."
        ) from exc
    return component


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
    if version not in {LEGACY_BACKUP_FORMAT_VERSION, BACKUP_FORMAT_VERSION}:
        raise UnsupportedBackupError(f"Backup format version {version} is not supported.")
    catalogue_version = manifest.get("catalogue_schema_version")
    if not isinstance(catalogue_version, int) or isinstance(catalogue_version, bool):
        raise InvalidBackupError("The backup has no valid catalogue schema version.")
    if catalogue_version != SCHEMA_VERSION:
        raise UnsupportedBackupError(
            f"The backup was created for catalogue schema version {catalogue_version}; "
            f"this JVVV build only restores schema version {SCHEMA_VERSION}."
        )
    components = manifest.get("components")
    expected_component_count = (
        2 if version == BACKUP_FORMAT_VERSION else 1
    )
    if (
        not isinstance(components, list)
        or len(components) != expected_component_count
    ):
        raise InvalidBackupError("The backup manifest has an invalid component list.")
    component = _validate_component_declaration(
        components[0],
        PAYLOAD_PATH,
        PAYLOAD_FORMAT_VERSION,
    )
    accelerator_component: Mapping[str, Any] | None = None
    if version == BACKUP_FORMAT_VERSION:
        accelerator_component = _validate_component_declaration(
            components[1],
            ANALYSIS_PAYLOAD_PATH,
            ANALYSIS_PAYLOAD_FORMAT_VERSION,
        )
    tables = component["tables"]
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
        "accelerator",
    }:
        raise InvalidBackupError("The backup reconstruction instructions are invalid.")
    if version == BACKUP_FORMAT_VERSION:
        if (
            analysis.get("storage") != "accelerator"
            or analysis.get("component") != ANALYSIS_PAYLOAD_PATH
        ):
            raise InvalidBackupError(
                "The v2 backup-analysis accelerator instructions are invalid."
            )
    elif analysis.get("storage") == "accelerator":
        raise InvalidBackupError(
            "A v1 backup cannot declare a backup-analysis accelerator."
        )
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
    if accelerator_component is not None:
        accelerator_tables = accelerator_component["tables"]
        if set(accelerator_tables) != set(ANALYSIS_ACCELERATOR_TABLE_COLUMNS) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in accelerator_tables.values()
        ):
            raise InvalidBackupError(
                "The backup-analysis accelerator table inventory is invalid."
            )
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


def _source_signature_from_payload(
    payload_path: Path,
    cancel_callback: CancelCallback | None,
) -> str:
    connection = sqlite3.connect(
        f"{payload_path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    try:
        rows = connection.execute(
            """
            WITH file_counts AS (
                SELECT volume_id, COUNT(*) AS item_count
                FROM files WHERE missing = 0 GROUP BY volume_id
            ),
            folder_counts AS (
                SELECT volume_id, COUNT(*) AS item_count
                FROM folders WHERE missing = 0 GROUP BY volume_id
            )
            SELECT v.id, v.last_scan_at,
                   COALESCE(e.indexed_file_count, f.item_count, 0)
                       AS indexed_file_count,
                   COALESCE(e.indexed_folder_count, d.item_count, 0)
                       AS indexed_folder_count
            FROM volumes v
            LEFT JOIN file_counts f ON f.volume_id = v.id
            LEFT JOIN folder_counts d ON d.volume_id = v.id
            LEFT JOIN volume_count_exceptions e ON e.volume_id = v.id
            ORDER BY v.id
            """
        )
        snapshot = [
            [
                int(row["id"]),
                row["last_scan_at"],
                int(row["indexed_file_count"] or 0),
                int(row["indexed_folder_count"] or 0),
            ]
            for row in rows
        ]
        return hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except sqlite3.DatabaseError as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled(
                "The catalogue restore operation was cancelled."
            ) from exc
        raise InvalidBackupError(
            f"The compact catalogue source cannot be matched to its accelerator: {exc}"
        ) from exc
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def _validate_analysis_payload(
    payload_path: Path,
    manifest: Mapping[str, Any],
    cancel_callback: CancelCallback | None,
    source_payload_path: Path | None = None,
) -> None:
    component = _manifest_components_by_path(manifest)[ANALYSIS_PAYLOAD_PATH]
    connection = sqlite3.connect(f"{payload_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        payload_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != ANALYSIS_PAYLOAD_APPLICATION_ID:
            raise InvalidBackupError(
                "The backup-analysis accelerator has an invalid application ID."
            )
        if payload_version > ANALYSIS_PAYLOAD_FORMAT_VERSION:
            raise UnsupportedBackupError(
                "The backup-analysis accelerator is newer than this JVVV version."
            )
        if payload_version != ANALYSIS_PAYLOAD_FORMAT_VERSION:
            raise UnsupportedBackupError(
                "The backup-analysis accelerator version is not supported."
            )
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        if tables != set(ANALYSIS_ACCELERATOR_TABLE_COLUMNS):
            raise InvalidBackupError(
                "The backup-analysis accelerator has an unexpected table schema."
            )
        named_index = connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'index' AND sql IS NOT NULL
            LIMIT 1
            """
        ).fetchone()
        if named_index is not None:
            raise InvalidBackupError(
                "The backup-analysis accelerator contains an unexpected index."
            )
        manifest_schema = component["schema"]
        for table, expected_columns in ANALYSIS_ACCELERATOR_TABLE_COLUMNS.items():
            actual = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            if actual != expected_columns or manifest_schema.get(table) != list(
                expected_columns
            ):
                raise InvalidBackupError(
                    f"The backup-analysis accelerator table '{table}' does not match its declared schema."
                )
            actual_count = int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            if actual_count != int(component["tables"][table]):
                raise InvalidBackupError(
                    f"The backup-analysis accelerator table '{table}' has an invalid row count."
                )
        _check_sqlite_integrity(connection, "main", InvalidBackupError)

        sequence_rows = list(
            connection.execute(
                f'SELECT name, seq FROM "{ANALYSIS_SEQUENCE_TABLE}" ORDER BY name'
            )
        )
        if any(
            str(row[0]) not in ANALYSIS_AUTOINCREMENT_TABLES
            or not isinstance(row[1], int)
            or int(row[1]) < 0
            for row in sequence_rows
        ):
            raise InvalidBackupError(
                "The backup-analysis accelerator has an invalid ID sequence."
            )

        active = connection.execute(
            """
            SELECT s.active_run_id, s.forced_stale, r.status,
                   r.rules_version, r.source_signature
            FROM backup_analysis_state s
            LEFT JOIN backup_analysis_runs r ON r.id = s.active_run_id
            WHERE s.id = 1
            """
        ).fetchone()
        analysis = manifest["reconstruction"]["backup_analysis"]
        if (
            int(component["tables"]["backup_analysis_state"]) != 1
            or active is None
            or active["active_run_id"] is None
            or bool(active["forced_stale"])
            or str(active["status"] or "") != "completed"
            or int(active["rules_version"] or 0) != RULES_VERSION
            or analysis.get("requested") is not True
            or analysis.get("source_was_stale") is not False
            or analysis.get("source_status") != "completed"
            or analysis.get("source_rules_version") != RULES_VERSION
        ):
            raise InvalidBackupError(
                "The backup-analysis accelerator does not describe current backup evidence."
            )
        if source_payload_path is None:
            raise InvalidBackupError(
                "The backup-analysis accelerator cannot be matched to its catalogue source."
            )
        expected_signature = _source_signature_from_payload(
            source_payload_path,
            cancel_callback,
        )
        if str(active["source_signature"] or "") != expected_signature:
            raise InvalidBackupError(
                "The backup-analysis accelerator does not match its catalogue source."
            )
    except sqlite3.DatabaseError as exc:
        if cancel_callback is not None and cancel_callback():
            raise BackupCancelled("The catalogue restore operation was cancelled.") from exc
        raise InvalidBackupError(
            f"The backup-analysis accelerator cannot be read: {exc}"
        ) from exc
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def _validate_archive_to_payload(
    backup_path: Path,
    payload_path: Path,
    *,
    analysis_payload_path: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> BackupInspection:
    _check_cancel(cancel_callback)
    _emit(progress_callback, "validate", 0, 0, "Validating backup archive…")
    try:
        with zipfile.ZipFile(backup_path, "r", allowZip64=True) as archive:
            infos = _safe_member_names(archive)
            manifest_info = infos.get(MANIFEST_PATH)
            if manifest_info is None:
                raise InvalidBackupError("The backup manifest is missing.")
            manifest = _read_manifest(archive, manifest_info)
            _validate_archive_member_set(infos, manifest)
            _validate_archive_compression(infos, manifest)
            components = _manifest_components_by_path(manifest)
            destinations = {PAYLOAD_PATH: payload_path}
            if ANALYSIS_PAYLOAD_PATH in components:
                accelerator_path = analysis_payload_path or payload_path.with_name(
                    ANALYSIS_PAYLOAD_PATH
                )
                if accelerator_path.resolve() == payload_path.resolve():
                    raise InvalidBackupError(
                        "The backup extraction paths for source and analysis must differ."
                    )
                destinations[ANALYSIS_PAYLOAD_PATH] = accelerator_path

            total_size = sum(int(component["size"]) for component in components.values())
            total_copied = 0
            for path, component in components.items():
                payload_info = infos[path]
                expected_size = int(component["size"])
                if payload_info.file_size != expected_size:
                    raise InvalidBackupError(
                        f"The backup component '{path}' size does not match its manifest."
                    )
                digest = hashlib.sha256()
                copied = 0
                with archive.open(payload_info, "r") as source, destinations[path].open(
                    "wb"
                ) as destination:
                    while True:
                        _check_cancel(cancel_callback)
                        chunk = source.read(COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        destination.write(chunk)
                        digest.update(chunk)
                        copied += len(chunk)
                        total_copied += len(chunk)
                        if copied > expected_size:
                            raise InvalidBackupError(
                                f"The backup component '{path}' exceeds its declared size."
                            )
                        _emit(
                            progress_callback,
                            "validate",
                            total_copied,
                            total_size,
                            "Checking backup integrity…",
                        )
                if copied != expected_size:
                    raise InvalidBackupError(
                        f"The backup component '{path}' is truncated."
                    )
                if digest.hexdigest() != str(component["sha256"]).casefold():
                    raise InvalidBackupError(
                        f"The backup component '{path}' checksum does not match its manifest."
                    )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        if isinstance(exc, CatalogueBackupError):
            raise
        raise InvalidBackupError("The selected file is not a readable JVVV backup ZIP.") from exc

    _validate_payload(payload_path, manifest, cancel_callback)
    analysis_size = 0
    if int(manifest["format_version"]) == BACKUP_FORMAT_VERSION:
        accelerator_path = analysis_payload_path or payload_path.with_name(
            ANALYSIS_PAYLOAD_PATH
        )
        _validate_analysis_payload(
            accelerator_path,
            manifest,
            cancel_callback,
            payload_path,
        )
        analysis_size = int(
            _manifest_components_by_path(manifest)[ANALYSIS_PAYLOAD_PATH]["size"]
        )
    return BackupInspection(
        backup_path,
        manifest,
        backup_path.stat().st_size,
        int(manifest["components"][0]["size"]),
        {name: int(value) for name, value in manifest["components"][0]["tables"].items()},
        analysis_size,
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
            analysis_payload_path=Path(temp_directory) / ANALYSIS_PAYLOAD_PATH,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )


def _prepare_restore_build(db: Database) -> dict[str, str]:
    """Tune and strip an unpublished restore database for fast bulk loading."""
    connection = db.connection
    connection.commit()
    if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "wal":
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    journal_mode = str(
        connection.execute("PRAGMA journal_mode = OFF").fetchone()[0]
    ).casefold()
    if journal_mode != "off":
        raise CatalogueBackupError(
            "The temporary restore database could not enter bulk-build mode."
        )
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA temp_store = FILE")
    connection.execute("PRAGMA cache_size = -131072")
    connection.execute(f"PRAGMA threads = {max(1, min(4, os.cpu_count() or 1))}")
    connection.execute("PRAGMA locking_mode = EXCLUSIVE")

    index_definitions = {
        str(row["name"]): str(row["sql"])
        for row in connection.execute(
            """
            SELECT name, sql FROM sqlite_schema
            WHERE type = 'index' AND sql IS NOT NULL
            ORDER BY name
            """
        )
    }
    for name in index_definitions:
        connection.execute(f'DROP INDEX "{name}"')
    connection.commit()
    return index_definitions


def _create_restore_indexes(
    db: Database,
    index_definitions: Mapping[str, str],
    names: Iterable[str],
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    phase: str = "rebuild_indexes",
) -> None:
    requested = [name for name in names if name in index_definitions]
    if not requested:
        return
    with db.transaction() as connection:
        for completed, name in enumerate(requested, start=1):
            _check_cancel(cancel_callback)
            connection.execute(index_definitions[name])
            _emit(
                progress_callback,
                phase,
                completed,
                len(requested),
                "Building catalogue indexes…",
            )


def _finish_restore_build(db: Database) -> None:
    connection = db.connection
    connection.commit()
    connection.execute("PRAGMA locking_mode = NORMAL")
    journal_mode = str(
        connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    ).casefold()
    if journal_mode != "delete":
        raise CatalogueBackupError(
            "The restored catalogue could not return to normal journal mode."
        )
    connection.execute("PRAGMA synchronous = NORMAL")


def _copy_payload_to_catalogue(
    db: Database,
    payload_path: Path,
    manifest: Mapping[str, Any],
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
    analysis_payload_path: Path | None = None,
) -> None:
    connection = db.connection
    connection.set_progress_handler(_sqlite_cancel_handler(cancel_callback), 10_000)
    analysis = manifest["reconstruction"]["backup_analysis"]
    analysis_attached = False
    connection.execute(
        "ATTACH DATABASE ? AS source_backup",
        (str(payload_path.resolve()),),
    )
    try:
        if analysis["storage"] == "accelerator":
            if analysis_payload_path is None or not analysis_payload_path.is_file():
                raise InvalidBackupError(
                    "The backup-analysis accelerator was not extracted."
                )
            connection.execute(
                "ATTACH DATABASE ? AS analysis_accelerator",
                (str(analysis_payload_path.resolve()),),
            )
            analysis_attached = True

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

            if analysis["storage"] in {"stored", "accelerator"}:
                conn.execute("DELETE FROM backup_analysis_state")
                analysis_database = (
                    "analysis_accelerator"
                    if analysis["storage"] == "accelerator"
                    else "source_backup"
                )
                analysis_component = _manifest_components_by_path(manifest)[
                    ANALYSIS_PAYLOAD_PATH
                    if analysis["storage"] == "accelerator"
                    else PAYLOAD_PATH
                ]
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
                    changes_before = conn.total_changes
                    conn.execute(
                        f'INSERT INTO "{table}" ({_quoted_columns(columns)}) '
                        f'SELECT {_quoted_columns(columns)} '
                        f'FROM {analysis_database}."{table}"'
                    )
                    copied = conn.total_changes - changes_before
                    expected = int(analysis_component["tables"][table])
                    if copied != expected:
                        raise InvalidBackupError(
                            f"The backup-evidence table '{table}' could not be copied exactly."
                        )
                _emit(
                    progress_callback,
                    "restore_source",
                    len(SOURCE_TABLES),
                    len(SOURCE_TABLES),
                    "Restoring preserved backup evidence…",
                )

                if analysis["storage"] == "accelerator":
                    sequence_rows = list(
                        conn.execute(
                            f"SELECT name, seq FROM analysis_accelerator."
                            f'"{ANALYSIS_SEQUENCE_TABLE}" ORDER BY name'
                        )
                    )
                    if any(
                        str(row["name"]) not in ANALYSIS_AUTOINCREMENT_TABLES
                        for row in sequence_rows
                    ):
                        raise InvalidBackupError(
                            "The backup-analysis accelerator has an invalid ID sequence."
                        )
                    for name in ANALYSIS_AUTOINCREMENT_TABLES:
                        conn.execute(
                            "DELETE FROM sqlite_sequence WHERE name = ?",
                            (name,),
                        )
                    for row in sequence_rows:
                        conn.execute(
                            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                            (row["name"], row["seq"]),
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
        if analysis_attached:
            connection.execute("DETACH DATABASE analysis_accelerator")
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
    remap_run_id = generated_run_id != source_run_id
    if remap_run_id:
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if remap_run_id:
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
        if remap_run_id:
            connection.execute("PRAGMA foreign_keys = ON")

    if remap_run_id:
        for table in (
            "backup_analysis_volume_snapshots",
            "backup_file_results",
            "backup_folder_results",
            "backup_folder_drive_matches",
            "backup_volume_results",
            "backup_mirror_candidates",
        ):
            if connection.execute(
                f"""
                SELECT 1 FROM "{table}" child
                LEFT JOIN backup_analysis_runs parent ON parent.id = child.run_id
                WHERE parent.id IS NULL LIMIT 1
                """
            ).fetchone() is not None:
                raise CatalogueBackupError(
                    "Regenerated backup evidence could not retain its original identity safely."
                )
        if connection.execute(
            """
            SELECT 1 FROM backup_analysis_state state
            LEFT JOIN backup_analysis_runs parent ON parent.id = state.active_run_id
            WHERE state.active_run_id IS NOT NULL AND parent.id IS NULL
            LIMIT 1
            """
        ).fetchone() is not None:
            raise CatalogueBackupError(
                "Regenerated backup evidence state refers to a missing analysis run."
            )


def _regenerate_derived_data(
    db: Database,
    payload_path: Path,
    manifest: Mapping[str, Any],
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
    *,
    defer_search: bool = False,
) -> tuple[str, ...]:
    volumes = list(
        db.connection.execute(
            "SELECT id, last_scan_at, updated_at FROM volumes ORDER BY id"
        )
    )
    total_volumes = len(volumes)
    with db.transaction() as conn:
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
                clear_existing=False,
            )

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
    regenerated = ["folder aggregates", "volume counts"]

    analysis = manifest.get("reconstruction", {}).get("backup_analysis", {})
    if isinstance(analysis, dict) and analysis.get("storage") == "regenerate":
        _emit(progress_callback, "rebuild_analysis", 0, 0, "Rebuilding backup evidence…")
        source_run_id = int(analysis["source_run"]["id"])
        # Make the regenerated run use its original ID from the outset. Older
        # code generated run 1 and could then rewrite millions of result rows
        # when restoring a later source run.
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name = 'backup_analysis_runs'"
            )
            connection.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                ("backup_analysis_runs", max(0, source_run_id - 1)),
            )
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
            defer_persistent_indexes=True,
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

    if not defer_search:
        _regenerate_search_indexes(db, progress_callback)
        regenerated.append("search indexes")
    return tuple(regenerated)


def _regenerate_search_indexes(
    db: Database,
    progress_callback: ProgressCallback | None,
) -> None:
    # Backup analysis and semantic source validation do not use FTS. Build it
    # last so those scans do not compete with a multi-gigabyte search index for
    # cache and temporary I/O.
    _emit(progress_callback, "rebuild_search", 0, 0, "Rebuilding search indexes…")
    db.rebuild_search_indexes()


def _table_has_keyed_difference(
    connection: sqlite3.Connection,
    table: str,
    columns: Iterable[str],
    key_columns: Iterable[str],
    expected_count: int,
    *,
    source_database: str = "source_backup",
) -> bool:
    """Compare a restored table using its key without multi-GB EXCEPT sorts."""
    if int(
        connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
    ) != expected_count:
        return True
    keys = tuple(key_columns)
    join = " AND ".join(f'm."{column}" = s."{column}"' for column in keys)
    different = " OR ".join(
        f'm."{column}" IS NOT s."{column}"' for column in columns
    )
    return connection.execute(
        f"""
        SELECT 1
        FROM {source_database}."{table}" s
        LEFT JOIN main."{table}" m ON {join}
        WHERE m."{keys[0]}" IS NULL OR {different}
        LIMIT 1
        """
    ).fetchone() is not None


def _validate_restored_semantics(
    db: Database,
    payload_path: Path,
    manifest: Mapping[str, Any],
    cancel_callback: CancelCallback | None,
    analysis_payload_path: Path | None = None,
) -> None:
    connection = db.connection
    table_rows = manifest["components"][0]["tables"]
    analysis = manifest["reconstruction"]["backup_analysis"]
    analysis_attached = False
    connection.execute(
        "ATTACH DATABASE ? AS source_backup",
        (str(payload_path.resolve()),),
    )
    try:
        if analysis["storage"] == "accelerator":
            if analysis_payload_path is None or not analysis_payload_path.is_file():
                raise CatalogueBackupError(
                    "The restored backup-evidence accelerator is unavailable."
                )
            connection.execute(
                "ATTACH DATABASE ? AS analysis_accelerator",
                (str(analysis_payload_path.resolve()),),
            )
            analysis_attached = True

        for table in (
            "volumes",
            "volume_register",
            "folders",
            "file_media_metadata",
            "scan_history",
            "scan_errors",
        ):
            columns = AUTHORITATIVE_TABLE_COLUMNS[table]
            if _table_has_keyed_difference(
                connection,
                table,
                columns,
                TABLE_PRIMARY_KEYS[table],
                int(table_rows[table]),
            ):
                raise CatalogueBackupError(
                    f"Restored source validation failed for the '{table}' component."
                )

        if int(connection.execute("SELECT COUNT(*) FROM main.files").fetchone()[0]) != int(
            table_rows["files"]
        ) or connection.execute(
            """
            SELECT 1
            FROM source_backup.files s
            LEFT JOIN source_backup.content_blobs b ON b.id = s.content_hash_id
            LEFT JOIN main.files m ON m.id = s.id
            WHERE m.id IS NULL
               OR m.volume_id IS NOT s.volume_id
               OR m.folder_id IS NOT s.folder_id
               OR m.name IS NOT s.name
               OR m.relative_path IS NOT s.relative_path
               OR m.extension IS NOT s.extension
               OR m.size_bytes IS NOT s.size_bytes
               OR m.modified_at IS NOT s.modified_at
               OR m.missing IS NOT s.missing
               OR m.scanned_at IS NOT s.scanned_at
               OR m.identity_device IS NOT s.identity_device
               OR m.identity_inode IS NOT s.identity_inode
               OR m.content_hash IS NOT b.digest
               OR m.content_hash_algorithm IS NOT s.content_hash_algorithm
            LIMIT 1
            """
        ).fetchone() is not None:
            raise CatalogueBackupError("Restored source validation failed for file records.")

        if connection.execute(
            """
            SELECT 1
            FROM source_backup.folder_state_exceptions e
            LEFT JOIN main.folders f ON f.id = e.folder_id
            WHERE f.id IS NULL
               OR f.recursive_size_bytes IS NOT e.recursive_size_bytes
               OR f.recursive_file_count IS NOT e.recursive_file_count
               OR f.recursive_subfolder_count IS NOT e.recursive_subfolder_count
               OR f.direct_file_count IS NOT e.direct_file_count
               OR f.direct_subfolder_count IS NOT e.direct_subfolder_count
               OR f.stats_updated_at IS NOT e.stats_updated_at
            LIMIT 1
            """
        ).fetchone() is not None:
            raise CatalogueBackupError(
                "Restored folder-statistic exception validation failed."
            )

        if connection.execute(
            """
            SELECT 1
            FROM source_backup.volume_count_exceptions e
            LEFT JOIN main.volumes v ON v.id = e.volume_id
            WHERE v.id IS NULL
               OR v.indexed_file_count IS NOT e.indexed_file_count
               OR v.indexed_folder_count IS NOT e.indexed_folder_count
            LIMIT 1
            """
        ).fetchone() is not None:
            raise CatalogueBackupError("Restored volume-count exception validation failed.")

        if connection.execute(
            """
            SELECT 1
            FROM source_backup.catalogue_sequences s
            LEFT JOIN main.sqlite_sequence m ON m.name = s.name
            WHERE m.name IS NULL OR m.seq IS NOT s.seq
            LIMIT 1
            """
        ).fetchone() is not None:
            raise CatalogueBackupError("Restored catalogue ID sequence validation failed.")

        if analysis["storage"] == "stored":
            for table, columns in ANALYSIS_TABLE_COLUMNS.items():
                if _table_has_keyed_difference(
                    connection,
                    table,
                    columns,
                    TABLE_PRIMARY_KEYS[table],
                    int(table_rows[table]),
                ):
                    raise CatalogueBackupError(
                        f"Preserved backup-evidence validation failed for '{table}'."
                    )
        elif analysis["storage"] == "accelerator":
            component = _manifest_components_by_path(manifest)[ANALYSIS_PAYLOAD_PATH]
            # The accelerator was checksum/integrity validated and copied with
            # identical column lists. Counts catch incomplete writes cheaply;
            # the compact identity/summary tables are also compared field for
            # field without rescanning millions of derived item rows.
            exact_tables = {
                "backup_analysis_runs",
                "backup_analysis_state",
                "backup_analysis_volume_snapshots",
                "backup_volume_results",
                "backup_mirror_candidates",
                "backup_analysis_invalidations",
            }
            for table, columns in ANALYSIS_TABLE_COLUMNS.items():
                expected_count = int(component["tables"][table])
                actual_count = int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM main."{table}"'
                    ).fetchone()[0]
                )
                if actual_count != expected_count or (
                    table in exact_tables
                    and _table_has_keyed_difference(
                        connection,
                        table,
                        columns,
                        TABLE_PRIMARY_KEYS[table],
                        expected_count,
                        source_database="analysis_accelerator",
                    )
                ):
                    raise CatalogueBackupError(
                        f"Accelerated backup-evidence validation failed for '{table}'."
                    )

            source_sequences = {
                str(row["name"]): int(row["seq"])
                for row in connection.execute(
                    f'SELECT name, seq FROM analysis_accelerator."{ANALYSIS_SEQUENCE_TABLE}"'
                )
            }
            restored_sequences = {
                str(row["name"]): int(row["seq"])
                for row in connection.execute(
                    "SELECT name, seq FROM main.sqlite_sequence "
                    "WHERE name IN ('backup_analysis_runs', "
                    "'backup_analysis_invalidations')"
                )
            }
            if restored_sequences != source_sequences:
                raise CatalogueBackupError(
                    "Accelerated backup-evidence ID sequence validation failed."
                )

            state = BackupAnalysisEngine(db).state()
            if (
                state.status != "completed"
                or state.is_stale
                or state.active_run_id is None
                or state.rules_version != RULES_VERSION
            ):
                raise CatalogueBackupError(
                    "Accelerated backup evidence does not match the restored catalogue."
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
        if analysis_attached:
            connection.execute("DETACH DATABASE analysis_accelerator")
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
    # Capture this before the long restore. The archive is fully extracted and
    # validated up front, so its later removal must not turn a completed,
    # atomically published catalogue into a reported failure.
    backup_size = backup.stat().st_size

    with tempfile.TemporaryDirectory(prefix="jvvv-restore-source-") as temp_directory:
        payload = Path(temp_directory) / PAYLOAD_PATH
        analysis_payload = Path(temp_directory) / ANALYSIS_PAYLOAD_PATH
        inspection = _validate_archive_to_payload(
            backup,
            payload,
            analysis_payload_path=analysis_payload,
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
            index_definitions = _prepare_restore_build(db)
            _copy_payload_to_catalogue(
                db,
                payload,
                inspection.manifest,
                progress_callback,
                cancel_callback,
                analysis_payload,
            )
            _create_restore_indexes(
                db,
                index_definitions,
                sorted(RESTORE_WORK_INDEXES),
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                phase="prepare_reconstruction",
            )
            regenerated = _regenerate_derived_data(
                db,
                payload,
                inspection.manifest,
                progress_callback,
                cancel_callback,
                defer_search=True,
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
                analysis_payload,
            )
            _regenerate_search_indexes(db, progress_callback)
            regenerated = (*regenerated, "search indexes")
            db.connection.execute("INSERT INTO files_fts(files_fts, rank) VALUES('integrity-check', 1)")
            db.connection.execute("INSERT INTO folders_fts(folders_fts, rank) VALUES('integrity-check', 1)")
            db.connection.commit()
            _create_restore_indexes(
                db,
                index_definitions,
                sorted(set(index_definitions) - RESTORE_WORK_INDEXES),
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
            )
            db.connection.set_progress_handler(None, 0)
            _finish_restore_build(db)
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
        backup_size,
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
