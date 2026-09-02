from __future__ import annotations

import os
import hashlib
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .database import Database, format_timestamp, utc_now
from .media_metadata import (
    MediaInspectionCancelled,
    MediaMetadataExtractor,
    MediaMetadata,
    media_kind_for_extension,
)
from .preview_cache import PreviewCancelled
from .preview_service import (
    MODE_SKIPPED_PREFLIGHT,
    PreviewService,
    PreviewStatistics,
    disabled_statistics,
    hash_unavailable_status_record,
    preview_warning_message,
    scan_outcome,
    status_record_for,
)
from .utils import capture_volume_snapshot, resolve_volume_source_path, volume_identity_known


ProgressCallback = Callable[[int, int, str], None]
StatsProgressCallback = Callable[[int, int, str, int, int], None]
CancelCallback = Callable[[], bool]
HASH_ALGORITHM = "sha256"
HASH_READ_SIZE = 4 * 1024 * 1024
HASH_PROGRESS_BYTES = 64 * 1024 * 1024
HASH_PROGRESS_SECONDS = 0.5
HASH_STABILITY_ATTEMPTS = 2


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
    hash_errors: int = 0

    @classmethod
    def from_dict(
        cls,
        values: dict[str, int],
        errors_count: int = 0,
        hash_errors: int = 0,
    ) -> ScanChanges:
        return cls(
            **{
                field: int(values.get(field, 0))
                for field in cls.__dataclass_fields__
                if field not in {"errors_count", "hash_errors"}
            },
            errors_count=errors_count,
            hash_errors=hash_errors,
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
                self.hash_errors,
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
    files_hashed: int = 0
    bytes_hashed: int = 0
    hash_errors: int = 0
    media_files: int = 0
    media_metadata_collected: int = 0
    preview: dict[str, Any] | None = None

    @property
    def preview_statistics(self) -> PreviewStatistics:
        return PreviewStatistics.from_dict(self.preview)

    @property
    def outcome(self) -> str:
        """``completed_with_warnings`` when indexing succeeded but previews failed.

        The persisted scan status is unchanged (``completed``) because other
        catalogue features key on it; the outcome is what the UI reports.
        """

        return scan_outcome(self.status, self.preview)


class ScanCancelled(Exception):
    pass


class ScanDiscarded(Exception):
    pass


class FileChangedDuringHashError(OSError):
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
        media_extractor: MediaMetadataExtractor | None = None,
        batch_size: int = 500,
        preview_service: PreviewService | None = None,
        preview_statistics: PreviewStatistics | None = None,
    ) -> None:
        self.db = db
        self.progress_callback = progress_callback
        self.stats_progress_callback = stats_progress_callback
        self.cancel_callback = cancel_callback
        self.preview_callback = preview_callback
        self.media_extractor = media_extractor or MediaMetadataExtractor()
        self.batch_size = batch_size
        # Offline previews are opt-in.  ``preview_service`` is only supplied
        # when previews are enabled and the scan-start preflight passed;
        # ``preview_statistics`` records the reason when they were skipped.
        self.preview_service = preview_service
        if preview_service is not None:
            self.preview_statistics = preview_service.statistics
        elif preview_statistics is not None:
            self.preview_statistics = preview_statistics
        else:
            self.preview_statistics = disabled_statistics()
        self.files_hashed = 0
        self.bytes_hashed = 0
        self.hash_errors = 0
        self.media_files = 0
        self.media_metadata_collected = 0
        self._last_hash_progress_bytes = 0
        self._last_hash_progress_time = 0.0
        catalogue_path = os.path.normcase(os.path.abspath(str(self.db.path)))
        self._catalogue_storage_paths = {
            catalogue_path,
            *(f"{catalogue_path}{suffix}" for suffix in ("-wal", "-shm", "-journal")),
        }

    def scan(self, volume_id: int) -> ScanResult:
        self.files_hashed = 0
        self.bytes_hashed = 0
        self.hash_errors = 0
        self.media_files = 0
        self.media_metadata_collected = 0
        self._last_hash_progress_bytes = 0
        self._last_hash_progress_time = time.monotonic()
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
        volume_label = " - ".join(
            str(part) for part in (volume["drive_id"], volume["name"]) if part
        )

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
                        if self._is_catalogue_storage_path(full_path):
                            continue
                        try:
                            stat_result = full_path.lstat()
                        except OSError as exc:
                            errors_count += 1
                            self.db.add_scan_error(scan_id, volume_id, rel_path, str(exc))
                            continue
                        if self._is_link_or_reparse_point(stat_result) or not stat.S_ISREG(stat_result.st_mode):
                            continue

                        extension = full_path.suffix[1:].lower() if full_path.suffix else ""
                        content_hash: bytes | None = None
                        try:
                            content_hash, stat_result = self._hash_stable_file(
                                full_path,
                                stat_result,
                                rel_path,
                                files_seen,
                                folders_seen,
                            )
                            self.files_hashed += 1
                        except ScanCancelled:
                            raise
                        except FileChangedDuringHashError as exc:
                            errors_count += 1
                            self.db.add_scan_error(
                                scan_id,
                                volume_id,
                                rel_path,
                                "File skipped because it changed or disappeared while "
                                f"being scanned: {exc}",
                            )
                            continue
                        except OSError as exc:
                            errors_count += 1
                            self.hash_errors += 1
                            self.db.add_scan_error(
                                scan_id,
                                volume_id,
                                rel_path,
                                f"SHA-256 hash unavailable: {exc}",
                            )

                        file_id = self.db.upsert_file(
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
                            content_hash=content_hash,
                            content_hash_algorithm=(
                                HASH_ALGORITHM if content_hash is not None else None
                            ),
                        )
                        media_kind = media_kind_for_extension(extension)
                        preserve_previous_media = False
                        if media_kind is not None and content_hash is not None:
                            previous_hash = self.db.scan_previous_file_hash(rel_path)
                            preserve_previous_media = bool(
                                previous_hash is not None
                                and previous_hash[0] == content_hash
                                and previous_hash[1].casefold() == HASH_ALGORITHM
                            )
                        try:
                            if media_kind is not None:
                                self._emit_progress(
                                    files_seen,
                                    folders_seen,
                                    f"Reading media details · {rel_path}",
                                )
                                media_metadata = self.media_extractor.inspect(
                                    full_path,
                                    extension=extension,
                                    cancel_callback=self._cancelled,
                                )
                            else:
                                media_metadata = None
                        except MediaInspectionCancelled as exc:
                            raise ScanCancelled(str(exc)) from exc
                        if media_metadata is not None:
                            self.db.replace_file_media_metadata(
                                file_id,
                                media_metadata.as_db_values(),
                                preserve_existing_on_failure=preserve_previous_media,
                            )
                            self.media_files += 1
                            if media_metadata.status in {"complete", "partial"}:
                                self.media_metadata_collected += 1
                        if (
                            media_kind in {"image", "video"}
                            and self.preview_service is not None
                        ):
                            # Never before the final SHA-256 of a stable file
                            # is known and the catalogue record is stored.
                            self._ensure_file_preview(
                                file_id=file_id,
                                media_kind=media_kind,
                                full_path=full_path,
                                relative_path=rel_path,
                                file_name=file_name,
                                content_hash=content_hash,
                                volume_id=volume_id,
                                volume_label=volume_label,
                                media_metadata=media_metadata,
                                files_seen=files_seen,
                                folders_seen=folders_seen,
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
                        hash_errors=self.hash_errors,
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

            if status == "completed" and message is None:
                message = self._preview_report_message()
            summary = changes.as_dict() if changes is not None else None
            self.db.finish_scan(
                scan_id,
                status,
                files_seen,
                folders_seen,
                errors_count,
                message,
                summary,
                files_hashed=self.files_hashed,
                bytes_hashed=self.bytes_hashed,
                hash_errors=self.hash_errors,
                media_files=self.media_files,
                media_metadata_collected=self.media_metadata_collected,
                preview_summary=self._preview_summary(),
            )
            return self._result(status, files_seen, folders_seen, errors_count, message, changes)
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
                files_hashed=self.files_hashed,
                bytes_hashed=self.bytes_hashed,
                hash_errors=self.hash_errors,
                media_files=self.media_files,
                media_metadata_collected=self.media_metadata_collected,
                preview_summary=self._preview_summary(),
            )
            return self._result("discarded", files_seen, folders_seen, errors_count, str(exc), changes)
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
                files_hashed=self.files_hashed,
                bytes_hashed=self.bytes_hashed,
                hash_errors=self.hash_errors,
                media_files=self.media_files,
                media_metadata_collected=self.media_metadata_collected,
                preview_summary=self._preview_summary(),
            )
            return self._result("cancelled", files_seen, folders_seen, errors_count, str(exc), changes)
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
                files_hashed=self.files_hashed,
                bytes_hashed=self.bytes_hashed,
                hash_errors=self.hash_errors,
                media_files=self.media_files,
                media_metadata_collected=self.media_metadata_collected,
                preview_summary=self._preview_summary(),
            )
            raise

    def _ensure_file_preview(
        self,
        *,
        file_id: int,
        media_kind: str,
        full_path: Path,
        relative_path: str,
        file_name: str,
        content_hash: bytes | None,
        volume_id: int,
        volume_label: str,
        media_metadata: MediaMetadata | None,
        files_seen: int,
        folders_seen: int,
    ) -> None:
        service = self.preview_service
        if service is None:
            return
        if content_hash is None:
            self.db.replace_file_preview_status(
                file_id,
                hash_unavailable_status_record(
                    media_kind,
                    service.cache.profile_id(media_kind),
                ),
            )
            return
        label = f"Creating {media_kind} preview · {relative_path}"
        self._emit_progress(files_seen, folders_seen, label)
        expected_duration_ms = (
            media_metadata.duration_ms
            if media_kind == "video" and media_metadata is not None
            else None
        )
        try:
            result = service.ensure_preview(
                media_kind=media_kind,
                source=full_path,
                content_hash=content_hash,
                relative_path=relative_path,
                source_name=file_name,
                volume_id=volume_id,
                volume_label=volume_label,
                cancel_callback=self._cancelled,
                progress_callback=lambda text: self._emit_progress(
                    files_seen,
                    folders_seen,
                    f"{label} · {text}",
                ),
                expected_duration_ms=expected_duration_ms,
            )
        except PreviewCancelled as exc:
            raise ScanCancelled("Scan cancelled.") from exc
        self.db.replace_file_preview_status(file_id, status_record_for(result, content_hash))

    def _preview_summary(self) -> dict[str, Any]:
        stats = self.preview_statistics
        message = stats.message
        if not message and stats.storage_unavailable_reason:
            message = f"Preview storage became unavailable: {stats.storage_unavailable_reason}"
        return {
            "mode": stats.mode,
            "image_generated": stats.image_generated,
            "image_reused": stats.image_reused,
            "image_failed": stats.image_failed,
            "video_generated": stats.video_generated,
            "video_reused": stats.video_reused,
            "video_failed": stats.video_failed,
            "storage_skipped": stats.storage_skipped,
            "bytes_written": stats.bytes_written,
            "message": message,
        }

    def _preview_report_message(self) -> str | None:
        """Scan-report sentence for preview problems or a preflight skip."""

        stats = self.preview_statistics
        if stats.mode == MODE_SKIPPED_PREFLIGHT:
            return stats.message or None
        if stats.has_problems:
            return preview_warning_message(stats) or None
        return None

    def _result(
        self,
        status: str,
        files_seen: int,
        folders_seen: int,
        errors_count: int,
        message: str | None,
        changes: ScanChanges | None,
    ) -> ScanResult:
        return ScanResult(
            status=status,
            files_seen=files_seen,
            folders_seen=folders_seen,
            errors_count=errors_count,
            message=message,
            changes=changes,
            files_hashed=self.files_hashed,
            bytes_hashed=self.bytes_hashed,
            hash_errors=self.hash_errors,
            media_files=self.media_files,
            media_metadata_collected=self.media_metadata_collected,
            preview=self.preview_statistics.as_dict(),
        )

    def _hash_stable_file(
        self,
        path: Path,
        initial_stat: os.stat_result,
        relative_path: str,
        files_seen: int,
        folders_seen: int,
    ) -> tuple[bytes, os.stat_result]:
        """Hash a stable snapshot, retrying once after a concurrent change."""
        current_stat = initial_stat
        last_error: OSError | None = None
        for attempt in range(HASH_STABILITY_ATTEMPTS):
            try:
                return (
                    self._hash_file(
                        path,
                        current_stat,
                        relative_path,
                        files_seen,
                        folders_seen,
                    ),
                    current_stat,
                )
            except ScanCancelled:
                raise
            except (FileChangedDuringHashError, FileNotFoundError) as exc:
                last_error = exc
                if attempt + 1 >= HASH_STABILITY_ATTEMPTS:
                    break
                if self._cancelled():
                    raise ScanCancelled("Scan cancelled.")
                try:
                    refreshed_stat = path.lstat()
                except OSError as refresh_error:
                    raise FileChangedDuringHashError(
                        f"the file could not be restated after a change: {refresh_error}"
                    ) from refresh_error
                if self._is_link_or_reparse_point(refreshed_stat) or not stat.S_ISREG(
                    refreshed_stat.st_mode
                ):
                    raise FileChangedDuringHashError(
                        "the path stopped being a regular file"
                    )
                current_stat = refreshed_stat
        raise FileChangedDuringHashError(
            str(last_error or "the file did not remain stable long enough to hash")
        ) from last_error

    def _hash_file(
        self,
        path: Path,
        initial_stat: os.stat_result,
        relative_path: str,
        files_seen: int,
        folders_seen: int,
    ) -> bytes:
        if self._cancelled():
            raise ScanCancelled("Scan cancelled.")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if not self._same_file_snapshot(initial_stat, opened_stat):
                raise FileChangedDuringHashError("file changed before its content was read")
            while True:
                if self._cancelled():
                    raise ScanCancelled("Scan cancelled.")
                chunk = stream.read(HASH_READ_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                self.bytes_hashed += len(chunk)
                self._emit_hash_progress(files_seen, folders_seen, relative_path)
            finished_stat = os.fstat(stream.fileno())
        final_stat = path.lstat()
        if (
            not self._same_file_snapshot(opened_stat, finished_stat)
            or not self._same_file_snapshot(finished_stat, final_stat)
        ):
            raise FileChangedDuringHashError("file changed while its content was read")
        if self._cancelled():
            raise ScanCancelled("Scan cancelled.")
        return hasher.digest()

    def _emit_hash_progress(
        self,
        files_seen: int,
        folders_seen: int,
        relative_path: str,
    ) -> None:
        now = time.monotonic()
        if (
            self.bytes_hashed - self._last_hash_progress_bytes < HASH_PROGRESS_BYTES
            and now - self._last_hash_progress_time < HASH_PROGRESS_SECONDS
        ):
            return
        self._last_hash_progress_bytes = self.bytes_hashed
        self._last_hash_progress_time = now
        self._emit_progress(
            files_seen,
            folders_seen,
            f"Hashing SHA-256 · {self.bytes_hashed:,} bytes read · {relative_path}",
        )

    @staticmethod
    def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
        first_mtime_ns = getattr(first, "st_mtime_ns", int(first.st_mtime * 1_000_000_000))
        second_mtime_ns = getattr(second, "st_mtime_ns", int(second.st_mtime * 1_000_000_000))
        if int(first.st_size) != int(second.st_size) or int(first_mtime_ns) != int(second_mtime_ns):
            return False
        for identity_name in ("st_dev", "st_ino"):
            first_identity = int(getattr(first, identity_name, 0) or 0)
            second_identity = int(getattr(second, identity_name, 0) or 0)
            if first_identity and second_identity and first_identity != second_identity:
                return False
        return stat.S_ISREG(second.st_mode)

    def _is_catalogue_storage_path(self, path: Path) -> bool:
        normalized = os.path.normcase(os.path.abspath(str(path)))
        return normalized in self._catalogue_storage_paths

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
