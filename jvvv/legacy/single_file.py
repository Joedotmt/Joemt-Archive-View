"""LEGACY: migrate a single-file schema-12 catalogue to the directory format.

The old ``.jvvv`` file *is* today's index schema minus the bookkeeping that the
directory format adds, so the migration is:

1. checkpoint the source (fold its WAL) and validate it;
2. copy it page-for-page into ``<name>.jvvv.migrating/index.sqlite`` with the
   SQLite backup API - every row, id, FTS structure and Backup Evidence result
   survives, nothing is re-derived;
3. add ``volumes.uuid``, ``volume_register.master_volume_uuid`` and the
   ``index_state``/``index_meta`` tables, and stamp schema version 13;
4. write ``catalogue.sqlite`` with the same integer volume ids;
5. export every volume to ``volumes/<uuid>.jvvvol`` and record its revision;
6. verify each volume file: row counts against the index, and the canonical
   digests re-read from the file against the ones the exporter stored;
7. rename the original to ``<name>.jvvv.legacy`` and the folder to ``<name>.jvvv``.

The original file is never modified beyond the WAL checkpoint and is kept
until the user deletes it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..catalogue_format import (
    INDEX_DB_NAME,
    CATALOGUE_DB_NAME,
    CatalogueLayout,
    create_registry_db,
    new_uuid,
    open_registry_db,
    read_sqlite_header,
    register_volume,
    set_registry_meta,
)
from ..database import (
    INDEX_STATE_TABLE_SQL,
    INDEX_META_TABLE_SQL,
    INDEX_APPLICATION_ID,
    SCHEMA_VERSION,
    CatalogueError,
    Database,
    InvalidCatalogueError,
    UnsupportedCatalogueError,
    sqlite_file_uri,
    utc_now,
)


LEGACY_SCHEMA_VERSION = 12
LEGACY_ARCHIVE_SUFFIX = ".legacy"
LEGACY_REQUIRED_TABLES = frozenset(
    {
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
        "backup_analysis_runs",
        "backup_analysis_state",
    }
)
BACKUP_PAGES_PER_STEP = 4096

ProgressCallback = Callable[["MigrationProgress"], None]
CancelCallback = Callable[[], bool]


class MigrationCancelled(CatalogueError):
    pass


@dataclass(frozen=True)
class MigrationProgress:
    phase: str
    completed: int
    total: int
    message: str


@dataclass(frozen=True)
class LegacyCatalogueInfo:
    path: Path
    schema_version: int
    size_bytes: int
    volume_count: int
    file_count: int
    folder_count: int
    wal_mode: bool


@dataclass(frozen=True)
class MigrationResult:
    catalogue_path: Path
    legacy_path: Path
    volume_count: int
    file_count: int
    folder_count: int
    index_bytes: int
    volume_files_bytes: int
    seconds: float


def is_legacy_catalogue_file(path: str | Path) -> bool:
    candidate = Path(path).expanduser()
    if not candidate.is_file() or candidate.name in {CATALOGUE_DB_NAME, INDEX_DB_NAME}:
        return False
    return read_sqlite_header(candidate) is not None


def legacy_archive_path(source: Path) -> Path:
    return source.with_name(f"{source.name}{LEGACY_ARCHIVE_SUFFIX}")


def _emit(callback: ProgressCallback | None, phase: str, completed: int, total: int, message: str) -> None:
    if callback is not None:
        callback(MigrationProgress(phase, int(completed), int(total), message))


def _check_cancel(callback: CancelCallback | None) -> None:
    if callback is not None and callback():
        raise MigrationCancelled("The catalogue conversion was cancelled.")


def _validate_legacy(connection: sqlite3.Connection, path: Path) -> int:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != LEGACY_SCHEMA_VERSION:
        if version == SCHEMA_VERSION:
            raise InvalidCatalogueError(
                f"{path.name} already uses the current index schema but is not inside a "
                "catalogue folder. Put it in a folder as index.sqlite next to its "
                "catalogue.sqlite, or restore the catalogue from a snapshot."
            )
        raise UnsupportedCatalogueError(
            f"{path.name} uses schema version {version}. Only single-file catalogues with "
            f"schema version {LEGACY_SCHEMA_VERSION} (the last single-file version) can be "
            "converted; open older files once with the previous JVVV release first."
        )
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = LEGACY_REQUIRED_TABLES - tables
    if missing:
        raise InvalidCatalogueError(
            f"{path.name} is missing catalogue tables: " + ", ".join(sorted(missing))
        )
    return version


def inspect_legacy_catalogue(path: str | Path) -> LegacyCatalogueInfo:
    source = Path(path).expanduser()
    header = read_sqlite_header(source)
    if header is None:
        raise InvalidCatalogueError(f"{source} is not a SQLite database.")
    connection = sqlite3.connect(sqlite_file_uri(source, mode="ro"), uri=True)
    try:
        version = _validate_legacy(connection, source)
        volumes = int(connection.execute("SELECT COUNT(*) FROM volumes").fetchone()[0])
        files = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        folders = int(connection.execute("SELECT COUNT(*) FROM folders").fetchone()[0])
    finally:
        connection.close()
    return LegacyCatalogueInfo(
        path=source,
        schema_version=version,
        size_bytes=source.stat().st_size,
        volume_count=volumes,
        file_count=files,
        folder_count=folders,
        wal_mode=header[18] == 2 or header[19] == 2,
    )


def _checkpoint_source(source: Path) -> None:
    """Fold a WAL into the main file so the renamed original stays complete."""

    connection = sqlite3.connect(sqlite_file_uri(source, mode="rw"), uri=True, timeout=5)
    try:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        if mode == "wal":
            busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if int(busy):
                raise CatalogueError(
                    "The catalogue is still in use by another process; close every JVVV "
                    "window that has it open and try again."
                )
    finally:
        connection.close()
    wal = Path(f"{source}-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise CatalogueError(
            "The catalogue's write-ahead log could not be folded into the file. Close every "
            "program using it and try again."
        )


def _copy_with_backup_api(
    source: Path,
    target: Path,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> None:
    src = sqlite3.connect(sqlite_file_uri(source, mode="ro"), uri=True)
    dst = sqlite3.connect(target)
    try:
        def report(status: int, remaining: int, total: int) -> None:
            _emit(
                progress_callback,
                "copy",
                total - remaining,
                total,
                f"Copying catalogue pages ({total - remaining:,}/{total:,})",
            )
            _check_cancel(cancel_callback)

        src.backup(dst, pages=BACKUP_PAGES_PER_STEP, progress=report)
    finally:
        dst.close()
        src.close()


def _upgrade_index_schema(index_path: Path) -> list[tuple[int, str]]:
    """Add the directory-format bookkeeping to the copied index; return (id, uuid)."""

    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(volumes)")}
        if "uuid" not in columns:
            connection.execute("ALTER TABLE volumes ADD COLUMN uuid TEXT NOT NULL DEFAULT ''")
        register_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(volume_register)")
        }
        if "master_volume_uuid" not in register_columns:
            connection.execute("ALTER TABLE volume_register ADD COLUMN master_volume_uuid TEXT")
        assigned: list[tuple[int, str]] = []
        for row in connection.execute("SELECT id, uuid FROM volumes ORDER BY id"):
            volume_uuid = str(row["uuid"] or "") or new_uuid()
            connection.execute("UPDATE volumes SET uuid = ? WHERE id = ?", (volume_uuid, row["id"]))
            assigned.append((int(row["id"]), volume_uuid))
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_volumes_uuid ON volumes(uuid)"
        )
        connection.execute(
            """
            UPDATE volume_register
            SET master_volume_uuid = (
                SELECT uuid FROM volumes WHERE volumes.id = volume_register.master_volume_id
            )
            WHERE master_volume_id IS NOT NULL
            """
        )
        connection.execute(INDEX_STATE_TABLE_SQL)
        connection.execute(INDEX_META_TABLE_SQL)
        connection.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('built_at', ?)",
            (utc_now(),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('migrated_from_single_file', ?)",
            (utc_now(),),
        )
        connection.execute(f"PRAGMA application_id = {INDEX_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
        return assigned
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def migrate_legacy_catalogue(
    source_path: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> MigrationResult:
    """Convert ``Name.jvvv`` (file) into ``Name.jvvv/`` (folder).

    On success the folder takes the original path and the original file is
    renamed to ``Name.jvvv.legacy``.  Any failure before the final rename
    leaves the original untouched and removes the partial folder.
    """

    started = time.monotonic()
    source = Path(source_path).expanduser()
    if not is_legacy_catalogue_file(source):
        raise InvalidCatalogueError(f"{source} is not a single-file JVVV catalogue.")
    archive = legacy_archive_path(source)
    if archive.exists():
        raise FileExistsError(
            f"{archive.name} already exists; move it away before converting {source.name}."
        )
    _emit(progress_callback, "validate", 0, 0, "Checking the catalogue file")
    _checkpoint_source(source)
    info = inspect_legacy_catalogue(source)
    _check_cancel(cancel_callback)

    staging = source.with_name(f"{source.name}.migrating-{os.urandom(4).hex()}")
    layout = CatalogueLayout(staging)
    try:
        staging.mkdir()
        layout.volumes_dir.mkdir()
        _copy_with_backup_api(source, layout.index_db, progress_callback, cancel_callback)
        _check_cancel(cancel_callback)
        _emit(progress_callback, "upgrade", 0, 0, "Adding catalogue-folder bookkeeping to the index")
        assigned = _upgrade_index_schema(layout.index_db)

        _emit(progress_callback, "registry", 0, 0, f"Writing {CATALOGUE_DB_NAME}")
        create_registry_db(layout.catalogue_db, catalogue_uuid=new_uuid(), created_at=utc_now())
        registry = open_registry_db(layout.catalogue_db)
        try:
            with registry:
                for volume_id, volume_uuid in assigned:
                    register_volume(registry, volume_id, volume_uuid, utc_now())
                highest = max((volume_id for volume_id, _ in assigned), default=0)
                set_registry_meta(registry, "next_volume_id", str(highest + 1))
        finally:
            registry.close()

        db = Database(staging, initialize=False, create=False)
        try:
            db.validate_catalogue()
            total = len(assigned)
            for position, (volume_id, volume_uuid) in enumerate(assigned, start=1):
                _check_cancel(cancel_callback)
                _emit(
                    progress_callback,
                    "export",
                    position - 1,
                    total,
                    f"Writing volume files ({position}/{total})",
                )
                db.export_volume(volume_id)
            _emit(progress_callback, "verify", 0, total, "Verifying volume files")
            for position, (volume_id, volume_uuid) in enumerate(assigned, start=1):
                _check_cancel(cancel_callback)
                _emit(progress_callback, "verify", position - 1, total, f"Verifying volume files ({position}/{total})")
                db.verify_volume_file(volume_id)
            report = db.reconcile()
            if report.imported or report.removed:
                raise CatalogueError(
                    "The converted catalogue did not verify: the index and the volume files "
                    "disagree after export."
                )
            volume_files_bytes = sum(path.stat().st_size for path in layout.iter_volume_files())
        finally:
            db.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{layout.index_db}{suffix}").unlink(missing_ok=True)
        index_bytes = layout.index_db.stat().st_size

        _emit(progress_callback, "finalize", 0, 0, "Renaming the original file and moving the folder into place")
        _check_cancel(cancel_callback)
        wal = Path(f"{source}-wal")
        if wal.is_file() and wal.stat().st_size > 0:
            raise CatalogueError("The original catalogue changed during the conversion; try again.")
        os.replace(source, archive)
        for suffix in ("-wal", "-shm"):
            Path(f"{source}{suffix}").unlink(missing_ok=True)
        try:
            os.replace(staging, source)
        except OSError:
            os.replace(archive, source)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return MigrationResult(
        catalogue_path=source,
        legacy_path=archive,
        volume_count=info.volume_count,
        file_count=info.file_count,
        folder_count=info.folder_count,
        index_bytes=index_bytes,
        volume_files_bytes=volume_files_bytes,
        seconds=time.monotonic() - started,
    )
