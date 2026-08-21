from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .database import Database, format_timestamp, utc_now
from .utils import capture_volume_snapshot, resolve_volume_source_path, volume_identity_known


ProgressCallback = Callable[[int, int, str], None]
StatsProgressCallback = Callable[[int, int, str, int, int], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class ScanChanges:
    files_before: int
    files_after: int
    files_added: int
    files_removed: int
    files_changed: int
    folders_before: int
    folders_after: int
    folders_added: int
    folders_removed: int
    bytes_before: int
    bytes_after: int
    bytes_added: int
    bytes_removed: int
    changed_bytes_before: int
    changed_bytes_after: int
    errors_count: int = 0

    @classmethod
    def from_dict(cls, values: dict[str, int], errors_count: int = 0) -> ScanChanges:
        return cls(
            **{
                field: int(values.get(field, 0))
                for field in cls.__dataclass_fields__
                if field != "errors_count"
            },
            errors_count=errors_count,
        )

    @property
    def has_previous_catalogue(self) -> bool:
        return self.files_before > 0 or self.folders_before > 0

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.files_added,
                self.files_removed,
                self.files_changed,
                self.folders_added,
                self.folders_removed,
            )
        )

    def as_dict(self) -> dict[str, int]:
        return {
            field: int(getattr(self, field))
            for field in self.__dataclass_fields__
        }


PreviewCallback = Callable[[ScanChanges], bool]


@dataclass(frozen=True)
class ScanResult:
    status: str
    files_seen: int
    folders_seen: int
    errors_count: int
    message: str | None = None
    changes: ScanChanges | None = None


class ScanCancelled(Exception):
    pass


class ScanDiscarded(Exception):
    pass


def normalize_relative_path(path: Path) -> str:
    text = path.as_posix()
    return "" if text == "." else text


def get_storage_stats(path: Path) -> tuple[int, int, int]:
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    return usage.total, used, usage.free


