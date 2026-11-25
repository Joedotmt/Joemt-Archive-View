from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from itertools import groupby
import json
import sqlite3
from typing import Callable, Iterable, TYPE_CHECKING

from .content_hash import HASH_LABELS, HASH_LENGTHS

if TYPE_CHECKING:
    from .database import Database

RULES_VERSION = 5
DETAIL_PAGE_SIZE = 100
_BATCH_SIZE = 4096
_PROTECTED_ROOTS = {"$recycle.bin", "recycler", "system volume information"}
_HASH_ERROR_PREFIXES = tuple(f"{label} hash unavailable:" for label in HASH_LABELS.values())

# Keep the original index schema readable by the previous portable build. The
# current rules use the shared run/state tables and the additive tables below.
ANALYSIS_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "backup_analysis_runs": (
        "id",
        "started_at",
        "completed_at",
        "status",
        "rules_version",
        "source_signature",
        "files_analyzed",
        "folders_analyzed",
        "likely_files",
        "possible_files",
        "ambiguous_files",
        "excluded_files",
        "single_files",
        "message",
    ),
    "backup_analysis_state": (
        "id",
        "active_run_id",
        "forced_stale",
        "stale_reason",
        "updated_at",
    ),
    "backup_analysis_volume_snapshots": (
        "run_id",
        "volume_id",
        "drive_id",
        "last_scan_at",
        "indexed_file_count",
        "indexed_folder_count",
    ),
    "backup_file_results": (
        "run_id",
        "file_id",
        "volume_id",
        "status",
        "other_volume_ids",
        "evidence_text",
        "strong_volume_ids",
        "possible_volume_ids",
        "verified_volume_ids",
    ),
    "backup_folder_results": (
        "run_id",
        "folder_id",
        "volume_id",
        "status",
        "other_volume_ids",
        "evidence_text",
        "best_target_volume_id",
        "matched_files",
        "total_files",
        "matched_bytes",
        "total_bytes",
        "best_coverage_files_percent",
        "best_coverage_bytes_percent",
        "scattered",
    ),
    "backup_folder_drive_matches": (
        "run_id",
        "folder_id",
        "target_volume_id",
        "status",
        "matched_files",
        "total_files",
        "matched_bytes",
        "total_bytes",
        "evidence_text",
    ),
    "backup_volume_results": (
        "run_id",
        "volume_id",
        "status",
        "health_status",
        "coverage_eligible",
        "total_files",
        "total_bytes",
        "coverage_files",
        "coverage_bytes",
        "likely_files",
        "likely_bytes",
        "possible_files",
        "possible_bytes",
        "ambiguous_files",
        "ambiguous_bytes",
        "excluded_files",
        "excluded_bytes",
        "single_files",
        "single_bytes",
        "likely_files_percent",
        "likely_bytes_percent",
        "latest_scan_status",
        "latest_scan_errors",
    ),
    "backup_mirror_candidates": (
        "run_id",
        "source_volume_id",
        "target_volume_id",
        "source_coverage_percent",
        "target_coverage_percent",
        "matched_files",
        "complete_structure",
        "evidence_text",
        "manual_mirror_link",
    ),
    "backup_analysis_invalidations": (
        "id",
        "volume_id",
        "reason",
        "created_at",
    ),
}

