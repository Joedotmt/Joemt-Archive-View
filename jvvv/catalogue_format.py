"""On-disk layout of a JVVV catalogue directory.

A catalogue is a directory (conventionally ``Name.jvvv``) holding three kinds
of SQLite files::

    Name.jvvv/
        catalogue.sqlite          registry: catalogue identity + volume id <-> uuid
        volumes/<uuid>.jvvvol     one self-contained file per volume (source of truth)
        index.sqlite              derived working database (search, evidence, browsing)

``catalogue.sqlite`` and the volume files are the durable state.  ``index.sqlite``
is a cache built from them: it can be deleted and is rebuilt on the next open.
Snapshots (see :mod:`jvvv.snapshot_repository`) store the registry and the volume
files only.
"""

from __future__ import annotations

import os
import sqlite3
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CATALOGUE_EXTENSION = ".jvvv"
CATALOGUE_DB_NAME = "catalogue.sqlite"
INDEX_DB_NAME = "index.sqlite"
VOLUMES_DIR_NAME = "volumes"
VOLUME_FILE_EXTENSION = ".jvvvol"
VOLUME_TOMBSTONE_SUFFIX = ".deleted"
VOLUME_TEMP_MARKER = ".tmp-"
CATALOGUE_LOCK_SUFFIX = ".lock"

CATALOGUE_FORMAT = "jvvv-catalogue-directory"
CATALOGUE_FORMAT_VERSION = 1
# application_id values make the three SQLite files self-identifying.
CATALOGUE_APPLICATION_ID = 0x4A565643  # "JVVC"
VOLUME_APPLICATION_ID = 0x4A56564C  # "JVVL"
INDEX_APPLICATION_ID = 0x4A565649  # "JVVI"
VOLUME_FILE_FORMAT = "jvvv-volume"
VOLUME_FILE_FORMAT_VERSION = 1

SQLITE_HEADER = b"SQLite format 3\x00"


class CatalogueLayoutError(Exception):
    """The directory does not have the expected catalogue layout."""


@dataclass(frozen=True)
class CatalogueLayout:
    """Paths inside one catalogue directory."""

    root: Path

    @property
    def catalogue_db(self) -> Path:
        return self.root / CATALOGUE_DB_NAME

    @property
    def index_db(self) -> Path:
        return self.root / INDEX_DB_NAME

    @property
    def volumes_dir(self) -> Path:
        return self.root / VOLUMES_DIR_NAME

    @property
    def lock_file(self) -> Path:
        return Path(f"{self.root}{CATALOGUE_LOCK_SUFFIX}")

    def volume_file(self, volume_uuid: str) -> Path:
        return self.volumes_dir / f"{volume_uuid}{VOLUME_FILE_EXTENSION}"

    def volume_tombstone(self, volume_uuid: str) -> Path:
        return self.volumes_dir / f"{volume_uuid}{VOLUME_FILE_EXTENSION}{VOLUME_TOMBSTONE_SUFFIX}"

    def exists(self) -> bool:
        return self.root.is_dir() and self.catalogue_db.is_file()

    def iter_volume_files(self) -> Iterable[Path]:
        if not self.volumes_dir.is_dir():
            return
        for entry in sorted(self.volumes_dir.iterdir(), key=lambda item: item.name):
            if entry.is_file() and entry.suffix == VOLUME_FILE_EXTENSION:
                yield entry

    def iter_tombstones(self) -> Iterable[Path]:
        if not self.volumes_dir.is_dir():
            return
        for entry in sorted(self.volumes_dir.iterdir(), key=lambda item: item.name):
            if entry.is_file() and entry.name.endswith(
                f"{VOLUME_FILE_EXTENSION}{VOLUME_TOMBSTONE_SUFFIX}"
            ):
                yield entry

    def iter_temp_files(self) -> Iterable[Path]:
        if not self.volumes_dir.is_dir():
            return
        for entry in sorted(self.volumes_dir.iterdir(), key=lambda item: item.name):
            if entry.is_file() and VOLUME_TEMP_MARKER in entry.name:
                yield entry


def catalogue_path_with_extension(path: str | Path) -> Path:
    catalogue_path = Path(path).expanduser()
    if catalogue_path.suffix.lower() == CATALOGUE_EXTENSION:
        return catalogue_path
    return Path(f"{catalogue_path}{CATALOGUE_EXTENSION}")


