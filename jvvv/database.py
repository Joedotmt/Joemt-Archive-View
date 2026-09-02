from __future__ import annotations

import os
import platform
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .backup_analysis import ANALYSIS_SCHEMA_SQL, ANALYSIS_TABLE_COLUMNS
from .folder_statistics import calculate_folder_statistics


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
SCHEMA_VERSION = 12
CATALOGUE_EXTENSION = ".jvvv"
AID_DRIVE_ID_RE = re.compile(r"^AID-(\d{3,})$")
ARCHIVE_STATUSES = ["Archive", "Maintenance", "In Use", "Retired", "Missing", "Faulty"]
VOLUME_CONDITIONS = ["New", "Good", "Fair", "Poor", "Damaged", "Failed", "Unknown"]
CONNECTOR_OPTIONS = ["USB-B", "USB-Micro-B", "USB-Mini", "USB-C", "Network", "Other", "Unknown"]
SQLITE_INTEGER_MIN = -(2**63)
SQLITE_INTEGER_MAX = 2**63 - 1
UINT64_MODULUS = 2**64
UINT64_MAX = UINT64_MODULUS - 1
DEFAULT_BUSY_TIMEOUT_MS = 2000
INTERACTIVE_BUSY_TIMEOUT_MS = 250
AUTOMATIC_INTEGRITY_CHECK_MAX_BYTES = 256 * 1024 * 1024
SCAN_HISTORY_PER_VOLUME_LIMIT = 100
WINDOWS_DRIVE_REMOTE = 4
SQLITE_EXTENDED_ERROR_NAMES = {
    8714: "SQLITE_IOERR_IN_PAGE",
}
REQUIRED_TABLES = set(ANALYSIS_TABLE_COLUMNS) | {
    "volumes",
    "volume_register",
    "folders",
    "files",
    "file_media_metadata",
    "file_preview_status",
    "files_fts",
    "folders_fts",
    "scan_history",
    "scan_errors",
}
PREVIEW_STATUS_VALUES = frozenset({"available", "failed", "missing", "unsupported"})
PREVIEW_SCAN_MODES = frozenset({"disabled", "enabled", "skipped-preflight"})
REQUIRED_COLUMNS = {
    **{table: set(columns) for table, columns in ANALYSIS_TABLE_COLUMNS.items()},
    "volumes": {
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
    },
    "volume_register": {
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
    },
    "folders": {
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
    },
    "files": {
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
    },
    "file_media_metadata": {
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
    },
    "file_preview_status": {
        "file_id",
        "media_kind",
        "profile_id",
        "status",
        "source_hash",
        "preview_size",
        "preview_width",
        "preview_height",
        "preview_duration_ms",
        "generated_at",
        "error_stage",
        "error_message",
        "updated_at",
    },
    "scan_history": {
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
        "preview_mode",
        "image_previews_generated",
        "image_previews_reused",
        "image_previews_failed",
        "video_previews_generated",
        "video_previews_reused",
        "video_previews_failed",
        "previews_storage_skipped",
        "preview_bytes_written",
        "preview_message",
    },
    "scan_errors": {
        "id",
        "scan_id",
        "volume_id",
        "path",
        "message",
        "created_at",
    },
}


CATALOGUE_SCHEMA_SQL: tuple[str, ...] = (
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
        media_metadata_collected INTEGER NOT NULL DEFAULT 0,
        preview_mode TEXT NOT NULL DEFAULT 'disabled',
        image_previews_generated INTEGER NOT NULL DEFAULT 0,
        image_previews_reused INTEGER NOT NULL DEFAULT 0,
        image_previews_failed INTEGER NOT NULL DEFAULT 0,
        video_previews_generated INTEGER NOT NULL DEFAULT 0,
        video_previews_reused INTEGER NOT NULL DEFAULT 0,
        video_previews_failed INTEGER NOT NULL DEFAULT 0,
        previews_storage_skipped INTEGER NOT NULL DEFAULT 0,
        preview_bytes_written INTEGER NOT NULL DEFAULT 0,
        preview_message TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE file_preview_status (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        media_kind TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        status TEXT NOT NULL,
        source_hash BLOB,
        preview_size INTEGER,
        preview_width INTEGER,
        preview_height INTEGER,
        preview_duration_ms INTEGER,
        generated_at TEXT,
        error_stage TEXT,
        error_message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
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
    "CREATE INDEX idx_file_preview_status_status ON file_preview_status(status)",
    "CREATE INDEX idx_volume_register_status ON volume_register(status COLLATE NOCASE)",
    "CREATE INDEX idx_volume_register_condition ON volume_register(condition COLLATE NOCASE)",
    "CREATE INDEX idx_volume_register_connector ON volume_register(connector COLLATE NOCASE)",
    "CREATE INDEX idx_volume_register_master ON volume_register(master_volume_id)",
    "CREATE INDEX idx_volumes_identity ON volumes(identity_kind, identity_token)",
)


class CatalogueError(Exception):
    def __init__(self, message: str, *, diagnostic_details: str = "") -> None:
        super().__init__(message)
        self.diagnostic_details = diagnostic_details


class CatalogueInUseError(CatalogueError):
    pass


class InvalidCatalogueError(CatalogueError):
    pass


class UnsupportedCatalogueError(CatalogueError):
    pass


FolderStatsProgress = Callable[[int, int, str], None]


def sqlite_file_uri(
    path: Path,
    *,
    mode: str = "rw",
    strict: bool = False,
) -> str:
    """Return a SQLite file URI, including Windows UNC path handling."""
    resolved = path.resolve(strict=strict)
    uri = resolved.as_uri()
    if resolved.drive.startswith("\\\\"):
        # pathlib represents a UNC host as a file-URI authority, but SQLite
        # rejects non-local authorities. Keep the UNC name in the URI path.
        uri = f"file:////{uri[len('file://'):]}"
    return f"{uri}?mode={mode}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def format_timestamp(value: float | int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime(ISO_FORMAT)
    except (OSError, OverflowError, ValueError):
        return None


def parse_db_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, ISO_FORMAT)
    except ValueError:
        return None


def is_valid_drive_id(value: str) -> bool:
    return bool(value.strip())


