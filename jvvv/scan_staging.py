"""Run a volume scan in a private index and publish it in short write phases."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

from . import volume_store
from .archive_job import check_archive_write_locations
from .catalogue_format import (
    FILE_COLUMNS, FOLDER_COLUMNS, MEDIA_COLUMNS, PREVIEW_COLUMNS, SCAN_ERROR_COLUMNS, SCAN_HISTORY_COLUMNS,
)
from .database import COMPLETED_SCAN_STATUSES, DIRTY_CONTENT, DIRTY_HISTORY, SCAN_HISTORY_PER_VOLUME_LIMIT, Database


class _ScanIndex(Database):
    def _mark_dirty(self, volume_id, kind):
        # The private index is disposable. The live catalogue owns its volume file.
        pass


def _column_values(columns, alias):
    return ", ".join(f"{alias}.{name}" for name in columns)


def _upsert_values(columns):
    return ", ".join(f"{name} = excluded.{name}" for name in columns)


class StagedScan:
    def __init__(self, live: Database, volume, writer_lock):
        self.live = live
        self.volume_id = volume["id"]
        self.volume_uuid = volume["uuid"]
        self.writer_lock = writer_lock
        self.index = None
        self.history_id = None
        self.initial_scan_id = 0
        self.revision = None

    def __enter__(self):
        check_archive_write_locations(None, Path(gettempdir()))
        self.storage = TemporaryDirectory(prefix="jvvv-scan-")
        try:
            self.index = _ScanIndex(Path(self.storage.name) / "scan.jvvv")
            with self.writer_lock:
                self.index.set_hash_algorithm(self.live.hash_algorithm)
                self.revision = self.live.index_state(self.volume_id)["revision"]
                volume_store.import_volume(
                    self.index.connection, self.volume_id, self.volume_uuid,
                    self.live.volume_file_path(self.volume_id),
                )
            self.initial_scan_id = self.index.connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM scan_history"
            ).fetchone()[0]
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.index is not None:
                self.index.close()
        finally:
            self.storage.cleanup()

    def publish(self) -> None:
        history = self.index.connection.execute(
            "SELECT * FROM scan_history WHERE id > ? ORDER BY id DESC LIMIT 1", (self.initial_scan_id,)
        ).fetchone()
        if history is None:
            return
        staged_volume = self.index.get_volume(self.volume_id)
        indexed = history["status"] in COMPLETED_SCAN_STATUSES
        with self.writer_lock:
            conn = self.live.connection
            conn.execute("ATTACH DATABASE ? AS staged_scan", (str(self.index.layout.index_db),))
            try:
                with self.live.transaction(immediate=True):
                    state = self.live.index_state(self.volume_id)
                    if state is None or state["uuid"] != self.volume_uuid or state["revision"] != self.revision:
                        raise OSError("The volume changed in the catalogue during the scan. Run the scan again.")
                    if indexed and self.history_id is None:
                        self._publish_items(conn)
                        location = {name: staged_volume[name] for name in (
                            "identity_kind", "identity_token", "identity_label", "identity_serial",
                            "identity_filesystem", "source_relative_path",
                        )}
                        self.live.update_volume_location(self.volume_id, staged_volume["source_path"], location)
                        self.live.update_volume_storage(self.volume_id, staged_volume["capacity_bytes"],
                                                        staged_volume["used_bytes"], staged_volume["free_bytes"])
                        self.live.refresh_volume_counts(self.volume_id, staged_volume["last_scan_at"])
                        self.live.update_volume_content_dates_from_index(self.volume_id)
                    if indexed:
                        self._publish_file_details(conn, "file_preview_status", PREVIEW_COLUMNS[1:])
                    history_id = self._publish_history(conn, history)
                    self.live._mark_dirty(self.volume_id, DIRTY_CONTENT if indexed else DIRTY_HISTORY)
                self.history_id = history_id
                self.revision = self.live.index_state(self.volume_id)["revision"]
            finally:
                conn.execute("DETACH DATABASE staged_scan")

    def _publish_items(self, conn) -> None:
        folders = FOLDER_COLUMNS[2:]
        files = FILE_COLUMNS[2:]
        conn.execute(
            f"""
            INSERT INTO main.folders (volume_id, {', '.join(folders)})
            SELECT ?, {_column_values(folders, 's')} FROM staged_scan.folders s WHERE 1
            ON CONFLICT(volume_id, relative_path) DO UPDATE SET {_upsert_values(folders)}
            """, (self.volume_id,),
        )
        conn.execute(
            """
            UPDATE main.folders AS target SET parent_id = parent.id
            FROM staged_scan.folders AS staged
            LEFT JOIN staged_scan.folders AS staged_parent ON staged_parent.id = staged.parent_id
            LEFT JOIN main.folders AS parent ON parent.relative_path = staged_parent.relative_path
                                            AND parent.volume_id = ?
            WHERE target.volume_id = ? AND target.relative_path = staged.relative_path
            """, (self.volume_id, self.volume_id),
        )
        conn.execute(
            f"""
            INSERT INTO main.files (volume_id, folder_id, {', '.join(files)})
            SELECT ?, folder.id, {_column_values(files, 's')}
            FROM staged_scan.files s
            LEFT JOIN staged_scan.folders staged_folder ON staged_folder.id = s.folder_id
            LEFT JOIN main.folders folder ON folder.relative_path = staged_folder.relative_path AND folder.volume_id = ?
            WHERE 1
            ON CONFLICT(volume_id, relative_path) DO UPDATE
                SET folder_id = excluded.folder_id, {_upsert_values(files)}
            """, (self.volume_id, self.volume_id),
        )
        # The completed scan removed unseen paths from its private index.
        # Publish those removals as well as the surviving rows.
        conn.execute(
            """
            DELETE FROM main.files AS live
            WHERE live.volume_id = ? AND NOT EXISTS (
                SELECT 1 FROM staged_scan.files AS staged
                WHERE staged.volume_id = live.volume_id AND staged.relative_path = live.relative_path
            )
            """, (self.volume_id,),
        )
        conn.execute(
            """
            DELETE FROM main.folders AS live
            WHERE live.volume_id = ? AND NOT EXISTS (
                SELECT 1 FROM staged_scan.folders AS staged
                WHERE staged.volume_id = live.volume_id AND staged.relative_path = live.relative_path
            )
            """, (self.volume_id,),
        )
        self._publish_file_details(conn, "file_media_metadata", MEDIA_COLUMNS[1:])

    def _publish_file_details(self, conn, table, columns) -> None:
        conn.execute(
            f"DELETE FROM main.{table} WHERE file_id IN (SELECT id FROM main.files WHERE volume_id = ?)",
            (self.volume_id,),
        )
        conn.execute(
            f"""
            INSERT INTO main.{table} (file_id, {', '.join(columns)})
            SELECT live.id, {_column_values(columns, 'detail')}
            FROM staged_scan.{table} detail JOIN staged_scan.files f ON f.id = detail.file_id
            JOIN main.files live ON live.relative_path = f.relative_path AND live.volume_id = ?
            """, (self.volume_id,),
        )

    def _publish_history(self, conn, history) -> int:
        columns = SCAN_HISTORY_COLUMNS[1:]
        values = tuple(history[name] for name in columns)
        if self.history_id is not None:
            conn.execute(
                f"UPDATE main.scan_history SET {', '.join(f'{name} = ?' for name in columns)} WHERE id = ?",
                (*values, self.history_id),
            )
            return self.history_id
        history_id = conn.execute(
            f"INSERT INTO main.scan_history (volume_id, {', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))})", (self.volume_id, *values),
        ).lastrowid
        columns = ", ".join(SCAN_ERROR_COLUMNS[2:])
        conn.execute(
            f"""
            INSERT INTO main.scan_errors (scan_id, volume_id, {columns})
            SELECT ?, ?, {columns} FROM staged_scan.scan_errors WHERE scan_id = ?
            """, (history_id, self.volume_id, history["id"]),
        )
        self.live._prune_scan_history_for_volume(conn, scan_id=history_id, keep=SCAN_HISTORY_PER_VOLUME_LIMIT)
        return history_id