def resolve_catalogue_root(path: str | Path) -> Path:
    """Return the catalogue directory for *path*.

    Accepts the directory itself or one of the files inside it (so a user who
    picks ``catalogue.sqlite`` or ``index.sqlite`` in a file dialog still opens
    the right catalogue).
    """

    candidate = Path(path).expanduser()
    if candidate.is_file() and candidate.name in {CATALOGUE_DB_NAME, INDEX_DB_NAME}:
        return candidate.parent
    if candidate.is_file() and candidate.parent.name == VOLUMES_DIR_NAME:
        parent = candidate.parent.parent
        if (parent / CATALOGUE_DB_NAME).is_file():
            return parent
    return candidate


def volume_uuid_from_file(path: Path) -> str:
    name = path.name
    if name.endswith(f"{VOLUME_FILE_EXTENSION}{VOLUME_TOMBSTONE_SUFFIX}"):
        name = name[: -len(VOLUME_TOMBSTONE_SUFFIX)]
    if name.endswith(VOLUME_FILE_EXTENSION):
        name = name[: -len(VOLUME_FILE_EXTENSION)]
    return name


def new_uuid() -> str:
    return str(uuid_module.uuid4())


def is_valid_uuid(value: str) -> bool:
    try:
        return str(uuid_module.UUID(str(value))) == str(value)
    except (ValueError, AttributeError, TypeError):
        return False


