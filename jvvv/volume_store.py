"""Move one volume between the working index and its ``.jvvvol`` file.

The index (``index.sqlite``) holds every volume with catalogue-wide integer
ids.  A volume file holds one volume with *local* ids that are dense and
assigned in a deterministic order (folders and files by ``relative_path``,
scan history and errors by their original order).  Because of that, the
canonical serialisation of a volume file is a pure function of its content:
the same folders, files and scan history always produce the same bytes and
therefore the same SHA-256, which is what makes snapshot blobs deduplicate.

Three write paths exist:

* :func:`export_volume_full` - regenerate the whole volume file from the index
  (after a scan or any change to folders/files/media/preview rows);
* :func:`export_volume_history` - replace only the scan history tables;
* :func:`export_volume_metadata` - replace only the single ``volume`` row.

Every write bumps the file's ``revision``; the index remembers the revision it
last synchronised (``index_state``), so a mismatch on open means the index must
be refreshed from the file (:func:`import_volume`).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .catalogue_format import (
    CONTENT_TABLES,
    FILE_COLUMNS,
    FOLDER_COLUMNS,
    HISTORY_TABLES,
    MEDIA_COLUMNS,
    PREVIEW_COLUMNS,
    SCAN_ERROR_COLUMNS,
    SCAN_HISTORY_COLUMNS,
    VOLUME_FILE_FORMAT_VERSION,
    VOLUME_ROW_COLUMNS,
    CatalogueLayoutError,
    VolumeFileMeta,
    initialize_volume_file,
    make_temp_volume_path,
    open_volume_file,
    read_volume_meta,
    set_volume_meta,
)


CANONICAL_FORMAT = "jvvv-canonical-rows"
CANONICAL_FORMAT_VERSION = 1
BLOB_MAGIC = b"JVVVBLOB"
BLOB_FORMAT_VERSION = 1
BLOB_COMPRESSION_LEVEL = 6
EXPORT_BATCH_ROWS = 2000
ProgressCallback = Callable[[int, int, str], None]


class VolumeStoreError(Exception):
    """A volume file could not be written, read or imported."""


@dataclass(frozen=True)
class RowCounts:
    folders: int = 0
    files: int = 0
    file_media_metadata: int = 0
    file_preview_status: int = 0
    scan_history: int = 0
    scan_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "folders": self.folders,
            "files": self.files,
            "file_media_metadata": self.file_media_metadata,
            "file_preview_status": self.file_preview_status,
            "scan_history": self.scan_history,
            "scan_errors": self.scan_errors,
        }

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "RowCounts":
        return cls(**{key: int(values.get(key, 0) or 0) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ExportResult:
    path: Path
    meta: VolumeFileMeta
    counts: RowCounts


@dataclass(frozen=True)
class ImportResult:
    volume_id: int
    meta: VolumeFileMeta
    counts: RowCounts


# --------------------------------------------------------------------------
# Canonical serialisation
# --------------------------------------------------------------------------

def encode_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"b": bytes(value).hex()}
    return value


def decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"b"}:
            return bytes.fromhex(value["b"])
        raise VolumeStoreError("Unexpected structured value in canonical row.")
    return value


def encode_row(values: Sequence[Any]) -> bytes:
    return json.dumps(
        [encode_value(value) for value in values],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def encode_record(values: dict[str, Any]) -> dict[str, Any]:
    return {key: encode_value(value) for key, value in values.items()}


def decode_record(values: dict[str, Any]) -> dict[str, Any]:
    return {key: decode_value(value) for key, value in values.items()}


class CanonicalStream:
    """Produce (and hash) the canonical byte stream of a set of tables."""

    def __init__(self, tables: Sequence[tuple[str, Sequence[str]]]) -> None:
        self.tables = [(name, tuple(columns)) for name, columns in tables]
        self.hasher = hashlib.sha256()
        self.size = 0
        self._chunks: list[bytes] = []

    def _emit(self, data: bytes) -> bytes:
        self.hasher.update(data)
        self.size += len(data)
        return data

    def header(self) -> bytes:
        return self._emit(
            json.dumps(
                {
                    "format": CANONICAL_FORMAT,
                    "version": CANONICAL_FORMAT_VERSION,
                    "tables": [[name, list(columns)] for name, columns in self.tables],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def table(self, name: str, rows: int) -> bytes:
        return self._emit(
            json.dumps({"table": name, "rows": int(rows)}, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )

    def row(self, values: Sequence[Any]) -> bytes:
        return self._emit(encode_row(values))

    def digest(self) -> str:
        return self.hasher.hexdigest()


def _count(connection: sqlite3.Connection, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])


def iter_canonical_bytes(
    connection: sqlite3.Connection,
    tables: Sequence[tuple[str, Sequence[str]]],
    *,
    schema: str = "main",
    stream: CanonicalStream | None = None,
) -> Iterator[bytes]:
    """Yield the canonical stream of *tables* as stored in a volume file.

    Rows are read ordered by their local ids, which the exporters assign in a
    deterministic order, so this equals the stream the exporter hashed.
    """

    stream = stream or CanonicalStream(tables)
    yield stream.header()
    for name, columns in tables:
        order = columns[0]
        total = _count(connection, f"{schema}.{name}")
        yield stream.table(name, total)
        cursor = connection.execute(
            f"SELECT {', '.join(columns)} FROM {schema}.{name} ORDER BY {order}"
        )
        cursor.arraysize = EXPORT_BATCH_ROWS
        while True:
            rows = cursor.fetchmany(EXPORT_BATCH_ROWS)
            if not rows:
                break
            for row in rows:
                yield stream.row(tuple(row))


def canonical_digest(
    connection: sqlite3.Connection,
    tables: Sequence[tuple[str, Sequence[str]]],
    *,
    schema: str = "main",
) -> tuple[str, int]:
    stream = CanonicalStream(tables)
    for _ in iter_canonical_bytes(connection, tables, schema=schema, stream=stream):
        pass
    return stream.digest(), stream.size


def volume_file_digests(path: Path) -> tuple[str, str]:
    """Recompute both digests from a volume file (ignores the stored values)."""

    connection = open_volume_file(path, read_only=True)
    try:
        content, _ = canonical_digest(connection, CONTENT_TABLES)
        history, _ = canonical_digest(connection, HISTORY_TABLES)
        return content, history
    finally:
        connection.close()


def volume_file_counts(connection: sqlite3.Connection, schema: str = "main") -> RowCounts:
    return RowCounts(
        folders=_count(connection, f"{schema}.folders"),
        files=_count(connection, f"{schema}.files"),
        file_media_metadata=_count(connection, f"{schema}.file_media_metadata"),
        file_preview_status=_count(connection, f"{schema}.file_preview_status"),
        scan_history=_count(connection, f"{schema}.scan_history"),
        scan_errors=_count(connection, f"{schema}.scan_errors"),
    )


# --------------------------------------------------------------------------
# Blobs (compressed canonical streams)
# --------------------------------------------------------------------------

def write_blob(chunks: Iterable[bytes], target: Path) -> tuple[int, int]:
    """Write *chunks* zlib-compressed to *target* atomically. Returns (raw, stored)."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp-{os.urandom(6).hex()}")
    compressor = zlib.compressobj(BLOB_COMPRESSION_LEVEL)
    raw = 0
    stored = 0
    try:
        with temp.open("wb") as handle:
            head = BLOB_MAGIC + bytes([BLOB_FORMAT_VERSION])
            handle.write(head)
            stored += len(head)
            for chunk in chunks:
                raw += len(chunk)
                data = compressor.compress(chunk)
                if data:
                    handle.write(data)
                    stored += len(data)
            data = compressor.flush()
            handle.write(data)
            stored += len(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return raw, stored


def iter_blob_bytes(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        head = handle.read(len(BLOB_MAGIC) + 1)
        if head[: len(BLOB_MAGIC)] != BLOB_MAGIC:
            raise VolumeStoreError(f"Not a JVVV blob: {path}")
        version = head[len(BLOB_MAGIC)] if len(head) > len(BLOB_MAGIC) else 0
        if version != BLOB_FORMAT_VERSION:
            raise VolumeStoreError(
                f"Blob {path.name} uses format version {version}; this build reads "
                f"version {BLOB_FORMAT_VERSION}."
            )
        decompressor = zlib.decompressobj()
        while True:
            data = handle.read(1024 * 1024)
            if not data:
                break
            out = decompressor.decompress(data)
            if out:
                yield out
        tail = decompressor.flush()
        if tail:
            yield tail
        if not decompressor.eof:
            raise VolumeStoreError(f"Blob {path.name} is truncated.")


def blob_digest(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    for chunk in iter_blob_bytes(path):
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size


def _iter_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Split a chunk stream into newline-terminated lines in linear time."""

    pending = b""
    for chunk in chunks:
        if pending:
            chunk = pending + chunk
        parts = chunk.split(b"\n")
        pending = parts.pop()
        for part in parts:
            yield part + b"\n"
    if pending:
        yield pending


def read_canonical_tables(
    chunks: Iterable[bytes],
    expected_tables: Sequence[tuple[str, Sequence[str]]],
) -> Iterator[tuple[str, tuple[str, ...], list[tuple[Any, ...]]]]:
    """Parse a canonical stream. Yields (table, columns, rows) in stream order.

    Columns are taken from the stream header so a blob written by an older
    build (fewer columns) still loads: missing columns are filled by the
    schema defaults when the rows are inserted.
    """

    lines = _iter_lines(chunks)
    try:
        header = json.loads(next(lines))
    except StopIteration as exc:
        raise VolumeStoreError("The blob is empty.") from exc
    except json.JSONDecodeError as exc:
        raise VolumeStoreError("The blob header is not valid JSON.") from exc
    if header.get("format") != CANONICAL_FORMAT:
        raise VolumeStoreError("The blob does not contain canonical JVVV rows.")
    if int(header.get("version", 0)) != CANONICAL_FORMAT_VERSION:
        raise VolumeStoreError(
            f"The blob uses canonical format version {header.get('version')}; this build "
            f"reads version {CANONICAL_FORMAT_VERSION}."
        )
    declared = [(str(name), tuple(str(column) for column in columns)) for name, columns in header.get("tables", [])]
    expected_names = [name for name, _ in expected_tables]
    if [name for name, _ in declared] != expected_names:
        raise VolumeStoreError(
            "The blob tables do not match the expected set: "
            f"{[name for name, _ in declared]} != {expected_names}"
        )
    known_columns = {name: set(columns) for name, columns in expected_tables}
    for name, columns in declared:
        unknown = set(columns) - known_columns[name]
        if unknown:
            raise VolumeStoreError(
                f"The blob table {name} has columns this build does not know: "
                + ", ".join(sorted(unknown))
            )
    for name, columns in declared:
        try:
            marker = json.loads(next(lines))
        except StopIteration as exc:
            raise VolumeStoreError(f"The blob ends before table {name}.") from exc
        if marker.get("table") != name:
            raise VolumeStoreError(f"Expected table {name} in blob, found {marker.get('table')!r}.")
        total = int(marker.get("rows", 0))
        rows: list[tuple[Any, ...]] = []
        for _ in range(total):
            try:
                raw = next(lines)
            except StopIteration as exc:
                raise VolumeStoreError(f"The blob table {name} is truncated.") from exc
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) != len(columns):
                raise VolumeStoreError(f"A row of blob table {name} has the wrong shape.")
            rows.append(tuple(decode_value(value) for value in values))
        yield name, columns, rows
    for extra in lines:
        if extra.strip():
            raise VolumeStoreError("The blob has trailing data after the last table.")


# --------------------------------------------------------------------------
# Index -> volume file
# --------------------------------------------------------------------------

def index_volume_row(index: sqlite3.Connection, volume_id: int) -> dict[str, Any]:
    """Compose the single ``volume`` row of a volume file from the index tables."""

    row = index.execute(
        """
        SELECT v.*, r.drive_id, r.is_mirror, r.status, r.condition, r.description,
               r.earliest_content_date, r.latest_content_date, r.connector,
               r.date_added, r.retired_date, r.mirror_date, r.master_volume_id,
               r.created_at AS register_created_at, r.updated_at AS register_updated_at,
               COALESCE(master.uuid, r.master_volume_uuid) AS master_volume_uuid
        FROM volumes v
        LEFT JOIN volume_register r ON r.volume_id = v.id
        LEFT JOIN volumes master ON master.id = r.master_volume_id
        WHERE v.id = ?
        """,
        (int(volume_id),),
    ).fetchone()
    if row is None:
        raise VolumeStoreError(f"Volume {volume_id} does not exist in the index.")
    keys = set(row.keys())
    values: dict[str, Any] = {}
    for column in VOLUME_ROW_COLUMNS:
        if column in keys:
            values[column] = row[column]
        else:
            values[column] = None
    now = row["updated_at"]
    if values["date_added"] is None:
        values["date_added"] = str(now or "")[:10] or "1970-01-01"
    if values["register_created_at"] is None:
        values["register_created_at"] = row["created_at"]
    if values["register_updated_at"] is None:
        values["register_updated_at"] = row["updated_at"]
    for column, default in (
        ("is_mirror", 0),
        ("status", "Archive"),
        ("condition", "Unknown"),
        ("description", ""),
        ("connector", "Unknown"),
        ("source_path", ""),
        ("identity_kind", ""),
        ("identity_token", ""),
        ("identity_label", ""),
        ("identity_serial", ""),
        ("identity_filesystem", ""),
        ("source_relative_path", ""),
    ):
        if values[column] is None:
            values[column] = default
    return values


def _write_volume_row(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    connection.execute("DELETE FROM volume")
    columns = ", ".join(VOLUME_ROW_COLUMNS)
    placeholders = ", ".join("?" for _ in VOLUME_ROW_COLUMNS)
    connection.execute(
        f"INSERT INTO volume ({columns}) VALUES ({placeholders})",
        tuple(values.get(column) for column in VOLUME_ROW_COLUMNS),
    )


def read_volume_row(connection: sqlite3.Connection, schema: str = "main") -> dict[str, Any]:
    row = connection.execute(f"SELECT {', '.join(VOLUME_ROW_COLUMNS)} FROM {schema}.volume").fetchone()
    if row is None:
        raise CatalogueLayoutError("The volume file has no volume row.")
    return {column: row[column] for column in VOLUME_ROW_COLUMNS}


def _insert_many(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> None:
    placeholders = ", ".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        rows,
    )


class _FullExport:
    """Regenerate a volume file from the index rows of one volume."""

    def __init__(
        self,
        index: sqlite3.Connection,
        volume_id: int,
        volume_uuid: str,
        target: sqlite3.Connection,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.index = index
        self.volume_id = int(volume_id)
        self.uuid = volume_uuid
        self.target = target
        self.progress = progress
        self.counts = RowCounts()
        self.content = CanonicalStream(CONTENT_TABLES)
        self.history = CanonicalStream(HISTORY_TABLES)

    def _report(self, done: int, total: int, message: str) -> None:
        if self.progress is not None:
            self.progress(done, total, message)

    def run(self) -> tuple[str, str, RowCounts]:
        counts = {
            "folders": _count(self.index, "folders", "WHERE volume_id = ?", (self.volume_id,)),
            "files": _count(self.index, "files", "WHERE volume_id = ?", (self.volume_id,)),
            "file_media_metadata": int(
                self.index.execute(
                    "SELECT COUNT(*) FROM file_media_metadata m JOIN files f ON f.id = m.file_id WHERE f.volume_id = ?",
                    (self.volume_id,),
                ).fetchone()[0]
            ),
            "file_preview_status": int(
                self.index.execute(
                    "SELECT COUNT(*) FROM file_preview_status p JOIN files f ON f.id = p.file_id WHERE f.volume_id = ?",
                    (self.volume_id,),
                ).fetchone()[0]
            ),
            "scan_history": _count(self.index, "scan_history", "WHERE volume_id = ?", (self.volume_id,)),
            "scan_errors": _count(self.index, "scan_errors", "WHERE volume_id = ?", (self.volume_id,)),
        }
        total_rows = sum(counts.values())
        done = 0

        self.content.header()
        # Folders: dense local ids in relative_path order.  A parent's path is a
        # strict prefix of its children's, so parents always come first.
        folder_map: dict[int, int] = {}
        self.content.table("folders", counts["folders"])
        cursor = self.index.execute(
            f"SELECT {', '.join(FOLDER_COLUMNS)} FROM folders WHERE volume_id = ? ORDER BY relative_path",
            (self.volume_id,),
        )
        cursor.arraysize = EXPORT_BATCH_ROWS
        batch: list[tuple[Any, ...]] = []
        next_id = 1
        while True:
            rows = cursor.fetchmany(EXPORT_BATCH_ROWS)
            if not rows:
                break
            for row in rows:
                local_id = next_id
                next_id += 1
                folder_map[int(row["id"])] = local_id
                parent = row["parent_id"]
                local_parent = folder_map.get(int(parent)) if parent is not None else None
                values = (local_id, local_parent) + tuple(row[column] for column in FOLDER_COLUMNS[2:])
                self.content.row(values)
                batch.append(values)
            _insert_many(self.target, "folders", FOLDER_COLUMNS, batch)
            done += len(batch)
            batch = []
            self._report(done, total_rows, "Writing folders")

        file_map: dict[int, int] = {}
        self.content.table("files", counts["files"])
        cursor = self.index.execute(
            f"SELECT {', '.join(FILE_COLUMNS)} FROM files WHERE volume_id = ? ORDER BY relative_path",
            (self.volume_id,),
        )
        cursor.arraysize = EXPORT_BATCH_ROWS
        next_id = 1
        while True:
            rows = cursor.fetchmany(EXPORT_BATCH_ROWS)
            if not rows:
                break
            for row in rows:
                local_id = next_id
                next_id += 1
                file_map[int(row["id"])] = local_id
                folder = row["folder_id"]
                local_folder = folder_map.get(int(folder)) if folder is not None else None
                values = (local_id, local_folder) + tuple(row[column] for column in FILE_COLUMNS[2:])
                self.content.row(values)
                batch.append(values)
            _insert_many(self.target, "files", FILE_COLUMNS, batch)
            done += len(batch)
            batch = []
            self._report(done, total_rows, "Writing files")

        for table, columns in (("file_media_metadata", MEDIA_COLUMNS), ("file_preview_status", PREVIEW_COLUMNS)):
            self.content.table(table, counts[table])
            # Ordered by the file's local id so the stream is deterministic and
            # equals ``ORDER BY file_id`` when read back from the volume file.
            cursor = self.index.execute(
                f"""
                SELECT {', '.join('t.' + column for column in columns)}
                FROM {table} t JOIN files f ON f.id = t.file_id
                WHERE f.volume_id = ? ORDER BY f.relative_path
                """,
                (self.volume_id,),
            )
            cursor.arraysize = EXPORT_BATCH_ROWS
            while True:
                rows = cursor.fetchmany(EXPORT_BATCH_ROWS)
                if not rows:
                    break
                for row in rows:
                    values = (file_map[int(row["file_id"])],) + tuple(row[column] for column in columns[1:])
                    self.content.row(values)
                    batch.append(values)
                _insert_many(self.target, table, columns, batch)
                done += len(batch)
                batch = []
            self._report(done, total_rows, f"Writing {table.replace('_', ' ')}")

        history_map = _export_history(self.index, self.volume_id, self.target, self.history, counts)
        done += counts["scan_history"] + counts["scan_errors"]
        self._report(done, total_rows, "Writing scan history")
        del history_map
        self.counts = RowCounts.from_dict(counts)
        return self.content.digest(), self.history.digest(), self.counts


def _export_history(
    index: sqlite3.Connection,
    volume_id: int,
    target: sqlite3.Connection,
    stream: CanonicalStream,
    counts: dict[str, int],
) -> dict[int, int]:
    stream.header()
    stream.table("scan_history", counts["scan_history"])
    history_map: dict[int, int] = {}
    rows = list(
        index.execute(
            f"SELECT {', '.join(SCAN_HISTORY_COLUMNS)} FROM scan_history WHERE volume_id = ? ORDER BY id",
            (volume_id,),
        )
    )
    batch: list[tuple[Any, ...]] = []
    for local_id, row in enumerate(rows, start=1):
        history_map[int(row["id"])] = local_id
        values = (local_id,) + tuple(row[column] for column in SCAN_HISTORY_COLUMNS[1:])
        stream.row(values)
        batch.append(values)
    _insert_many(target, "scan_history", SCAN_HISTORY_COLUMNS, batch)

    stream.table("scan_errors", counts["scan_errors"])
    rows = list(
        index.execute(
            f"SELECT {', '.join(SCAN_ERROR_COLUMNS)} FROM scan_errors WHERE volume_id = ? ORDER BY id",
            (volume_id,),
        )
    )
    batch = []
    for local_id, row in enumerate(rows, start=1):
        scan_id = row["scan_id"]
        local_scan = history_map.get(int(scan_id)) if scan_id is not None else None
        values = (local_id, local_scan) + tuple(row[column] for column in SCAN_ERROR_COLUMNS[2:])
        stream.row(values)
        batch.append(values)
    _insert_many(target, "scan_errors", SCAN_ERROR_COLUMNS, batch)
    return history_map


def _finalize_meta(
    target: sqlite3.Connection,
    *,
    revision: int,
    content_digest: str,
    history_digest: str,
    exported_at: str,
) -> None:
    set_volume_meta(
        target,
        revision=revision,
        content_digest=content_digest,
        history_digest=history_digest,
        exported_at=exported_at,
    )


def current_revision(path: Path) -> int:
    if not path.is_file():
        return 0
    connection = open_volume_file(path, read_only=True)
    try:
        return read_volume_meta(connection).revision
    except CatalogueLayoutError:
        return 0
    finally:
        connection.close()


def export_volume_full(
    index: sqlite3.Connection,
    volume_id: int,
    volume_uuid: str,
    final_path: Path,
    *,
    exported_at: str,
    progress: ProgressCallback | None = None,
) -> ExportResult:
    """Write a fresh volume file from the index and atomically replace *final_path*."""

    final_path.parent.mkdir(parents=True, exist_ok=True)
    revision = current_revision(final_path) + 1
    temp = make_temp_volume_path(final_path)
    connection = sqlite3.connect(temp, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        initialize_volume_file(connection, volume_uuid)
        connection.execute("BEGIN")
        _write_volume_row(connection, index_volume_row(index, volume_id))
        exporter = _FullExport(index, volume_id, volume_uuid, connection, progress)
        content_digest, history_digest, counts = exporter.run()
        _finalize_meta(
            connection,
            revision=revision,
            content_digest=content_digest,
            history_digest=history_digest,
            exported_at=exported_at,
        )
        connection.commit()
        connection.close()
        connection = None
        os.replace(temp, final_path)
    except BaseException:
        if connection is not None:
            connection.close()
        temp.unlink(missing_ok=True)
        raise
    meta = VolumeFileMeta(
        uuid=volume_uuid,
        revision=revision,
        content_digest=content_digest,
        history_digest=history_digest,
        exported_at=exported_at,
        format_version=VOLUME_FILE_FORMAT_VERSION,
    )
    return ExportResult(final_path, meta, counts)


def export_volume_metadata(
    index: sqlite3.Connection,
    volume_id: int,
    volume_uuid: str,
    final_path: Path,
    *,
    exported_at: str,
) -> ExportResult:
    """Replace only the ``volume`` row of an existing volume file."""

    if not final_path.is_file():
        return export_volume_full(index, volume_id, volume_uuid, final_path, exported_at=exported_at)
    connection = open_volume_file(final_path)
    try:
        meta = read_volume_meta(connection)
        if meta.uuid != volume_uuid:
            raise VolumeStoreError(
                f"Volume file {final_path.name} belongs to {meta.uuid}, not {volume_uuid}."
            )
        connection.execute("BEGIN IMMEDIATE")
        _write_volume_row(connection, index_volume_row(index, volume_id))
        revision = meta.revision + 1
        set_volume_meta(connection, revision=revision, exported_at=exported_at)
        counts = volume_file_counts(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return ExportResult(
        final_path,
        VolumeFileMeta(
            uuid=volume_uuid,
            revision=revision,
            content_digest=meta.content_digest,
            history_digest=meta.history_digest,
            exported_at=exported_at,
            format_version=meta.format_version,
        ),
        counts,
    )


def export_volume_history(
    index: sqlite3.Connection,
    volume_id: int,
    volume_uuid: str,
    final_path: Path,
    *,
    exported_at: str,
) -> ExportResult:
    """Replace the ``volume`` row and both scan-history tables of an existing file."""

    if not final_path.is_file():
        return export_volume_full(index, volume_id, volume_uuid, final_path, exported_at=exported_at)
    connection = open_volume_file(final_path)
    try:
        meta = read_volume_meta(connection)
        if meta.uuid != volume_uuid:
            raise VolumeStoreError(
                f"Volume file {final_path.name} belongs to {meta.uuid}, not {volume_uuid}."
            )
        connection.execute("BEGIN IMMEDIATE")
        _write_volume_row(connection, index_volume_row(index, volume_id))
        connection.execute("DELETE FROM scan_errors")
        connection.execute("DELETE FROM scan_history")
        counts = {
            "scan_history": _count(index, "scan_history", "WHERE volume_id = ?", (volume_id,)),
            "scan_errors": _count(index, "scan_errors", "WHERE volume_id = ?", (volume_id,)),
        }
        stream = CanonicalStream(HISTORY_TABLES)
        _export_history(index, int(volume_id), connection, stream, counts)
        revision = meta.revision + 1
        history_digest = stream.digest()
        set_volume_meta(
            connection,
            revision=revision,
            history_digest=history_digest,
            exported_at=exported_at,
        )
        all_counts = volume_file_counts(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return ExportResult(
        final_path,
        VolumeFileMeta(
            uuid=volume_uuid,
            revision=revision,
            content_digest=meta.content_digest,
            history_digest=history_digest,
            exported_at=exported_at,
            format_version=meta.format_version,
        ),
        all_counts,
    )


# --------------------------------------------------------------------------
# Volume file -> index
# --------------------------------------------------------------------------

def _attach(index: sqlite3.Connection, path: Path, alias: str) -> None:
    uri = path.resolve(strict=False).as_uri()
    if path.resolve(strict=False).drive.startswith("\\\\"):
        uri = f"file:////{uri[len('file://'):]}"
    index.execute(f"ATTACH DATABASE ? AS {alias}", (f"{uri}?mode=ro",))


def delete_volume_rows(index: sqlite3.Connection, volume_id: int) -> None:
    """Remove every index row of one volume (the ``volumes`` row included)."""

    volume_id = int(volume_id)
    index.execute(
        "DELETE FROM file_media_metadata WHERE file_id IN (SELECT id FROM files WHERE volume_id = ?)",
        (volume_id,),
    )
    index.execute(
        "DELETE FROM file_preview_status WHERE file_id IN (SELECT id FROM files WHERE volume_id = ?)",
        (volume_id,),
    )
    index.execute("DELETE FROM scan_errors WHERE volume_id = ?", (volume_id,))
    index.execute("DELETE FROM scan_history WHERE volume_id = ?", (volume_id,))
    index.execute("DELETE FROM files WHERE volume_id = ?", (volume_id,))
    index.execute("DELETE FROM folders WHERE volume_id = ?", (volume_id,))
    index.execute("DELETE FROM volume_register WHERE volume_id = ?", (volume_id,))
    index.execute("DELETE FROM volumes WHERE id = ?", (volume_id,))


def import_volume(
    index: sqlite3.Connection,
    volume_id: int,
    volume_uuid: str,
    volume_file: Path,
    *,
    progress: ProgressCallback | None = None,
) -> ImportResult:
    """Replace the index rows of *volume_id* with the contents of *volume_file*.

    Must run inside an open transaction on *index*; the file is attached
    read-only, so ``ATTACH`` happens here and the caller must not already be
    inside a transaction when this is called (SQLite forbids attaching within
    one).  Local ids are offset past the current maximum of each index table.
    """

    if index.in_transaction:
        raise VolumeStoreError("import_volume must start its own transaction.")
    _attach(index, volume_file, "vol")
    try:
        meta = read_volume_meta(index, schema="vol")
        if meta.uuid != volume_uuid:
            raise VolumeStoreError(
                f"Volume file {volume_file.name} belongs to {meta.uuid}, not {volume_uuid}."
            )
        volume_row = read_volume_row(index, "vol")
        counts = volume_file_counts(index, "vol")
        index.execute("BEGIN IMMEDIATE")
        try:
            if progress is not None:
                progress(0, 1, f"Importing {volume_row.get('drive_id') or volume_row.get('name') or volume_uuid}")
            delete_volume_rows(index, volume_id)
            _insert_volume_rows(index, int(volume_id), volume_uuid, volume_row)
            fb = int(index.execute("SELECT COALESCE(MAX(id), 0) FROM folders").fetchone()[0])
            xb = int(index.execute("SELECT COALESCE(MAX(id), 0) FROM files").fetchone()[0])
            hb = int(index.execute("SELECT COALESCE(MAX(id), 0) FROM scan_history").fetchone()[0])
            eb = int(index.execute("SELECT COALESCE(MAX(id), 0) FROM scan_errors").fetchone()[0])
            folder_columns = ", ".join(FOLDER_COLUMNS[2:])
            index.execute(
                f"""
                INSERT INTO folders (id, volume_id, parent_id, {folder_columns})
                SELECT id + ?, ?, parent_id + ?, {folder_columns}
                FROM vol.folders ORDER BY id
                """,
                (fb, int(volume_id), fb),
            )
            file_columns = ", ".join(FILE_COLUMNS[2:])
            index.execute(
                f"""
                INSERT INTO files (id, volume_id, folder_id, {file_columns})
                SELECT id + ?, ?, folder_id + ?, {file_columns}
                FROM vol.files ORDER BY id
                """,
                (xb, int(volume_id), fb),
            )
            media_columns = ", ".join(MEDIA_COLUMNS[1:])
            index.execute(
                f"""
                INSERT INTO file_media_metadata (file_id, {media_columns})
                SELECT file_id + ?, {media_columns} FROM vol.file_media_metadata ORDER BY file_id
                """,
                (xb,),
            )
            preview_columns = ", ".join(PREVIEW_COLUMNS[1:])
            index.execute(
                f"""
                INSERT INTO file_preview_status (file_id, {preview_columns})
                SELECT file_id + ?, {preview_columns} FROM vol.file_preview_status ORDER BY file_id
                """,
                (xb,),
            )
            history_columns = ", ".join(SCAN_HISTORY_COLUMNS[1:])
            index.execute(
                f"""
                INSERT INTO scan_history (id, volume_id, {history_columns})
                SELECT id + ?, ?, {history_columns} FROM vol.scan_history ORDER BY id
                """,
                (hb, int(volume_id)),
            )
            error_columns = ", ".join(SCAN_ERROR_COLUMNS[2:])
            index.execute(
                f"""
                INSERT INTO scan_errors (id, scan_id, volume_id, {error_columns})
                SELECT id + ?, scan_id + ?, ?, {error_columns} FROM vol.scan_errors ORDER BY id
                """,
                (eb, hb, int(volume_id)),
            )
            index.execute(
                """
                INSERT INTO index_state (volume_id, uuid, revision, content_digest, history_digest, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(volume_id) DO UPDATE SET
                    uuid = excluded.uuid,
                    revision = excluded.revision,
                    content_digest = excluded.content_digest,
                    history_digest = excluded.history_digest,
                    synced_at = excluded.synced_at
                """,
                (
                    int(volume_id),
                    volume_uuid,
                    meta.revision,
                    meta.content_digest,
                    meta.history_digest,
                    meta.exported_at,
                ),
            )
            index.commit()
        except BaseException:
            index.rollback()
            raise
    finally:
        index.execute("DETACH DATABASE vol")
    if progress is not None:
        progress(1, 1, "Imported")
    return ImportResult(int(volume_id), meta, counts)


def _insert_volume_rows(
    index: sqlite3.Connection,
    volume_id: int,
    volume_uuid: str,
    row: dict[str, Any],
) -> None:
    index.execute(
        """
        INSERT INTO volumes (
            id, uuid, name, source_path, capacity_bytes, used_bytes, free_bytes,
            indexed_file_count, indexed_folder_count, last_scan_at, created_at, updated_at,
            identity_kind, identity_token, identity_label, identity_serial,
            identity_filesystem, source_relative_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            volume_id,
            volume_uuid,
            row["name"],
            row["source_path"] or "",
            int(row["capacity_bytes"] or 0),
            int(row["used_bytes"] or 0),
            int(row["free_bytes"] or 0),
            int(row["indexed_file_count"] or 0),
            int(row["indexed_folder_count"] or 0),
            row["last_scan_at"],
            row["created_at"],
            row["updated_at"],
            row["identity_kind"] or "",
            row["identity_token"] or "",
            row["identity_label"] or "",
            row["identity_serial"] or "",
            row["identity_filesystem"] or "",
            row["source_relative_path"] or "",
        ),
    )
    master_id = None
    master_uuid = row.get("master_volume_uuid")
    if master_uuid:
        found = index.execute("SELECT id FROM volumes WHERE uuid = ?", (master_uuid,)).fetchone()
        master_id = int(found[0]) if found is not None else None
    index.execute(
        """
        INSERT INTO volume_register (
            volume_id, drive_id, is_mirror, status, condition, description,
            earliest_content_date, latest_content_date, connector, date_added,
            retired_date, mirror_date, master_volume_id, master_volume_uuid,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            volume_id,
            row["drive_id"],
            int(row["is_mirror"] or 0),
            row["status"] or "Archive",
            row["condition"] or "Unknown",
            row["description"] or "",
            row["earliest_content_date"],
            row["latest_content_date"],
            row["connector"] or "Unknown",
            row["date_added"],
            row["retired_date"],
            row["mirror_date"],
            master_id,
            master_uuid or None,
            row["register_created_at"],
            row["register_updated_at"],
        ),
    )


def relink_mirror_masters(index: sqlite3.Connection) -> int:
    """Resolve mirror links by UUID after volumes were imported in any order.

    ``volume_register.master_volume_uuid`` is the durable link; the integer
    ``master_volume_id`` is only a cache for joins and may be NULL when the
    master was imported after the mirror.
    """

    cursor = index.execute(
        """
        UPDATE volume_register
        SET master_volume_id = (
            SELECT id FROM volumes WHERE volumes.uuid = volume_register.master_volume_uuid
        )
        WHERE master_volume_uuid IS NOT NULL
          AND master_volume_id IS NOT (
            SELECT id FROM volumes WHERE volumes.uuid = volume_register.master_volume_uuid
          )
        """
    )
    return int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0)


# --------------------------------------------------------------------------
# Snapshot blobs <-> volume files
# --------------------------------------------------------------------------

def materialize_volume_file(
    target: Path,
    *,
    volume_uuid: str,
    volume_row: dict[str, Any],
    content_chunks: Iterable[bytes],
    history_chunks: Iterable[bytes],
    revision: int,
    exported_at: str,
    expected_content_blob: str | None = None,
    expected_history_blob: str | None = None,
) -> ExportResult:
    """Build a volume file from canonical streams (snapshot restore).

    The incoming streams are verified against the blob digests they were
    stored under.  The digests written into ``volume_meta`` are recomputed over
    this build's serialisation, so a blob written by an older build with fewer
    columns restores correctly and simply produces a new blob next time.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = make_temp_volume_path(target)
    connection = sqlite3.connect(temp, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        initialize_volume_file(connection, volume_uuid)
        connection.execute("BEGIN")
        _write_volume_row(connection, volume_row)
        content_stream = CanonicalStream(CONTENT_TABLES)
        history_stream = CanonicalStream(HISTORY_TABLES)
        content_tee = HashingTee(content_chunks)
        history_tee = HashingTee(history_chunks)
        _load_canonical(connection, content_tee, CONTENT_TABLES, content_stream)
        _load_canonical(connection, history_tee, HISTORY_TABLES, history_stream)
        if expected_content_blob and content_tee.digest() != expected_content_blob:
            raise VolumeStoreError("The content blob does not match its recorded digest.")
        if expected_history_blob and history_tee.digest() != expected_history_blob:
            raise VolumeStoreError("The history blob does not match its recorded digest.")
        content_digest = content_stream.digest()
        history_digest = history_stream.digest()
        _finalize_meta(
            connection,
            revision=revision,
            content_digest=content_digest,
            history_digest=history_digest,
            exported_at=exported_at,
        )
        counts = volume_file_counts(connection)
        connection.commit()
        connection.close()
        connection = None
        os.replace(temp, target)
    except BaseException:
        if connection is not None:
            connection.close()
        temp.unlink(missing_ok=True)
        raise
    return ExportResult(
        target,
        VolumeFileMeta(
            uuid=volume_uuid,
            revision=revision,
            content_digest=content_digest,
            history_digest=history_digest,
            exported_at=exported_at,
            format_version=VOLUME_FILE_FORMAT_VERSION,
        ),
        counts,
    )


class HashingTee:
    """Iterate chunks while hashing them (verifies a blob against its name)."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = chunks
        self.hasher = hashlib.sha256()

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.hasher.update(chunk)
            yield chunk

    def digest(self) -> str:
        return self.hasher.hexdigest()


def _load_canonical(
    connection: sqlite3.Connection,
    chunks: Iterable[bytes],
    tables: Sequence[tuple[str, Sequence[str]]],
    stream: CanonicalStream,
) -> None:
    """Insert canonical rows and re-hash them so the stored digest is verified."""

    # Re-serialising the rows in this build's column order lets a blob written
    # with fewer columns still verify: the digest is recomputed on the rows we
    # actually store, and the caller compares it against the manifest.
    stream.header()
    for name, columns, rows in read_canonical_tables(chunks, tables):
        stream.table(name, len(rows))
        full_columns = dict(tables)[name]
        if tuple(columns) == tuple(full_columns):
            for row in rows:
                stream.row(row)
            _insert_many(connection, name, columns, rows)
        else:
            position = {column: index for index, column in enumerate(columns)}
            widened = [
                tuple(row[position[column]] if column in position else None for column in full_columns)
                for row in rows
            ]
            for row in widened:
                stream.row(row)
            _insert_many(connection, name, full_columns, widened)