ANALYSIS_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS backup_analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL,
        rules_version INTEGER NOT NULL,
        source_signature TEXT NOT NULL,
        files_analyzed INTEGER NOT NULL DEFAULT 0,
        folders_analyzed INTEGER NOT NULL DEFAULT 0,
        likely_files INTEGER NOT NULL DEFAULT 0,
        possible_files INTEGER NOT NULL DEFAULT 0,
        ambiguous_files INTEGER NOT NULL DEFAULT 0,
        excluded_files INTEGER NOT NULL DEFAULT 0,
        single_files INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_analysis_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        active_run_id INTEGER REFERENCES backup_analysis_runs(id) ON DELETE SET NULL,
        forced_stale INTEGER NOT NULL DEFAULT 0,
        stale_reason TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_analysis_volume_snapshots (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        volume_id INTEGER NOT NULL,
        drive_id TEXT NOT NULL DEFAULT '',
        last_scan_at TEXT,
        indexed_file_count INTEGER NOT NULL DEFAULT 0,
        indexed_folder_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, volume_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_file_results (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        file_id INTEGER NOT NULL,
        volume_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        other_volume_ids TEXT NOT NULL DEFAULT '[]',
        evidence_text TEXT NOT NULL DEFAULT '',
        strong_volume_ids TEXT NOT NULL DEFAULT '[]',
        possible_volume_ids TEXT NOT NULL DEFAULT '[]',
        verified_volume_ids TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, file_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_folder_results (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        folder_id INTEGER NOT NULL,
        volume_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        other_volume_ids TEXT NOT NULL DEFAULT '[]',
        evidence_text TEXT NOT NULL DEFAULT '',
        best_target_volume_id INTEGER,
        matched_files INTEGER NOT NULL DEFAULT 0,
        total_files INTEGER NOT NULL DEFAULT 0,
        matched_bytes INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        best_coverage_files_percent REAL,
        best_coverage_bytes_percent REAL,
        scattered INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, folder_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_folder_drive_matches (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        folder_id INTEGER NOT NULL,
        target_volume_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        matched_files INTEGER NOT NULL DEFAULT 0,
        total_files INTEGER NOT NULL DEFAULT 0,
        matched_bytes INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        evidence_text TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, folder_id, target_volume_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_volume_results (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        volume_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        health_status TEXT NOT NULL,
        coverage_eligible INTEGER NOT NULL DEFAULT 0,
        total_files INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        coverage_files INTEGER NOT NULL DEFAULT 0,
        coverage_bytes INTEGER NOT NULL DEFAULT 0,
        likely_files INTEGER NOT NULL DEFAULT 0,
        likely_bytes INTEGER NOT NULL DEFAULT 0,
        possible_files INTEGER NOT NULL DEFAULT 0,
        possible_bytes INTEGER NOT NULL DEFAULT 0,
        ambiguous_files INTEGER NOT NULL DEFAULT 0,
        ambiguous_bytes INTEGER NOT NULL DEFAULT 0,
        excluded_files INTEGER NOT NULL DEFAULT 0,
        excluded_bytes INTEGER NOT NULL DEFAULT 0,
        single_files INTEGER NOT NULL DEFAULT 0,
        single_bytes INTEGER NOT NULL DEFAULT 0,
        likely_files_percent REAL,
        likely_bytes_percent REAL,
        latest_scan_status TEXT,
        latest_scan_errors INTEGER,
        PRIMARY KEY (run_id, volume_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_mirror_candidates (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        source_volume_id INTEGER NOT NULL,
        target_volume_id INTEGER NOT NULL,
        source_coverage_percent REAL NOT NULL DEFAULT 0,
        target_coverage_percent REAL NOT NULL DEFAULT 0,
        matched_files INTEGER NOT NULL DEFAULT 0,
        complete_structure INTEGER NOT NULL DEFAULT 0,
        evidence_text TEXT NOT NULL DEFAULT '',
        manual_mirror_link INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, source_volume_id, target_volume_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_analysis_invalidations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        volume_id INTEGER,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_backup_file_results_item ON backup_file_results(file_id, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_backup_folder_results_item ON backup_folder_results(folder_id, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_backup_volume_results_volume ON backup_volume_results(volume_id, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_backup_folder_matches_item ON backup_folder_drive_matches(folder_id, run_id)",
)

_HASH_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS backup_hash_records (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        file_id INTEGER NOT NULL,
        volume_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        hash_key BLOB,
        PRIMARY KEY (run_id, file_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_hash_location
        ON backup_hash_records(run_id, hash_key, volume_id, path, file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_hash_path
        ON backup_hash_records(run_id, volume_id, path, file_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_hash_drives (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        hash_key BLOB NOT NULL,
        volume_id INTEGER NOT NULL,
        PRIMARY KEY (run_id, hash_key, volume_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_hash_trees (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        folder_id INTEGER NOT NULL,
        volume_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        total_files INTEGER NOT NULL,
        missing_hashes INTEGER NOT NULL,
        incomplete INTEGER NOT NULL,
        fingerprint BLOB,
        PRIMARY KEY (run_id, folder_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_hash_tree_group
        ON backup_hash_trees(run_id, fingerprint, volume_id, path, folder_id)
        WHERE fingerprint IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_hash_coverage (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        folder_id INTEGER NOT NULL,
        target_volume_id INTEGER NOT NULL,
        matched_files INTEGER NOT NULL,
        PRIMARY KEY (run_id, folder_id, target_volume_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_hash_scans (
        run_id INTEGER NOT NULL REFERENCES backup_analysis_runs(id) ON DELETE CASCADE,
        volume_id INTEGER NOT NULL,
        scanned_at TEXT,
        incomplete INTEGER NOT NULL,
        missing_hashes INTEGER NOT NULL,
        PRIMARY KEY (run_id, volume_id)
    ) WITHOUT ROWID
    """,
)
ANALYSIS_SCHEMA_SQL += _HASH_SCHEMA_SQL

_STAGE_COLUMNS = {
    "records": "file_id INTEGER PRIMARY KEY, volume_id INTEGER, folder_id INTEGER, path TEXT, hash_key BLOB",
    "drives": "hash_key BLOB, volume_id INTEGER, PRIMARY KEY(hash_key, volume_id)",
    "trees": "folder_id INTEGER PRIMARY KEY, volume_id INTEGER, path TEXT, total_files INTEGER, "
             "missing_hashes INTEGER, incomplete INTEGER, fingerprint BLOB",
    "coverage": "folder_id INTEGER, target_volume_id INTEGER, matched_files INTEGER",
    "scans": "volume_id INTEGER PRIMARY KEY, scanned_at TEXT, incomplete INTEGER, missing_hashes INTEGER",
}


@dataclass(frozen=True)
class AnalysisProgress:
    phase: str
    completed: int
    total: int
    message: str


@dataclass(frozen=True)
class AnalysisSummary:
    status: str
    run_id: int | None = None
    message: str = ""


@dataclass(frozen=True)
class AnalysisState:
    status: str
    active_run_id: int | None = None
    analysed_at: str | None = None
    is_stale: bool = False
    stale_reason: str = ""
    rules_version: int | None = None


@dataclass(frozen=True)
class ItemBackupStatus:
    item_type: str
    item_id: int
    status: str
    total_files: int = 0
    matched_files: int = 0
    missing_hashes: int = 0
    scan_incomplete: bool = False
    best_target_volume_id: int | None = None
    other_drive_count: int = 0


@dataclass(frozen=True)
class BackupDrive:
    volume_id: int
    name: str
    scanned_at: str | None
    matched_files: int
    total_files: int
    full_match: bool
    scan_incomplete: bool
    missing_hashes: int


@dataclass(frozen=True)
class BackupLocation:
    item_type: str
    item_id: int
    volume_id: int
    relative_path: str


@dataclass(frozen=True)
class BackupFileDetail:
    source_path: str
    status: str
    target: BackupLocation | None


@dataclass(frozen=True)
class VolumeBackupSummary:
    volume_id: int
    item: ItemBackupStatus


@dataclass
class _Folder:
    volume_id: int
    parent_id: int | None
    path: str
    name: str
    children: list[int] = field(default_factory=list)
    incomplete: bool = False
    total_files: int = 0
    missing_hashes: int = 0
    direct_fingerprint: bytes = b""


@dataclass
class _Scan:
    scanned_at: str | None
    error_count: int
    recorded_errors: int = 0
    unlocated_error: bool = False
    errors: set[str] = field(default_factory=set)
    ordered_errors: list[str] = field(default_factory=list)

    @property
    def untrustworthy(self) -> bool:
        return (
            self.scanned_at is None or self.unlocated_error
            or self.error_count > self.recorded_errors
        )

    def incomplete_at(self, folder_path: str) -> bool:
        if self.untrustworthy:
            return True
        prefix = folder_path.casefold()
        if not prefix:
            return bool(self.errors)
        nested_prefix = prefix + "/"
        index = bisect_left(self.ordered_errors, nested_prefix)
        if index < len(self.ordered_errors) and self.ordered_errors[index].startswith(nested_prefix):
            return True
        while prefix:
            if prefix in self.errors:
                return True
            prefix = prefix.rpartition("/")[0]
        return False


class _Cancelled(Exception):
    pass


class _SourceChanged(Exception):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f%z")


def _path(value: str) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _protected(path: str) -> bool:
    return path.partition("/")[0].casefold() in _PROTECTED_ROOTS


def _hash_key(algorithm: str | None, digest: bytes | None) -> bytes | None:
    algorithm = str(algorithm or "").strip().casefold()
    if (
        not isinstance(digest, bytes) or algorithm not in HASH_LENGTHS
        or len(digest) != HASH_LENGTHS[algorithm]
    ):
        return None
    return algorithm.encode("ascii") + b"\0" + digest


def _entry(hasher, kind: bytes, name: str, value: bytes) -> None:
    name_bytes = name.encode("utf-8")
    hasher.update(kind)
    hasher.update(len(name_bytes).to_bytes(8, "big"))
    hasher.update(name_bytes)
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _ids_in_chunks(values: Iterable[int]):
    values = tuple(dict.fromkeys(int(value) for value in values))
    for start in range(0, len(values), 500):
        yield values[start:start + 500]


def _file_status(row: sqlite3.Row, count: int) -> ItemBackupStatus:
    if row["hash_key"] is None:
        return ItemBackupStatus("file", row["file_id"], "needs_rescan", total_files=1, missing_hashes=1)
    return ItemBackupStatus(
        "file", row["file_id"], "hash_match" if count else "no_match",
        total_files=1, matched_files=int(bool(count)), other_drive_count=count,
    )


def _tree_status(
    row: sqlite3.Row, best: sqlite3.Row | None, drive_count: int,
) -> ItemBackupStatus:
    total = row["total_files"]
    missing = row["missing_hashes"]
    incomplete = bool(row["incomplete"])
    matched = best["matched_files"] if best is not None else 0
    if best is not None and best["full_match"]:
        status = "full_match"
    elif matched == total and total and not incomplete:
        status = "all_contents"
    elif matched:
        status = "partial"
    elif incomplete:
        status = "scan_incomplete"
    elif not total:
        status = "no_files"
    elif missing == total:
        status = "needs_rescan"
    else:
        status = "no_match"
    return ItemBackupStatus(
        "folder", row["folder_id"], status, total, matched, missing, incomplete,
        best["target_volume_id"] if best is not None else None, drive_count,
    )


class BackupAnalysisEngine:
    """Compare file hashes and exact directory trees within the saved catalogue."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.connection = db.connection

    def ensure_schema(self) -> None:
        with self.db.transaction():
            for statement in _HASH_SCHEMA_SQL:
                self.connection.execute(statement)

    def _source_signature(self) -> str:
        # The previous portable build uses this same signature. The rules
        # version distinguishes its evidence from the hash-only results.
        payload = [list(row) for row in self.connection.execute(
            "SELECT id, last_scan_at, COALESCE(indexed_file_count, 0), "
            "COALESCE(indexed_folder_count, 0) FROM volumes ORDER BY id"
        )]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def state(self) -> AnalysisState:
        row = self.connection.execute(
            """
            SELECT s.active_run_id, s.forced_stale, s.stale_reason,
                   r.completed_at, r.rules_version, r.source_signature
            FROM backup_analysis_state s
            JOIN backup_analysis_runs r ON r.id = s.active_run_id
            WHERE s.id = 1
            """
        ).fetchone()
        if row is None:
            return AnalysisState("not_analyzed")
        reason = ""
        if row["rules_version"] != RULES_VERSION:
            reason = "The comparison rules changed."
        elif row["forced_stale"]:
            reason = row["stale_reason"] or "The catalogue changed."
        elif row["source_signature"] != self._source_signature():
            reason = "The catalogue changed."
        return AnalysisState(
            "outdated" if reason else "completed", row["active_run_id"], row["completed_at"],
            bool(reason), reason, row["rules_version"],
        )

    def invalidate_all(self, reason: str = "Catalogue contents changed.") -> None:
        now = _utc_now()
        with self.db.transaction():
            self.connection.execute(
                "INSERT INTO backup_analysis_invalidations (volume_id, reason, created_at) VALUES (NULL, ?, ?)",
                (reason, now),
            )
            self.connection.execute(
                """
                UPDATE backup_analysis_state
                SET forced_stale = active_run_id IS NOT NULL,
                    stale_reason = CASE WHEN active_run_id IS NULL THEN '' ELSE ? END,
                    updated_at = ?
                WHERE id = 1
                """,
                (reason, now),
            )

    def _watermark(self) -> int:
        return self.connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM backup_analysis_invalidations"
        ).fetchone()[0]

    def _scan_health(self) -> dict[int, _Scan]:
        rows = self.connection.execute(
            """
            SELECT v.id AS volume_id, s.id AS scan_id,
                   CASE WHEN s.id IS NOT NULL
                        THEN COALESCE(v.last_scan_at, s.finished_at, s.started_at) END AS scanned_at,
                   COALESCE(s.errors_count, 0) AS errors_count
            FROM volumes v LEFT JOIN scan_history s ON s.id = (
                SELECT id FROM scan_history
                WHERE volume_id = v.id AND status IN ('completed', 'completed_with_warnings')
                ORDER BY id DESC LIMIT 1
            )
            """
        )
        scans = {}
        by_scan: dict[int, _Scan] = {}
        for row in rows:
            scan = _Scan(row["scanned_at"], row["errors_count"])
            scans[row["volume_id"]] = scan
            if row["scan_id"] is not None:
                by_scan[row["scan_id"]] = scan
        for ids in _ids_in_chunks(by_scan):
            placeholders = ",".join("?" for _ in ids)
            for row in self.connection.execute(
                f"SELECT scan_id, path, message FROM scan_errors WHERE scan_id IN ({placeholders})", ids,
            ):
                scan = by_scan[row["scan_id"]]
                scan.recorded_errors += 1
                raw_path = (row["path"] or "").replace("\\", "/")
                path = _path(raw_path)
                if _protected(path) or (row["message"] or "").startswith(_HASH_ERROR_PREFIXES):
                    continue
                if path in {"", "."} or raw_path.startswith("/") or ":" in path or ".." in path.split("/"):
                    scan.unlocated_error = True
                else:
                    scan.errors.add(path.casefold())
        for scan in scans.values():
            scan.ordered_errors = sorted(scan.errors)
        return scans

    def scan_date(self, volume_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT last_scan_at FROM volumes WHERE id = ?", (volume_id,),
        ).fetchone()
        return row["last_scan_at"] if row is not None else None

    def analyse(
        self,
        progress_callback: Callable[[AnalysisProgress], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> AnalysisSummary:
        def check_cancel() -> None:
            if cancel_callback is not None and cancel_callback():
                raise _Cancelled

        def report(phase: str, completed: int, total: int, message: str) -> None:
            check_cancel()
            if progress_callback is not None:
                progress_callback(AnalysisProgress(phase, completed, total, message))
            check_cancel()

        conn = self.connection
        started = _utc_now()
        try:
            check_cancel()
            self.ensure_schema()
            self._clear_staging()
            if cancel_callback is not None:
                conn.set_progress_handler(lambda: int(cancel_callback()), 1000)
            with self.db.transaction():
                signature, watermark = self._source_signature(), self._watermark()
                scans = self._scan_health()
                for table, columns in _STAGE_COLUMNS.items():
                    conn.execute(f"CREATE TEMP TABLE copies_{table} ({columns})")
                folders = self._read_folders(scans, check_cancel)
                file_count = self._stage_files(folders, report)
                self._stage_trees(folders, scans, report)
            report("publish", 0, 0, "Saving comparison")
            run_id = self._publish(signature, watermark, started, file_count, len(folders), check_cancel)
            return AnalysisSummary("completed", run_id)
        except _SourceChanged:
            return AnalysisSummary("discarded", message="The catalogue changed. Compare saved hashes again.")
        except _Cancelled:
            return AnalysisSummary("cancelled")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                check_requested = cancel_callback is not None and cancel_callback()
                if check_requested:
                    return AnalysisSummary("cancelled")
            raise
        finally:
            conn.set_progress_handler(None, 0)
            try:
                self._clear_staging()
            except sqlite3.Error:
                # Staging belongs to this connection. A late interrupt must
                # not change the outcome of an already published run.
                pass

    def _clear_staging(self) -> None:
        for table in _STAGE_COLUMNS:
            self.connection.execute(f"DROP TABLE IF EXISTS temp.copies_{table}")

    def _read_folders(self, scans: dict[int, _Scan], check_cancel) -> dict[int, _Folder]:
        folders = {}
        for row in self.connection.execute(
            "SELECT id, volume_id, parent_id, name, relative_path FROM folders WHERE missing = 0"
        ):
            check_cancel()
            path = _path(row["relative_path"])
            if not _protected(path):
                folders[row["id"]] = _Folder(
                    row["volume_id"], row["parent_id"], path, row["name"],
                    incomplete=scans[row["volume_id"]].incomplete_at(path),
                )
        for folder_id, folder in folders.items():
            if folder.parent_id in folders:
                folders[folder.parent_id].children.append(folder_id)
        return folders

    def _stage_files(self, folders: dict[int, _Folder], report) -> int:
        conn = self.connection
        total = conn.execute("SELECT COUNT(*) FROM files WHERE missing = 0").fetchone()[0]
        report("files", 0, total, "Comparing saved file hashes")
        records = []
        included = processed = 0
        rows = conn.execute(
            """
            SELECT f.id, f.volume_id, f.name, f.relative_path, f.content_hash_algorithm, f.content_hash,
                   COALESCE(f.folder_id, (SELECT root.id FROM folders root
                       WHERE root.volume_id = f.volume_id AND root.parent_id IS NULL
                         AND root.missing = 0)) AS parent_folder_id
            FROM files f WHERE f.missing = 0
            ORDER BY parent_folder_id, f.name COLLATE BINARY, f.id
            """
        )
        for folder_id, group in groupby(rows, key=lambda row: row["parent_folder_id"]):
            folder = folders.get(folder_id)
            direct = hashlib.sha256()
            for row in group:
                processed += 1
                path = _path(row["relative_path"])
                if not _protected(path):
                    key = _hash_key(row["content_hash_algorithm"], row["content_hash"])
                    records.append((row["id"], row["volume_id"], folder_id, path, key))
                    included += 1
                    if folder is not None:
                        folder.total_files += 1
                        if key is None:
                            folder.missing_hashes += 1
                        else:
                            _entry(direct, b"f", row["name"], key)
                if processed % _BATCH_SIZE == 0:
                    report("files", processed, total, "Comparing saved file hashes")
                    conn.executemany("INSERT INTO copies_records VALUES (?, ?, ?, ?, ?)", records)
                    records.clear()
            if folder is not None:
                folder.direct_fingerprint = direct.digest()
        conn.executemany("INSERT INTO copies_records VALUES (?, ?, ?, ?, ?)", records)
        conn.execute(
            "INSERT INTO copies_drives SELECT DISTINCT hash_key, volume_id "
            "FROM copies_records WHERE hash_key IS NOT NULL"
        )
        report("files", total, total, "File hashes compared")
        return included

    def _stage_trees(self, folders: dict[int, _Folder], scans: dict[int, _Scan], report) -> None:
        conn = self.connection
        report("folders", 0, len(folders), "Checking folder trees")
        coverage: dict[int, Counter] = defaultdict(Counter)
        for row in conn.execute(
            """
            WITH weights AS (
                SELECT folder_id, volume_id, hash_key, COUNT(*) AS files
                FROM copies_records WHERE hash_key IS NOT NULL AND folder_id IS NOT NULL
                GROUP BY folder_id, volume_id, hash_key
            )
            SELECT w.folder_id, d.volume_id AS target, SUM(w.files) AS matched
            FROM weights w JOIN copies_drives d USING (hash_key)
            WHERE d.volume_id != w.volume_id GROUP BY w.folder_id, d.volume_id
            """
        ):
            coverage[row["folder_id"]][row["target"]] = row["matched"]

        empty_files = hashlib.sha256().digest()
        fingerprints = {}
        tree_rows = []
        coverage_rows = []
        scan_missing = {}
        ordered_ids = sorted(folders, key=lambda key: (bool(folders[key].path), folders[key].path.count("/")), reverse=True)
        for position, folder_id in enumerate(ordered_ids, 1):
            folder = folders[folder_id]
            counts = coverage.pop(folder_id, Counter())
            hasher = hashlib.sha256()
            hasher.update(folder.direct_fingerprint or empty_files)
            for child_id in sorted(folder.children, key=lambda key: folders[key].name):
                child = folders[child_id]
                folder.total_files += child.total_files
                folder.missing_hashes += child.missing_hashes
                folder.incomplete |= child.incomplete
                _entry(hasher, b"d", child.name, fingerprints.pop(child_id))
            fingerprint = hasher.digest()
            if folder.parent_id in folders:
                fingerprints[folder_id] = fingerprint
                if counts:
                    coverage[folder.parent_id].update(counts)
            elif not folder.path:
                scan_missing[folder.volume_id] = folder.missing_hashes
            trusted = folder.total_files and not folder.missing_hashes and not folder.incomplete
            tree_rows.append((
                folder_id, folder.volume_id, folder.path, folder.total_files, folder.missing_hashes,
                folder.incomplete, fingerprint if trusted else None,
            ))
            coverage_rows.extend((folder_id, target, count) for target, count in counts.items())
            if position % _BATCH_SIZE == 0 or len(coverage_rows) >= _BATCH_SIZE:
                report("folders", position, len(folders), "Checking folder trees")
                conn.executemany("INSERT INTO copies_trees VALUES (?, ?, ?, ?, ?, ?, ?)", tree_rows)
                conn.executemany("INSERT INTO copies_coverage VALUES (?, ?, ?)", coverage_rows)
                tree_rows.clear()
                coverage_rows.clear()
        conn.executemany("INSERT INTO copies_trees VALUES (?, ?, ?, ?, ?, ?, ?)", tree_rows)
        conn.executemany("INSERT INTO copies_coverage VALUES (?, ?, ?)", coverage_rows)
        conn.executemany(
            "INSERT INTO copies_scans VALUES (?, ?, ?, ?)",
            ((volume, scan.scanned_at, scan.incomplete_at(""), scan_missing.get(volume, 0))
             for volume, scan in scans.items()),
        )
        report("folders", len(folders), len(folders), "Folder trees compared")

    def _publish(self, signature, watermark, started, file_count, folder_count, check_cancel) -> int:
        with self.db.transaction(immediate=True) as conn:
            if self._source_signature() != signature or self._watermark() != watermark:
                raise _SourceChanged
            finished = _utc_now()
            run_id = conn.execute(
                """
                INSERT INTO backup_analysis_runs (
                    started_at, completed_at, status, rules_version, source_signature, files_analyzed,
                    folders_analyzed, likely_files, possible_files, ambiguous_files, excluded_files,
                    single_files, message
                ) VALUES (?, ?, 'completed', ?, ?, ?, ?, 0, 0, 0, 0, 0, 'Compared saved file hashes.')
                """,
                (started, finished, RULES_VERSION, signature, file_count, folder_count),
            ).lastrowid
            conn.execute("DELETE FROM backup_analysis_runs WHERE id != ?", (run_id,))
            for table in _STAGE_COLUMNS:
                check_cancel()
                fields = "file_id, volume_id, path, hash_key" if table == "records" else "*"
                conn.execute(f"INSERT INTO backup_hash_{table} SELECT ?, {fields} FROM copies_{table}", (run_id,))
            conn.execute(
                "UPDATE backup_analysis_state SET active_run_id = ?, forced_stale = 0, "
                "stale_reason = '', updated_at = ? WHERE id = 1",
                (run_id, finished),
            )
            conn.execute("DELETE FROM backup_analysis_invalidations")
            check_cancel()
        return int(run_id)

    def _available_run(self, requested: int | None = None) -> int | None:
        state = self.state()
        if state.status == "completed" and (requested is None or requested == state.active_run_id):
            return state.active_run_id
        return None

    def _coverages(self, run_id: int, ids: tuple[int, ...]):
        placeholders = ",".join("?" for _ in ids)
        return self.connection.execute(
            f"""
            SELECT c.folder_id, c.target_volume_id, c.matched_files,
                   EXISTS (SELECT 1 FROM backup_hash_trees target
                           WHERE target.run_id = c.run_id AND target.volume_id = c.target_volume_id
                             AND target.fingerprint = source.fingerprint
                             AND target.fingerprint IS NOT NULL) AS full_match
            FROM backup_hash_coverage c
            JOIN backup_hash_trees source ON source.run_id = c.run_id AND source.folder_id = c.folder_id
            JOIN volumes v ON v.id = c.target_volume_id
            LEFT JOIN volume_register r ON r.volume_id = v.id
            WHERE c.run_id = ? AND c.folder_id IN ({placeholders})
            ORDER BY c.folder_id, full_match DESC, c.matched_files DESC,
                     v.name COLLATE NOCASE, COALESCE(r.drive_id, '') COLLATE NOCASE, v.id
            """,
            (run_id, *ids),
        )

    def item_statuses(self, item_type: str, item_ids: Iterable[int]) -> dict[int, ItemBackupStatus]:
        state = self.state()
        results = {}
        for ids in _ids_in_chunks(item_ids):
            if state.status != "completed":
                results.update((item_id, ItemBackupStatus(item_type, item_id, state.status)) for item_id in ids)
                continue
            run_id = state.active_run_id
            placeholders = ",".join("?" for _ in ids)
            if item_type == "file":
                for row in self.connection.execute(
                    f"""
                    SELECT f.file_id, f.hash_key,
                           (SELECT COUNT(*) FROM backup_hash_drives d
                            WHERE d.run_id = f.run_id AND d.hash_key = f.hash_key
                              AND d.volume_id != f.volume_id) AS drives
                    FROM backup_hash_records f WHERE f.run_id = ? AND f.file_id IN ({placeholders})
                    """,
                    (run_id, *ids),
                ):
                    results[row["file_id"]] = _file_status(row, row["drives"])
            else:
                covers = {
                    folder_id: (rows[0], len(rows))
                    for folder_id, group in groupby(self._coverages(run_id, ids), key=lambda row: row["folder_id"])
                    if (rows := list(group))
                }
                for row in self.connection.execute(
                    f"SELECT * FROM backup_hash_trees WHERE run_id = ? AND folder_id IN ({placeholders})",
                    (run_id, *ids),
                ):
                    best, count = covers.get(row["folder_id"], (None, 0))
                    results[row["folder_id"]] = _tree_status(row, best, count)
            for item_id in ids:
                results.setdefault(item_id, ItemBackupStatus(item_type, item_id, "unavailable"))
        return results

    def file_status(self, file_id: int) -> ItemBackupStatus:
        return self.item_statuses("file", [file_id])[file_id]

    def folder_status(self, folder_id: int) -> ItemBackupStatus:
        return self.item_statuses("folder", [folder_id])[folder_id]

    def volume_summaries(self) -> list[VolumeBackupSummary]:
        roots = list(self.connection.execute(
            "SELECT id, volume_id FROM folders WHERE parent_id IS NULL AND missing = 0"
        ))
        statuses = self.item_statuses("folder", (row["id"] for row in roots))
        return [VolumeBackupSummary(row["volume_id"], statuses[row["id"]]) for row in roots]

    def drives_for(self, item: ItemBackupStatus, source_volume_id: int, run_id: int) -> list[BackupDrive]:
        if self._available_run(run_id) is None or item.status == "unavailable":
            return []
        if item.item_type == "file":
            rows = self.connection.execute(
                """
                SELECT d.volume_id FROM backup_hash_records f JOIN backup_hash_drives d
                ON d.run_id = f.run_id AND d.hash_key = f.hash_key
                WHERE f.run_id = ? AND f.file_id = ? AND d.volume_id != f.volume_id
                """,
                (run_id, item.item_id),
            )
            by_target = {row["volume_id"]: (1, False) for row in rows}
        else:
            by_target = {
                row["target_volume_id"]: (row["matched_files"], bool(row["full_match"]))
                for row in self._coverages(run_id, (item.item_id,))
            }
        results = []
        for row in self.connection.execute(
            """
            SELECT s.*, v.name, COALESCE(r.drive_id, '') AS drive_id
            FROM backup_hash_scans s JOIN volumes v ON v.id = s.volume_id
            LEFT JOIN volume_register r ON r.volume_id = v.id
            WHERE s.run_id = ? AND s.volume_id != ?
            ORDER BY v.name COLLATE NOCASE, drive_id COLLATE NOCASE, v.id
            """,
            (run_id, source_volume_id),
        ):
            matched, exact = by_target.get(row["volume_id"], (0, False))
            label = f'{row["drive_id"]} - {row["name"]}' if row["drive_id"] else row["name"]
            results.append(BackupDrive(
                row["volume_id"], label, row["scanned_at"], matched, item.total_files,
                exact, bool(row["incomplete"]), row["missing_hashes"],
            ))
        return sorted(results, key=lambda drive: (-drive.full_match, -drive.matched_files))

    def locations(
        self, item_type: str, item_id: int, target_volume: int, run_id: int, offset: int = 0,
    ) -> list[BackupLocation]:
        if self._available_run(run_id) is None:
            return []
        if item_type == "folder":
            table, id_column, key = "backup_hash_trees", "folder_id", "fingerprint"
        else:
            table, id_column, key = "backup_hash_records", "file_id", "hash_key"
        rows = self.connection.execute(
            f"""
            SELECT target.{id_column} AS item_id, target.path FROM {table} source
            JOIN {table} target ON target.run_id = source.run_id AND target.{key} = source.{key}
            WHERE source.run_id = ? AND source.{id_column} = ? AND target.volume_id = ?
              AND target.volume_id != source.volume_id AND target.{key} IS NOT NULL
            ORDER BY target.path, target.{id_column}
            LIMIT ? OFFSET ?
            """,
            (run_id, item_id, target_volume, DETAIL_PAGE_SIZE + 1, offset),
        )
        return [BackupLocation(item_type, row["item_id"], target_volume, row["path"]) for row in rows]

    def folder_files(
        self, folder_id: int, target_volume: int, run_id: int, *,
        file_filter: str = "all", offset: int = 0, exact_folder_id: int | None = None,
    ) -> list[BackupFileDetail]:
        if self._available_run(run_id) is None:
            return []
        source = self.connection.execute(
            "SELECT path, volume_id, fingerprint FROM backup_hash_trees WHERE run_id = ? AND folder_id = ?",
            (run_id, folder_id),
        ).fetchone()
        if source is None or source["volume_id"] == target_volume:
            return []
        prefix = source["path"] + "/" if source["path"] else ""
        exact_prefix = None
        if exact_folder_id is not None and source["fingerprint"] is not None:
            exact = self.connection.execute(
                "SELECT path FROM backup_hash_trees "
                "WHERE run_id = ? AND folder_id = ? AND volume_id = ? AND fingerprint = ?",
                (run_id, exact_folder_id, target_volume, source["fingerprint"]),
            ).fetchone()
            if exact is not None:
                exact_prefix = exact["path"] + "/" if exact["path"] else ""

        bounds = "AND f.path >= ? AND f.path < ?" if prefix else ""
        filter_sql = {
            "no_match": "AND f.hash_key IS NOT NULL AND d.hash_key IS NULL",
            "needs_rescan": "AND f.hash_key IS NULL",
        }.get(file_filter, "")
        path_sql = "AND t.path = ? || substr(f.path, ?)" if exact_prefix is not None else ""
        params = [target_volume, target_volume]
        if exact_prefix is not None:
            params.extend((exact_prefix, len(prefix) + 1))
        params.extend((run_id, source["volume_id"]))
        if prefix:
            params.extend((prefix, prefix[:-1] + "0"))
        params.extend((DETAIL_PAGE_SIZE + 1, offset))
        rows = self.connection.execute(
            f"""
            SELECT f.path AS source_path, f.hash_key, target.file_id, target.path AS target_path
            FROM backup_hash_records f
            LEFT JOIN backup_hash_drives d ON d.run_id = f.run_id AND d.hash_key = f.hash_key
                                          AND d.volume_id = ?
            LEFT JOIN backup_hash_records target ON target.run_id = f.run_id AND target.file_id = (
                SELECT t.file_id FROM backup_hash_records t
                WHERE t.run_id = f.run_id AND t.hash_key = d.hash_key AND t.volume_id = ? {path_sql}
                ORDER BY t.path, t.file_id LIMIT 1
            )
            WHERE f.run_id = ? AND f.volume_id = ? {bounds} {filter_sql}
            ORDER BY f.path, f.file_id LIMIT ? OFFSET ?
            """,
            params,
        )
        results = []
        for row in rows:
            target = None
            if row["file_id"] is not None:
                target = BackupLocation("file", row["file_id"], target_volume, row["target_path"])
            status = "found" if target else "needs_rescan" if row["hash_key"] is None else "no_match"
            results.append(BackupFileDetail(row["source_path"][len(prefix):], status, target))
        return results
