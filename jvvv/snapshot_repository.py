"""Version history for a catalogue: a content-addressed snapshot repository.

::

    Name.jvvvrepo/
        repository.json                 identity of the repository and its catalogue
        blobs/<hh>/<sha256>.blob        zlib-compressed canonical volume streams
        snapshots/<snapshot-id>.json    one manifest per snapshot

A snapshot manifest lists every volume of the catalogue with its metadata row
and the digests of its *content* blob (folders, files, media, preview rows) and
*history* blob (scan history, scan errors).  Blobs are named by the SHA-256 of
their uncompressed canonical stream, which the volume files already carry in
``volume_meta`` - so taking a snapshot reads only the volumes whose blobs are
not in the repository yet.  Unchanged volumes cost nothing; a rescanned drive
costs one new blob.

Restoring rebuilds a complete catalogue folder (registry, volume files, and a
freshly imported index) in a temporary location and swaps it into place only
after every blob verified.  A single volume can also be restored into a live
catalogue; the next open re-imports it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .catalogue_format import (
    CONTENT_TABLES,
    HISTORY_TABLES,
    VOLUME_ROW_COLUMNS,
    CatalogueLayout,
    CatalogueLayoutError,
    catalogue_path_with_extension,
    create_registry_db,
    is_valid_uuid,
    new_uuid,
    open_registry_db,
    open_volume_file,
    read_registry,
    read_volume_meta,
    register_volume,
    registry_meta,
    registry_next_volume_id,
    set_registry_meta,
    validate_registry,
)
from . import volume_store
from .volume_store import (
    HashingTee,
    RowCounts,
    VolumeStoreError,
    blob_digest,
    decode_record,
    encode_record,
    iter_blob_bytes,
    iter_canonical_bytes,
    materialize_volume_file,
    read_volume_row,
    volume_file_counts,
    write_blob,
)
from .content_hash import DEFAULT_HASH_ALGORITHM, normalize_hash_algorithm


REPOSITORY_EXTENSION = ".jvvvrepo"
REPOSITORY_FORMAT = "jvvv-snapshot-repository"
REPOSITORY_FORMAT_VERSION = 1
REPOSITORY_FILE_NAME = "repository.json"
BLOBS_DIR_NAME = "blobs"
SNAPSHOTS_DIR_NAME = "snapshots"
BLOB_EXTENSION = ".blob"
SNAPSHOT_FORMAT = "jvvv-snapshot"
SNAPSHOT_FORMAT_VERSION = 2
SNAPSHOT_FILE_EXTENSION = ".json"
CATALOGUE_META_REPOSITORY_KEY = "snapshot_repository"
CATALOGUE_META_REPOSITORY_UUID_KEY = "snapshot_repository_uuid"
# Link states between a catalogue and its repository (see resolve_repository_link).
LINK_LINKED = "linked"
LINK_MISSING = "missing"
LINK_UNLINKED = "unlinked"
LINK_WRONG = "wrong_repository"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"

ProgressCallback = Callable[["SnapshotProgress"], None]
CancelCallback = Callable[[], bool]


class SnapshotError(Exception):
    """Base class for snapshot repository failures."""


class InvalidRepositoryError(SnapshotError):
    """The directory is not a JVVV snapshot repository, or it is damaged."""


class SnapshotCancelled(SnapshotError):
    """The operation was cancelled by the caller."""


@dataclass(frozen=True)
class SnapshotProgress:
    phase: str
    completed: int
    total: int
    message: str


@dataclass(frozen=True)
class VolumeSnapshotRecord:
    volume_id: int
    uuid: str
    revision: int
    content_blob: str
    history_blob: str
    content_size: int
    history_size: int
    counts: RowCounts
    volume: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.volume.get("name") or "")

    @property
    def drive_id(self) -> str:
        return str(self.volume.get("drive_id") or "")

    def to_json(self) -> dict[str, Any]:
        return {
            "volume_id": self.volume_id,
            "uuid": self.uuid,
            "revision": self.revision,
            "content_blob": self.content_blob,
            "history_blob": self.history_blob,
            "content_size": self.content_size,
            "history_size": self.history_size,
            "counts": self.counts.as_dict(),
            "volume": encode_record(self.volume),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "VolumeSnapshotRecord":
        try:
            volume = decode_record(dict(data["volume"]))
            for column in VOLUME_ROW_COLUMNS:
                volume.setdefault(column, None)
            return cls(
                volume_id=int(data["volume_id"]),
                uuid=str(data["uuid"]),
                revision=int(data["revision"]),
                content_blob=str(data["content_blob"]),
                history_blob=str(data["history_blob"]),
                content_size=int(data.get("content_size", 0)),
                history_size=int(data.get("history_size", 0)),
                counts=RowCounts.from_dict(dict(data.get("counts") or {})),
                volume=volume,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryError(f"A snapshot volume record is malformed: {exc}") from exc


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_id: str
    created_at: str
    label: str
    catalogue_uuid: str
    catalogue_name: str
    next_volume_id: int
    volumes: tuple[VolumeSnapshotRecord, ...]
    hash_algorithm: str = DEFAULT_HASH_ALGORITHM
    hash_algorithm_locked: bool = False

    @property
    def volume_count(self) -> int:
        return len(self.volumes)

    @property
    def file_count(self) -> int:
        return sum(record.counts.files for record in self.volumes)

    @property
    def folder_count(self) -> int:
        return sum(record.counts.folders for record in self.volumes)

    @property
    def blob_digests(self) -> set[str]:
        digests: set[str] = set()
        for record in self.volumes:
            digests.add(record.content_blob)
            digests.add(record.history_blob)
        return digests

    def to_json(self) -> dict[str, Any]:
        return {
            "format": SNAPSHOT_FORMAT,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "label": self.label,
            "catalogue": {
                "uuid": self.catalogue_uuid,
                "name": self.catalogue_name,
                "next_volume_id": self.next_volume_id,
                "hash_algorithm": self.hash_algorithm,
                "hash_algorithm_locked": self.hash_algorithm_locked,
            },
            "totals": {
                "volumes": self.volume_count,
                "files": self.file_count,
                "folders": self.folder_count,
            },
            "volumes": [record.to_json() for record in self.volumes],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SnapshotManifest":
        if data.get("format") != SNAPSHOT_FORMAT:
            raise InvalidRepositoryError("The file is not a JVVV snapshot manifest.")
        version = int(data.get("format_version", 0))
        if version not in (1, SNAPSHOT_FORMAT_VERSION):
            raise InvalidRepositoryError(
                f"The snapshot uses manifest version {version}; this build reads "
                f"version {SNAPSHOT_FORMAT_VERSION}."
            )
        try:
            catalogue = dict(data.get("catalogue") or {})
            return cls(
                snapshot_id=str(data["snapshot_id"]),
                created_at=str(data["created_at"]),
                label=str(data.get("label") or ""),
                catalogue_uuid=str(catalogue.get("uuid") or ""),
                catalogue_name=str(catalogue.get("name") or ""),
                next_volume_id=int(catalogue.get("next_volume_id") or 1),
                hash_algorithm=normalize_hash_algorithm(catalogue.get("hash_algorithm", DEFAULT_HASH_ALGORITHM)),
                hash_algorithm_locked=bool(catalogue.get("hash_algorithm_locked", False)),
                volumes=tuple(
                    VolumeSnapshotRecord.from_json(item) for item in data.get("volumes", [])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryError(f"The snapshot manifest is malformed: {exc}") from exc


@dataclass(frozen=True)
class SnapshotResult:
    repository_path: Path
    manifest: SnapshotManifest
    volumes_written: int
    volumes_reused: int
    blobs_added: int
    bytes_written: int
    raw_bytes_serialised: int
    digests_corrected: tuple[str, ...]
    seconds: float


@dataclass(frozen=True)
class RestoreResult:
    repository_path: Path
    snapshot_id: str
    catalogue_path: Path
    volumes_restored: int
    files_restored: int
    folders_restored: int
    seconds: float


@dataclass(frozen=True)
class VolumeRestoreResult:
    repository_path: Path
    snapshot_id: str
    catalogue_path: Path
    volume_id: int
    uuid: str
    volume_file: Path
    registered: bool


@dataclass(frozen=True)
class PruneResult:
    removed_blobs: int
    bytes_freed: int
    kept_blobs: int
    removed_temp_files: int


@dataclass(frozen=True)
class VerifyResult:
    snapshots_checked: int
    blobs_checked: int
    corrupt_blobs: tuple[str, ...]
    missing_blobs: tuple[tuple[str, str], ...]  # (snapshot_id, digest)
    unreadable_snapshots: tuple[tuple[str, str], ...]  # (file name, error)

    @property
    def ok(self) -> bool:
        return not (self.corrupt_blobs or self.missing_blobs or self.unreadable_snapshots)


@dataclass(frozen=True)
class RepositoryStatistics:
    snapshot_count: int
    blob_count: int
    blob_bytes: int
    unreferenced_blobs: int
    unreferenced_bytes: int


@dataclass
class SnapshotDelta:
    """How one snapshot differs from the one before it."""

    added: list[VolumeSnapshotRecord] = field(default_factory=list)
    removed: list[VolumeSnapshotRecord] = field(default_factory=list)
    content_changed: list[VolumeSnapshotRecord] = field(default_factory=list)
    history_changed: list[VolumeSnapshotRecord] = field(default_factory=list)
    metadata_changed: list[VolumeSnapshotRecord] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return (
            len(self.added)
            + len(self.removed)
            + len(self.content_changed)
            + len(self.history_changed)
            + len(self.metadata_changed)
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def _emit(callback: ProgressCallback | None, phase: str, completed: int, total: int, message: str) -> None:
    if callback is not None:
        callback(SnapshotProgress(phase, int(completed), int(total), message))


def _check_cancel(callback: CancelCallback | None) -> None:
    if callback is not None and callback():
        raise SnapshotCancelled("The snapshot operation was cancelled.")


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.urandom(4).hex()}")
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False)
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise InvalidRepositoryError(f"{path.name} could not be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InvalidRepositoryError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidRepositoryError(f"{path.name} does not contain a JSON object.")
    return data


def repository_path_with_extension(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.suffix.lower() == REPOSITORY_EXTENSION:
        return candidate
    return Path(f"{candidate}{REPOSITORY_EXTENSION}")


def default_repository_path(catalogue_path: str | Path) -> Path:
    """``Name.jvvv`` -> sibling ``Name.jvvvrepo``."""

    catalogue = catalogue_path_with_extension(catalogue_path)
    return catalogue.with_name(f"{catalogue.stem}{REPOSITORY_EXTENSION}")


def new_snapshot_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(4).hex()}"


class SnapshotRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    @property
    def repository_file(self) -> Path:
        return self.path / REPOSITORY_FILE_NAME

    @property
    def blobs_dir(self) -> Path:
        return self.path / BLOBS_DIR_NAME

    @property
    def snapshots_dir(self) -> Path:
        return self.path / SNAPSHOTS_DIR_NAME

    def blob_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InvalidRepositoryError(f"Invalid blob digest: {digest!r}")
        return self.blobs_dir / digest[:2] / f"{digest}{BLOB_EXTENSION}"

    def has_blob(self, digest: str) -> bool:
        try:
            return self.blob_path(digest).is_file()
        except InvalidRepositoryError:
            return False

    def snapshot_path(self, snapshot_id: str) -> Path:
        if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id.startswith("."):
            raise InvalidRepositoryError(f"Invalid snapshot id: {snapshot_id!r}")
        return self.snapshots_dir / f"{snapshot_id}{SNAPSHOT_FILE_EXTENSION}"

    def exists(self) -> bool:
        return self.repository_file.is_file()

    @classmethod
    def create(cls, path: str | Path) -> "SnapshotRepository":
        repository = cls(path)
        if repository.exists():
            raise FileExistsError(f"Snapshot repository already exists: {repository.path}")
        if repository.path.exists() and not repository.path.is_dir():
            raise InvalidRepositoryError(f"The repository path is not a folder: {repository.path}")
        if repository.path.is_dir() and any(repository.path.iterdir()):
            raise InvalidRepositoryError(
                f"The folder is not empty and is not a snapshot repository: {repository.path}"
            )
        repository.path.mkdir(parents=True, exist_ok=True)
        repository.blobs_dir.mkdir(exist_ok=True)
        repository.snapshots_dir.mkdir(exist_ok=True)
        _write_json_atomic(
            repository.repository_file,
            {
                "format": REPOSITORY_FORMAT,
                "format_version": REPOSITORY_FORMAT_VERSION,
                "repository_uuid": new_uuid(),
                "created_at": utc_now(),
                "catalogue_uuid": "",
                "catalogue_name": "",
            },
        )
        return repository

    @classmethod
    def open(cls, path: str | Path) -> "SnapshotRepository":
        repository = cls(path)
        repository.meta()
        return repository

    @classmethod
    def open_or_create(cls, path: str | Path) -> "SnapshotRepository":
        repository = cls(path)
        if repository.exists():
            repository.meta()
            return repository
        return cls.create(path)

    def meta(self) -> dict[str, Any]:
        if not self.repository_file.is_file():
            raise InvalidRepositoryError(
                f"The folder is not a JVVV snapshot repository (no {REPOSITORY_FILE_NAME}): {self.path}"
            )
        data = _read_json(self.repository_file)
        if data.get("format") != REPOSITORY_FORMAT:
            raise InvalidRepositoryError("The repository has an unexpected format marker.")
        version = int(data.get("format_version", 0))
        if version != REPOSITORY_FORMAT_VERSION:
            raise InvalidRepositoryError(
                f"The repository uses format version {version}; this build supports "
                f"version {REPOSITORY_FORMAT_VERSION}."
            )
        self.blobs_dir.mkdir(exist_ok=True)
        self.snapshots_dir.mkdir(exist_ok=True)
        return data

    def _update_meta(self, **values: Any) -> None:
        data = self.meta()
        data.update(values)
        _write_json_atomic(self.repository_file, data)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot_ids(self) -> list[str]:
        if not self.snapshots_dir.is_dir():
            return []
        return sorted(
            entry.name[: -len(SNAPSHOT_FILE_EXTENSION)]
            for entry in self.snapshots_dir.iterdir()
            if entry.is_file()
            and entry.suffix == SNAPSHOT_FILE_EXTENSION
            and ".tmp-" not in entry.name
        )

    def read_snapshot(self, snapshot_id: str) -> SnapshotManifest:
        path = self.snapshot_path(snapshot_id)
        if not path.is_file():
            raise InvalidRepositoryError(f"Snapshot {snapshot_id} does not exist.")
        manifest = SnapshotManifest.from_json(_read_json(path))
        if manifest.snapshot_id != snapshot_id:
            raise InvalidRepositoryError(
                f"Snapshot file {path.name} declares id {manifest.snapshot_id}."
            )
        return manifest

    def list_snapshots(self) -> list[SnapshotManifest]:
        manifests = [self.read_snapshot(snapshot_id) for snapshot_id in self.snapshot_ids()]
        manifests.sort(key=lambda item: (item.created_at, item.snapshot_id))
        return manifests

    def latest_snapshot(self) -> SnapshotManifest | None:
        manifests = self.list_snapshots()
        return manifests[-1] if manifests else None

    @staticmethod
    def delta(current: SnapshotManifest, previous: SnapshotManifest | None) -> SnapshotDelta:
        delta = SnapshotDelta()
        before = {record.uuid: record for record in previous.volumes} if previous else {}
        for record in current.volumes:
            old = before.pop(record.uuid, None)
            if old is None:
                delta.added.append(record)
            elif old.content_blob != record.content_blob:
                delta.content_changed.append(record)
            elif old.history_blob != record.history_blob:
                delta.history_changed.append(record)
            elif old.volume != record.volume:
                delta.metadata_changed.append(record)
        delta.removed.extend(before.values())
        return delta

    def create_snapshot(
        self,
        catalogue_path: str | Path,
        *,
        label: str = "",
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
        deep: bool = False,
    ) -> SnapshotResult:
        """Record the current state of the catalogue folder at *catalogue_path*.

        Only volumes whose blobs are not yet stored are read; with ``deep=True``
        every volume file is re-serialised and its stored digests are checked.
        """

        started = time.monotonic()
        meta = self.meta()
        layout = CatalogueLayout(Path(catalogue_path).expanduser())
        if not layout.exists():
            raise SnapshotError(f"The catalogue folder does not exist: {layout.root}")
        _emit(progress_callback, "prepare", 0, 0, "Reading the catalogue registry")
        registry = open_registry_db(layout.catalogue_db, read_only=True)
        try:
            registry_values = validate_registry(registry)
            entries = read_registry(registry)
            next_volume_id = registry_next_volume_id(registry)
        finally:
            registry.close()
        catalogue_uuid = registry_values["catalogue_uuid"]
        stored_uuid = str(meta.get("catalogue_uuid") or "")
        if stored_uuid and stored_uuid != catalogue_uuid:
            raise SnapshotError(
                "This repository holds snapshots of a different catalogue. Use a separate "
                "repository folder for each catalogue."
            )
        catalogue_name = layout.root.stem if layout.root.suffix else layout.root.name

        records: list[VolumeSnapshotRecord] = []
        volumes_written = 0
        volumes_reused = 0
        blobs_added = 0
        bytes_written = 0
        raw_bytes = 0
        corrected: list[str] = []
        total = len(entries)
        for position, entry in enumerate(entries, start=1):
            _check_cancel(cancel_callback)
            path = layout.volume_file(entry.uuid)
            if not path.is_file():
                raise SnapshotError(
                    f"The registered volume file is missing: {path.name} (volume {entry.volume_id})."
                )
            connection = open_volume_file(path, read_only=True)
            try:
                file_meta = read_volume_meta(connection)
                if file_meta.uuid != entry.uuid:
                    raise SnapshotError(f"{path.name} belongs to volume {file_meta.uuid}.")
                volume_row = read_volume_row(connection)
                counts = volume_file_counts(connection)
                label_text = volume_row.get("drive_id") or volume_row.get("name") or entry.uuid
                _emit(
                    progress_callback,
                    "volumes",
                    position - 1,
                    total,
                    f"Checking {label_text} ({position}/{total})",
                )
                content_digest = file_meta.content_digest
                history_digest = file_meta.history_digest
                content_size = 0
                history_size = 0
                wrote_any = False
                if deep or not content_digest or not self.has_blob(content_digest):
                    _emit(progress_callback, "volumes", position - 1, total, f"Storing content of {label_text}")
                    content_digest, content_size, added, stored = self._store_stream(
                        iter_canonical_bytes(connection, CONTENT_TABLES),
                        expected=file_meta.content_digest,
                        corrected=corrected,
                        label=f"{path.name} content",
                    )
                    raw_bytes += content_size
                    if added:
                        blobs_added += 1
                        bytes_written += stored
                        wrote_any = True
                if deep or not history_digest or not self.has_blob(history_digest):
                    _emit(progress_callback, "volumes", position - 1, total, f"Storing scan history of {label_text}")
                    history_digest, history_size, added, stored = self._store_stream(
                        iter_canonical_bytes(connection, HISTORY_TABLES),
                        expected=file_meta.history_digest,
                        corrected=corrected,
                        label=f"{path.name} history",
                    )
                    raw_bytes += history_size
                    if added:
                        blobs_added += 1
                        bytes_written += stored
                        wrote_any = True
                if wrote_any:
                    volumes_written += 1
                else:
                    volumes_reused += 1
                records.append(
                    VolumeSnapshotRecord(
                        volume_id=entry.volume_id,
                        uuid=entry.uuid,
                        revision=file_meta.revision,
                        content_blob=content_digest,
                        history_blob=history_digest,
                        content_size=content_size,
                        history_size=history_size,
                        counts=counts,
                        volume=volume_row,
                    )
                )
            finally:
                connection.close()
        _emit(progress_callback, "manifest", total, max(total, 1), "Writing the snapshot manifest")
        now = datetime.now(timezone.utc)
        manifest = SnapshotManifest(
            snapshot_id=new_snapshot_id(now),
            created_at=now.strftime(ISO_FORMAT),
            label=label.strip(),
            catalogue_uuid=catalogue_uuid,
            catalogue_name=catalogue_name,
            next_volume_id=next_volume_id,
            volumes=tuple(records),
            hash_algorithm=normalize_hash_algorithm(registry_values.get("hash_algorithm", DEFAULT_HASH_ALGORITHM)),
            hash_algorithm_locked=bool(registry_values.get("hash_algorithm_locked") == "1" or next_volume_id > 1 or entries),
        )
        _write_json_atomic(self.snapshot_path(manifest.snapshot_id), manifest.to_json())
        if not stored_uuid or meta.get("catalogue_name") != catalogue_name:
            self._update_meta(catalogue_uuid=catalogue_uuid, catalogue_name=catalogue_name)
        return SnapshotResult(
            repository_path=self.path,
            manifest=manifest,
            volumes_written=volumes_written,
            volumes_reused=volumes_reused,
            blobs_added=blobs_added,
            bytes_written=bytes_written,
            raw_bytes_serialised=raw_bytes,
            digests_corrected=tuple(corrected),
            seconds=time.monotonic() - started,
        )

    def _store_stream(
        self,
        chunks: Iterable[bytes],
        *,
        expected: str,
        corrected: list[str],
        label: str,
    ) -> tuple[str, int, bool, int]:
        """Write a canonical stream as a blob named by its digest.

        Returns ``(digest, raw_size, added, stored_size)``.  The stream is
        hashed while it is compressed into a temporary file, which is then
        renamed to its digest (or discarded when that blob already exists).
        """

        staging = self.blobs_dir / f".staging-{os.urandom(6).hex()}{BLOB_EXTENSION}"
        tee = HashingTee(chunks)
        try:
            raw, stored = write_blob(tee, staging)
            digest = tee.digest()
            if expected and digest != expected:
                corrected.append(label)
            final = self.blob_path(digest)
            if final.is_file():
                staging.unlink(missing_ok=True)
                return digest, raw, False, 0
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final)
            return digest, raw, True, stored
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    def delete_snapshot(self, snapshot_id: str) -> None:
        path = self.snapshot_path(snapshot_id)
        if not path.is_file():
            raise InvalidRepositoryError(f"Snapshot {snapshot_id} does not exist.")
        path.unlink()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def _iter_blob_files(self) -> Iterator[Path]:
        if not self.blobs_dir.is_dir():
            return
        for bucket in sorted(self.blobs_dir.iterdir(), key=lambda item: item.name):
            if not bucket.is_dir():
                continue
            for entry in sorted(bucket.iterdir(), key=lambda item: item.name):
                if entry.is_file() and entry.suffix == BLOB_EXTENSION:
                    yield entry

    def referenced_blobs(self) -> set[str]:
        digests: set[str] = set()
        for manifest in self.list_snapshots():
            digests |= manifest.blob_digests
        return digests

    def statistics(self) -> RepositoryStatistics:
        self.meta()
        referenced = self.referenced_blobs()
        blob_count = 0
        blob_bytes = 0
        unreferenced = 0
        unreferenced_bytes = 0
        for entry in self._iter_blob_files():
            size = entry.stat().st_size
            blob_count += 1
            blob_bytes += size
            if entry.name[: -len(BLOB_EXTENSION)] not in referenced:
                unreferenced += 1
                unreferenced_bytes += size
        return RepositoryStatistics(
            snapshot_count=len(self.snapshot_ids()),
            blob_count=blob_count,
            blob_bytes=blob_bytes,
            unreferenced_blobs=unreferenced,
            unreferenced_bytes=unreferenced_bytes,
        )

    def prune(self, progress_callback: ProgressCallback | None = None) -> PruneResult:
        """Delete blobs no snapshot references, plus abandoned staging files."""

        self.meta()
        referenced = self.referenced_blobs()
        removed = 0
        freed = 0
        kept = 0
        temp_removed = 0
        blobs = list(self._iter_blob_files())
        for position, entry in enumerate(blobs, start=1):
            _emit(progress_callback, "prune", position, len(blobs), "Removing unreferenced blobs")
            digest = entry.name[: -len(BLOB_EXTENSION)]
            if digest in referenced:
                kept += 1
                continue
            size = entry.stat().st_size
            entry.unlink()
            removed += 1
            freed += size
        for entry in list(self.blobs_dir.iterdir()) if self.blobs_dir.is_dir() else []:
            if entry.is_file() and entry.name.startswith(".staging-"):
                entry.unlink()
                temp_removed += 1
        for entry in list(self.snapshots_dir.iterdir()) if self.snapshots_dir.is_dir() else []:
            if entry.is_file() and ".tmp-" in entry.name:
                entry.unlink()
                temp_removed += 1
        return PruneResult(removed, freed, kept, temp_removed)

    def verify(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
    ) -> VerifyResult:
        """Re-hash every blob and check that every snapshot's blobs exist."""

        self.meta()
        unreadable: list[tuple[str, str]] = []
        manifests: list[SnapshotManifest] = []
        for snapshot_id in self.snapshot_ids():
            try:
                manifests.append(self.read_snapshot(snapshot_id))
            except InvalidRepositoryError as exc:
                unreadable.append((snapshot_id, str(exc)))
        missing: list[tuple[str, str]] = []
        for manifest in manifests:
            for digest in sorted(manifest.blob_digests):
                if not self.has_blob(digest):
                    missing.append((manifest.snapshot_id, digest))
        corrupt: list[str] = []
        blobs = list(self._iter_blob_files())
        for position, entry in enumerate(blobs, start=1):
            _check_cancel(cancel_callback)
            _emit(progress_callback, "verify", position, len(blobs), f"Verifying blobs ({position}/{len(blobs)})")
            digest = entry.name[: -len(BLOB_EXTENSION)]
            try:
                computed, _ = blob_digest(entry)
            except (VolumeStoreError, OSError, EOFError, ValueError, zlib.error) as exc:
                corrupt.append(f"{digest}: {exc}")
                continue
            if computed != digest:
                corrupt.append(digest)
        return VerifyResult(
            snapshots_checked=len(manifests),
            blobs_checked=len(blobs),
            corrupt_blobs=tuple(corrupt),
            missing_blobs=tuple(missing),
            unreadable_snapshots=tuple(unreadable),
        )

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def _require_blobs(self, manifest: SnapshotManifest) -> None:
        missing = sorted(digest for digest in manifest.blob_digests if not self.has_blob(digest))
        if missing:
            raise InvalidRepositoryError(
                f"Snapshot {manifest.snapshot_id} references {len(missing)} blob(s) that are "
                "missing from the repository; it cannot be restored."
            )

    def restore_snapshot(
        self,
        snapshot_id: str,
        target_path: str | Path,
        *,
        overwrite: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
    ) -> RestoreResult:
        """Rebuild a complete catalogue folder from one snapshot.

        Everything is built in a temporary sibling folder and verified before
        the destination is touched; with ``overwrite`` an existing catalogue at
        the destination is replaced only after the new one is complete.
        """

        from .database import Database  # local import: database imports this module's siblings

        started = time.monotonic()
        manifest = self.read_snapshot(snapshot_id)
        self._require_blobs(manifest)
        target = catalogue_path_with_extension(target_path)
        if target.exists() and not overwrite:
            raise FileExistsError(f"Catalogue already exists: {target}")
        if target.exists() and not target.is_dir():
            raise SnapshotError(
                f"The destination exists and is not a catalogue folder: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f"{target.name}.restoring-{os.urandom(4).hex()}")
        layout = CatalogueLayout(staging)
        try:
            _emit(progress_callback, "prepare", 0, 0, "Creating the catalogue folder")
            staging.mkdir()
            layout.volumes_dir.mkdir()
            create_registry_db(
                layout.catalogue_db,
                catalogue_uuid=manifest.catalogue_uuid if is_valid_uuid(manifest.catalogue_uuid) else new_uuid(),
                created_at=utc_now(),
            )
            registry = open_registry_db(layout.catalogue_db)
            try:
                with registry:
                    set_registry_meta(registry, "hash_algorithm", manifest.hash_algorithm)
                    set_registry_meta(registry, "hash_algorithm_locked", "1" if manifest.hash_algorithm_locked else "0")
                total = len(manifest.volumes)
                for position, record in enumerate(manifest.volumes, start=1):
                    _check_cancel(cancel_callback)
                    label_text = record.drive_id or record.name or record.uuid
                    _emit(
                        progress_callback,
                        "volumes",
                        position - 1,
                        total,
                        f"Restoring {label_text} ({position}/{total})",
                    )
                    self._materialize(record, layout.volume_file(record.uuid), manifest.created_at)
                    with registry:
                        register_volume(registry, record.volume_id, record.uuid, manifest.created_at)
                with registry:
                    set_registry_meta(
                        registry,
                        "next_volume_id",
                        str(max(manifest.next_volume_id, registry_next_volume_id(registry))),
                    )
                    # The restored catalogue knows which repository it came from.
                    for key, value in repository_link_settings(target, self.path).items():
                        set_registry_meta(registry, key, value)
            finally:
                registry.close()
            _check_cancel(cancel_callback)
            _emit(progress_callback, "index", 0, 0, "Building the catalogue index")
            db = Database(staging, initialize=True, create=True)
            try:
                db.validate_catalogue()
                db.reconcile(
                    lambda done, total_items, message: _emit(
                        progress_callback, "index", done, total_items, message
                    )
                )
                restored_files = sum(record.counts.files for record in manifest.volumes)
                restored_folders = sum(record.counts.folders for record in manifest.volumes)
            finally:
                db.close()
            _emit(progress_callback, "finalize", 0, 0, "Moving the restored catalogue into place")
            replaced: Path | None = None
            if target.exists():
                replaced = target.with_name(f"{target.name}.replaced-{os.urandom(4).hex()}")
                os.replace(target, replaced)
            try:
                os.replace(staging, target)
            except OSError:
                if replaced is not None and not target.exists():
                    os.replace(replaced, target)
                raise
            if replaced is not None:
                shutil.rmtree(replaced, ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return RestoreResult(
            repository_path=self.path,
            snapshot_id=snapshot_id,
            catalogue_path=target,
            volumes_restored=len(manifest.volumes),
            files_restored=restored_files,
            folders_restored=restored_folders,
            seconds=time.monotonic() - started,
        )

    def _materialize(self, record: VolumeSnapshotRecord, target: Path, exported_at: str, *, revision: int | None = None) -> None:
        try:
            materialize_volume_file(
                target,
                volume_uuid=record.uuid,
                volume_row=record.volume,
                content_chunks=iter_blob_bytes(self.blob_path(record.content_blob)),
                history_chunks=iter_blob_bytes(self.blob_path(record.history_blob)),
                revision=record.revision if revision is None else revision,
                exported_at=exported_at,
                expected_content_blob=record.content_blob,
                expected_history_blob=record.history_blob,
            )
        except (VolumeStoreError, CatalogueLayoutError) as exc:
            raise SnapshotError(
                f"Volume {record.drive_id or record.name or record.uuid} could not be restored: {exc}"
            ) from exc

    def restore_volume(
        self,
        snapshot_id: str,
        volume_uuid: str,
        catalogue_path: str | Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> VolumeRestoreResult:
        """Put one volume back to its state in *snapshot_id* inside a live catalogue.

        The volume file is replaced (or re-created and re-registered when the
        volume had been deleted) with a revision the index has never seen, so
        the next :meth:`jvvv.database.Database.reconcile` re-imports it.  The
        caller must make sure no scan is writing to that catalogue meanwhile.
        """

        manifest = self.read_snapshot(snapshot_id)
        record = next((item for item in manifest.volumes if item.uuid == volume_uuid), None)
        if record is None:
            raise InvalidRepositoryError(
                f"Snapshot {snapshot_id} does not contain volume {volume_uuid}."
            )
        for digest in (record.content_blob, record.history_blob):
            if not self.has_blob(digest):
                raise InvalidRepositoryError(
                    f"Snapshot {snapshot_id} references a blob that is missing from the repository."
                )
        layout = CatalogueLayout(Path(catalogue_path).expanduser())
        if not layout.exists():
            raise SnapshotError(f"The catalogue folder does not exist: {layout.root}")
        registry_check = open_registry_db(layout.catalogue_db, read_only=True)
        try:
            target_algorithm = normalize_hash_algorithm(
                validate_registry(registry_check).get("hash_algorithm", DEFAULT_HASH_ALGORITHM)
            )
        finally:
            registry_check.close()
        if manifest.hash_algorithm != target_algorithm:
            raise SnapshotError(
                "The snapshot uses a different hash algorithm from this catalogue. "
                "Restore the complete snapshot as a separate catalogue instead."
            )
        _emit(progress_callback, "volume", 0, 1, f"Restoring {record.drive_id or record.name or record.uuid}")
        target = layout.volume_file(record.uuid)
        current = volume_store.current_revision(target)
        self._materialize(record, target, manifest.created_at, revision=max(current, record.revision) + 1)
        registered = False
        registry = open_registry_db(layout.catalogue_db)
        try:
            validate_registry(registry)
            entries = {entry.uuid: entry for entry in read_registry(registry)}
            if record.uuid not in entries:
                taken = {entry.volume_id for entry in entries.values()}
                volume_id = record.volume_id
                if volume_id in taken:
                    volume_id = registry_next_volume_id(registry)
                with registry:
                    register_volume(registry, volume_id, record.uuid, utc_now())
                registered = True
            else:
                volume_id = entries[record.uuid].volume_id
        finally:
            registry.close()
        _emit(progress_callback, "volume", 1, 1, "Volume file restored")
        return VolumeRestoreResult(
            repository_path=self.path,
            snapshot_id=snapshot_id,
            catalogue_path=layout.root,
            volume_id=volume_id,
            uuid=record.uuid,
            volume_file=target,
            registered=registered,
        )


# ----------------------------------------------------------------------
# Repository <-> catalogue association
# ----------------------------------------------------------------------

def repository_path_for_catalogue(catalogue_path: str | Path, stored: str | None) -> Path:
    """Resolve the repository a catalogue uses (stored setting or the default)."""

    catalogue = Path(catalogue_path).expanduser()
    if stored:
        candidate = Path(stored).expanduser()
        if not candidate.is_absolute():
            candidate = (catalogue.parent / candidate).resolve(strict=False)
        return candidate
    return default_repository_path(catalogue)


def repository_setting_value(catalogue_path: str | Path, repository_path: str | Path) -> str:
    """Store the repository relative to the catalogue's folder when possible."""

    catalogue = Path(catalogue_path).expanduser().resolve(strict=False)
    repository = Path(repository_path).expanduser().resolve(strict=False)
    try:
        return os.path.relpath(repository, catalogue.parent)
    except ValueError:
        return str(repository)


# ----------------------------------------------------------------------
# Repository link: where a catalogue's repository is, and relinking it
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class RepositoryLink:
    """The relationship between an open catalogue and its snapshot repository.

    ``status`` is one of :data:`LINK_LINKED` (usable at ``path``),
    :data:`LINK_MISSING` (a repository was recorded but nothing is at
    ``recorded_path``), :data:`LINK_UNLINKED` (no repository yet; the first
    snapshot creates ``recorded_path``) or :data:`LINK_WRONG` (a folder is at
    ``recorded_path`` but it is not this catalogue's repository; see ``detail``).
    """

    status: str
    path: Path | None
    recorded_path: Path
    repository_uuid: str
    detail: str = ""

    @property
    def linked(self) -> bool:
        return self.status == LINK_LINKED

    @property
    def offline(self) -> bool:
        return self.status in (LINK_MISSING, LINK_WRONG)


@dataclass(frozen=True)
class RelinkCandidate:
    path: Path
    repository_uuid: str
    same_repository: bool
    snapshot_count: int
    latest_snapshot_id: str

    @property
    def match_label(self) -> str:
        return "same repository" if self.same_repository else "same catalogue"


def _repository_identity(path: Path) -> tuple[str, str]:
    meta = SnapshotRepository(path).meta()
    return str(meta.get("repository_uuid") or ""), str(meta.get("catalogue_uuid") or "")


def resolve_repository_link(catalogue_path: str | Path, meta: dict[str, Any]) -> RepositoryLink:
    """Work out where the catalogue's repository is from its recorded settings."""

    catalogue = Path(catalogue_path).expanduser()
    stored_path = str(meta.get(CATALOGUE_META_REPOSITORY_KEY) or "") or None
    stored_uuid = str(meta.get(CATALOGUE_META_REPOSITORY_UUID_KEY) or "")
    catalogue_uuid = str(meta.get("catalogue_uuid") or "")
    recorded = repository_path_for_catalogue(catalogue, stored_path)
    repository = SnapshotRepository(recorded)
    if not repository.exists():
        if stored_path is None and not stored_uuid:
            return RepositoryLink(LINK_UNLINKED, None, recorded, "")
        return RepositoryLink(LINK_MISSING, None, recorded, stored_uuid)
    try:
        found_uuid, owner = _repository_identity(recorded)
    except InvalidRepositoryError as exc:
        return RepositoryLink(LINK_WRONG, None, recorded, stored_uuid, str(exc))
    if owner and catalogue_uuid and owner != catalogue_uuid:
        return RepositoryLink(
            LINK_WRONG,
            None,
            recorded,
            stored_uuid,
            "the folder holds snapshots of a different catalogue",
        )
    # A different repository for the same catalogue (a clone, or a fresh one
    # created after the old one was lost) is a valid link.
    return RepositoryLink(LINK_LINKED, recorded, recorded, found_uuid)


def repository_link_settings(catalogue_path: str | Path, repository_path: str | Path) -> dict[str, str]:
    """The registry settings that link *catalogue_path* to *repository_path*."""

    repository_uuid, _ = _repository_identity(Path(repository_path))
    return {
        CATALOGUE_META_REPOSITORY_KEY: repository_setting_value(catalogue_path, repository_path),
        CATALOGUE_META_REPOSITORY_UUID_KEY: repository_uuid,
    }


def inspect_relink_target(
    path: str | Path,
    *,
    repository_uuid: str,
    catalogue_uuid: str,
) -> RelinkCandidate:
    """Check that *path* may be relinked as this catalogue's repository."""

    candidate = Path(path).expanduser()
    repository = SnapshotRepository(candidate)
    if not repository.exists():
        raise InvalidRepositoryError(
            f"{candidate} is not a JVVV snapshot repository (no {REPOSITORY_FILE_NAME})."
        )
    found_uuid, owner = _repository_identity(candidate)
    if owner and catalogue_uuid and owner != catalogue_uuid:
        raise SnapshotError(
            f"{candidate.name} holds snapshots of a different catalogue and cannot be linked to this one."
        )
    ids = repository.snapshot_ids()
    return RelinkCandidate(
        path=candidate,
        repository_uuid=found_uuid,
        same_repository=bool(repository_uuid) and found_uuid == repository_uuid,
        snapshot_count=len(ids),
        latest_snapshot_id=ids[-1] if ids else "",
    )


def locate_repository_candidates(
    catalogue_path: str | Path,
    *,
    repository_uuid: str,
    catalogue_uuid: str,
    extra_roots: Iterable[Path] = (),
) -> list[RelinkCandidate]:
    """Find repositories of this catalogue near it (folder, parent, one level down).

    The same repository (by id) sorts first, then the most recently used.
    Repositories of other catalogues are never offered.
    """

    catalogue = Path(catalogue_path).expanduser()
    roots: list[Path] = [catalogue.parent, catalogue.parent.parent, *extra_roots]
    seen: set[Path] = set()
    candidates: list[RelinkCandidate] = []

    def consider(folder: Path) -> None:
        try:
            resolved = folder.resolve(strict=False)
        except OSError:
            return
        if resolved in seen or folder.suffix.lower() != REPOSITORY_EXTENSION:
            return
        seen.add(resolved)
        try:
            candidates.append(
                inspect_relink_target(
                    folder, repository_uuid=repository_uuid, catalogue_uuid=catalogue_uuid
                )
            )
        except (SnapshotError, OSError):
            return

    for root in roots:
        try:
            entries = [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for entry in entries:
            consider(entry)
            if entry.suffix.lower() in (REPOSITORY_EXTENSION, ".jvvv"):
                continue
            try:
                for child in entry.iterdir():
                    if child.is_dir():
                        consider(child)
            except OSError:
                continue
    # Same repository first; within each group the most recently used first.
    candidates.sort(key=lambda item: (item.same_repository, item.latest_snapshot_id), reverse=True)
    return candidates