class VolumeScanner:
    def __init__(
        self,
        db: Database,
        progress_callback: ProgressCallback | None = None,
        stats_progress_callback: StatsProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
        preview_callback: PreviewCallback | None = None,
        batch_size: int = 500,
    ) -> None:
        self.db = db
        self.progress_callback = progress_callback
        self.stats_progress_callback = stats_progress_callback
        self.cancel_callback = cancel_callback
        self.preview_callback = preview_callback
        self.batch_size = batch_size

    def scan(self, volume_id: int) -> ScanResult:
        volume = self.db.get_volume(volume_id)
        if volume is None:
            raise ValueError(f"Volume does not exist: {volume_id}")

        if not volume["source_path"]:
            message = "Source path is not set for this volume."
            scan_id = self.db.start_scan(volume_id)
            self.db.finish_scan(scan_id, "failed", 0, 0, 0, message)
            return ScanResult("failed", 0, 0, 0, message)

        identity_known = volume_identity_known(volume)
        resolved_source_path = resolve_volume_source_path(volume)
        if resolved_source_path is None and identity_known:
            message = f"Identified volume is not connected: {volume['source_path']}"
            scan_id = self.db.start_scan(volume_id)
            self.db.finish_scan(scan_id, "failed", 0, 0, 0, message)
            return ScanResult("failed", 0, 0, 0, message)
        if resolved_source_path is None:
            resolved_source_path = volume["source_path"]

        root = Path(resolved_source_path)
        scan_id = self.db.start_scan(volume_id)
        scanned_at = utc_now()
        files_seen = 0
        folders_seen = 0
        errors_count = 0
        status = "completed"
        message: str | None = None
        changes: ScanChanges | None = None

        if not root.exists():
            message = f"Source path is not connected: {root}"
            self.db.finish_scan(scan_id, "failed", 0, 0, 0, message)
            return ScanResult("failed", 0, 0, 0, message)

        snapshot = capture_volume_snapshot(root)
        if snapshot is not None:
            self.db.update_volume_location(volume_id, snapshot.source_path, snapshot.as_db_fields())

        try:
            capacity, used, free = get_storage_stats(root)
        except OSError:
            capacity = used = free = 0

        folder_ids: dict[str, int] = {}

        try:
            def on_walk_error(exc: OSError) -> None:
                nonlocal errors_count
                errors_count += 1
                error_path = getattr(exc, "filename", "") or str(root)
                try:
                    relative = normalize_relative_path(Path(error_path).relative_to(root))
                except ValueError:
                    relative = str(error_path)
                self.db.add_scan_error(scan_id, volume_id, relative, str(exc))

            with self.db.transaction():
                self.db.prepare_scan_comparison(volume_id)
                self.db.update_volume_storage(volume_id, capacity, used, free)
                root_modified = self._modified_at(root)
                root_id = self.db.ensure_folder(
                    volume_id=volume_id,
                    parent_id=None,
                    name=root.name or str(root),
                    relative_path="",
                    scanned_at=scanned_at,
                    modified_at=root_modified,
                )
                folder_ids[""] = root_id
                folders_seen = 1
                self._emit_progress(files_seen, folders_seen, str(root))

                for current_root, dir_names, file_names in os.walk(root, topdown=True, onerror=on_walk_error):
                    if self._cancelled():
                        status = "cancelled"
                        message = "Scan cancelled."
                        break

                    current_path = Path(current_root)
                    rel_current = normalize_relative_path(current_path.relative_to(root))
                    parent_folder_id = folder_ids.get(rel_current)
                    if parent_folder_id is None:
                        parent_rel = normalize_relative_path(current_path.parent.relative_to(root))
                        parent_folder_id = folder_ids.get(parent_rel, root_id)

                    accessible_dirs: list[str] = []
                    for directory in sorted(dir_names, key=str.casefold):
                        if self._cancelled():
                            status = "cancelled"
                            message = "Scan cancelled."
                            break
                        full_path = current_path / directory
                        rel_path = normalize_relative_path(full_path.relative_to(root))
                        try:
                            stat_result = full_path.lstat()
                        except OSError as exc:
                            errors_count += 1
                            self.db.add_scan_error(scan_id, volume_id, rel_path, str(exc))
                            continue
                        if self._is_link_or_reparse_point(stat_result) or not stat.S_ISDIR(stat_result.st_mode):
                            continue

                        folder_id = self.db.ensure_folder(
                            volume_id=volume_id,
                            parent_id=parent_folder_id,
                            name=directory,
                            relative_path=rel_path,
                            scanned_at=scanned_at,
                            modified_at=format_timestamp(stat_result.st_mtime),
                        )
                        folder_ids[rel_path] = folder_id
                        folders_seen += 1
                        accessible_dirs.append(directory)

                    dir_names[:] = accessible_dirs

                    if status == "cancelled":
                        break

                    for file_name in sorted(file_names, key=str.casefold):
                        if self._cancelled():
                            status = "cancelled"
                            message = "Scan cancelled."
                            break
                        full_path = current_path / file_name
                        rel_path = normalize_relative_path(full_path.relative_to(root))
                        try:
                            stat_result = full_path.lstat()
                        except OSError as exc:
                            errors_count += 1
                            self.db.add_scan_error(scan_id, volume_id, rel_path, str(exc))
                            continue
                        if self._is_link_or_reparse_point(stat_result) or not stat.S_ISREG(stat_result.st_mode):
                            continue

                        extension = full_path.suffix[1:].lower() if full_path.suffix else ""
                        self.db.upsert_file(
                            volume_id=volume_id,
                            folder_id=parent_folder_id,
                            name=file_name,
                            relative_path=rel_path,
                            extension=extension,
                            size_bytes=stat_result.st_size,
                            modified_at=format_timestamp(stat_result.st_mtime),
                            scanned_at=scanned_at,
                            identity_device=self._stat_identity_value(stat_result, "st_dev"),
                            identity_inode=self._stat_identity_value(stat_result, "st_ino"),
                        )
                        files_seen += 1
                        if files_seen % self.batch_size == 0:
                            self._emit_progress(files_seen, folders_seen, rel_path)

                    self._emit_progress(files_seen, folders_seen, rel_current)

                if status == "cancelled":
                    raise ScanCancelled(message or "Scan cancelled.")

                if status == "completed":
                    changes = ScanChanges.from_dict(
                        self.db.scan_change_summary(volume_id, scanned_at),
                        errors_count=errors_count,
                    )
                    had_previous_scan = bool(volume["last_scan_at"]) or changes.has_previous_catalogue
                    if (
                        had_previous_scan
                        and changes.has_changes
                        and self.preview_callback is not None
                    ):
                        self._emit_progress(files_seen, folders_seen, "Reviewing scan changes")
                        apply_changes = self.preview_callback(changes)
                        if self._cancelled():
                            raise ScanCancelled("Scan cancelled.")
                        if not apply_changes:
                            status = "discarded"
                            message = "Catalogue update was not applied."
                            raise ScanDiscarded(message)
                    if self._cancelled():
                        raise ScanCancelled("Scan cancelled.")
                    self.db.finalize_scan_items(volume_id, scanned_at)
                    self._emit_progress(files_seen, folders_seen, "Preparing folder sizes...")
                    self.db.rebuild_folder_statistics(
                        volume_id,
                        stats_updated_at=scanned_at,
                        progress_callback=lambda done, total, message: self._on_stats_progress(
                            files_seen,
                            folders_seen,
                            done,
                            total,
                            message,
                        ),
                    )
                    self.db.refresh_volume_counts(volume_id, scanned_at)
                    self.db.update_volume_content_dates_from_index(volume_id)
                else:
                    self.db.refresh_volume_counts(volume_id)

            summary = changes.as_dict() if changes is not None else None
            self.db.finish_scan(
                scan_id,
                status,
                files_seen,
                folders_seen,
                errors_count,
                message,
                summary,
            )
            return ScanResult(status, files_seen, folders_seen, errors_count, message, changes)
        except ScanDiscarded as exc:
            summary = changes.as_dict() if changes is not None else None
            self.db.finish_scan(
                scan_id,
                "discarded",
                files_seen,
                folders_seen,
                errors_count,
                str(exc),
                summary,
            )
            return ScanResult(
                "discarded",
                files_seen,
                folders_seen,
                errors_count,
                str(exc),
                changes,
            )
        except ScanCancelled as exc:
            summary = changes.as_dict() if changes is not None else None
            self.db.finish_scan(
                scan_id,
                "cancelled",
                files_seen,
                folders_seen,
                errors_count,
                str(exc),
                summary,
            )
            return ScanResult(
                "cancelled",
                files_seen,
                folders_seen,
                errors_count,
                str(exc),
                changes,
            )
        except Exception as exc:
            summary = changes.as_dict() if changes is not None else None
            self.db.finish_scan(
                scan_id,
                "failed",
                files_seen,
                folders_seen,
                errors_count,
                str(exc),
                summary,
            )
            raise

    def _modified_at(self, path: Path) -> str | None:
        try:
            return format_timestamp(path.stat().st_mtime)
        except OSError:
            return None

    def _cancelled(self) -> bool:
        return bool(self.cancel_callback and self.cancel_callback())

    def _emit_progress(self, files_seen: int, folders_seen: int, current_path: str) -> None:
        if self.progress_callback:
            self.progress_callback(files_seen, folders_seen, current_path)

    def _on_stats_progress(
        self,
        files_seen: int,
        folders_seen: int,
        done: int,
        total: int,
        message: str,
    ) -> None:
        if self._cancelled():
            raise ScanCancelled("Scan cancelled.")
        if self.stats_progress_callback:
            self.stats_progress_callback(files_seen, folders_seen, message, done, total)
        elif total:
            self._emit_progress(files_seen, folders_seen, f"{message} ({done}/{total})")
        else:
            self._emit_progress(files_seen, folders_seen, message)

    def _is_link_or_reparse_point(self, stat_result: os.stat_result) -> bool:
        if stat.S_ISLNK(stat_result.st_mode):
            return True
        attributes = getattr(stat_result, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse_flag and attributes & reparse_flag)

    def _stat_identity_value(self, stat_result: os.stat_result, name: str) -> int | None:
        value = getattr(stat_result, name, None)
        if value is None:
            return None
        integer = int(value)
        return integer if integer > 0 else None
