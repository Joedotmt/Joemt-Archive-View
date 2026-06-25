from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from .config import default_db_path


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


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


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.initialize()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self._apply_migration_1()
            self.connection.execute("PRAGMA user_version = 1")
            self.connection.commit()

    def _apply_migration_1(self) -> None:
        self.connection.executescript(
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
            );

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
            );

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
            );

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
            );

            CREATE TABLE IF NOT EXISTS scan_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER REFERENCES scan_history(id) ON DELETE CASCADE,
                volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_folders_volume_parent
                ON folders(volume_id, parent_id);
            CREATE INDEX IF NOT EXISTS idx_folders_name
                ON folders(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_folders_path
                ON folders(relative_path COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_files_volume_folder
                ON files(volume_id, folder_id);
            CREATE INDEX IF NOT EXISTS idx_files_name
                ON files(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_files_extension
                ON files(extension COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_files_path
                ON files(relative_path COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_scan_errors_scan
                ON scan_errors(scan_id);
            """
        )

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
    ) -> int:
        cur = self.connection.execute(
            """
            INSERT INTO files (
                volume_id, folder_id, name, relative_path, extension,
                size_bytes, modified_at, missing, scanned_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(volume_id, relative_path) DO UPDATE SET
                folder_id = excluded.folder_id,
                name = excluded.name,
                extension = excluded.extension,
                size_bytes = excluded.size_bytes,
                modified_at = excluded.modified_at,
                missing = 0,
                scanned_at = excluded.scanned_at
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
                    0 AS size_bytes,
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


def open_database(path: str | Path | None = None) -> Database:
    return Database(path)


def count_rows(db: Database, table: str) -> int:
    if table not in {"volumes", "folders", "files", "scan_history", "scan_errors"}:
        raise ValueError(f"Unsupported table: {table}")
    return int(db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
