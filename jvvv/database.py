from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
SCHEMA_VERSION = 2
CATALOGUE_EXTENSION = ".jvvv"
SQLITE_INTEGER_MIN = -(2**63)
SQLITE_INTEGER_MAX = 2**63 - 1
UINT64_MODULUS = 2**64
UINT64_MAX = UINT64_MODULUS - 1
REQUIRED_TABLES = {"volumes", "folders", "files", "scan_history", "scan_errors"}
REQUIRED_COLUMNS = {
    "volumes": {
        "id",
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


class CatalogueError(Exception):
    pass


class CatalogueInUseError(CatalogueError):
    pass


class InvalidCatalogueError(CatalogueError):
    pass


class UnsupportedCatalogueError(CatalogueError):
    pass


FolderStatsProgress = Callable[[int, int, str], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def format_timestamp(value: float | int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).strftime(ISO_FORMAT)


def parse_db_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, ISO_FORMAT)
    except ValueError:
        return None


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


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        initialize: bool = True,
        create: bool = True,
    ) -> None:
        self.path = Path(path).expanduser()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connect_target = str(self.path)
            use_uri = False
        else:
            if not self.path.is_file():
                raise InvalidCatalogueError(f"Catalogue file does not exist: {self.path}")
            connect_target = self._sqlite_uri(self.path)
            use_uri = True

        try:
            self.connection = sqlite3.connect(connect_target, timeout=2.0, uri=use_uri)
            self.connection.row_factory = sqlite3.Row
            self._configure_connection()
            if initialize:
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
    def _sqlite_uri(path: Path) -> str:
        return f"{path.resolve(strict=False).as_uri()}?mode=rw"

    def _configure_connection(self) -> None:
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 2000")
        self.connection.execute("PRAGMA journal_mode = DELETE")
        self.connection.execute("PRAGMA synchronous = NORMAL")

    def _catalogue_error(self, exc: sqlite3.Error) -> CatalogueError:
        message = str(exc)
        lower = message.lower()
        if "database is locked" in lower or "database table is locked" in lower:
            return CatalogueInUseError(
                "The catalogue is locked or already in use by another process."
            )
        if (
            "file is not a database" in lower
            or "malformed" in lower
            or "database disk image is malformed" in lower
        ):
            return InvalidCatalogueError("The selected file is not a valid catalogue database.")
        return CatalogueError(message)

    def initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise UnsupportedCatalogueError(
                f"This catalogue uses schema version {version}, but this version of JVVV "
                f"supports up to version {SCHEMA_VERSION}."
            )
        if version < SCHEMA_VERSION:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                if version < 1:
                    self._apply_migration_1()
                    version = 1
                if version < 2:
                    self._apply_migration_2()
                    version = 2
                self.connection.execute(f"PRAGMA user_version = {version}")
                self.connection.commit()
            except sqlite3.Error:
                self.connection.rollback()
                raise
        self.validate_schema()

    def validate_catalogue(self) -> None:
        try:
            check = self.connection.execute("PRAGMA quick_check(1)").fetchone()
            if check is None or check[0] != "ok":
                raise InvalidCatalogueError("The selected catalogue database appears to be corrupted.")

            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            existing_tables = self._table_names()
            if version == 0 and not REQUIRED_TABLES <= existing_tables:
                raise InvalidCatalogueError(
                    "The selected file is a SQLite database, but it is not a JVVV catalogue."
                )
            if version > SCHEMA_VERSION:
                raise UnsupportedCatalogueError(
                    f"This catalogue uses schema version {version}, but this version of JVVV "
                    f"supports up to version {SCHEMA_VERSION}."
                )
            if version < SCHEMA_VERSION:
                self.initialize()
            else:
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

    def _apply_migration_1(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS volumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                source_path TEXT NOT NULL,
                capacity_bytes INTEGER NOT NULL DEFAULT 0,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                free_bytes INTEGER NOT NULL DEFAULT 0,
                indexed_file_count INTEGER NOT NULL DEFAULT 0,
                indexed_folder_count INTEGER NOT NULL DEFAULT 0,
                last_scan_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                missing INTEGER NOT NULL DEFAULT 0,
                scanned_at TEXT,
                modified_at TEXT,
                UNIQUE(volume_id, relative_path)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
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
                UNIQUE(volume_id, relative_path)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                files_seen INTEGER NOT NULL DEFAULT 0,
                folders_seen INTEGER NOT NULL DEFAULT 0,
                errors_count INTEGER NOT NULL DEFAULT 0,
                message TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER REFERENCES scan_history(id) ON DELETE CASCADE,
                volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_folders_volume_parent ON folders(volume_id, parent_id)",
            "CREATE INDEX IF NOT EXISTS idx_folders_name ON folders(name COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_folders_path ON folders(relative_path COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_files_volume_folder ON files(volume_id, folder_id)",
            "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_files_path ON files(relative_path COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_scan_errors_scan ON scan_errors(scan_id)",
        ]
        for statement in statements:
            self.connection.execute(statement)

    def _apply_migration_2(self) -> None:
        folder_columns = {
            "recursive_size_bytes": "INTEGER",
            "recursive_file_count": "INTEGER",
            "recursive_subfolder_count": "INTEGER",
            "direct_file_count": "INTEGER",
            "direct_subfolder_count": "INTEGER",
            "stats_updated_at": "TEXT",
        }
        file_columns = {
            "identity_device": "INTEGER",
            "identity_inode": "INTEGER",
        }
        for column, definition in folder_columns.items():
            self._add_column_if_missing("folders", column, definition)
        for column, definition in file_columns.items():
            self._add_column_if_missing("files", column, definition)

        statements = [
            "CREATE INDEX IF NOT EXISTS idx_folders_volume_stats_size ON folders(volume_id, recursive_size_bytes)",
            "CREATE INDEX IF NOT EXISTS idx_files_identity ON files(volume_id, identity_device, identity_inode)",
        ]
        for statement in statements:
            self.connection.execute(statement)

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        if column not in self._column_names(table):
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            yield self.connection
            return
        try:
            self.connection.execute("BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def create_volume(self, name: str, source_path: str) -> int:
        now = utc_now()
        source = str(Path(source_path).expanduser())
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO volumes (name, source_path, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), source, now, now),
            )
            return int(cur.lastrowid)

    def update_volume(self, volume_id: int, name: str, source_path: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE volumes
                SET name = ?, source_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (name.strip(), str(Path(source_path).expanduser()), utc_now(), volume_id),
            )

    def delete_volume(self, volume_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM volumes WHERE id = ?", (volume_id,))

    def get_volume(self, volume_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM volumes WHERE id = ?", (volume_id,)
        ).fetchone()

    def list_volumes(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM volumes ORDER BY name COLLATE NOCASE"
            )
        )

    def volume_is_connected(self, volume: sqlite3.Row | int) -> bool:
        row = self.get_volume(volume) if isinstance(volume, int) else volume
        return bool(row and Path(row["source_path"]).exists())

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
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE scan_history
                SET finished_at = ?,
                    status = ?,
                    files_seen = ?,
                    folders_seen = ?,
                    errors_count = ?,
                    message = ?
                WHERE id = ?
                """,
                (utc_now(), status, files_seen, folders_seen, errors_count, message, scan_id),
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
    ) -> int:
        identity_device = normalize_identity_integer(identity_device)
        identity_inode = normalize_identity_integer(identity_inode)
        cur = self.connection.execute(
            """
            INSERT INTO files (
                volume_id, folder_id, name, relative_path, extension,
                size_bytes, modified_at, missing, scanned_at, identity_device, identity_inode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(volume_id, relative_path) DO UPDATE SET
                folder_id = excluded.folder_id,
                name = excluded.name,
                extension = excluded.extension,
                size_bytes = excluded.size_bytes,
                modified_at = excluded.modified_at,
                missing = 0,
                scanned_at = excluded.scanned_at,
                identity_device = excluded.identity_device,
                identity_inode = excluded.identity_inode
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
            ),
        )
        return int(cur.fetchone()["id"])

    def finalize_scan_items(
        self,
        volume_id: int,
        scanned_at: str,
        remove_deleted: bool,
    ) -> None:
        if remove_deleted:
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
        else:
            self.connection.execute(
                """
                UPDATE files SET missing = 1
                WHERE volume_id = ?
                  AND (scanned_at IS NULL OR scanned_at != ?)
                """,
                (volume_id, scanned_at),
            )
            self.connection.execute(
                """
                UPDATE folders SET missing = 1
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

            stats: dict[int, dict[str, int]] = {}
            depth_by_id: dict[int, int] = {}
            parent_by_id: dict[int, int | None] = {}
            children_by_parent: dict[int, list[int]] = {}
            for row in folder_rows:
                folder_id = int(row["id"])
                relative_path = row["relative_path"] or ""
                parent_id = row["parent_id"]
                stats[folder_id] = {
                    "direct_size": 0,
                    "direct_file_count": 0,
                    "direct_subfolder_count": 0,
                    "recursive_size": 0,
                    "recursive_file_count": 0,
                    "recursive_subfolder_count": 0,
                }
                depth_by_id[folder_id] = 0 if not relative_path else relative_path.count("/") + 1
                parent_by_id[folder_id] = int(parent_id) if parent_id is not None else None
                if parent_id is not None:
                    children_by_parent.setdefault(int(parent_id), []).append(folder_id)

            for folder_id, children in children_by_parent.items():
                if folder_id in stats:
                    stats[folder_id]["direct_subfolder_count"] = len(children)

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
            for row in direct_file_rows:
                folder_id = int(row["folder_id"])
                if folder_id in stats:
                    stats[folder_id]["direct_size"] = int(row["direct_size"] or 0)
                    stats[folder_id]["direct_file_count"] = int(row["direct_file_count"] or 0)

            processed = 0
            for folder_id in sorted(depth_by_id, key=lambda key: depth_by_id[key], reverse=True):
                folder_stats = stats[folder_id]
                recursive_size = folder_stats["direct_size"]
                recursive_file_count = folder_stats["direct_file_count"]
                recursive_subfolder_count = folder_stats["direct_subfolder_count"]
                for child_id in children_by_parent.get(folder_id, []):
                    if child_id not in stats:
                        continue
                    child_stats = stats[child_id]
                    recursive_size += child_stats["recursive_size"]
                    recursive_file_count += child_stats["recursive_file_count"]
                    recursive_subfolder_count += child_stats["recursive_subfolder_count"]
                folder_stats["recursive_size"] = recursive_size
                folder_stats["recursive_file_count"] = recursive_file_count
                folder_stats["recursive_subfolder_count"] = recursive_subfolder_count

                processed += 1
                if progress_callback and (processed == total or processed % 1000 == 0):
                    progress_callback(processed, total, "Calculating folder statistics")

            self._dedupe_linked_file_sizes(conn, volume_id, stats, parent_by_id)

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

    def _dedupe_linked_file_sizes(
        self,
        conn: sqlite3.Connection,
        volume_id: int,
        stats: dict[int, dict[str, int]],
        parent_by_id: dict[int, int | None],
    ) -> None:
        rows = conn.execute(
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

        current_identity: tuple[int, int] | None = None
        current_size = 0
        ancestor_counts: dict[int, int] = {}

        def apply_current_group() -> None:
            if current_identity is None:
                return
            for folder_id, count in ancestor_counts.items():
                if count > 1:
                    stats[folder_id]["recursive_size"] -= (count - 1) * current_size

        for row in rows:
            identity = (int(row["identity_device"]), int(row["identity_inode"]))
            if identity != current_identity:
                apply_current_group()
                current_identity = identity
                current_size = int(row["size_bytes"] or 0)
                ancestor_counts = {}

            folder_id = int(row["folder_id"])
            current = folder_id
            while current is not None and current in stats:
                ancestor_counts[current] = ancestor_counts.get(current, 0) + 1
                current = parent_by_id.get(current)

        apply_current_group()

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

    def search(self, query: str, limit: int = 500) -> list[sqlite3.Row]:
        text = query.strip()
        if not text:
            return []
        if text.startswith("."):
            extension = text[1:].lower()
            file_clause = "f.extension = ?"
            file_params: tuple[object, ...] = (extension,)
        else:
            needle = f"%{text}%"
            file_clause = """
                f.name LIKE ? COLLATE NOCASE
                OR f.relative_path LIKE ? COLLATE NOCASE
                OR f.extension LIKE ? COLLATE NOCASE
            """
            file_params = (needle, needle, needle)

        folder_needle = f"%{text}%"
        sql = f"""
            SELECT *
            FROM (
                SELECT
                    'file' AS item_type,
                    f.id AS item_id,
                    f.name,
                    v.id AS volume_id,
                    v.name AS volume_name,
                    f.relative_path,
                    f.size_bytes,
                    f.modified_at,
                    f.missing,
                    v.source_path,
                    CASE WHEN f.missing = 0 THEN 0 ELSE 1 END AS missing_rank
                FROM files f
                JOIN volumes v ON v.id = f.volume_id
                WHERE {file_clause}
                UNION ALL
                SELECT
                    'folder' AS item_type,
                    fo.id AS item_id,
                    fo.name,
                    v.id AS volume_id,
                    v.name AS volume_name,
                    fo.relative_path,
                    fo.recursive_size_bytes AS size_bytes,
                    fo.modified_at,
                    fo.missing,
                    v.source_path,
                    CASE WHEN fo.missing = 0 THEN 0 ELSE 1 END AS missing_rank
                FROM folders fo
                JOIN volumes v ON v.id = fo.volume_id
                WHERE fo.relative_path != ''
                  AND (
                      fo.name LIKE ? COLLATE NOCASE
                      OR fo.relative_path LIKE ? COLLATE NOCASE
                  )
            )
            ORDER BY missing_rank, name COLLATE NOCASE
            LIMIT ?
        """
        return list(
            self.connection.execute(
                sql,
                (*file_params, folder_needle, folder_needle, limit),
            )
        )

    def prune_scan_history(self, keep_per_volume: int = 100) -> None:
        volume_ids = [row["id"] for row in self.list_volumes()]
        with self.transaction() as conn:
            for volume_id in volume_ids:
                stale = list(
                    conn.execute(
                        """
                        SELECT id FROM scan_history
                        WHERE volume_id = ?
                        ORDER BY id DESC
                        LIMIT -1 OFFSET ?
                        """,
                        (volume_id, keep_per_volume),
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


def open_catalogue(path: str | Path) -> Database:
    db = Database(catalogue_path_with_extension(path), initialize=False, create=False)
    try:
        db.validate_catalogue()
        return db
    except Exception:
        db.close()
        raise


def open_database(path: str | Path) -> Database:
    return open_catalogue(path)


def count_rows(db: Database, table: str) -> int:
    if table not in REQUIRED_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    return int(db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