def drive_id_sequence(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    match = AID_DRIVE_ID_RE.fullmatch(text)
    if match is None:
        return None
    return int(match.group(1))


def drive_id_sort_key(value: str | None) -> tuple[int, int, str]:
    sequence = drive_id_sequence(value)
    if sequence is None:
        return (1, 0, (value or "").casefold())
    return (0, sequence, value or "")


def validate_iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date: {text}") from exc


def normalize_identity_integer(value: int | None) -> int | None:
    if value is None:
        return None
    integer = int(value)
    if integer == 0:
        return None
    if SQLITE_INTEGER_MIN <= integer <= SQLITE_INTEGER_MAX:
        return integer
    if SQLITE_INTEGER_MAX < integer <= UINT64_MAX:
        return integer - UINT64_MODULUS
    return None


def _windows_drive_is_remote(drive: str) -> bool:
    if os.name != "nt" or not drive:
        return False

    import ctypes

    drive_root = drive.rstrip("\\/") + "\\"
    return ctypes.windll.kernel32.GetDriveTypeW(drive_root) == WINDOWS_DRIVE_REMOTE


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        initialize: bool = True,
        create: bool = True,
        read_only: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        check_same_thread: bool = True,
    ) -> None:
        if create and read_only:
            raise ValueError("A read-only database connection cannot create a catalogue.")
        self.path = Path(path).expanduser()
        self.read_only = read_only
        self.busy_timeout_ms = busy_timeout_ms
        self._operation = "preparing the catalogue path"
        self._connect_target = ""
        self._use_uri = False
        self._network_storage = self._uses_network_storage(self.path)
        self._requested_journal_mode = ""
        self._stored_journal_mode = self._database_header_journal_mode(self.path)
        if self._network_storage and self._stored_journal_mode == "WAL":
            raise CatalogueError(
                "This network catalogue is still in WAL mode. Move it to a local drive, "
                "open and close it there to convert it, then move it back to the network share."
            )
        stored_schema_version = self._database_header_schema_version(self.path)
        if stored_schema_version not in (None, 0, SCHEMA_VERSION):
            raise UnsupportedCatalogueError(
                self._unsupported_schema_message(stored_schema_version)
            )
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connect_target = str(self.path)
            use_uri = False
        else:
            if not self.path.is_file():
                raise InvalidCatalogueError(f"Catalogue file does not exist: {self.path}")
            connect_target = sqlite_file_uri(
                self.path,
                mode="ro" if self.read_only else "rw",
            )
            use_uri = True

        self._connect_target = connect_target
        self._use_uri = use_uri
        try:
            self._operation = "opening the SQLite connection"
            self.connection = sqlite3.connect(
                connect_target,
                timeout=max(self.busy_timeout_ms, 0) / 1000,
                uri=use_uri,
                check_same_thread=check_same_thread,
            )
            self.connection.row_factory = sqlite3.Row
            self._configure_connection()
            if initialize:
                self._operation = "initializing the catalogue schema"
                self.initialize()
        except sqlite3.Error as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise self._catalogue_error(exc) from exc
        except Exception:
            if hasattr(self, "connection"):
                self.connection.close()
            raise

    def close(self) -> None:
        try:
            if self.connection.in_transaction:
                self.connection.rollback()
        finally:
            self.connection.close()

    @staticmethod
    def _uses_network_storage(path: Path) -> bool:
        resolved = path.resolve(strict=False)
        return resolved.drive.startswith("\\\\") or _windows_drive_is_remote(
            resolved.drive
        )

    @staticmethod
    def _database_header_journal_mode(path: Path) -> str:
        if not path.is_file():
            return "New database"
        try:
            with path.open("rb") as database_file:
                header = database_file.read(20)
        except OSError:
            return "Unreadable"
        if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
            return "Unknown"
        if 2 in (header[18], header[19]):
            return "WAL"
        if header[18:20] == b"\x01\x01":
            return "Rollback"
        return f"Unknown ({header[18]}/{header[19]})"

    @staticmethod
    def _database_header_schema_version(path: Path) -> int | None:
        if not path.is_file():
            return None
        try:
            with path.open("rb") as database_file:
                header = database_file.read(64)
        except OSError:
            return None
        if len(header) < 64 or header[:16] != b"SQLite format 3\x00":
            return None
        return int.from_bytes(header[60:64], "big")

    def _configure_connection(self) -> None:
        self._operation = "enabling SQLite foreign-key checks"
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._operation = "setting the SQLite busy timeout"
        self.connection.execute(f"PRAGMA busy_timeout = {max(self.busy_timeout_ms, 0)}")
        if self.read_only:
            self._requested_journal_mode = "Read-only"
            self._operation = "enabling SQLite query-only mode"
            self.connection.execute("PRAGMA query_only = ON")
            return
        if self._network_storage:
            # Network catalogues are required to already use rollback
            # journaling. Do not execute journal_mode here: even asking SQLite
            # to re-select DELETE can trigger unreliable SMB memory mapping.
            self._requested_journal_mode = "Preserve rollback mode"
        else:
            self._requested_journal_mode = "WAL"
            self._operation = "setting SQLite journal mode to WAL"
            self.connection.execute("PRAGMA journal_mode = WAL").fetchone()

        self._operation = "setting SQLite synchronous mode to NORMAL"
        self.connection.execute("PRAGMA synchronous = NORMAL")

    def _catalogue_error(self, exc: sqlite3.Error) -> CatalogueError:
        message = str(exc)
        lower = message.lower()
        if "database is locked" in lower or "database table is locked" in lower:
            error: CatalogueError = CatalogueInUseError(
                "The catalogue is locked or already in use by another process."
            )
        elif (
            "file is not a database" in lower
            or "malformed" in lower
            or "database disk image is malformed" in lower
        ):
            error = InvalidCatalogueError("The selected file is not a valid catalogue database.")
        else:
            error = CatalogueError(message)
        error.diagnostic_details = self._diagnostic_details(exc)
        return error

    def _diagnostic_details(self, exc: sqlite3.Error) -> str:
        sqlite_error_code = getattr(exc, "sqlite_errorcode", "Unavailable")
        sqlite_error_name = getattr(exc, "sqlite_errorname", "Unavailable")
        if not sqlite_error_name or sqlite_error_name.lower() == "unknown":
            sqlite_error_name = SQLITE_EXTENDED_ERROR_NAMES.get(
                sqlite_error_code,
                "Unknown",
            )
        return "\n".join(
            [
                f"Operation: {self._operation}",
                f"Catalogue path: {self.path}",
                f"Connection target: {self._connect_target}",
                f"SQLite URI mode: {'Yes' if self._use_uri else 'No'}",
                f"Network storage detected: {'Yes' if self._network_storage else 'No'}",
                f"Journal mode in file header: {self._stored_journal_mode}",
                f"Requested journal mode: {self._requested_journal_mode or 'Not reached'}",
                f"SQLite error: {type(exc).__name__}: {exc}",
                f"SQLite error name: {sqlite_error_name}",
                f"SQLite error code: {sqlite_error_code}",
                f"SQLite library version: {sqlite3.sqlite_version}",
                f"Operating system: {platform.platform()}",
            ]
        )

    def initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            self.validate_schema()
            return
        if version != 0 or self._table_names():
            raise UnsupportedCatalogueError(
                self._unsupported_schema_message(int(version))
            )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for statement in CATALOGUE_SCHEMA_SQL:
                self.connection.execute(statement)

            for statement in ANALYSIS_SCHEMA_SQL:
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT INTO backup_analysis_state (
                    id, active_run_id, forced_stale, stale_reason, updated_at
                ) VALUES (1, NULL, 0, '', ?)
                """,
                (utc_now(),),
            )
            self._create_or_rebuild_search_indexes()
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise
        self.validate_schema()

    @staticmethod
    def _unsupported_schema_message(version: int) -> str:
        if version < SCHEMA_VERSION:
            return (
                f"This catalogue uses the retired schema version {version}. "
                f"This JVVV build only opens the current schema version {SCHEMA_VERSION}."
            )
        return (
            f"This catalogue uses schema version {version}, but this JVVV build only "
            f"supports version {SCHEMA_VERSION}."
        )

    def validate_catalogue(self) -> None:
        try:
            # quick_check walks every table and index. On multi-gigabyte
            # catalogues (especially ones with FTS indexes), doing that on
            # every open can take minutes even when the database is healthy.
            # Schema validation and normal SQLite reads still catch malformed
            # data as it is accessed; reserve the automatic full scan for
            # catalogues small enough for it to remain an inexpensive guard.
            if (
                not self._network_storage
                and self.path.stat().st_size <= AUTOMATIC_INTEGRITY_CHECK_MAX_BYTES
            ):
                self._operation = "checking catalogue database integrity"
                check = self.connection.execute("PRAGMA quick_check(1)").fetchone()
                if check is None or check[0] != "ok":
                    raise InvalidCatalogueError(
                        "The selected catalogue database appears to be corrupted."
                    )

            self._operation = "reading the catalogue schema version"
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            self._operation = "reading the catalogue schema"
            existing_tables = self._table_names()
            if version == 0 and not REQUIRED_TABLES <= existing_tables:
                raise InvalidCatalogueError(
                    "The selected file is a SQLite database, but it is not a JVVV catalogue."
                )
            if version != SCHEMA_VERSION:
                raise UnsupportedCatalogueError(
                    self._unsupported_schema_message(int(version))
                )
            self._operation = "validating the catalogue schema"
            self.validate_schema()
        except sqlite3.Error as exc:
            raise self._catalogue_error(exc) from exc

    def validate_schema(self) -> None:
        missing = REQUIRED_TABLES - self._table_names()
        if missing:
            names = ", ".join(sorted(missing))
            raise InvalidCatalogueError(
                f"The selected file is missing required catalogue tables: {names}."
            )
        missing_columns: list[str] = []
        for table, required_columns in REQUIRED_COLUMNS.items():
            for column in sorted(required_columns - self._column_names(table)):
                missing_columns.append(f"{table}.{column}")
        if missing_columns:
            names = ", ".join(missing_columns)
            raise InvalidCatalogueError(
                f"The selected file is missing required catalogue columns: {names}."
            )

    def _table_names(self) -> set[str]:
        return {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    def _column_names(self, table: str) -> set[str]:
        return {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def _create_or_rebuild_search_indexes(self) -> None:
        statements = [
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
        ]
        for statement in statements:
            self.connection.execute(statement)
        self.connection.execute("INSERT INTO files_fts(files_fts) VALUES ('rebuild')")
        self.connection.execute("INSERT INTO folders_fts(folders_fts) VALUES ('rebuild')")

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            yield self.connection
            return
        try:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _normalize_source_path(self, source_path: str) -> str:
        text = source_path.strip()
        if not text:
            return ""
        return str(Path(text).expanduser())

    def _normalize_volume_location(self, location: dict[str, Any] | None) -> dict[str, str]:
        data = {
            "identity_kind": "",
            "identity_token": "",
            "identity_label": "",
            "identity_serial": "",
            "identity_filesystem": "",
            "source_relative_path": "",
        }
        if location:
            for key in data:
                data[key] = str(location.get(key) or "")
            data["identity_kind"] = data["identity_kind"].strip()
            data["identity_token"] = data["identity_token"].strip()
        if not data["identity_kind"] or not data["identity_token"]:
            data["identity_kind"] = ""
            data["identity_token"] = ""
        return data

    def _normalize_volume_name(self, name: str | None) -> str | None:
        if name is None:
            return None
        text = name.strip()
        return text or None

    def _volume_select_sql(self, where: str = "") -> str:
        return f"""
            SELECT
                v.*,
                r.drive_id,
                r.is_mirror,
                r.status AS register_status,
                r.condition,
                r.description,
                r.earliest_content_date,
                r.latest_content_date,
                r.connector,
                r.date_added,
                r.retired_date,
                r.mirror_date,
                r.master_volume_id,
                master.name AS master_name,
                master_register.drive_id AS master_drive_id
            FROM volumes v
            JOIN volume_register r ON r.volume_id = v.id
            LEFT JOIN volumes master ON master.id = r.master_volume_id
            LEFT JOIN volume_register master_register ON master_register.volume_id = master.id
            {where}
        """

    def next_drive_id(self, conn: sqlite3.Connection | None = None) -> str:
        db = conn or self.connection
        highest = 0
        for row in db.execute("SELECT drive_id FROM volume_register WHERE drive_id IS NOT NULL"):
            sequence = drive_id_sequence(row["drive_id"])
            if sequence is not None:
                highest = max(highest, sequence)
        return f"AID-{highest + 1:03d}"

    def _coerce_volume_register(
        self,
        conn: sqlite3.Connection,
        register: dict[str, Any],
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "drive_id": self.next_drive_id(conn),
            "is_mirror": False,
            "status": ARCHIVE_STATUSES[0],
            "condition": "Unknown",
            "description": "",
            "earliest_content_date": None,
            "latest_content_date": None,
            "connector": "Unknown",
            "date_added": date.today().isoformat(),
            "retired_date": None,
            "mirror_date": None,
            "master_volume_id": None,
        }
        data.update(register)

        drive_id = str(data.get("drive_id") or "").strip()
        if not is_valid_drive_id(drive_id):
            raise ValueError("Drive ID is required.")
        data["drive_id"] = drive_id

        status = str(data.get("status") or "").strip()
        if status not in ARCHIVE_STATUSES:
            raise ValueError(f"Unsupported volume status: {status}")
        data["status"] = status

        condition = str(data.get("condition") or "").strip()
        if condition not in VOLUME_CONDITIONS:
            raise ValueError(f"Unsupported volume condition: {condition}")
        data["condition"] = condition

        connector = str(data.get("connector") or "").strip() or "Unknown"
        data["connector"] = connector
        data["description"] = str(data.get("description") or "")

        data["earliest_content_date"] = validate_iso_date(data.get("earliest_content_date"))
        data["latest_content_date"] = validate_iso_date(data.get("latest_content_date"))
        data["date_added"] = validate_iso_date(data.get("date_added")) or date.today().isoformat()
        data["retired_date"] = validate_iso_date(data.get("retired_date"))
        data["mirror_date"] = validate_iso_date(data.get("mirror_date"))

        if (
            data["earliest_content_date"] is not None
            and data["latest_content_date"] is not None
            and data["earliest_content_date"] > data["latest_content_date"]
        ):
            raise ValueError("Earliest Content Date cannot be after Latest Content Date.")
        if data["retired_date"] is not None and data["retired_date"] < data["date_added"]:
            raise ValueError("Retired Date cannot be before Date Added.")
        if data["mirror_date"] is not None and data["mirror_date"] < data["date_added"]:
            raise ValueError("Mirror Date cannot be before Date Added.")

        data["is_mirror"] = 1 if bool(data.get("is_mirror")) else 0
        if data["is_mirror"]:
            master_volume_id = data.get("master_volume_id")
            data["master_volume_id"] = int(master_volume_id) if master_volume_id is not None else None
        else:
            data["master_volume_id"] = None
            data["mirror_date"] = None

        return data

    def _validate_volume_register(
        self,
        conn: sqlite3.Connection,
        volume_id: int,
        data: dict[str, Any],
    ) -> None:
        if data["is_mirror"]:
            master_volume_id = data["master_volume_id"]
            if master_volume_id is None:
                raise ValueError("Mirror drives must have a master drive.")
            if int(master_volume_id) == int(volume_id):
                raise ValueError("A volume cannot mirror itself.")

            master = conn.execute(
                """
                SELECT v.id, r.is_mirror
                FROM volumes v
                JOIN volume_register r ON r.volume_id = v.id
                WHERE v.id = ?
                """,
                (master_volume_id,),
            ).fetchone()
            if master is None:
                raise ValueError("The selected master drive does not exist.")
            if bool(master["is_mirror"]):
                raise ValueError("Mirror drives cannot be selected as master drives.")
            if self._mirror_relationship_would_cycle(conn, volume_id, int(master_volume_id)):
                raise ValueError("Circular mirror relationships are not allowed.")

            dependents = self._list_mirror_dependents(conn, volume_id)
            if dependents:
                raise ValueError(
                    "This volume is already a master drive. Remove its mirror relationships before marking it as a mirror."
                )

    def _mirror_relationship_would_cycle(
        self,
        conn: sqlite3.Connection,
        volume_id: int,
        master_volume_id: int,
    ) -> bool:
        seen = {int(volume_id)}
        current = int(master_volume_id)
        while current:
            if current in seen:
                return True
            seen.add(current)
            row = conn.execute(
                "SELECT master_volume_id FROM volume_register WHERE volume_id = ?",
                (current,),
            ).fetchone()
            if row is None or row["master_volume_id"] is None:
                return False
            current = int(row["master_volume_id"])
        return False

    def _upsert_volume_register(
        self,
        conn: sqlite3.Connection,
        volume_id: int,
        register: dict[str, Any],
    ) -> None:
        data = self._coerce_volume_register(conn, register)
        self._validate_volume_register(conn, volume_id, data)
        now = utc_now()
        conn.execute(
            """
            INSERT INTO volume_register (
                volume_id, drive_id, is_mirror, status, condition, description,
                earliest_content_date, latest_content_date, connector, date_added,
                retired_date, mirror_date, master_volume_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(volume_id) DO UPDATE SET
                drive_id = excluded.drive_id,
                is_mirror = excluded.is_mirror,
                status = excluded.status,
                condition = excluded.condition,
                description = excluded.description,
                earliest_content_date = excluded.earliest_content_date,
                latest_content_date = excluded.latest_content_date,
                connector = excluded.connector,
                date_added = excluded.date_added,
                retired_date = excluded.retired_date,
                mirror_date = excluded.mirror_date,
                master_volume_id = excluded.master_volume_id,
                updated_at = excluded.updated_at
            """,
            (
                volume_id,
                data["drive_id"],
                data["is_mirror"],
                data["status"],
                data["condition"],
                data["description"],
                data["earliest_content_date"],
                data["latest_content_date"],
                data["connector"],
                data["date_added"],
                data["retired_date"],
                data["mirror_date"],
                data["master_volume_id"],
                now,
                now,
            ),
        )

    def create_volume(
        self,
        name: str | None,
        source_path: str = "",
        register: dict[str, Any] | None = None,
        location: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        source = self._normalize_source_path(source_path)
        clean_name = self._normalize_volume_name(name)
        location_data = self._normalize_volume_location(location)
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO volumes (
                    name, source_path, identity_kind, identity_token,
                    identity_label, identity_serial, identity_filesystem,
                    source_relative_path, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_name,
                    source,
                    location_data["identity_kind"],
                    location_data["identity_token"],
                    location_data["identity_label"],
                    location_data["identity_serial"],
                    location_data["identity_filesystem"],
                    location_data["source_relative_path"],
                    now,
                    now,
                ),
            )
            volume_id = int(cur.lastrowid)
            self._upsert_volume_register(conn, volume_id, register or {})
            return volume_id

    def update_volume(
        self,
        volume_id: int,
        name: str | None,
        source_path: str,
        register: dict[str, Any] | None = None,
    ) -> None:
        clean_name = self._normalize_volume_name(name)
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE volumes
                SET name = ?, source_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_name, self._normalize_source_path(source_path), utc_now(), volume_id),
            )
            if register is not None:
                self._upsert_volume_register(conn, volume_id, register)

    def update_volume_location(
        self,
        volume_id: int,
        source_path: str,
        location: dict[str, Any] | None,
    ) -> None:
        source = self._normalize_source_path(source_path)
        location_data = self._normalize_volume_location(location)
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE volumes
                SET source_path = ?,
                    identity_kind = ?,
                    identity_token = ?,
                    identity_label = ?,
                    identity_serial = ?,
                    identity_filesystem = ?,
                    source_relative_path = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    source,
                    location_data["identity_kind"],
                    location_data["identity_token"],
                    location_data["identity_label"],
                    location_data["identity_serial"],
                    location_data["identity_filesystem"],
                    location_data["source_relative_path"],
                    utc_now(),
                    volume_id,
                ),
            )

    def delete_volume(self, volume_id: int) -> None:
        with self.transaction(immediate=True) as conn:
            dependents = self._list_mirror_dependents(conn, volume_id)
            if dependents:
                names = ", ".join(self.volume_reference(row) for row in dependents)
                raise CatalogueError(
                    f"This volume is selected as the master drive for: {names}. "
                    "Remove those mirror relationships before deleting it."
                )
            conn.execute("DELETE FROM scan_errors WHERE volume_id = ?", (volume_id,))
            conn.execute("DELETE FROM scan_history WHERE volume_id = ?", (volume_id,))
            conn.execute("DELETE FROM files WHERE volume_id = ?", (volume_id,))
            conn.execute("DELETE FROM folders WHERE volume_id = ?", (volume_id,))
            conn.execute("DELETE FROM volume_register WHERE volume_id = ?", (volume_id,))
            conn.execute("DELETE FROM volumes WHERE id = ?", (volume_id,))

    def get_volume(self, volume_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            self._volume_select_sql("WHERE v.id = ?"),
            (volume_id,),
        ).fetchone()

    def list_volumes(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                self._volume_select_sql("ORDER BY v.name COLLATE NOCASE")
            )
        )

    def get_catalogue_info(self) -> sqlite3.Row:
        return self.connection.execute(
            """
            WITH volume_stats AS (
                SELECT
                    COUNT(*) AS volume_count,
                    COALESCE(SUM(capacity_bytes), 0) AS total_capacity_bytes,
                    COALESCE(SUM(used_bytes), 0) AS total_used_bytes,
                    COALESCE(SUM(free_bytes), 0) AS total_free_bytes,
                    MAX(last_scan_at) AS latest_scan_at
                FROM volumes
            ),
            file_stats AS (
                SELECT
                    COALESCE(SUM(CASE WHEN missing = 0 THEN 1 ELSE 0 END), 0) AS file_count,
                    COALESCE(SUM(CASE WHEN missing = 0 THEN size_bytes ELSE 0 END), 0) AS indexed_size_bytes,
                    COALESCE(SUM(CASE WHEN missing != 0 THEN 1 ELSE 0 END), 0) AS missing_file_count
                FROM files
            ),
            folder_stats AS (
                SELECT
                    COALESCE(SUM(CASE WHEN missing = 0 THEN 1 ELSE 0 END), 0) AS folder_count,
                    COALESCE(SUM(CASE WHEN missing != 0 THEN 1 ELSE 0 END), 0) AS missing_folder_count
                FROM folders
            ),
            scan_stats AS (
                SELECT COUNT(*) AS scan_count
                FROM scan_history
            )
            SELECT *
            FROM volume_stats, file_stats, folder_stats, scan_stats
            """
        ).fetchone()

    def upsert_volume_register(self, volume_id: int, register: dict[str, Any]) -> None:
        with self.transaction() as conn:
            self._upsert_volume_register(conn, volume_id, register)

    def _list_mirror_dependents(
        self,
        conn: sqlite3.Connection,
        volume_id: int,
    ) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT v.id, v.name, r.drive_id
                FROM volume_register r
                JOIN volumes v ON v.id = r.volume_id
                WHERE r.master_volume_id = ?
                ORDER BY r.drive_id COLLATE NOCASE, v.name COLLATE NOCASE
                """,
                (volume_id,),
            )
        )

    def list_mirror_dependents(self, volume_id: int) -> list[sqlite3.Row]:
        return self._list_mirror_dependents(self.connection, volume_id)

    def list_master_volume_options(self, current_volume_id: int | None = None) -> list[sqlite3.Row]:
        params: tuple[object, ...]
        where = "WHERE r.is_mirror = 0"
        if current_volume_id is not None:
            where += " AND v.id != ?"
            params = (current_volume_id,)
        else:
            params = ()
        return list(
            self.connection.execute(
                f"""
                SELECT v.id, v.name, r.drive_id
                FROM volumes v
                JOIN volume_register r ON r.volume_id = v.id
                {where}
                ORDER BY r.drive_id COLLATE NOCASE, v.name COLLATE NOCASE
                """,
                params,
            )
        )

    def volume_reference(self, row: sqlite3.Row) -> str:
        drive_id = row["drive_id"] if "drive_id" in row.keys() else None
        name = row["name"] if "name" in row.keys() else row["volume_name"]
        if drive_id and name:
            return f"{drive_id} - {name}"
        return drive_id or name or "Unnamed volume"

    def update_volume_content_dates_from_index(self, volume_id: int) -> None:
        row = self.connection.execute(
            """
            SELECT MIN(content_date) AS earliest, MAX(content_date) AS latest
            FROM (
                SELECT substr(modified_at, 1, 10) AS content_date
                FROM files
                WHERE volume_id = ?
                  AND missing = 0
                  AND modified_at IS NOT NULL
                UNION ALL
                SELECT substr(modified_at, 1, 10) AS content_date
                FROM folders
                WHERE volume_id = ?
                  AND missing = 0
                  AND modified_at IS NOT NULL
            )
            WHERE content_date IS NOT NULL
            """,
            (volume_id, volume_id),
        ).fetchone()
        if row is None:
            return
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE volume_register
                SET earliest_content_date = ?,
                    latest_content_date = ?,
                    updated_at = ?
                WHERE volume_id = ?
                """,
                (row["earliest"], row["latest"], utc_now(), volume_id),
            )

    def update_volume_storage(
        self,
        volume_id: int,
        capacity_bytes: int,
        used_bytes: int,
        free_bytes: int,
        scanned_at: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE volumes
                SET capacity_bytes = ?,
                    used_bytes = ?,
                    free_bytes = ?,
                    last_scan_at = COALESCE(?, last_scan_at),
                    updated_at = ?
                WHERE id = ?
                """,
                (capacity_bytes, used_bytes, free_bytes, scanned_at, utc_now(), volume_id),
            )

    def start_scan(self, volume_id: int) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO scan_history (volume_id, started_at, status)
                VALUES (?, ?, 'running')
                """,
                (volume_id, utc_now()),
            )
            return int(cur.lastrowid)

    def finish_scan(
        self,
        scan_id: int,
        status: str,
        files_seen: int,
        folders_seen: int,
        errors_count: int,
        message: str | None = None,
        changes: dict[str, int] | None = None,
        *,
        files_hashed: int = 0,
        bytes_hashed: int = 0,
        hash_errors: int = 0,
        media_files: int = 0,
        media_metadata_collected: int = 0,
        preview_summary: dict[str, Any] | None = None,
    ) -> None:
        summary = changes or {}
        previews = preview_summary or {}
        preview_mode = str(previews.get("mode") or "disabled")
        if preview_mode not in PREVIEW_SCAN_MODES:
            raise ValueError(f"Unsupported preview scan mode: {preview_mode}")
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE scan_history
                SET finished_at = ?,
                    status = ?,
                    files_seen = ?,
                    folders_seen = ?,
                    errors_count = ?,
                    message = ?,
                    files_added = ?,
                    files_removed = ?,
                    files_changed = ?,
                    folders_added = ?,
                    folders_removed = ?,
                    bytes_before = ?,
                    bytes_after = ?,
                    files_hashed = ?,
                    bytes_hashed = ?,
                    hash_errors = ?,
                    media_files = ?,
                    media_metadata_collected = ?,
                    preview_mode = ?,
                    image_previews_generated = ?,
                    image_previews_reused = ?,
                    image_previews_failed = ?,
                    video_previews_generated = ?,
                    video_previews_reused = ?,
                    video_previews_failed = ?,
                    previews_storage_skipped = ?,
                    preview_bytes_written = ?,
                    preview_message = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    files_seen,
                    folders_seen,
                    errors_count,
                    message,
                    summary.get("files_added"),
                    summary.get("files_removed"),
                    summary.get("files_changed"),
                    summary.get("folders_added"),
                    summary.get("folders_removed"),
                    summary.get("bytes_before"),
                    summary.get("bytes_after"),
                    int(files_hashed),
                    int(bytes_hashed),
                    int(hash_errors),
                    int(media_files),
                    int(media_metadata_collected),
                    preview_mode,
                    int(previews.get("image_generated") or 0),
                    int(previews.get("image_reused") or 0),
                    int(previews.get("image_failed") or 0),
                    int(previews.get("video_generated") or 0),
                    int(previews.get("video_reused") or 0),
                    int(previews.get("video_failed") or 0),
                    int(previews.get("storage_skipped") or 0),
                    int(previews.get("bytes_written") or 0),
                    str(previews.get("message") or ""),
                    scan_id,
                ),
            )
            self._prune_scan_history_for_volume(
                conn,
                scan_id=scan_id,
                keep=SCAN_HISTORY_PER_VOLUME_LIMIT,
            )

    def add_scan_error(self, scan_id: int, volume_id: int, path: str, message: str) -> None:
        self.connection.execute(
            """
            INSERT INTO scan_errors (scan_id, volume_id, path, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scan_id, volume_id, path, message, utc_now()),
        )

    def list_scan_errors(self, volume_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT se.*, sh.started_at
                FROM scan_errors se
                LEFT JOIN scan_history sh ON sh.id = se.scan_id
                WHERE se.volume_id = ?
                ORDER BY se.id DESC
                LIMIT ?
                """,
                (volume_id, limit),
            )
        )

    def list_scan_history(self, volume_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM scan_history
                WHERE volume_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (volume_id, limit),
            )
        )

    def ensure_folder(
        self,
        volume_id: int,
        parent_id: int | None,
        name: str,
        relative_path: str,
        scanned_at: str,
        modified_at: str | None = None,
    ) -> int:
        cur = self.connection.execute(
            """
            INSERT INTO folders (
                volume_id, parent_id, name, relative_path, missing, scanned_at, modified_at
            )
            VALUES (?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(volume_id, relative_path) DO UPDATE SET
                parent_id = excluded.parent_id,
                name = excluded.name,
                missing = 0,
                scanned_at = excluded.scanned_at,
                modified_at = excluded.modified_at
            RETURNING id
            """,
            (volume_id, parent_id, name, relative_path, scanned_at, modified_at),
        )
        return int(cur.fetchone()["id"])

    def upsert_file(
        self,
        volume_id: int,
        folder_id: int,
        name: str,
        relative_path: str,
        extension: str,
        size_bytes: int,
        modified_at: str | None,
        scanned_at: str,
        identity_device: int | None = None,
        identity_inode: int | None = None,
        content_hash: bytes | None = None,
        content_hash_algorithm: str | None = None,
    ) -> int:
        identity_device = normalize_identity_integer(identity_device)
        identity_inode = normalize_identity_integer(identity_inode)
        normalized_hash = bytes(content_hash) if content_hash is not None else None
        normalized_algorithm = str(content_hash_algorithm or "").strip().casefold() or None
        if (normalized_hash is None) != (normalized_algorithm is None):
            raise ValueError("A content hash and its algorithm must be stored together.")
        if normalized_algorithm == "sha256" and len(normalized_hash or b"") != 32:
            raise ValueError("A SHA-256 content hash must contain exactly 32 bytes.")
        cur = self.connection.execute(
            """
            INSERT INTO files (
                volume_id, folder_id, name, relative_path, extension,
                size_bytes, modified_at, missing, scanned_at, identity_device, identity_inode,
                content_hash, content_hash_algorithm
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(volume_id, relative_path) DO UPDATE SET
                folder_id = excluded.folder_id,
                name = excluded.name,
                extension = excluded.extension,
                size_bytes = excluded.size_bytes,
                modified_at = excluded.modified_at,
                missing = 0,
                scanned_at = excluded.scanned_at,
                identity_device = excluded.identity_device,
                identity_inode = excluded.identity_inode,
                content_hash = excluded.content_hash,
                content_hash_algorithm = excluded.content_hash_algorithm
            RETURNING id
            """,
            (
                volume_id,
                folder_id,
                name,
                relative_path,
                extension.lower(),
                size_bytes,
                modified_at,
                scanned_at,
                identity_device,
                identity_inode,
                normalized_hash,
                normalized_algorithm,
            ),
        )
        return int(cur.fetchone()["id"])

    def replace_file_media_metadata(
        self,
        file_id: int,
        metadata: dict[str, Any] | None,
        *,
        preserve_existing_on_failure: bool = False,
    ) -> None:
        if (
            metadata is not None
            and preserve_existing_on_failure
            and str(metadata.get("status") or "").casefold()
            in {"unavailable", "failed"}
        ):
            existing = self.get_file_media_metadata(file_id)
            if existing is not None and str(existing["status"] or "").casefold() in {
                "complete",
                "partial",
            }:
                latest_message = str(metadata.get("message") or "").strip()
                retained_message = (
                    "Previously collected details were retained; the latest scan "
                    "could not refresh them"
                )
                if latest_message:
                    retained_message += f": {latest_message}"
                metadata = {
                    "status": "partial",
                    "media_kind": existing["media_kind"],
                    "source": existing["source"],
                    "container": existing["container"],
                    "duration_ms": existing["duration_ms"],
                    "width": existing["width"],
                    "height": existing["height"],
                    "video_codecs": existing["video_codecs"],
                    "audio_codecs": existing["audio_codecs"],
                    "sample_rate_hz": existing["sample_rate_hz"],
                    "channels": existing["channels"],
                    "bit_rate": existing["bit_rate"],
                    "message": retained_message,
                    "probed_at": existing["probed_at"],
                }
        self.connection.execute(
            "DELETE FROM file_media_metadata WHERE file_id = ?",
            (int(file_id),),
        )
        if metadata is None:
            return
        self.connection.execute(
            """
            INSERT INTO file_media_metadata (
                file_id, status, media_kind, source, container, duration_ms,
                width, height, video_codecs, audio_codecs, sample_rate_hz,
                channels, bit_rate, message, probed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(file_id),
                str(metadata.get("status") or "unavailable"),
                str(metadata.get("media_kind") or ""),
                str(metadata.get("source") or ""),
                metadata.get("container") or metadata.get("container_format"),
                metadata.get("duration_ms"),
                metadata.get("width"),
                metadata.get("height"),
                metadata.get("video_codecs") or metadata.get("video_codec"),
                metadata.get("audio_codecs") or metadata.get("audio_codec"),
                metadata.get("sample_rate_hz"),
                metadata.get("channels"),
                metadata.get("bit_rate"),
                str(metadata.get("message") or ""),
                metadata.get("probed_at") or utc_now(),
            ),
        )

    def get_file_media_metadata(self, file_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM file_media_metadata WHERE file_id = ?",
            (int(file_id),),
        ).fetchone()

    def replace_file_preview_status(
        self,
        file_id: int,
        status: dict[str, Any] | None,
    ) -> None:
        """Store the latest offline-preview outcome for one file.

        ``status`` uses the keys ``media_kind``, ``profile_id``, ``status``
        (``available``/``failed``/``missing``/``unsupported``), ``source_hash``,
        ``preview_size``, ``preview_width``, ``preview_height``,
        ``preview_duration_ms``, ``generated_at``, ``error_stage`` and
        ``error_message``.  Passing ``None`` removes any stored status.  The
        preview file location is never stored: it is derived from the current
        preview root, the profile ID, and the file's SHA-256.
        """

        self.connection.execute(
            "DELETE FROM file_preview_status WHERE file_id = ?",
            (int(file_id),),
        )
        if status is None:
            return
        status_value = str(status.get("status") or "").strip().casefold()
        if status_value not in PREVIEW_STATUS_VALUES:
            raise ValueError(f"Unsupported preview status: {status_value!r}")
        media_kind = str(status.get("media_kind") or "").strip().casefold()
        if media_kind not in {"image", "video"}:
            raise ValueError(f"Unsupported preview media kind: {media_kind!r}")
        profile_id = str(status.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("A preview profile ID is required.")
        source_hash = status.get("source_hash")
        if source_hash is not None:
            source_hash = bytes(source_hash)
            if len(source_hash) != 32:
                raise ValueError("A preview source hash must be a 32-byte SHA-256 digest.")
        self.connection.execute(
            """
            INSERT INTO file_preview_status (
                file_id, media_kind, profile_id, status, source_hash,
                preview_size, preview_width, preview_height, preview_duration_ms,
                generated_at, error_stage, error_message, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(file_id),
                media_kind,
                profile_id,
                status_value,
                source_hash,
                status.get("preview_size"),
                status.get("preview_width"),
                status.get("preview_height"),
                status.get("preview_duration_ms"),
                status.get("generated_at"),
                status.get("error_stage"),
                str(status.get("error_message") or ""),
                status.get("updated_at") or utc_now(),
            ),
        )

    def get_file_preview_status(self, file_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM file_preview_status WHERE file_id = ?",
            (int(file_id),),
        ).fetchone()

    def list_preview_failures(
        self,
        volume_id: int,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Return indexed files whose last recorded preview attempt failed."""

        params: list[object] = [int(volume_id)]
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(max(int(limit), 0))
        return list(
            self.connection.execute(
                f"""
                SELECT
                    f.id AS file_id,
                    f.name,
                    f.relative_path,
                    f.content_hash,
                    p.media_kind,
                    p.profile_id,
                    p.status,
                    p.error_stage,
                    p.error_message,
                    p.updated_at
                FROM file_preview_status p
                JOIN files f ON f.id = p.file_id
                WHERE f.volume_id = ? AND f.missing = 0 AND p.status = 'failed'
                ORDER BY f.relative_path COLLATE NOCASE
                {limit_clause}
                """,
                params,
            )
        )

    def count_preview_statuses(self, volume_id: int | None = None) -> dict[str, int]:
        """Return ``{status: count}`` for stored preview outcomes."""

        if volume_id is None:
            rows = self.connection.execute(
                "SELECT status, COUNT(*) AS total FROM file_preview_status GROUP BY status"
            )
        else:
            rows = self.connection.execute(
                """
                SELECT p.status, COUNT(*) AS total
                FROM file_preview_status p
                JOIN files f ON f.id = p.file_id
                WHERE f.volume_id = ? AND f.missing = 0
                GROUP BY p.status
                """,
                (int(volume_id),),
            )
        return {str(row["status"]): int(row["total"]) for row in rows}

    def prepare_content_hash_lookup(self, extensions: Iterable[str]) -> int:
        """Build a connection-local index of SHA-256 digests for some extensions.

        Used by the preview cache manager to decide whether a preview file on
        disk is referenced by this catalogue.  The lookup lives in a temporary
        table so multi-million-row catalogues never need to be loaded into
        Python memory; call :meth:`drop_content_hash_lookup` when done.
        """

        normalized = sorted(
            {str(extension or "").strip().casefold().lstrip(".") for extension in extensions}
            - {""}
        )
        # ``PRAGMA query_only`` (set for read-only connections) also refuses
        # connection-local TEMP tables.  The lookup never touches the main
        # database, and a ``mode=ro`` URI connection still rejects any write to
        # it, so temporarily allowing temp-schema statements is safe.
        with self._temporary_schema_writes():
            self.connection.execute("DROP TABLE IF EXISTS temp.preview_referenced_hashes")
            self.connection.execute(
                """
                CREATE TEMP TABLE preview_referenced_hashes (
                    digest BLOB PRIMARY KEY
                ) WITHOUT ROWID
                """
            )
            if not normalized:
                return 0
            placeholders = ",".join("?" for _ in normalized)
            cursor = self.connection.execute(
                f"""
                INSERT OR IGNORE INTO preview_referenced_hashes (digest)
                SELECT content_hash
                FROM files
                WHERE content_hash IS NOT NULL
                  AND content_hash_algorithm = 'sha256'
                  AND lower(extension) IN ({placeholders})
                """,
                normalized,
            )
            return int(
                cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
            )

    def content_hash_referenced(self, digest: bytes) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM temp.preview_referenced_hashes WHERE digest = ? LIMIT 1",
            (bytes(digest),),
        ).fetchone()
        return row is not None

    def drop_content_hash_lookup(self) -> None:
        with self._temporary_schema_writes():
            self.connection.execute("DROP TABLE IF EXISTS temp.preview_referenced_hashes")

    @contextmanager
    def _temporary_schema_writes(self) -> Iterator[None]:
        """Allow TEMP-table statements on a read-only connection, then restore."""

        if not self.read_only:
            yield
            return
        self.connection.execute("PRAGMA query_only = OFF")
        try:
            yield
        finally:
            self.connection.execute("PRAGMA query_only = ON")

    def prepare_scan_comparison(self, volume_id: int) -> None:
        """Take a connection-local snapshot used only while reviewing one scan."""
        self.connection.execute("DROP TABLE IF EXISTS temp.scan_previous_files")
        self.connection.execute("DROP TABLE IF EXISTS temp.scan_previous_folders")
        self.connection.execute(
            """
            CREATE TEMP TABLE scan_previous_files (
                relative_path TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                modified_at TEXT,
                content_hash BLOB,
                content_hash_algorithm TEXT
            ) WITHOUT ROWID
            """
        )
        self.connection.execute(
            """
            INSERT INTO scan_previous_files (
                relative_path, size_bytes, modified_at, content_hash,
                content_hash_algorithm
            )
            SELECT relative_path, size_bytes, modified_at, content_hash,
                   content_hash_algorithm
            FROM files
            WHERE volume_id = ? AND missing = 0
            """,
            (volume_id,),
        )
        self.connection.execute(
            """
            CREATE TEMP TABLE scan_previous_folders (
                relative_path TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        self.connection.execute(
            """
            INSERT INTO scan_previous_folders (relative_path)
            SELECT relative_path
            FROM folders
            WHERE volume_id = ? AND missing = 0
            """,
            (volume_id,),
        )

    def scan_previous_file_hash(
        self,
        relative_path: str,
    ) -> tuple[bytes, str] | None:
        row = self.connection.execute(
            """
            SELECT content_hash, content_hash_algorithm
            FROM scan_previous_files
            WHERE relative_path = ?
            """,
            (relative_path,),
        ).fetchone()
        if (
            row is None
            or row["content_hash"] is None
            or not str(row["content_hash_algorithm"] or "").strip()
        ):
            return None
        return bytes(row["content_hash"]), str(row["content_hash_algorithm"])

    def scan_change_summary(self, volume_id: int, scanned_at: str) -> dict[str, int]:
        file_totals = self.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM scan_previous_files) AS files_before,
                (SELECT COALESCE(SUM(size_bytes), 0) FROM scan_previous_files) AS bytes_before,
                COUNT(*) AS files_after,
                COALESCE(SUM(f.size_bytes), 0) AS bytes_after
            FROM files f
            WHERE f.volume_id = ? AND f.scanned_at = ?
            """,
            (volume_id, scanned_at),
        ).fetchone()
        file_changes = self.connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN p.relative_path IS NULL THEN 1 ELSE 0 END), 0) AS files_added,
                COALESCE(SUM(CASE WHEN p.relative_path IS NULL THEN f.size_bytes ELSE 0 END), 0) AS bytes_added,
                COALESCE(SUM(CASE
                    WHEN p.relative_path IS NOT NULL
                     AND (
                         p.size_bytes != f.size_bytes
                         OR CASE
                             WHEN p.content_hash IS NOT NULL
                              AND f.content_hash IS NOT NULL
                              AND p.content_hash_algorithm = f.content_hash_algorithm
                             THEN p.content_hash != f.content_hash
                             ELSE p.modified_at IS NOT f.modified_at
                            END
                     )
                    THEN 1 ELSE 0 END), 0) AS files_changed,
                COALESCE(SUM(CASE
                    WHEN p.relative_path IS NOT NULL
                     AND (
                         p.size_bytes != f.size_bytes
                         OR CASE
                             WHEN p.content_hash IS NOT NULL
                              AND f.content_hash IS NOT NULL
                              AND p.content_hash_algorithm = f.content_hash_algorithm
                             THEN p.content_hash != f.content_hash
                             ELSE p.modified_at IS NOT f.modified_at
                            END
                     )
                    THEN p.size_bytes ELSE 0 END), 0) AS changed_bytes_before,
                COALESCE(SUM(CASE
                    WHEN p.relative_path IS NOT NULL
                     AND (
                         p.size_bytes != f.size_bytes
                         OR CASE
                             WHEN p.content_hash IS NOT NULL
                              AND f.content_hash IS NOT NULL
                              AND p.content_hash_algorithm = f.content_hash_algorithm
                             THEN p.content_hash != f.content_hash
                             ELSE p.modified_at IS NOT f.modified_at
                            END
                     )
                    THEN f.size_bytes ELSE 0 END), 0) AS changed_bytes_after
            FROM files f
            LEFT JOIN scan_previous_files p ON p.relative_path = f.relative_path
            WHERE f.volume_id = ? AND f.scanned_at = ?
            """,
            (volume_id, scanned_at),
        ).fetchone()
        removed_files = self.connection.execute(
            """
            SELECT COUNT(*) AS files_removed, COALESCE(SUM(p.size_bytes), 0) AS bytes_removed
            FROM scan_previous_files p
            LEFT JOIN files f
              ON f.volume_id = ?
             AND f.relative_path = p.relative_path
             AND f.scanned_at = ?
            WHERE f.id IS NULL
            """,
            (volume_id, scanned_at),
        ).fetchone()
        folder_changes = self.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM scan_previous_folders) AS folders_before,
                COUNT(*) AS folders_after,
                COALESCE(SUM(CASE WHEN p.relative_path IS NULL THEN 1 ELSE 0 END), 0) AS folders_added
            FROM folders f
            LEFT JOIN scan_previous_folders p ON p.relative_path = f.relative_path
            WHERE f.volume_id = ? AND f.scanned_at = ?
            """,
            (volume_id, scanned_at),
        ).fetchone()
        removed_folders = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM scan_previous_folders p
            LEFT JOIN folders f
              ON f.volume_id = ?
             AND f.relative_path = p.relative_path
             AND f.scanned_at = ?
            WHERE f.id IS NULL
            """,
            (volume_id, scanned_at),
        ).fetchone()[0]
        return {
            "files_before": int(file_totals["files_before"]),
            "files_after": int(file_totals["files_after"]),
            "files_added": int(file_changes["files_added"]),
            "files_removed": int(removed_files["files_removed"]),
            "files_changed": int(file_changes["files_changed"]),
            "folders_before": int(folder_changes["folders_before"]),
            "folders_after": int(folder_changes["folders_after"]),
            "folders_added": int(folder_changes["folders_added"]),
            "folders_removed": int(removed_folders),
            "bytes_before": int(file_totals["bytes_before"]),
            "bytes_after": int(file_totals["bytes_after"]),
            "bytes_added": int(file_changes["bytes_added"]),
            "bytes_removed": int(removed_files["bytes_removed"]),
            "changed_bytes_before": int(file_changes["changed_bytes_before"]),
            "changed_bytes_after": int(file_changes["changed_bytes_after"]),
        }

    def finalize_scan_items(
        self,
        volume_id: int,
        scanned_at: str,
    ) -> None:
        self.connection.execute(
            """
            DELETE FROM files
            WHERE volume_id = ?
              AND (scanned_at IS NULL OR scanned_at != ?)
            """,
            (volume_id, scanned_at),
        )
        self.connection.execute(
            """
            DELETE FROM folders
            WHERE volume_id = ?
              AND (scanned_at IS NULL OR scanned_at != ?)
              AND relative_path != ''
            """,
            (volume_id, scanned_at),
        )

    def rebuild_folder_statistics(
        self,
        volume_id: int,
        stats_updated_at: str | None = None,
        progress_callback: FolderStatsProgress | None = None,
        *,
        clear_existing: bool = True,
    ) -> int:
        updated_at = stats_updated_at or utc_now()
        with self.transaction() as conn:
            folder_rows = list(
                conn.execute(
                    """
                    SELECT id, parent_id, relative_path
                    FROM folders
                    WHERE volume_id = ? AND missing = 0
                    """,
                    (volume_id,),
                )
            )
            total = len(folder_rows)
            if progress_callback:
                progress_callback(0, total, "Preparing folder statistics")

            direct_file_rows = conn.execute(
                """
                SELECT
                    folder_id,
                    COUNT(*) AS direct_file_count,
                    COALESCE(SUM(size_bytes), 0) AS direct_size
                FROM files
                WHERE volume_id = ?
                  AND missing = 0
                  AND folder_id IS NOT NULL
                GROUP BY folder_id
                """,
                (volume_id,),
            )
            duplicate_file_rows = conn.execute(
                """
                WITH duplicate_identities AS (
                    SELECT
                        identity_device,
                        identity_inode,
                        MAX(size_bytes) AS size_bytes
                    FROM files
                    WHERE volume_id = ?
                      AND missing = 0
                      AND folder_id IS NOT NULL
                      AND identity_device IS NOT NULL
                      AND identity_inode IS NOT NULL
                    GROUP BY identity_device, identity_inode
                    HAVING COUNT(*) > 1
                )
                SELECT
                    f.identity_device,
                    f.identity_inode,
                    f.folder_id,
                    d.size_bytes
                FROM files f
                JOIN duplicate_identities d
                  ON d.identity_device = f.identity_device
                 AND d.identity_inode = f.identity_inode
                WHERE f.volume_id = ?
                  AND f.missing = 0
                  AND f.folder_id IS NOT NULL
                ORDER BY f.identity_device, f.identity_inode
                """,
                (volume_id, volume_id),
            )
            stats, _ = calculate_folder_statistics(
                folder_rows,
                direct_file_rows,
                duplicate_file_rows,
                progress_callback,
            )

            if clear_existing:
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
            update_rows = [
                (
                    folder_stats["recursive_size"],
                    folder_stats["recursive_file_count"],
                    folder_stats["recursive_subfolder_count"],
                    folder_stats["direct_file_count"],
                    folder_stats["direct_subfolder_count"],
                    updated_at,
                    folder_id,
                )
                for folder_id, folder_stats in stats.items()
            ]
            conn.executemany(
                """
                UPDATE folders
                SET recursive_size_bytes = ?,
                    recursive_file_count = ?,
                    recursive_subfolder_count = ?,
                    direct_file_count = ?,
                    direct_subfolder_count = ?,
                    stats_updated_at = ?
                WHERE id = ?
                """,
                update_rows,
            )
            if progress_callback:
                progress_callback(total, total, "Folder statistics updated")
            return total

    def refresh_volume_counts(self, volume_id: int, scanned_at: str | None = None) -> None:
        file_count = self.connection.execute(
            "SELECT COUNT(*) FROM files WHERE volume_id = ? AND missing = 0",
            (volume_id,),
        ).fetchone()[0]
        folder_count = self.connection.execute(
            "SELECT COUNT(*) FROM folders WHERE volume_id = ? AND missing = 0",
            (volume_id,),
        ).fetchone()[0]
        self.connection.execute(
            """
            UPDATE volumes
            SET indexed_file_count = ?,
                indexed_folder_count = ?,
                last_scan_at = COALESCE(?, last_scan_at),
                updated_at = ?
            WHERE id = ?
            """,
            (file_count, folder_count, scanned_at, utc_now(), volume_id),
        )

    def get_root_folder(self, volume_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM folders
            WHERE volume_id = ? AND relative_path = ''
            """,
            (volume_id,),
        ).fetchone()

    def list_child_folders(self, volume_id: int, parent_id: int | None) -> list[sqlite3.Row]:
        if parent_id is None:
            where = "parent_id IS NULL"
            params: Sequence[object] = (volume_id,)
        else:
            where = "parent_id = ?"
            params = (volume_id, parent_id)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM folders
                WHERE volume_id = ? AND {where}
                ORDER BY name COLLATE NOCASE
                """,
                params,
            )
        )

    def list_files(self, volume_id: int, folder_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM files
                WHERE volume_id = ? AND folder_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (volume_id, folder_id),
            )
        )

    def get_folder(self, folder_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()

    def get_file(self, file_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def get_item_properties(self, item_type: str, item_id: int) -> sqlite3.Row | None:
        if item_type == "file":
            return self.connection.execute(
                """
                SELECT
                    'file' AS item_type,
                    f.id AS item_id,
                    f.volume_id,
                    f.folder_id AS parent_id,
                    f.name,
                    f.relative_path,
                    f.extension,
                    f.size_bytes,
                    f.modified_at,
                    f.missing,
                    f.scanned_at,
                    f.identity_device,
                    f.identity_inode,
                    f.content_hash,
                    f.content_hash_algorithm,
                    media.status AS media_status,
                    media.media_kind,
                    media.source AS media_source,
                    media.container AS media_container,
                    media.duration_ms AS media_duration_ms,
                    media.width AS media_width,
                    media.height AS media_height,
                    media.video_codecs,
                    media.audio_codecs,
                    media.sample_rate_hz AS media_sample_rate_hz,
                    media.channels AS media_channels,
                    media.bit_rate AS media_bit_rate,
                    media.message AS media_message,
                    media.probed_at AS media_probed_at,
                    preview.media_kind AS preview_media_kind,
                    preview.profile_id AS preview_profile_id,
                    preview.status AS preview_status,
                    preview.source_hash AS preview_source_hash,
                    preview.preview_size,
                    preview.preview_width,
                    preview.preview_height,
                    preview.preview_duration_ms,
                    preview.generated_at AS preview_generated_at,
                    preview.error_stage AS preview_error_stage,
                    preview.error_message AS preview_error_message,
                    preview.updated_at AS preview_updated_at,
                    v.name AS volume_name,
                    v.source_path,
                    parent.id AS parent_folder_id,
                    parent.name AS parent_folder_name,
                    parent.relative_path AS parent_relative_path,
                    NULL AS recursive_file_count,
                    NULL AS recursive_subfolder_count,
                    NULL AS direct_file_count,
                    NULL AS direct_subfolder_count,
                    NULL AS stats_updated_at
                FROM files f
                JOIN volumes v ON v.id = f.volume_id
                LEFT JOIN folders parent ON parent.id = f.folder_id
                LEFT JOIN file_media_metadata media ON media.file_id = f.id
                LEFT JOIN file_preview_status preview ON preview.file_id = f.id
                WHERE f.id = ?
                """,
                (item_id,),
            ).fetchone()

        if item_type == "folder":
            return self.connection.execute(
                """
                SELECT
                    'folder' AS item_type,
                    fo.id AS item_id,
                    fo.volume_id,
                    fo.parent_id,
                    fo.name,
                    fo.relative_path,
                    '' AS extension,
                    fo.recursive_size_bytes AS size_bytes,
                    fo.modified_at,
                    fo.missing,
                    fo.scanned_at,
                    NULL AS identity_device,
                    NULL AS identity_inode,
                    v.name AS volume_name,
                    v.source_path,
                    parent.id AS parent_folder_id,
                    parent.name AS parent_folder_name,
                    parent.relative_path AS parent_relative_path,
                    fo.recursive_file_count,
                    fo.recursive_subfolder_count,
                    fo.direct_file_count,
                    fo.direct_subfolder_count,
                    fo.stats_updated_at
                FROM folders fo
                JOIN volumes v ON v.id = fo.volume_id
                LEFT JOIN folders parent ON parent.id = fo.parent_id
                WHERE fo.id = ?
                """,
                (item_id,),
            ).fetchone()

        return None

    def get_folder_by_path(self, volume_id: int, relative_path: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM folders
            WHERE volume_id = ? AND relative_path = ?
            """,
            (volume_id, relative_path),
        ).fetchone()

    def iter_search(
        self,
        query: str,
        limit: int | None = None,
        *,
        include_paths: bool = False,
    ) -> Iterator[sqlite3.Row]:
        text = query.strip()
        if not text:
            return iter(())
        escaped_text = (
            text.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        needle = f"%{escaped_text}%"
        params: dict[str, object] = {
            "text": text,
            "needle": needle,
        }
        file_like_columns = ["f.name", "f.extension"]
        folder_like_columns = ["fo.name"]
        if include_paths:
            file_like_columns.insert(1, "f.relative_path")
            folder_like_columns.append("fo.relative_path")
        file_like_clause = "\n                    OR ".join(
            f"{column} LIKE :needle ESCAPE '\\' COLLATE NOCASE"
            for column in file_like_columns
        )
        folder_like_clause = "\n                    OR ".join(
            f"{column} LIKE :needle ESCAPE '\\' COLLATE NOCASE"
            for column in folder_like_columns
        )
        if len(text) >= 3:
            trigram_query = " AND ".join(
                f'"{trigram.replace(chr(34), chr(34) * 2)}"'
                for trigram in dict.fromkeys(
                    text[index : index + 3]
                    for index in range(len(text) - 2)
                )
            )
            params["file_fts_query"] = (
                trigram_query
                if include_paths
                else f"{{name extension}} : ({trigram_query})"
            )
            params["folder_fts_query"] = (
                trigram_query if include_paths else f"name : ({trigram_query})"
            )
        if text.startswith("."):
            params["extension"] = text[1:].lower()
            file_search_join = ""
            file_clause = "f.extension = :extension"
            file_match_rank = "0"
        elif len(text) >= 3:
            file_search_join = "JOIN files_fts ON files_fts.rowid = f.id"
            file_clause = f"""
                files_fts MATCH :file_fts_query
                AND (
                    {file_like_clause}
                )
            """
            file_match_rank = """
                CASE
                    WHEN f.name = :text COLLATE NOCASE THEN 0
                    WHEN f.name LIKE :needle ESCAPE '\\' COLLATE NOCASE THEN 2
                    ELSE 4
                END
            """
        else:
            file_search_join = ""
            file_clause = file_like_clause
            file_match_rank = """
                CASE
                    WHEN f.name = :text COLLATE NOCASE THEN 0
                    WHEN f.name LIKE :needle ESCAPE '\\' COLLATE NOCASE THEN 2
                    ELSE 4
                END
            """

        if len(text) >= 3:
            folder_search_join = "JOIN folders_fts ON folders_fts.rowid = fo.id"
            folder_clause = f"""
                folders_fts MATCH :folder_fts_query
                AND (
                    {folder_like_clause}
                )
            """
        else:
            folder_search_join = ""
            folder_clause = folder_like_clause

        limit_clause = ""
        if limit is not None:
            params["limit"] = max(int(limit), 0)
            limit_clause = "LIMIT :limit"

        sql = f"""
            SELECT *
            FROM (
                SELECT
                    'file' AS item_type,
                    f.id AS item_id,
                    f.name,
                    v.id AS volume_id,
                    r.drive_id AS drive_id,
                    v.name AS volume_name,
                    f.relative_path,
                    f.size_bytes,
                    f.modified_at,
                    f.missing,
                    v.source_path,
                    v.identity_kind,
                    v.identity_token,
                    v.source_relative_path,
                    CASE WHEN f.missing = 0 THEN 0 ELSE 1 END AS missing_rank,
                    {file_match_rank} AS match_rank
                FROM files f
                {file_search_join}
                JOIN volumes v ON v.id = f.volume_id
                JOIN volume_register r ON r.volume_id = v.id
                WHERE {file_clause}
                UNION ALL
                SELECT
                    'folder' AS item_type,
                    fo.id AS item_id,
                    fo.name,
                    v.id AS volume_id,
                    r.drive_id AS drive_id,
                    v.name AS volume_name,
                    fo.relative_path,
                    fo.recursive_size_bytes AS size_bytes,
                    fo.modified_at,
                    fo.missing,
                    v.source_path,
                    v.identity_kind,
                    v.identity_token,
                    v.source_relative_path,
                    CASE WHEN fo.missing = 0 THEN 0 ELSE 1 END AS missing_rank,
                    CASE
                        WHEN fo.name = :text COLLATE NOCASE THEN 0
                        WHEN fo.name LIKE :needle ESCAPE '\\' COLLATE NOCASE THEN 1
                        ELSE 3
                    END AS match_rank
                FROM folders fo
                {folder_search_join}
                JOIN volumes v ON v.id = fo.volume_id
                JOIN volume_register r ON r.volume_id = v.id
                WHERE {folder_clause}
            )
            ORDER BY
                match_rank,
                missing_rank,
                CASE WHEN item_type = 'folder' THEN 0 ELSE 1 END,
                name COLLATE NOCASE
            {limit_clause}
        """
        return iter(self.connection.execute(sql, params))

    def search(
        self,
        query: str,
        limit: int | None = None,
        *,
        include_paths: bool = False,
    ) -> list[sqlite3.Row]:
        return list(
            self.iter_search(
                query,
                limit=limit,
                include_paths=include_paths,
            )
        )

    def rebuild_search_indexes(self) -> None:
        """Recreate the external-content FTS indexes from authoritative rows."""
        with self.transaction():
            self._create_or_rebuild_search_indexes()

    def _prune_scan_history_for_volume(
        self,
        conn: sqlite3.Connection,
        *,
        keep: int,
        volume_id: int | None = None,
        scan_id: int | None = None,
    ) -> None:
        if volume_id is None:
            if scan_id is None:
                raise ValueError("volume_id or scan_id is required")
            row = conn.execute(
                "SELECT volume_id FROM scan_history WHERE id = ?",
                (scan_id,),
            ).fetchone()
            if row is None:
                return
            volume_id = int(row["volume_id"])
        stale = list(
            conn.execute(
                """
                SELECT id FROM scan_history
                WHERE volume_id = ?
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
                """,
                (volume_id, max(keep, 0)),
            )
        )
        if stale:
            conn.executemany(
                "DELETE FROM scan_history WHERE id = ?",
                [(row["id"],) for row in stale],
            )


def catalogue_path_with_extension(path: str | Path) -> Path:
    catalogue_path = Path(path).expanduser()
    if catalogue_path.suffix.lower() == CATALOGUE_EXTENSION:
        return catalogue_path
    return Path(f"{catalogue_path}{CATALOGUE_EXTENSION}")


def create_catalogue(path: str | Path, *, overwrite: bool = False) -> Database:
    target = catalogue_path_with_extension(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Catalogue already exists: {target}")

    fd, temp_name = tempfile.mkstemp(
        prefix=f"{target.name}.",
        suffix=".creating",
        dir=target.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    temp_path.unlink()

    db: Database | None = None
    try:
        db = Database(temp_path)
        db.close()
        db = None
        os.replace(temp_path, target)
        return open_catalogue(target)
    except Exception:
        if db is not None:
            db.close()
        temp_path.unlink(missing_ok=True)
        raise


def open_catalogue(
    path: str | Path,
    *,
    busy_timeout_ms: int = INTERACTIVE_BUSY_TIMEOUT_MS,
    check_same_thread: bool = True,
) -> Database:
    db = Database(
        catalogue_path_with_extension(path),
        initialize=False,
        create=False,
        busy_timeout_ms=busy_timeout_ms,
        check_same_thread=check_same_thread,
    )
    try:
        db.validate_catalogue()
        return db
    except Exception:
        db.close()
        raise


def count_rows(db: Database, table: str) -> int:
    if table not in REQUIRED_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    return int(db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