def read_sqlite_header(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(100)
    except OSError:
        return None
    if len(header) < 100 or header[:16] != SQLITE_HEADER:
        return None
    return header


def header_application_id(header: bytes) -> int:
    return int.from_bytes(header[68:72], "big")


def header_user_version(header: bytes) -> int:
    return int.from_bytes(header[60:64], "big")


def sqlite_uri(path: Path, *, mode: str = "rw") -> str:
    resolved = path.resolve(strict=False)
    uri = resolved.as_uri()
    if resolved.drive.startswith("\\\\"):
        uri = f"file:////{uri[len('file://'):]}"
    return f"{uri}?mode={mode}"


# --------------------------------------------------------------------------
# catalogue.sqlite (registry)
# --------------------------------------------------------------------------

REGISTRY_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE catalogue_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE volume_registry (
        volume_id INTEGER PRIMARY KEY,
        uuid TEXT NOT NULL UNIQUE,
        added_at TEXT NOT NULL
    )
    """,
)
REGISTRY_REQUIRED_TABLES = frozenset({"catalogue_meta", "volume_registry"})


@dataclass(frozen=True)
class RegistryEntry:
    volume_id: int
    uuid: str
    added_at: str


def create_registry_db(path: Path, *, catalogue_uuid: str, created_at: str) -> None:
    if path.exists():
        raise FileExistsError(f"Registry database already exists: {path}")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(f"PRAGMA application_id = {CATALOGUE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {CATALOGUE_FORMAT_VERSION}")
        for statement in REGISTRY_SCHEMA_SQL:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO catalogue_meta (key, value) VALUES (?, ?)",
            [
                ("format", CATALOGUE_FORMAT),
                ("format_version", str(CATALOGUE_FORMAT_VERSION)),
                ("catalogue_uuid", catalogue_uuid),
                ("created_at", created_at),
                ("next_volume_id", "1"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def open_registry_db(
    path: Path,
    *,
    read_only: bool = False,
    check_same_thread: bool = True,
    timeout: float = 2.0,
) -> sqlite3.Connection:
    if not path.is_file():
        raise CatalogueLayoutError(f"The catalogue registry does not exist: {path}")
    connection = sqlite3.connect(
        sqlite_uri(path, mode="ro" if read_only else "rw"),
        uri=True,
        timeout=timeout,
        check_same_thread=check_same_thread,
    )
    connection.row_factory = sqlite3.Row
    if read_only:
        connection.execute("PRAGMA query_only = ON")
    return connection


def validate_registry(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = REGISTRY_REQUIRED_TABLES - tables
    if missing:
        raise CatalogueLayoutError(
            "The catalogue registry is missing required tables: " + ", ".join(sorted(missing))
        )
    meta = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM catalogue_meta")}
    if meta.get("format") != CATALOGUE_FORMAT:
        raise CatalogueLayoutError("The catalogue registry has an unexpected format marker.")
    try:
        version = int(meta.get("format_version", "0"))
    except ValueError:
        version = 0
    if version != CATALOGUE_FORMAT_VERSION:
        raise CatalogueLayoutError(
            f"The catalogue registry uses format version {version}; this build supports "
            f"version {CATALOGUE_FORMAT_VERSION}."
        )
    if not is_valid_uuid(meta.get("catalogue_uuid", "")):
        raise CatalogueLayoutError("The catalogue registry has no valid catalogue UUID.")
    return meta


def read_registry(connection: sqlite3.Connection) -> list[RegistryEntry]:
    return [
        RegistryEntry(int(row["volume_id"]), str(row["uuid"]), str(row["added_at"]))
        for row in connection.execute(
            "SELECT volume_id, uuid, added_at FROM volume_registry ORDER BY volume_id"
        )
    ]


def registry_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in connection.execute("SELECT key, value FROM catalogue_meta")}


def set_registry_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO catalogue_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def registry_next_volume_id(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM catalogue_meta WHERE key = 'next_volume_id'"
    ).fetchone()
    highest = connection.execute(
        "SELECT COALESCE(MAX(volume_id), 0) FROM volume_registry"
    ).fetchone()[0]
    try:
        stored = int(row[0]) if row is not None else 1
    except ValueError:
        stored = 1
    return max(stored, int(highest) + 1)


def register_volume(
    connection: sqlite3.Connection,
    volume_id: int,
    volume_uuid: str,
    added_at: str,
) -> None:
    connection.execute(
        "INSERT INTO volume_registry (volume_id, uuid, added_at) VALUES (?, ?, ?)",
        (int(volume_id), volume_uuid, added_at),
    )
    set_registry_meta(connection, "next_volume_id", str(int(volume_id) + 1))


def unregister_volume(connection: sqlite3.Connection, volume_id: int) -> None:
    connection.execute("DELETE FROM volume_registry WHERE volume_id = ?", (int(volume_id),))


# --------------------------------------------------------------------------
# volumes/<uuid>.jvvvol
# --------------------------------------------------------------------------

# The single-row ``volume`` table: everything the catalogue knows about the
# drive itself (registry metadata and the latest storage figures).  The mirror
# link is stored by UUID so the file stays meaningful outside its catalogue.
VOLUME_ROW_COLUMNS: tuple[str, ...] = (
    "name",
    "source_path",
    "capacity_bytes",
    "used_bytes",
    "free_bytes",
    "indexed_file_count",
    "indexed_folder_count",
    "last_scan_at",
    "created_at",
    "updated_at",
    "identity_kind",
    "identity_token",
    "identity_label",
    "identity_serial",
    "identity_filesystem",
    "source_relative_path",
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
    "master_volume_uuid",
    "register_created_at",
    "register_updated_at",
)

FOLDER_COLUMNS: tuple[str, ...] = (
    "id",
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
)
FILE_COLUMNS: tuple[str, ...] = (
    "id",
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
MEDIA_COLUMNS: tuple[str, ...] = (
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
)
PREVIEW_COLUMNS: tuple[str, ...] = (
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
)
SCAN_HISTORY_COLUMNS: tuple[str, ...] = (
    "id",
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
)
SCAN_ERROR_COLUMNS: tuple[str, ...] = (
    "id",
    "scan_id",
    "path",
    "message",
    "created_at",
)

# Content tables travel in the content blob of a snapshot; history tables in the
# history blob.  Both are keyed by local ids that are dense and assigned in a
# deterministic order (see volume_store), so identical content always
# serialises to identical bytes.
CONTENT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("folders", FOLDER_COLUMNS),
    ("files", FILE_COLUMNS),
    ("file_media_metadata", MEDIA_COLUMNS),
    ("file_preview_status", PREVIEW_COLUMNS),
)
HISTORY_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("scan_history", SCAN_HISTORY_COLUMNS),
    ("scan_errors", SCAN_ERROR_COLUMNS),
)
VOLUME_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "volume": VOLUME_ROW_COLUMNS,
    **dict(CONTENT_TABLES),
    **dict(HISTORY_TABLES),
}

VOLUME_FILE_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE volume_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE volume (
        name TEXT,
        source_path TEXT NOT NULL DEFAULT '',
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
        source_relative_path TEXT NOT NULL DEFAULT '',
        drive_id TEXT,
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
        master_volume_uuid TEXT,
        register_created_at TEXT NOT NULL,
        register_updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE folders (
        id INTEGER PRIMARY KEY,
        parent_id INTEGER REFERENCES folders(id),
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL UNIQUE,
        missing INTEGER NOT NULL DEFAULT 0,
        scanned_at TEXT,
        modified_at TEXT,
        recursive_size_bytes INTEGER,
        recursive_file_count INTEGER,
        recursive_subfolder_count INTEGER,
        direct_file_count INTEGER,
        direct_subfolder_count INTEGER,
        stats_updated_at TEXT
    )
    """,
    """
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        folder_id INTEGER REFERENCES folders(id),
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL UNIQUE,
        extension TEXT NOT NULL DEFAULT '',
        size_bytes INTEGER NOT NULL DEFAULT 0,
        modified_at TEXT,
        missing INTEGER NOT NULL DEFAULT 0,
        scanned_at TEXT,
        identity_device INTEGER,
        identity_inode INTEGER,
        content_hash BLOB,
        content_hash_algorithm TEXT
    )
    """,
    """
    CREATE TABLE file_media_metadata (
        file_id INTEGER PRIMARY KEY REFERENCES files(id),
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
    CREATE TABLE file_preview_status (
        file_id INTEGER PRIMARY KEY REFERENCES files(id),
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
    CREATE TABLE scan_history (
        id INTEGER PRIMARY KEY,
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
    CREATE TABLE scan_errors (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER REFERENCES scan_history(id),
        path TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)
VOLUME_FILE_REQUIRED_TABLES = frozenset(
    {"volume_meta", "volume", *(name for name, _ in CONTENT_TABLES), *(name for name, _ in HISTORY_TABLES)}
)


@dataclass(frozen=True)
class VolumeFileMeta:
    uuid: str
    revision: int
    content_digest: str
    history_digest: str
    exported_at: str
    format_version: int

    @property
    def is_valid(self) -> bool:
        return is_valid_uuid(self.uuid) and self.revision >= 0


def open_volume_file(
    path: Path,
    *,
    read_only: bool = False,
    check_same_thread: bool = True,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    # Volume files are always written inside an explicit BEGIN/COMMIT, so the
    # connection must not open implicit transactions of its own.
    connection = sqlite3.connect(
        sqlite_uri(path, mode="ro" if read_only else "rw"),
        uri=True,
        timeout=timeout,
        check_same_thread=check_same_thread,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    if read_only:
        connection.execute("PRAGMA query_only = ON")
    return connection


def initialize_volume_file(connection: sqlite3.Connection, volume_uuid: str) -> None:
    """Create the schema of a brand-new (empty) volume file on *connection*."""

    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(f"PRAGMA application_id = {VOLUME_APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {VOLUME_FILE_FORMAT_VERSION}")
    for statement in VOLUME_FILE_SCHEMA_SQL:
        connection.execute(statement)
    connection.executemany(
        "INSERT INTO volume_meta (key, value) VALUES (?, ?)",
        [
            ("format", VOLUME_FILE_FORMAT),
            ("format_version", str(VOLUME_FILE_FORMAT_VERSION)),
            ("uuid", volume_uuid),
            ("revision", "0"),
            ("content_digest", ""),
            ("history_digest", ""),
            ("exported_at", ""),
        ],
    )


def read_volume_meta(connection: sqlite3.Connection, *, schema: str = "main") -> VolumeFileMeta:
    tables = {
        row[0]
        for row in connection.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
        )
    }
    missing = VOLUME_FILE_REQUIRED_TABLES - tables
    if missing:
        raise CatalogueLayoutError(
            "The volume file is missing required tables: " + ", ".join(sorted(missing))
        )
    meta = {
        row[0]: row[1]
        for row in connection.execute(f"SELECT key, value FROM {schema}.volume_meta")
    }
    if meta.get("format") != VOLUME_FILE_FORMAT:
        raise CatalogueLayoutError("The volume file has an unexpected format marker.")
    try:
        version = int(meta.get("format_version", "0"))
        revision = int(meta.get("revision", "0"))
    except ValueError as exc:
        raise CatalogueLayoutError("The volume file has a corrupt metadata table.") from exc
    if version != VOLUME_FILE_FORMAT_VERSION:
        raise CatalogueLayoutError(
            f"The volume file uses format version {version}; this build supports "
            f"version {VOLUME_FILE_FORMAT_VERSION}."
        )
    return VolumeFileMeta(
        uuid=str(meta.get("uuid", "")),
        revision=revision,
        content_digest=str(meta.get("content_digest", "")),
        history_digest=str(meta.get("history_digest", "")),
        exported_at=str(meta.get("exported_at", "")),
        format_version=version,
    )


def read_volume_meta_from_path(path: Path) -> VolumeFileMeta:
    header = read_sqlite_header(path)
    if header is None:
        raise CatalogueLayoutError(f"The volume file is not a SQLite database: {path}")
    connection = open_volume_file(path, read_only=True)
    try:
        return read_volume_meta(connection)
    finally:
        connection.close()


def set_volume_meta(connection: sqlite3.Connection, **values: str | int) -> None:
    connection.executemany(
        "INSERT INTO volume_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(key, str(value)) for key, value in values.items()],
    )


def make_temp_volume_path(final_path: Path) -> Path:
    token = os.urandom(6).hex()
    return final_path.with_name(f"{final_path.name}{VOLUME_TEMP_MARKER}{token}")
