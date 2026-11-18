"""Resumable, verified copies of project folders to filesystem destinations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
from tempfile import TemporaryDirectory, gettempdir
from time import monotonic
from uuid import UUID, uuid4

from .archive_io import (
    ARCHIVE_STAGE_NAME, ARCHIVE_STAGE_PREFIX, ARCHIVE_STAGE_SUFFIX, CHUNK_SIZE,
    DestinationReader, ProjectGuard, file_identity, fingerprint, hold_folder, is_link,
)
from .content_hash import new_hasher
from .database import Database, format_timestamp, utc_now
from .drive_marker import MARKER_IDENTITY
from .media_metadata import MediaInspectionCancelled, MediaMetadataExtractor, media_kind_for_extension
from .preview_cache import PreviewCancelled
from .preview_service import PreviewService, status_record_for
from .utils import capture_volume_snapshot, format_size, resolve_volume_source_path, snapshot_matches_volume


JOB_FILE = "archive-job.sqlite"
RAM_PER_FILE = 64 * 1024 * 1024


class ArchivePaused(Exception):
    """The current attempt was interrupted and can be resumed."""


@dataclass(frozen=True)
class ProjectEntry:
    relative: str
    info: os.stat_result
    directory: bool


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    volume_id: int
    files: int
    bytes_copied: int
    preview_failures: int
    phase_seconds: dict[str, float]


def pending_archive(catalogue: Path) -> dict | None:
    job = _read_job(catalogue / JOB_FILE)
    return job if job is not None and not job.get("complete") else None


def _read_job(path: Path) -> dict | None:
    if not os.path.lexists(path):
        return None
    _checked_file(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as journal:
        if not journal.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'job'").fetchone():
            return None
        row = journal.execute("SELECT value FROM job WHERE id = 1").fetchone()
    return json.loads(row[0]) if row is not None else None


def is_archive_volume_root(volume) -> bool:
    return (volume is not None and bool(volume["identity_kind"] and volume["identity_token"])
            and not volume["source_relative_path"])


def is_scanned_archive_volume(db: Database, volume) -> bool:
    if not is_archive_volume_root(volume) or not volume["last_scan_at"]:
        return False
    root = db.get_root_folder(volume["id"])
    return root is not None and not root["missing"]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _volume_key(snapshot) -> tuple[str, str]:
    return snapshot.identity_kind, snapshot.identity_token.casefold()


def _matches_archive_volume(volume, snapshot) -> bool:
    return (
        snapshot is not None
        and not snapshot.source_relative_path
        and Path(snapshot.source_path) == Path(snapshot.mount_root)
        and snapshot_matches_volume(snapshot, volume)
    )


def check_archive_write_locations(source: Path | None, *locations: Path) -> None:
    """Validate actual mount locations before opening any archive output."""
    # Q: is the protected project drive at this installation. Check its
    # volume identity too, since a drive can have more than one mount path.
    protected = capture_volume_snapshot(Path("Q:/")) if os.name == "nt" else None
    for location in locations:
        path = location.resolve()
        if source is not None and _inside(path, source):
            raise OSError(f"An archive write location is inside the source project: {path}")
        if os.name == "nt" and (path.drive.casefold() == "q:" or location.drive.casefold() == "q:"):
            raise OSError(f"Choose an archive write location outside the protected Q: drive: {location}")
        if protected is not None and protected.identity_token:
            existing = next((parent for parent in (path, *path.parents) if parent.is_dir()), None)
            volume = capture_volume_snapshot(existing) if existing is not None else None
            if volume is not None and _volume_key(volume) == _volume_key(protected):
                raise OSError(f"Choose an archive write location outside the protected Q: drive: {path}")


def _checked_folder(path: Path) -> os.stat_result:
    info = path.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"Expected a regular folder: {path}")
    return info


def _checked_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode):
        raise OSError(f"Expected a regular file: {path}")
    return info


def _scan_tree(root, check, guard=None) -> list[ProjectEntry]:
    info = _checked_folder(root)
    entries = [ProjectEntry("", info, True)]
    if guard is not None:
        guard.hold(root, directory=True)
    dirs = [root]
    while dirs:
        folder = dirs.pop()
        check()
        with os.scandir(folder) as children:
            for child in sorted(children, key=lambda item: item.name):
                check()
                path = Path(child.path)
                info = path.lstat()  # On Windows, DirEntry.stat can omit IDs.
                directory = stat.S_ISDIR(info.st_mode)
                if is_link(info) or not (directory or stat.S_ISREG(info.st_mode)):
                    raise OSError(f"Archive projects must contain regular files and folders: {path}")
                if guard is not None:
                    guard.hold(path, directory=directory)
                    current = path.lstat()
                    if fingerprint(info) != fingerprint(current) or is_link(current):
                        raise OSError(f"Source changed during preparation: {path}")
                entries.append(ProjectEntry(path.relative_to(root).as_posix(), info, directory))
                if directory:
                    dirs.append(path)
    return entries


def _manifest(entries, check) -> str:
    hasher = new_hasher()
    for entry in entries:
        check()
        value = entry.relative, entry.directory, fingerprint(entry.info)
        hasher.update(json.dumps(value, ensure_ascii=True).encode("ascii") + b"\n")
    return hasher.hexdigest()


class MediaInput:
    """Keep small inputs in RAM until the existing path-based decoder needs them."""

    def __init__(self, scratch: Path, suffix: str, size: int):
        self.path = scratch / (uuid4().hex + suffix)
        if size > RAM_PER_FILE and shutil.disk_usage(scratch).free < size:
            raise OSError(f"The temporary media cache needs {format_size(size)} of free space: {scratch}")
        self.stream = io.BytesIO() if size <= RAM_PER_FILE else self.path.open("xb")

    def write(self, data) -> None:
        self.stream.write(data)

    def materialize(self) -> Path:
        if isinstance(self.stream, io.BytesIO):
            with self.stream.getbuffer() as data, self.path.open("xb") as output:
                output.write(data)
        self.stream.close()
        return self.path

    def close(self) -> None:
        self.stream.close()
        self.path.unlink(missing_ok=True)


class ArchiveJob:
    """Own one attempt; journal completed files and commit the catalogue once."""

    def __init__(self, db: Database, *, preview_settings=None, cancelled=None, progress=None):
        self.db = db
        self.preview_settings = preview_settings
        self.cancelled = cancelled or (lambda: False)
        self.progress = progress or (lambda *_: None)
        self._last_progress = 0.0
        self.algorithm = db.hash_algorithm

    def _check(self) -> None:
        if self.cancelled():
            raise ArchivePaused("Archive paused. Choose Archive Project to resume.")
        self.guard.check()

    def _progress(self, label, done, total, *, force=False):
        now = monotonic()
        if force or now - self._last_progress >= 0.15:
            self._last_progress = now
            self.progress(label, done, total)

    def run(
        self, source_path: str | Path, volume_id: int, *,
        volume_uuid=None, start_new=False, job_id: str | None = None,
    ) -> ArchiveResult:
        if self.db.read_only:
            raise OSError("Open the catalogue for editing to archive a project.")
        volume = self.db.get_volume(volume_id)
        if not is_scanned_archive_volume(self.db, volume):
            raise OSError("Choose a drive that has completed its initial volume scan.")
        if volume_uuid is not None and volume["uuid"] != volume_uuid:
            raise OSError("The selected volume was replaced in the catalogue. Choose the drive again.")
        destination_path = resolve_volume_source_path(volume)
        if destination_path is None:
            raise OSError("Connect the selected destination drive and try again.")
        destination = Path(destination_path).resolve(strict=True)
        snapshot = capture_volume_snapshot(destination)
        if not _matches_archive_volume(volume, snapshot):
            raise OSError("The connected drive does not match the selected volume.")
        journal_name = JOB_FILE if job_id is None else f"archive-job-{UUID(job_id).hex}.sqlite"
        journal_path = self.db.path / journal_name
        if job_id is not None:
            state = _read_job(journal_path)
            if state and state.get("complete") and state.get("result"):
                final = destination / Path(state["source"]).name
                if (state["volume_uuid"] != volume["uuid"] or state["catalogue"] != self.db.catalogue_uuid()
                        or not self.db.get_folder_by_path(volume_id, final.name)
                        or list(file_identity(_checked_folder(final))) != state["payload_identity"]):
                    raise OSError("The previously verified archive could not be found at its saved location.")
                return ArchiveResult(final, volume_id, **state["result"])
        source = Path(source_path).resolve(strict=True)
        if not source.name:
            raise OSError("Choose a project folder inside the source drive.")
        if ARCHIVE_STAGE_NAME.fullmatch(source.name):
            raise OSError("The project folder name is reserved for partial archives.")
        if self.preview_settings is not None:
            self.preview_settings.validate(require_root=True)
        scratch = Path(gettempdir()).resolve(strict=True)
        locations = [Path(destination_path), self.db.path, scratch]
        if self.preview_settings is not None and self.preview_settings.root_path is not None:
            locations.append(self.preview_settings.root_path)
        check_archive_write_locations(source, *locations)
        final = destination / source.name
        if _inside(final, source) or _inside(source, final):
            raise OSError("The archive folder overlaps the project folder.")
        marker_name = source.name + ".jvvv-project.json"
        if len(marker_name.encode("utf-8", errors="surrogatepass")) > 255:
            marker_name = "JVVV Project.json"

        total_start = monotonic()
        with ProjectGuard(source) as self.guard, ExitStack() as resources:
            resources.enter_context(hold_folder(destination))
            destination_identity = file_identity(_checked_folder(destination))
            if not _matches_archive_volume(volume, capture_volume_snapshot(destination)):
                raise OSError("Reconnect the selected destination drive.")
            self._progress("Protecting and listing source files…", 0, 0, force=True)
            entries = _scan_tree(source, self._check, self.guard)
            if any(entry.relative.casefold() == marker_name.casefold() for entry in entries):
                raise OSError(f"The source already contains the reserved archive marker: {marker_name}")
            source_manifest = _manifest(entries, self._check)
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = journal_path.with_name(journal_path.name + suffix)
                if os.path.lexists(candidate):
                    _checked_file(candidate)
            journal = resources.enter_context(closing(sqlite3.connect(journal_path)))
            state, digests = self._open_job(journal, source, destination, snapshot, volume, source_manifest, start_new)
            stage = destination / f"{ARCHIVE_STAGE_PREFIX}{state['id']}{ARCHIVE_STAGE_SUFFIX}"
            payload = stage / "project"
            marker_bytes = (json.dumps({
                "schema_version": 1,
                "project_name": source.name,
                "archived_by": "JVVV",
                "archive_job_id": state["id"],
                "archive_started_at": state["started_at"],
            }, indent=2) + "\n").encode("utf-8")

            def check_destination():
                if file_identity(_checked_folder(destination)) != destination_identity:
                    raise OSError("The destination folder was replaced or disconnected.")

            self._check()
            check_destination()
            if "payload_identity" not in state:
                if os.path.lexists(final):
                    raise FileExistsError(f"An archive folder already exists: {final}")
                if os.path.lexists(stage):
                    identity = list(file_identity(_checked_folder(stage)))
                    if state.get("stage_identity") != identity and any(stage.iterdir()):
                        raise OSError(f"An unrecognized staging folder exists: {stage}")
                else:
                    stage.mkdir()
                state["stage_identity"] = list(file_identity(_checked_folder(stage)))
                self._save_job(journal, state)
                if os.path.lexists(payload):
                    _checked_folder(payload)
                    if any(payload.iterdir()):
                        raise OSError(f"An unrecognized staging project exists: {payload}")
                else:
                    payload.mkdir()
                state["payload_identity"] = list(file_identity(_checked_folder(payload)))
                self._save_job(journal, state)

            published = os.path.lexists(final)
            if published and os.path.lexists(payload):
                raise FileExistsError(f"An archive folder already exists: {final}")
            work = final if published else payload
            if list(file_identity(_checked_folder(work))) != state["payload_identity"]:
                raise OSError(f"The archive folder was replaced: {work}")
            if not published:
                if list(file_identity(_checked_folder(stage))) != state["stage_identity"]:
                    raise OSError(f"The archive staging folder was replaced: {stage}")

            files = [entry for entry in entries if not entry.directory]
            total = sum(entry.info.st_size for entry in files)
            existing = {entry.relative: entry for entry in _scan_tree(work, self._check)}
            marker_path = work / marker_name
            marker_exists = existing.pop(marker_name, None) is not None
            if marker_exists:
                self._check_marker(marker_path, marker_bytes)
            elif published:
                raise OSError("The published archive is missing its JVVV project marker.")
            expected = {entry.relative: entry for entry in entries}
            for relative, entry in existing.items():
                if relative not in expected or expected[relative].directory != entry.directory:
                    raise OSError(f"Unexpected item in the archive folder: {relative}")
                if not entry.directory and relative not in digests:
                    raise OSError(f"Unexpected file in the archive folder: {relative}")
            if published and (existing.keys() != expected.keys() or len(digests) != len(files)):
                raise OSError("The published archive is incomplete. Inspect it before retrying.")
            reuse = {entry.relative for entry in files if entry.relative in existing}
            needed = sum(entry.info.st_size for entry in files if entry.relative not in reuse)
            if not marker_exists:
                needed += len(marker_bytes)
            if not published:
                check_destination()
                part = stage / "copy.part"
                if os.path.lexists(part):
                    _checked_file(part)
                    part.unlink()
                if shutil.disk_usage(destination).free < needed:
                    raise OSError(f"Destination needs {format_size(needed)} of free space to finish this copy.")
                if snapshot.identity_filesystem.upper() == "FAT32" and any(
                    entry.info.st_size >= 2**32 for entry in files
                ):
                    raise OSError("FAT32 cannot hold one or more files in this project. Use a different drive.")
                for entry in entries:
                    if entry.directory and entry.relative not in existing:
                        check_destination()
                        (work / entry.relative).mkdir()

            extractor = MediaMetadataExtractor()
            service = None
            if self.preview_settings is not None:
                service = PreviewService(self.preview_settings, hash_algorithm=self.algorithm)
            cache_root = Path(resources.enter_context(TemporaryDirectory(prefix="jvvv-archive-", dir=scratch)))
            worker = resources.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-media"))
            pending = None
            media = {}

            def finish_media():
                nonlocal pending
                if pending is not None:
                    relative, future = pending
                    media[relative] = future.result()
                    pending = None

            def queue_media(entry, cache, digest):
                nonlocal pending
                try:
                    finish_media()
                    pending = entry.relative, worker.submit(self._inspect_media, entry, cache, digest, extractor, service)
                except BaseException:
                    cache.close()
                    raise

            copy_started = monotonic()
            copied = 0
            self._progress("Copying and hashing originals…", 0, total, force=True)
            for entry in files:
                self._check()
                if entry.relative in reuse:
                    # Source protections are released between attempts. Check
                    # the saved content even if the file's timestamp was restored.
                    self._check_resumed_source(source / entry.relative, entry, digests[entry.relative])
                    copied += entry.info.st_size
                    self._progress("Checking previously copied files…", copied, total)
                    continue
                check_destination()
                path = work / entry.relative
                _checked_folder(path.parent)
                if list(file_identity(_checked_folder(stage))) != state["stage_identity"]:
                    raise OSError(f"The archive staging folder was replaced: {stage}")
                cache = self._media_input(cache_root, entry)
                try:
                    digest = self._copy_file(source / entry.relative, part, entry, cache, copied, total)
                    self._record_digest(journal, entry.relative, digest)
                    digests[entry.relative] = digest
                    if os.path.lexists(path):
                        raise FileExistsError(f"A file appeared in the archive staging folder: {path}")
                    part.rename(path)
                except BaseException:
                    if cache is not None:
                        cache.close()
                    raise
                copied += entry.info.st_size
                if cache is not None:
                    queue_media(entry, cache, digest)
            copy_seconds = monotonic() - copy_started

            verify_started = monotonic()
            verified = 0
            self._progress("Reading back the destination…", 0, total, force=True)
            destination_stats = {}
            with DestinationReader() as reader:
                for entry in files:
                    self._check()
                    check_destination()
                    path = work / entry.relative
                    info = _checked_file(path)
                    # For a resumed file, feed the verification read to the
                    # media queue. This never rereads the original project.
                    cache = self._media_input(cache_root, entry) if entry.relative in reuse else None
                    try:
                        digest = None
                        if info.st_size == entry.info.st_size:
                            digest = reader.digest(path, info.st_size, self.algorithm, self._check, cache)
                        if digest != digests[entry.relative] or info.st_size != entry.info.st_size:
                            if not published:
                                path.unlink()
                                with journal:
                                    journal.execute("DELETE FROM files WHERE path = ?", (entry.relative,))
                            detail = " Resume to copy this file again." if not published else " Inspect the published archive."
                            raise OSError(f"Destination verification failed: {entry.relative}.{detail}")
                        if fingerprint(_checked_file(path)) != fingerprint(info):
                            raise OSError(f"Destination changed during verification: {path}")
                    except BaseException:
                        if cache is not None:
                            cache.close()
                        raise
                    destination_stats[entry.relative] = info
                    verified += info.st_size
                    self._progress("Reading back the destination…", verified, total)
                    if cache is not None:
                        queue_media(entry, cache, digest)
            verify_seconds = monotonic() - verify_started
            self._progress("Finishing previews…", 0, 0, force=True)
            finish_media()
            self._check()
            if _manifest(_scan_tree(source, self._check), self._check) != source_manifest:
                raise OSError("The project contents changed during archiving.")
            final_entries = {entry.relative: entry for entry in _scan_tree(work, self._check)}
            if marker_exists:
                final_entries.pop(marker_name, None)
            if final_entries.keys() != expected.keys() or any(
                fingerprint(final_entries[relative].info) != fingerprint(info)
                for relative, info in destination_stats.items()
            ):
                raise OSError("The destination changed after verification.")
            current_destination = capture_volume_snapshot(destination)
            if not _matches_archive_volume(volume, current_destination):
                raise OSError("Reconnect the original destination drive and resume the archive.")
            self._progress("Saving the verified archive to the catalogue…", 0, 0, force=True)
            self._check()
            check_destination()
            if not marker_exists:
                with part.open("xb") as output:
                    output.write(marker_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                if os.path.lexists(marker_path):
                    raise FileExistsError(f"An archive marker already exists: {marker_path}")
                part.rename(marker_path)
            marker_info = self._check_marker(marker_path, marker_bytes)
            marker_hasher = new_hasher(self.algorithm)
            marker_hasher.update(marker_bytes)
            destination_stats[marker_name] = marker_info
            digests[marker_name] = marker_hasher.digest()
            if not published:
                for entry in reversed(entries):
                    if entry.directory:
                        _checked_folder(work / entry.relative)
                        os.utime(work / entry.relative, ns=(entry.info.st_atime_ns, entry.info.st_mtime_ns))
                if os.path.lexists(final):
                    raise FileExistsError(f"An archive folder already exists: {final}")
                work.rename(final)
            entries.append(ProjectEntry(marker_name, marker_info, False))
            self._register(volume, current_destination, final, entries, destination_stats, digests, media)
            failures = sum(status is not None and status["status"] != "available" for _, status in media.values())
            state["result"] = {
                "files": len(files), "bytes_copied": total, "preview_failures": failures,
                "phase_seconds": {"copy": copy_seconds, "verify": verify_seconds, "total": monotonic() - total_start},
            }
            state["complete"] = True
            with journal:
                journal.execute("DELETE FROM files")
                self._save_job(journal, state, commit=False)
            try:
                if list(file_identity(_checked_folder(stage))) == state["stage_identity"]:
                    stage.rmdir()
            except OSError:
                pass  # An empty staging folder needs no recovery work.
            return ArchiveResult(final, volume_id, **state["result"])

    @staticmethod
    def _check_marker(path, expected):
        info = _checked_file(path)
        with path.open("rb") as marker:
            if marker.read(len(expected) + 1) != expected:
                raise OSError(f"The JVVV project marker changed: {path}")
        return info

    def _open_job(self, journal, source, destination, snapshot, volume, source_manifest, start_new):
        journal.execute("CREATE TABLE IF NOT EXISTS job (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        journal.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, digest BLOB NOT NULL)")
        row = journal.execute("SELECT value FROM job WHERE id = 1").fetchone()
        state = json.loads(row[0]) if row else None
        dest_identity = list(file_identity(_checked_folder(destination)))
        if state and not state.get("complete") and not start_new:
            target = state["destination"]
            if not state.get("volume_uuid"):
                raise OSError("This saved job predates volume selection. Start a different archive job.")
            saved_identity = target["identity_kind"], target["identity_token"].casefold()
            current_identity = (volume["identity_kind"], volume["identity_token"].casefold())
            if (state["volume_uuid"] != volume["uuid"]
                or (saved_identity != _volume_key(snapshot)
                    and not (saved_identity[0] == MARKER_IDENTITY and saved_identity == current_identity))
                or dest_identity != state["destination_identity"]
            ):
                raise OSError("Choose the original destination volume and drive to resume.")
            if (state["source"] != str(source) or state["manifest"] != source_manifest
                    or state["algorithm"] != self.algorithm or state["catalogue"] != self.db.catalogue_uuid()):
                raise OSError("The project or catalogue changed since the archive started. Start a new archive job.")
            return state, dict(journal.execute("SELECT path, digest FROM files"))
        if os.path.lexists(destination / source.name):
            raise FileExistsError(f"An archive folder already exists: {destination / source.name}")
        root = self.db.get_root_folder(volume["id"])
        indexed_items = self.db.list_child_folders(volume["id"], root["id"]) + self.db.list_files(
            volume["id"], root["id"]
        )
        if any(item["name"].casefold() == source.name.casefold() for item in indexed_items):
            raise OSError(f"The selected volume's catalogue already contains {source.name}.")
        state = {
            "id": uuid4().hex,
            "started_at": utc_now(),
            "source": str(source),
            "destination": {"source_path": str(destination), **snapshot.location_for(volume)},
            "destination_identity": dest_identity,
            "manifest": source_manifest,
            "algorithm": self.algorithm,
            "catalogue": self.db.catalogue_uuid(),
            "volume_uuid": volume["uuid"],
        }
        with journal:
            journal.execute("DELETE FROM files")
            self._save_job(journal, state, commit=False)
        return state, {}

    @staticmethod
    def _save_job(journal, state, *, commit=True):
        journal.execute("INSERT OR REPLACE INTO job VALUES (1, ?)", (json.dumps(state),))
        if commit:
            journal.commit()

    @staticmethod
    def _record_digest(journal, relative, digest):
        with journal:
            journal.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (relative, digest))

    @staticmethod
    def _media_input(cache_root, entry):
        suffix = PurePosixPath(entry.relative).suffix
        if media_kind_for_extension(suffix) is not None:
            return MediaInput(cache_root, suffix, entry.info.st_size)
        return None

    def _copy_file(self, source, part, entry, cache, copied, total):
        hasher = new_hasher(self.algorithm)
        count = 0
        with source.open("rb") as input_file, part.open("xb") as output:
            if fingerprint(os.fstat(input_file.fileno())) != fingerprint(entry.info):
                raise OSError(f"The source changed before copying: {source}")
            while (block := input_file.read(CHUNK_SIZE)):
                self._check()
                if count + len(block) > entry.info.st_size:
                    raise OSError(f"The source grew while copying: {source}")
                hasher.update(block)
                output.write(block)
                if cache is not None:
                    cache.write(block)
                count += len(block)
                self._progress("Copying and hashing originals…", copied + count, total)
            if count != entry.info.st_size or fingerprint(os.fstat(input_file.fileno())) != fingerprint(entry.info):
                raise OSError(f"The source changed while copying: {source}")
            output.flush()
            os.fsync(output.fileno())
        os.utime(part, ns=(entry.info.st_atime_ns, entry.info.st_mtime_ns))
        return hasher.digest()

    def _check_resumed_source(self, source, entry, expected_digest):
        hasher = new_hasher(self.algorithm)
        with source.open("rb") as input_file:
            if fingerprint(os.fstat(input_file.fileno())) != fingerprint(entry.info):
                raise OSError(f"The source changed since the archive started: {source}")
            while (block := input_file.read(CHUNK_SIZE)):
                self._check()
                hasher.update(block)
            if (hasher.digest() != expected_digest
                    or fingerprint(os.fstat(input_file.fileno())) != fingerprint(entry.info)):
                raise OSError(f"The source changed since the archive started: {source}. Start a new job.")

    def _inspect_media(self, entry, cache, digest, extractor, service):
        try:
            path = cache.materialize()
            extension = PurePosixPath(entry.relative).suffix
            metadata = extractor.inspect(path, extension=extension, cancel_callback=self.cancelled)
            kind = media_kind_for_extension(extension)
            status = None
            if service is not None and kind in {"image", "video"}:
                preview = service.cache.preview_path(kind, digest.hex())
                check_archive_write_locations(self.guard.source, preview.parent)
                result = service.ensure_preview(
                    media_kind=kind, source=path, content_hash=digest, relative_path=entry.relative,
                    source_name=PurePosixPath(entry.relative).name, volume_id=0,
                    cancel_callback=self.cancelled, source_stat=path.stat(),
                    expected_duration_ms=metadata.duration_ms if metadata is not None else None,
                )
                status = status_record_for(result, digest, self.algorithm)
            return metadata.as_db_values() if metadata is not None else None, status
        except (PreviewCancelled, MediaInspectionCancelled) as exc:
            raise ArchivePaused("Archive paused.") from exc
        finally:
            cache.close()

    def _register(self, volume, snapshot, final, entries, dest_stats, digests, media):
        volume_id = volume["id"]
        scanned_at = utc_now()
        usage = shutil.disk_usage(final.parent)
        with self.db.transaction(immediate=True):
            current = self.db.get_volume(volume_id)
            if (not is_scanned_archive_volume(self.db, current) or current["uuid"] != volume["uuid"]
                    or not _matches_archive_volume(current, snapshot)):
                raise OSError("The selected destination volume changed in the catalogue.")
            root = self.db.get_root_folder(volume_id)
            folders = {}
            for entry in entries:
                path = PurePosixPath(final.name) / entry.relative
                parent = "" if str(path.parent) == "." else path.parent.as_posix()
                if entry.directory:
                    folders[path.as_posix()] = self.db.ensure_folder(
                        volume_id, root["id"] if not parent else folders[parent], path.name,
                        path.as_posix(), scanned_at, format_timestamp(entry.info.st_mtime),
                    )
                    continue
                # Registration uses destination identities, never source inodes.
                info = dest_stats[entry.relative]
                file_id = self.db.upsert_file(
                    volume_id, folders[parent], path.name, path.as_posix(), path.suffix.lower(),
                    info.st_size, format_timestamp(info.st_mtime), scanned_at, info.st_dev,
                    info.st_ino, digests[entry.relative], self.algorithm,
                )
                metadata, preview = media.get(entry.relative, (None, None))
                if metadata is not None:
                    self.db.replace_file_media_metadata(file_id, metadata)
                if preview is not None:
                    self.db.replace_file_preview_status(file_id, preview)
            self.db.rebuild_folder_statistics(volume_id, stats_updated_at=scanned_at)
            self.db.refresh_volume_counts(volume_id)
            self.db.update_volume_content_dates_from_index(volume_id)
            self.db.update_volume_storage(volume_id, usage.total, usage.used, usage.free)
            if Path(current["source_path"]) != final.parent:
                self.db.update_volume_location(volume_id, str(final.parent), snapshot.location_for(current))
