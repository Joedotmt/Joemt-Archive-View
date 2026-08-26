from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from itertools import chain, groupby
import json
import re
import sqlite3
import unicodedata
from typing import Any, Callable, Iterable, NamedTuple, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database


RULES_VERSION = 2
FILE_STATUS_VALUES = {
    "likely",
    "possible",
    "single",
    "ambiguous",
    "excluded",
    "unknown",
}
FOLDER_STATUS_VALUES = FILE_STATUS_VALUES | {"empty"}
IGNORED_SYSTEM_SCAN_ROOTS = {
    "$recycle.bin",
    "recycler",
    "system volume information",
}
IGNORED_COPY_METADATA_NAMES = {
    ".ds_store",
    "desktop.ini",
    "ehthumbs.db",
    "indexervolumeguid",
    "thumbs.db",
    "wpsettings.dat",
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


@dataclass(frozen=True)
class AnalysisOptions:
    batch_size: int = 10_000
    mirror_candidate_threshold_percent: float = 50.0
    max_candidate_volumes_per_key: int = 8
    max_candidate_records_per_key: int = 64
    max_candidate_edges_per_key: int = 256
    max_strong_volumes_per_signature: int = 32
    max_strong_records_per_signature: int = 512
    max_strong_edges_per_signature: int = 4_096
    max_folder_volumes_per_fingerprint: int = 8
    max_folder_records_per_fingerprint: int = 64
    max_folder_edges_per_fingerprint: int = 256


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
    started_at: str | None = None
    completed_at: str | None = None
    files_analyzed: int = 0
    folders_analyzed: int = 0
    likely_files: int = 0
    possible_files: int = 0
    ambiguous_files: int = 0
    excluded_files: int = 0
    single_files: int = 0
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
    other_volume_ids: tuple[int, ...] = ()
    other_drive_count: int = 0
    strong_volume_ids: tuple[int, ...] = ()
    possible_volume_ids: tuple[int, ...] = ()
    evidence_text: str = ""
    analysed_at: str | None = None
    is_stale: bool = False
    stale_reason: str = ""
    best_target_volume_id: int | None = None
    matched_files: int = 0
    total_files: int = 0
    matched_bytes: int = 0
    total_bytes: int = 0
    best_coverage_files_percent: float | None = None
    best_coverage_bytes_percent: float | None = None
    scattered: bool = False


@dataclass(frozen=True)
class MatchLocation:
    target_volume_id: int
    status: str
    evidence_text: str
    matched_files: int | None = None
    total_files: int | None = None
    matched_bytes: int | None = None
    total_bytes: int | None = None
    is_stale: bool = False
    analysed_at: str | None = None


@dataclass(frozen=True)
class VolumeBackupSummary:
    volume_id: int
    status: str
    health_status: str
    coverage_eligible: bool
    total_files: int
    total_bytes: int
    coverage_files: int
    coverage_bytes: int
    likely_files: int
    likely_bytes: int
    possible_files: int
    possible_bytes: int
    ambiguous_files: int
    ambiguous_bytes: int
    excluded_files: int
    excluded_bytes: int
    single_files: int
    single_bytes: int
    likely_files_percent: float | None
    likely_bytes_percent: float | None
    latest_scan_status: str | None
    latest_scan_errors: int | None
    analysed_at: str | None
    is_stale: bool = False
    stale_reason: str = ""

    @property
    def strong_files(self) -> int:
        return self.likely_files

    @property
    def strong_bytes(self) -> int:
        return self.likely_bytes

    @property
    def strong_files_percent(self) -> float | None:
        """Compatibility/display alias for conservative likely-file coverage."""
        return self.likely_files_percent

    @property
    def file_coverage_percent(self) -> float | None:
        """Percentage of files with likely metadata evidence elsewhere."""
        return self.likely_files_percent

    @property
    def coverage_files_percent(self) -> float | None:
        return self.likely_files_percent

    @property
    def byte_coverage_percent(self) -> float | None:
        """Percentage of bytes represented by likely file matches."""
        return self.likely_bytes_percent

    @property
    def strong_bytes_percent(self) -> float | None:
        return self.likely_bytes_percent

    @property
    def coverage_bytes_percent(self) -> float | None:
        return self.likely_bytes_percent


@dataclass(frozen=True)
class MirrorCandidate:
    source_volume_id: int
    target_volume_id: int
    source_coverage_percent: float | None
    target_coverage_percent: float | None
    matched_files: int
    complete_structure: bool
    evidence_text: str
    manual_mirror_link: bool = False

    @property
    def complete(self) -> bool:
        return self.complete_structure

    @property
    def is_complete(self) -> bool:
        return self.complete_structure

    @property
    def exact(self) -> bool:
        return self.complete_structure


@dataclass(frozen=True)
class ScanRecord:
    volume_id: int
    drive_id: str
    volume_name: str | None
    indexed_file_count: int
    indexed_folder_count: int
    latest_attempt_status: str | None
    latest_attempt_at: str | None
    latest_attempt_errors: int | None
    latest_attempt_files: int | None
    latest_attempt_folders: int | None
    latest_attempt_message: str | None
    last_applied_at: str | None
    last_applied_errors: int | None
    health_status: str
    latest_attempt_ignored_errors: int = 0
    last_applied_ignored_errors: int = 0


ProgressCallback = Callable[[AnalysisProgress], None]
CancelCallback = Callable[[], bool]


class _AnalysisCancelled(Exception):
    pass


class _AnalysisSourceChanged(Exception):
    pass


class _FolderInfo(NamedTuple):
    volume_id: int
    parent_id: int | None
    normalized_name: str
    indexed_files: int
    depth: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f%z")


def _normalized_text(value: str | None) -> str:
    return unicodedata.normalize("NFC", value or "").casefold()


def _normalized_path(value: str | None) -> str:
    return "/".join(
        _normalized_text(part)
        for part in (value or "").replace("\\", "/").split("/")
        if part not in {"", "."}
    )


def _digest(*parts: bytes) -> bytes:
    hasher = hashlib.blake2b(digest_size=16)
    for part in parts:
        hasher.update(len(part).to_bytes(4, "big"))
        hasher.update(part)
    return hasher.digest()


def _file_key(name: str, size_bytes: int) -> bytes:
    return _digest(_normalized_text(name).encode("utf-8"), str(int(size_bytes)).encode("ascii"))


def _path_key(relative_path: str) -> bytes:
    parent = relative_path.replace("\\", "/").rsplit("/", 1)[0] if "/" in relative_path.replace("\\", "/") else ""
    return _digest(_normalized_path(parent).encode("utf-8"))


def _json_ids(values: Iterable[int]) -> str:
    return json.dumps(sorted({int(value) for value in values}), separators=(",", ":"))


def _parse_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    try:
        return tuple(int(item) for item in json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()


def _percent(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return min(100.0, max(0.0, (int(part) * 100.0) / int(whole)))


def _is_ignored_system_scan_path(value: str | None) -> bool:
    """Return whether an error is confined to protected drive metadata.

    Scanner error paths are normally relative to the scanned root. Keep this
    intentionally narrow: user folders and unrecorded errors remain relevant.
    """
    parts = [part for part in re.split(r"[\\/]+", str(value or "").strip("\\/")) if part]
    if parts and parts[0].endswith(":"):
        parts = parts[1:]
    return bool(parts and parts[0].casefold() in IGNORED_SYSTEM_SCAN_ROOTS)


def _is_copy_metadata_noise(name: str | None, relative_path: str | None) -> bool:
    """Identify OS bookkeeping that should not raise copy confidence."""
    normalized_name = _normalized_text(name)
    if normalized_name in IGNORED_COPY_METADATA_NAMES or normalized_name.startswith("._"):
        return True
    parts = [
        part
        for part in re.split(r"[\\/]+", str(relative_path or "").strip("\\/"))
        if part
    ]
    return bool(parts and parts[0].casefold() in IGNORED_SYSTEM_SCAN_ROOTS)


class BackupAnalysisEngine:
    """Compare records already stored in a catalogue; never access source paths."""

    def __init__(self, db: Database, options: AnalysisOptions | None = None) -> None:
        self.db = db
        self.connection = db.connection
        self.options = options or AnalysisOptions()

    def ensure_schema(self) -> None:
        with self.db.transaction():
            for statement in ANALYSIS_SCHEMA_SQL:
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO backup_analysis_state (
                    id, active_run_id, forced_stale, stale_reason, updated_at
                ) VALUES (1, NULL, 0, '', ?)
                """,
                (_utc_now(),),
            )

    def _schema_available(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_analysis_state'"
        ).fetchone()
        return row is not None

    def _volume_snapshot_rows(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT v.id, COALESCE(r.drive_id, '') AS drive_id, v.last_scan_at,
                       v.indexed_file_count, v.indexed_folder_count
                FROM volumes v
                LEFT JOIN volume_register r ON r.volume_id = v.id
                ORDER BY v.id
                """
            )
        )

    def _source_signature(self, rows: Sequence[sqlite3.Row] | None = None) -> str:
        snapshot = rows if rows is not None else self._volume_snapshot_rows()
        payload = [
            [
                int(row["id"]),
                row["last_scan_at"],
                int(row["indexed_file_count"] or 0),
                int(row["indexed_folder_count"] or 0),
            ]
            for row in snapshot
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def state(self) -> AnalysisState:
        if not self._schema_available():
            return AnalysisState("not_analyzed")
        row = self.connection.execute(
            """
            SELECT s.active_run_id, s.forced_stale, s.stale_reason,
                   r.completed_at, r.rules_version, r.source_signature, r.status
            FROM backup_analysis_state s
            LEFT JOIN backup_analysis_runs r ON r.id = s.active_run_id
            WHERE s.id = 1
            """
        ).fetchone()
        if row is None or row["active_run_id"] is None:
            return AnalysisState("not_analyzed")
        stale = bool(row["forced_stale"])
        reason = str(row["stale_reason"] or "")
        if not stale and str(row["source_signature"] or "") != self._source_signature():
            stale = True
            reason = "Catalogue contents changed after this analysis."
        if int(row["rules_version"] or 0) != RULES_VERSION:
            stale = True
            reason = "The matching rules changed after this analysis."
        return AnalysisState(
            status="outdated" if stale else str(row["status"] or "completed"),
            active_run_id=int(row["active_run_id"]),
            analysed_at=row["completed_at"],
            is_stale=stale,
            stale_reason=reason,
            rules_version=int(row["rules_version"] or 0),
        )

    def invalidate_volume(self, volume_id: int, reason: str = "Catalogue contents changed.") -> None:
        self._invalidate(int(volume_id), reason)

    def invalidate_all(self, reason: str = "Catalogue contents changed.") -> None:
        self._invalidate(None, reason)

    def _invalidate(self, volume_id: int | None, reason: str) -> None:
        self.ensure_schema()
        now = _utc_now()
        with self.db.transaction():
            self.connection.execute(
                "INSERT INTO backup_analysis_invalidations (volume_id, reason, created_at) VALUES (?, ?, ?)",
                (volume_id, reason, now),
            )
            self.connection.execute(
                """
                UPDATE backup_analysis_state
                SET forced_stale = CASE WHEN active_run_id IS NULL THEN 0 ELSE 1 END,
                    stale_reason = CASE WHEN active_run_id IS NULL THEN '' ELSE ? END,
                    updated_at = ?
                WHERE id = 1
                """,
                (reason, now),
            )

    def _emit(
        self,
        callback: ProgressCallback | None,
        phase: str,
        completed: int,
        total: int,
        message: str,
    ) -> None:
        if callback is not None:
            callback(AnalysisProgress(phase, int(completed), int(total), message))

    @staticmethod
    def _check_cancel(cancel_callback: CancelCallback | None) -> None:
        if cancel_callback is not None and cancel_callback():
            raise _AnalysisCancelled

    def _drop_work_tables(self) -> None:
        for name in (
            "backup_work_files",
            "backup_work_folders",
            "backup_stage_file_results",
            "backup_stage_file_targets",
            "backup_stage_folder_results",
            "backup_stage_folder_matches",
            "backup_stage_volume_results",
            "backup_stage_mirrors",
        ):
            self.connection.execute(f"DROP TABLE IF EXISTS temp.{name}")

    def _create_work_tables(self) -> None:
        self._drop_work_tables()
        statements = (
            """
            CREATE TEMP TABLE backup_work_files (
                file_id INTEGER PRIMARY KEY,
                volume_id INTEGER NOT NULL,
                folder_id INTEGER,
                file_key BLOB NOT NULL,
                parent_path_key BLOB NOT NULL,
                modified_at TEXT,
                size_bytes INTEGER NOT NULL,
                evidence_eligible INTEGER NOT NULL
            )
            """,
            """
            CREATE TEMP TABLE backup_stage_file_results (
                file_id INTEGER PRIMARY KEY,
                volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                other_volume_ids TEXT NOT NULL,
                evidence_text TEXT NOT NULL,
                strong_volume_ids TEXT NOT NULL,
                possible_volume_ids TEXT NOT NULL
            )
            """,
            """
            CREATE TEMP TABLE backup_stage_file_targets (
                file_id INTEGER NOT NULL,
                target_volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (file_id, target_volume_id)
            ) WITHOUT ROWID
            """,
            """
            CREATE TEMP TABLE backup_work_folders (
                folder_id INTEGER PRIMARY KEY,
                volume_id INTEGER NOT NULL,
                parent_id INTEGER,
                fingerprint BLOB NOT NULL,
                recursive_file_count INTEGER NOT NULL,
                recursive_size_bytes INTEGER NOT NULL
            )
            """,
            """
            CREATE TEMP TABLE backup_stage_folder_results (
                folder_id INTEGER PRIMARY KEY,
                volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                other_volume_ids TEXT NOT NULL,
                evidence_text TEXT NOT NULL,
                best_target_volume_id INTEGER,
                matched_files INTEGER NOT NULL,
                total_files INTEGER NOT NULL,
                matched_bytes INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                best_coverage_files_percent REAL,
                best_coverage_bytes_percent REAL,
                scattered INTEGER NOT NULL
            )
            """,
            """
            CREATE TEMP TABLE backup_stage_folder_matches (
                folder_id INTEGER NOT NULL,
                target_volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                matched_files INTEGER NOT NULL,
                total_files INTEGER NOT NULL,
                matched_bytes INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                evidence_text TEXT NOT NULL,
                PRIMARY KEY (folder_id, target_volume_id)
            ) WITHOUT ROWID
            """,
            """
            CREATE TEMP TABLE backup_stage_volume_results (
                volume_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                health_status TEXT NOT NULL,
                coverage_eligible INTEGER NOT NULL,
                total_files INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                coverage_files INTEGER NOT NULL,
                coverage_bytes INTEGER NOT NULL,
                likely_files INTEGER NOT NULL,
                likely_bytes INTEGER NOT NULL,
                possible_files INTEGER NOT NULL,
                possible_bytes INTEGER NOT NULL,
                ambiguous_files INTEGER NOT NULL,
                ambiguous_bytes INTEGER NOT NULL,
                excluded_files INTEGER NOT NULL,
                excluded_bytes INTEGER NOT NULL,
                single_files INTEGER NOT NULL,
                single_bytes INTEGER NOT NULL,
                likely_files_percent REAL,
                likely_bytes_percent REAL,
                latest_scan_status TEXT,
                latest_scan_errors INTEGER
            )
            """,
            """
            CREATE TEMP TABLE backup_stage_mirrors (
                source_volume_id INTEGER NOT NULL,
                target_volume_id INTEGER NOT NULL,
                source_coverage_percent REAL NOT NULL,
                target_coverage_percent REAL NOT NULL,
                matched_files INTEGER NOT NULL,
                complete_structure INTEGER NOT NULL,
                evidence_text TEXT NOT NULL,
                manual_mirror_link INTEGER NOT NULL,
                PRIMARY KEY (source_volume_id, target_volume_id)
            ) WITHOUT ROWID
            """,
        )
        for statement in statements:
            self.connection.execute(statement)

    def analyse(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
    ) -> AnalysisSummary:
        started_at = _utc_now()
        if self.connection.in_transaction:
            raise RuntimeError(
                "Backup analysis must start outside an existing database transaction."
            )
        sql_cancelled = False

        def interrupt_long_sql() -> int:
            nonlocal sql_cancelled
            if cancel_callback is not None and cancel_callback():
                sql_cancelled = True
                return 1
            return 0

        if cancel_callback is not None:
            # GROUP BY/index construction can spend a long time inside one
            # SQLite call on a multi-million-file catalogue. The progress
            # handler makes those phases cancellable as well as Python loops.
            self.connection.set_progress_handler(interrupt_long_sql, 10_000)
        try:
            self._check_cancel(cancel_callback)
            self.ensure_schema()
            invalidation_watermark = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM backup_analysis_invalidations"
                ).fetchone()[0]
            )
            self._create_work_tables()
            volumes = self._volume_snapshot_rows()
            source_signature = self._source_signature(volumes)
            total_files = int(
                self.connection.execute("SELECT COUNT(*) FROM files WHERE missing = 0").fetchone()[0]
            )
            total_folders = int(
                self.connection.execute("SELECT COUNT(*) FROM folders WHERE missing = 0").fetchone()[0]
            )

            self._index_files(total_files, progress_callback, cancel_callback)
            self._emit(
                progress_callback,
                "index_metadata",
                0,
                0,
                "Building temporary comparison indexes…",
            )
            self.connection.execute(
                "CREATE INDEX temp.idx_backup_work_file_key "
                "ON backup_work_files(file_key, volume_id)"
            )
            self.connection.execute(
                "CREATE INDEX temp.idx_backup_work_file_folder "
                "ON backup_work_files(folder_id, file_key)"
            )
            pair_counts = self._compare_files(total_files, progress_callback, cancel_callback)
            root_fingerprints = self._compare_folders(
                total_folders,
                progress_callback,
                cancel_callback,
            )
            self._build_volume_results(progress_callback, cancel_callback)
            self._build_mirror_candidates(pair_counts, root_fingerprints)
            self._check_cancel(cancel_callback)

            counts = self.connection.execute(
                """
                SELECT
                    COALESCE(SUM(status='likely'), 0),
                    COALESCE(SUM(status='possible'), 0),
                    COALESCE(SUM(status='ambiguous'), 0),
                    COALESCE(SUM(status='excluded'), 0)
                FROM backup_stage_file_results
                """
            ).fetchone()
            likely_files = int(counts[0] or 0)
            possible_files = int(counts[1] or 0)
            ambiguous_files = int(counts[2] or 0)
            excluded_files = int(counts[3] or 0)
            single_files = max(
                0,
                total_files
                - likely_files
                - possible_files
                - ambiguous_files
                - excluded_files,
            )
            completed_at = _utc_now()
            # The build uses TEMP tables. End that work transaction before
            # publishing so Database.transaction() opens one real, atomic
            # publication transaction. Otherwise its nested-transaction guard
            # would leave the generation uncommitted and close() would roll it
            # back.
            self.connection.commit()
            self._emit(
                progress_callback,
                "publish",
                0,
                0,
                "Saving the new analysis atomically…",
            )
            self._check_cancel(cancel_callback)
            run_id = self._publish(
                started_at,
                completed_at,
                source_signature,
                volumes,
                total_files,
                total_folders,
                likely_files,
                possible_files,
                ambiguous_files,
                excluded_files,
                single_files,
                invalidation_watermark,
            )
            # Publication is already committed. Cleanup must not be allowed to
            # turn a successful new generation into a reported cancellation.
            self.connection.set_progress_handler(None, 0)
            try:
                self._drop_work_tables()
                self.connection.commit()
            except sqlite3.Error:
                self.connection.rollback()
            return AnalysisSummary(
                "completed",
                run_id,
                started_at,
                completed_at,
                total_files,
                total_folders,
                likely_files,
                possible_files,
                ambiguous_files,
                excluded_files,
                single_files,
                "Saved catalogue metadata analysed. No source drive or file content was read.",
            )
        except _AnalysisSourceChanged:
            self.connection.set_progress_handler(None, 0)
            self.connection.rollback()
            self._drop_work_tables()
            self.connection.commit()
            return AnalysisSummary(
                "discarded",
                started_at=started_at,
                message=(
                    "The catalogue changed while it was being analysed. The unfinished "
                    "snapshot was not applied; run the analysis again after scans finish."
                ),
            )
        except _AnalysisCancelled:
            self.connection.set_progress_handler(None, 0)
            self.connection.rollback()
            self._drop_work_tables()
            self.connection.commit()
            return AnalysisSummary(
                "cancelled",
                started_at=started_at,
                message="Analysis cancelled; the previously published evidence was kept.",
            )
        except Exception as exc:
            self.connection.set_progress_handler(None, 0)
            # A failed build is never referenced by backup_analysis_state. Roll
            # back TEMP-table work and keep the last published run intact.
            self.connection.rollback()
            self._drop_work_tables()
            self.connection.commit()
            if sql_cancelled and isinstance(exc, sqlite3.OperationalError):
                return AnalysisSummary(
                    "cancelled",
                    started_at=started_at,
                    message=(
                        "Analysis cancelled; the previously published evidence "
                        "was kept."
                    ),
                )
            raise
        finally:
            self.connection.set_progress_handler(None, 0)

    def _index_files(
        self,
        total: int,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None,
    ) -> None:
        self._emit(progress_callback, "index_files", 0, total, "Indexing saved file metadata…")
        batch: list[tuple[Any, ...]] = []
        excluded_batch: list[tuple[Any, ...]] = []
        processed = 0
        for row in self.connection.execute(
            """
            SELECT id, volume_id, folder_id, name, relative_path, size_bytes, modified_at
            FROM files
            WHERE missing = 0
            ORDER BY id
            """
        ):
            evidence_eligible = not _is_copy_metadata_noise(
                row["name"], row["relative_path"]
            )
            batch.append(
                (
                    int(row["id"]),
                    int(row["volume_id"]),
                    row["folder_id"],
                    _file_key(row["name"], int(row["size_bytes"] or 0)),
                    _path_key(row["relative_path"]),
                    row["modified_at"],
                    int(row["size_bytes"] or 0),
                    int(evidence_eligible),
                )
            )
            if not evidence_eligible:
                excluded_batch.append(
                    (
                        int(row["id"]),
                        int(row["volume_id"]),
                        "excluded",
                        "[]",
                        "Known operating-system metadata is excluded from copy coverage.",
                        "[]",
                        "[]",
                    )
                )
            processed += 1
            if len(batch) >= self.options.batch_size:
                self.connection.executemany(
                    "INSERT INTO backup_work_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch
                )
                if excluded_batch:
                    self.connection.executemany(
                        "INSERT INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                        excluded_batch,
                    )
                batch.clear()
                excluded_batch.clear()
                self._check_cancel(cancel_callback)
                self._emit(
                    progress_callback,
                    "index_files",
                    processed,
                    total,
                    f"Indexing saved file metadata… {processed:,}/{total:,}",
                )
        if batch:
            self.connection.executemany(
                "INSERT INTO backup_work_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch
            )
        if excluded_batch:
            self.connection.executemany(
                "INSERT INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                excluded_batch,
            )
        self._emit(progress_callback, "index_files", total, total, "Saved file metadata indexed")

    def _compare_files(
        self,
        total: int,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None,
    ) -> dict[tuple[int, int], list[int]]:
        self._emit(progress_callback, "compare_files", 0, total, "Comparing saved file metadata…")
        candidate_members = self.connection.execute(
            """
            WITH candidate_keys AS (
                SELECT file_key, COUNT(*) AS record_count,
                       COUNT(DISTINCT volume_id) AS volume_count
                FROM backup_work_files
                WHERE evidence_eligible = 1
                GROUP BY file_key
                HAVING COUNT(DISTINCT volume_id) > 1
            )
            SELECT wf.file_key, wf.file_id, wf.volume_id, wf.folder_id,
                   wf.parent_path_key, wf.modified_at, wf.size_bytes,
                   candidates.record_count, candidates.volume_count
            FROM backup_work_files wf
            JOIN candidate_keys candidates ON candidates.file_key = wf.file_key
            WHERE wf.evidence_eligible = 1
            ORDER BY wf.file_key,
                     CASE WHEN wf.modified_at IS NULL THEN 1 ELSE 0 END,
                     wf.modified_at, wf.parent_path_key, wf.volume_id, wf.file_id
            """
        )
        result_batch: list[tuple[Any, ...]] = []
        target_batch: list[tuple[Any, ...]] = []
        pair_counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        processed = 0

        def flush_batches(force: bool = False) -> None:
            if not force and (
                len(result_batch) < self.options.batch_size
                and len(target_batch) < self.options.batch_size * 4
            ):
                return
            if result_batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                    result_batch,
                )
                result_batch.clear()
            if target_batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_file_targets VALUES (?, ?, ?)",
                    target_batch,
                )
                target_batch.clear()

        def add_pair_count(first: int, second: int, size_bytes: int) -> None:
            pair_counts[(first, second)][0] += 1
            pair_counts[(first, second)][1] += size_bytes

        def pair_rows(
            rows: list[sqlite3.Row],
            *,
            allow_weak: bool,
        ) -> tuple[dict[int, dict[int, str]], set[int]]:
            """Assign at most one candidate per source/target drive pair.

            A filename may occur repeatedly on one drive. Pairing by multiplicity
            prevents two source records from both claiming the same lone target
            record in folder and whole-drive percentages.
            """
            assignments: dict[int, dict[int, str]] = defaultdict(dict)
            competing_files: set[int] = set()
            by_volume: dict[int, list[sqlite3.Row]] = defaultdict(list)
            for row in rows:
                by_volume[int(row["volume_id"])].append(row)
            volume_ids = sorted(by_volume)
            size_bytes = int(rows[0]["size_bytes"] or 0) if rows else 0
            for source_index, source in enumerate(volume_ids):
                for target in volume_ids[source_index + 1 :]:
                    source_rows = by_volume[source]
                    target_rows = by_volume[target]
                    source_by_signature: dict[tuple[str, bytes], list[sqlite3.Row]] = defaultdict(list)
                    target_by_signature: dict[tuple[str, bytes], list[sqlite3.Row]] = defaultdict(list)
                    for row in source_rows:
                        if row["modified_at"]:
                            source_by_signature[
                                (str(row["modified_at"]), bytes(row["parent_path_key"]))
                            ].append(row)
                    for row in target_rows:
                        if row["modified_at"]:
                            target_by_signature[
                                (str(row["modified_at"]), bytes(row["parent_path_key"]))
                            ].append(row)

                    used_source: set[int] = set()
                    used_target: set[int] = set()
                    matched = 0
                    for signature in source_by_signature.keys() & target_by_signature.keys():
                        signature_source = source_by_signature[signature]
                        signature_target = target_by_signature[signature]
                        if len(signature_source) != len(signature_target):
                            competing_files.update(
                                int(row["file_id"])
                                for row in (*signature_source, *signature_target)
                            )
                            continue
                        for source_row, target_row in zip(
                            signature_source,
                            signature_target,
                        ):
                            source_file_id = int(source_row["file_id"])
                            target_file_id = int(target_row["file_id"])
                            used_source.add(source_file_id)
                            used_target.add(target_file_id)
                            assignments[source_file_id][target] = "likely"
                            assignments[target_file_id][source] = "likely"
                            matched += 1

                    if allow_weak:
                        remaining_source = [
                            row for row in source_rows if int(row["file_id"]) not in used_source
                        ]
                        remaining_target = [
                            row for row in target_rows if int(row["file_id"]) not in used_target
                        ]
                        if len(remaining_source) == len(remaining_target) == 1:
                            source_row = remaining_source[0]
                            target_row = remaining_target[0]
                            source_file_id = int(source_row["file_id"])
                            target_file_id = int(target_row["file_id"])
                            assignments[source_file_id][target] = "possible"
                            assignments[target_file_id][source] = "possible"
                            matched += 1
                        elif remaining_source and remaining_target:
                            # Weak name+size evidence cannot choose a truthful
                            # one-to-one correspondence between repeated rows.
                            # Suppress the arbitrary pair instead of making one
                            # sibling amber and another red.
                            competing_files.update(
                                int(row["file_id"])
                                for row in (*remaining_source, *remaining_target)
                            )

                    for _ in range(matched):
                        add_pair_count(source, target, size_bytes)
                        add_pair_count(target, source, size_bytes)
            return assignments, competing_files

        def stage_rows(
            rows: Iterable[sqlite3.Row],
            assignments: dict[int, dict[int, str]],
            *,
            ambiguous_group: bool,
            group_records: int,
            group_volumes: int,
            competing_files: set[int] | None = None,
        ) -> None:
            nonlocal processed
            competing = competing_files or set()
            for member in rows:
                file_id = int(member["file_id"])
                source_volume = int(member["volume_id"])
                targets = assignments.get(file_id, {})
                likely_targets = sorted(
                    volume_id
                    for volume_id, match_status in targets.items()
                    if match_status == "likely"
                )
                possible_targets = sorted(
                    volume_id
                    for volume_id, match_status in targets.items()
                    if match_status == "possible"
                )
                if likely_targets:
                    status = "likely"
                    reported_volumes = sorted(set(likely_targets) | set(possible_targets))
                    evidence = (
                        "Normalized filename, exact size, modified time, and parent path "
                        "match. Repeated records are paired one-to-one per drive."
                    )
                elif possible_targets:
                    status = "possible"
                    reported_volumes = possible_targets
                    evidence = (
                        "Normalized filename and exact size match; parent path or modified "
                        "time differs or is unavailable. Repeated records are paired "
                        "one-to-one per drive for coverage."
                    )
                elif file_id in competing:
                    status = "ambiguous"
                    reported_volumes = []
                    evidence = (
                        "Several records on these drives share the same normalized filename "
                        "and exact size. The saved metadata cannot identify which repeated "
                        "item corresponds, so no arbitrary one-to-one copy is claimed."
                    )
                elif ambiguous_group:
                    status = "ambiguous"
                    reported_volumes = []
                    evidence = (
                        "The normalized filename-and-size combination is too common to "
                        f"identify a useful copy: {group_records:,} records across "
                        f"{group_volumes:,} drives. Weak matches are excluded from folder "
                        "and drive-copy coverage; bounded exact path-and-time matches can "
                        "still be used."
                    )
                else:
                    status = "single"
                    reported_volumes = []
                    evidence = (
                        "Other records share this filename and size, but no unused "
                        "one-to-one candidate remained on another drive."
                    )
                result_batch.append(
                    (
                        file_id,
                        source_volume,
                        status,
                        _json_ids(reported_volumes),
                        evidence,
                        _json_ids(likely_targets),
                        _json_ids(possible_targets),
                    )
                )
                for target_volume, target_status in sorted(targets.items()):
                    target_batch.append((file_id, target_volume, target_status))
                processed += 1
                flush_batches()

        for _file_key, member_rows in groupby(
            candidate_members,
            key=lambda row: bytes(row["file_key"]),
        ):
            self._check_cancel(cancel_callback)
            first_member = next(member_rows)
            group_records = int(first_member["record_count"])
            group_volumes = int(first_member["volume_count"])
            candidate_edges = group_records * (group_volumes - 1)
            ambiguous_group = (
                group_volumes > self.options.max_candidate_volumes_per_key
                or group_records > self.options.max_candidate_records_per_key
                or candidate_edges > self.options.max_candidate_edges_per_key
            )
            all_members = chain((first_member,), member_rows)
            if not ambiguous_group:
                members = list(all_members)
                assignments, competing_files = pair_rows(members, allow_weak=True)
                stage_rows(
                    members,
                    assignments,
                    ambiguous_group=False,
                    group_records=group_records,
                    group_volumes=group_volumes,
                    competing_files=competing_files,
                )
            else:
                # The cursor is ordered by strong signature. Buffer only a
                # bounded signature subgroup; a huge common key never becomes
                # one huge Python list or an unbounded target-edge expansion.
                for signature, signature_rows in groupby(
                    all_members,
                    key=lambda row: (
                        str(row["modified_at"]) if row["modified_at"] else "",
                        bytes(row["parent_path_key"]),
                    ),
                ):
                    buffered_rows: list[sqlite3.Row] = []
                    buffered_volumes: set[int] = set()
                    signature_too_common = not bool(signature[0])
                    for signature_row in signature_rows:
                        if signature_too_common:
                            stage_rows(
                                (signature_row,),
                                {},
                                ambiguous_group=True,
                                group_records=group_records,
                                group_volumes=group_volumes,
                            )
                            continue
                        buffered_rows.append(signature_row)
                        buffered_volumes.add(int(signature_row["volume_id"]))
                        buffered_edges = len(buffered_rows) * max(
                            0, len(buffered_volumes) - 1
                        )
                        if (
                            len(buffered_volumes)
                            > self.options.max_strong_volumes_per_signature
                            or len(buffered_rows)
                            > self.options.max_strong_records_per_signature
                            or buffered_edges
                            > self.options.max_strong_edges_per_signature
                        ):
                            stage_rows(
                                buffered_rows,
                                {},
                                ambiguous_group=True,
                                group_records=group_records,
                                group_volumes=group_volumes,
                            )
                            buffered_rows.clear()
                            buffered_volumes.clear()
                            signature_too_common = True
                    if buffered_rows:
                        assignments = (
                            pair_rows(buffered_rows, allow_weak=False)[0]
                            if len(buffered_volumes) > 1
                            else {}
                        )
                        stage_rows(
                            buffered_rows,
                            assignments,
                            ambiguous_group=True,
                            group_records=group_records,
                            group_volumes=group_volumes,
                        )
            self._emit(
                progress_callback,
                "compare_files",
                min(processed, total),
                total,
                f"Comparing saved file metadata… {processed:,} candidate files",
            )
        flush_batches(force=True)
        self._emit(progress_callback, "compare_files", total, total, "Saved file metadata compared")
        return pair_counts

    def _compare_folders(
        self,
        total: int,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None,
    ) -> dict[int, bytes]:
        progress_total = total * 2
        self._emit(
            progress_callback,
            "compare_folders",
            0,
            progress_total,
            "Building saved folder structure fingerprints…",
        )
        info: dict[int, _FolderInfo] = {}
        children: dict[int, list[int]] = defaultdict(list)
        root_folder_ids: dict[int, int] = {}
        for row in self.connection.execute(
            """
            SELECT id, volume_id, parent_id, name, relative_path,
                   COALESCE(recursive_file_count, 0) AS recursive_file_count
            FROM folders
            WHERE missing = 0
            """
        ):
            folder_id = int(row["id"])
            volume_id = int(row["volume_id"])
            parent_id = int(row["parent_id"]) if row["parent_id"] is not None else None
            if parent_id is not None:
                children[parent_id].append(folder_id)
            else:
                root_folder_ids[volume_id] = folder_id
            path = str(row["relative_path"] or "").replace("\\", "/")
            info[folder_id] = _FolderInfo(
                volume_id,
                parent_id,
                _normalized_text(str(row["name"] or "")),
                int(row["recursive_file_count"] or 0),
                0 if not path else path.count("/") + 1,
            )

        direct_evidence: dict[int, tuple[int, int]] = {
            int(row["folder_id"]): (int(row["files"]), int(row["bytes"]))
            for row in self.connection.execute(
                """
                SELECT folder_id, COUNT(*) AS files,
                       COALESCE(SUM(size_bytes), 0) AS bytes
                FROM backup_work_files
                WHERE folder_id IS NOT NULL AND evidence_eligible = 1
                GROUP BY folder_id
                """
            )
        }
        direct_digest: dict[int, bytes] = {}
        current_folder: int | None = None
        hasher: Any = None
        for file_row in self.connection.execute(
            """
            SELECT folder_id, file_key
            FROM backup_work_files
            WHERE folder_id IS NOT NULL AND evidence_eligible = 1
            ORDER BY folder_id, file_key
            """
        ):
            folder_id = int(file_row["folder_id"])
            if folder_id != current_folder:
                if current_folder is not None and hasher is not None:
                    direct_digest[current_folder] = hasher.digest()
                current_folder = folder_id
                hasher = hashlib.blake2b(digest_size=16)
            hasher.update(file_row["file_key"])
        if current_folder is not None and hasher is not None:
            direct_digest[current_folder] = hasher.digest()

        fingerprints: dict[int, bytes] = {}
        evidence_totals: dict[int, tuple[int, int]] = {}
        ordered_ids = sorted(
            info,
            key=lambda folder_id: info[folder_id].depth,
            reverse=True,
        )
        work_batch: list[tuple[Any, ...]] = []
        for processed, folder_id in enumerate(ordered_ids, start=1):
            self._check_cancel(cancel_callback)
            hasher = hashlib.blake2b(digest_size=16)
            hasher.update(b"jvvv-folder-metadata-v1")
            hasher.update(direct_digest.get(folder_id, b"\x00" * 16))
            child_tokens = []
            evidence_files, evidence_bytes = direct_evidence.get(folder_id, (0, 0))
            for child_id in children.get(folder_id, ()):
                child_files, child_bytes = evidence_totals[child_id]
                evidence_files += child_files
                evidence_bytes += child_bytes
                if child_files <= 0:
                    continue
                child_name = info[child_id].normalized_name.encode("utf-8")
                child_tokens.append(_digest(child_name, fingerprints[child_id]))
            for token in sorted(child_tokens):
                hasher.update(token)
            fingerprint = hasher.digest()
            fingerprints[folder_id] = fingerprint
            evidence_totals[folder_id] = (evidence_files, evidence_bytes)
            folder_info = info[folder_id]
            work_batch.append(
                (
                    folder_id,
                    folder_info.volume_id,
                    folder_info.parent_id,
                    fingerprint,
                    evidence_files,
                    evidence_bytes,
                )
            )
            if len(work_batch) >= self.options.batch_size:
                self.connection.executemany(
                    "INSERT INTO backup_work_folders VALUES (?, ?, ?, ?, ?, ?)", work_batch
                )
                work_batch.clear()
                self._emit(
                    progress_callback,
                    "compare_folders",
                    processed,
                    progress_total,
                    f"Building folder fingerprints… {processed:,}/{total:,}",
                )
        if work_batch:
            self.connection.executemany(
                "INSERT INTO backup_work_folders VALUES (?, ?, ?, ?, ?, ?)", work_batch
            )
        self.connection.execute(
            "CREATE INDEX temp.idx_backup_work_folder_fingerprint "
            "ON backup_work_folders(fingerprint, volume_id)"
        )

        exact_matches: dict[int, set[int]] = defaultdict(set)
        renamed_structure_matches: dict[int, set[int]] = defaultdict(set)
        ambiguous_structure_fingerprints: set[bytes] = set()
        ambiguous_structure_folders: set[int] = set()
        fingerprint_members = self.connection.execute(
            """
            WITH fingerprint_stats AS (
                SELECT fingerprint, COUNT(*) AS record_count,
                       COUNT(DISTINCT volume_id) AS volume_count
                FROM backup_work_folders
                WHERE recursive_file_count >= 2
                GROUP BY fingerprint
                HAVING COUNT(DISTINCT volume_id) > 1
            )
            SELECT wf.folder_id, wf.volume_id, wf.recursive_file_count,
                   wf.fingerprint, stats.record_count, stats.volume_count
            FROM backup_work_folders wf
            JOIN fingerprint_stats stats ON stats.fingerprint = wf.fingerprint
            ORDER BY wf.fingerprint, wf.volume_id, wf.folder_id
            """
        )
        for fingerprint, member_rows in groupby(
            fingerprint_members,
            key=lambda row: bytes(row["fingerprint"]),
        ):
            first_member = next(member_rows)
            folder_records = int(first_member["record_count"])
            folder_volumes = int(first_member["volume_count"])
            if (
                folder_volumes > self.options.max_folder_volumes_per_fingerprint
                or folder_records > self.options.max_folder_records_per_fingerprint
                or folder_records * (folder_volumes - 1)
                > self.options.max_folder_edges_per_fingerprint
            ):
                ambiguous_structure_fingerprints.add(fingerprint)
                # Consume the cursor group without retaining its potentially
                # enormous membership list.
                for _ in member_rows:
                    pass
                continue
            group = list(chain((first_member,), member_rows))
            by_volume: dict[int, list[sqlite3.Row]] = defaultdict(list)
            for row in group:
                by_volume[int(row["volume_id"])].append(row)
            volume_ids = sorted(by_volume)
            for source_index, source in enumerate(volume_ids):
                for target in volume_ids[source_index + 1 :]:
                    source_rows = by_volume[source]
                    target_rows = by_volume[target]
                    source_by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
                    target_by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
                    for row in source_rows:
                        source_by_name[
                            info[int(row["folder_id"])].normalized_name
                        ].append(row)
                    for row in target_rows:
                        target_by_name[
                            info[int(row["folder_id"])].normalized_name
                        ].append(row)

                    used_source: set[int] = set()
                    used_target: set[int] = set()
                    for name in source_by_name.keys() & target_by_name.keys():
                        named_source = source_by_name[name]
                        named_target = target_by_name[name]
                        named_ids = {
                            int(row["folder_id"])
                            for row in (*named_source, *named_target)
                        }
                        used_source.update(
                            int(row["folder_id"]) for row in named_source
                        )
                        used_target.update(
                            int(row["folder_id"]) for row in named_target
                        )
                        if len(named_source) != len(named_target):
                            ambiguous_structure_folders.update(named_ids)
                            continue
                        for source_row, target_row in zip(
                            named_source,
                            named_target,
                        ):
                            source_folder = int(source_row["folder_id"])
                            target_folder = int(target_row["folder_id"])
                            exact_matches[source_folder].add(target)
                            exact_matches[target_folder].add(source)

                    remaining_source = [
                        row
                        for row in source_rows
                        if int(row["folder_id"]) not in used_source
                    ]
                    remaining_target = [
                        row
                        for row in target_rows
                        if int(row["folder_id"]) not in used_target
                    ]
                    if len(remaining_source) == len(remaining_target) == 1:
                        source_folder = int(remaining_source[0]["folder_id"])
                        target_folder = int(remaining_target[0]["folder_id"])
                        renamed_structure_matches[source_folder].add(target)
                        renamed_structure_matches[target_folder].add(source)
                    elif remaining_source and remaining_target:
                        ambiguous_structure_folders.update(
                            int(row["folder_id"])
                            for row in (*remaining_source, *remaining_target)
                        )

        # Fingerprints intentionally omit the folder's own display name, so a
        # complete project tree can be recognized after its top-level folder is
        # renamed. That is strong folder evidence, but it must not silently
        # promote a relocated individual file. A file is likely only when its
        # own non-NULL modified time and normalized parent path also match.

        coverage: dict[int, dict[int, list[int]]] = defaultdict(dict)
        union_counts: dict[int, list[int]] = {}
        for row in self.connection.execute(
            """
            SELECT wf.folder_id, t.target_volume_id,
                   COUNT(*) AS matched_files, COALESCE(SUM(wf.size_bytes), 0) AS matched_bytes
            FROM backup_stage_file_targets t
            JOIN backup_work_files wf ON wf.file_id = t.file_id
            WHERE wf.folder_id IS NOT NULL
            GROUP BY wf.folder_id, t.target_volume_id
            """
        ):
            coverage[int(row["folder_id"])][int(row["target_volume_id"])] = [
                int(row["matched_files"]),
                int(row["matched_bytes"]),
            ]
        for row in self.connection.execute(
            """
            SELECT wf.folder_id, COUNT(*) AS matched_files, COALESCE(SUM(wf.size_bytes), 0) AS matched_bytes
            FROM backup_stage_file_results r
            JOIN backup_work_files wf ON wf.file_id = r.file_id
            WHERE wf.folder_id IS NOT NULL
              AND wf.evidence_eligible = 1
              AND r.status IN ('likely', 'possible')
            GROUP BY wf.folder_id
            """
        ):
            union_counts[int(row["folder_id"])] = [
                int(row["matched_files"]),
                int(row["matched_bytes"]),
            ]
        ambiguous_counts: dict[int, int] = {
            int(row["folder_id"]): int(row["ambiguous_files"])
            for row in self.connection.execute(
                """
                SELECT wf.folder_id, COUNT(*) AS ambiguous_files
                FROM backup_stage_file_results r
                JOIN backup_work_files wf ON wf.file_id = r.file_id
                WHERE wf.folder_id IS NOT NULL
                  AND wf.evidence_eligible = 1
                  AND r.status = 'ambiguous'
                GROUP BY wf.folder_id
                """
            )
        }

        health_by_volume = {record.volume_id: record.health_status for record in self.scan_records()}
        stage_rows: list[tuple[Any, ...]] = []
        stage_matches: list[tuple[Any, ...]] = []
        for processed, folder_id in enumerate(ordered_ids, start=1):
            folder_info = info[folder_id]
            volume_id = folder_info.volume_id
            total_files, total_bytes = evidence_totals[folder_id]
            indexed_files = folder_info.indexed_files
            folder_coverage = coverage.get(folder_id, {})
            union = union_counts.get(folder_id, [0, 0])
            ambiguous_descendants = ambiguous_counts.get(folder_id, 0)
            all_exact_targets = sorted(exact_matches.get(folder_id, ()))
            trusted_exact_targets = [
                target
                for target in all_exact_targets
                if health_by_volume.get(volume_id) == "completed"
                and health_by_volume.get(target) == "completed"
            ]
            untrusted_exact_targets = sorted(
                set(all_exact_targets) - set(trusted_exact_targets)
            )
            renamed_targets = sorted(renamed_structure_matches.get(folder_id, ()))
            if total_files == 0:
                if indexed_files > 0:
                    status = "excluded"
                    evidence = (
                        "This folder contains only known operating-system metadata, "
                        "which is excluded from copy coverage."
                    )
                elif health_by_volume.get(volume_id) in {"completed", "empty"}:
                    status = "empty"
                    evidence = "The saved catalogue contains no files in this folder."
                else:
                    status = "unknown"
                    evidence = "The folder is empty in the catalogue, but the latest successful scan is missing or reported access errors."
                other_ids: list[int] = []
                best_target = None
                matched_files = matched_bytes = 0
                files_percent = bytes_percent = None
                scattered = 0
            elif trusted_exact_targets:
                status = "likely"
                evidence = (
                    "A complete metadata structure matches on another drive: "
                    "at least two content-file names and exact sizes plus the content-bearing "
                    "folder layout. The folder name also matches. Known operating-system "
                    "metadata is excluded."
                )
                if folder_id in ambiguous_structure_folders:
                    evidence += (
                        " Additional repeated structures were suppressed because they "
                        "could not be paired without reusing a target folder."
                    )
                other_ids = sorted(
                    set(folder_coverage)
                    | set(all_exact_targets)
                    | set(renamed_targets)
                )
                best_target = trusted_exact_targets[0]
                matched_files = total_files
                matched_bytes = total_bytes
                files_percent = bytes_percent = 100.0
                scattered = 0
            elif renamed_targets or untrusted_exact_targets:
                status = "possible"
                if untrusted_exact_targets and renamed_targets:
                    evidence = (
                        "A content-bearing subtree matches structurally on other drives, "
                        "but some folder names differ and at least one exact-name pair has "
                        "an incomplete or error-bearing scan. This remains possible evidence."
                    )
                elif untrusted_exact_targets:
                    evidence = (
                        "The saved folder name and content-bearing structure match, but at "
                        "least one applied scan has access errors or no trustworthy "
                        "denominator, so the result is not labelled complete."
                    )
                else:
                    evidence = (
                        "The content-bearing subtree has the same descendant names, exact "
                        "sizes, and layout, but the folder's own name differs. This is "
                        "possible evidence, not a complete folder match. Known "
                        "operating-system metadata is excluded."
                    )
                possible_structure_targets = sorted(
                    set(renamed_targets) | set(untrusted_exact_targets)
                )
                other_ids = sorted(set(folder_coverage) | set(possible_structure_targets))
                best_target = possible_structure_targets[0]
                matched_files = total_files
                matched_bytes = total_bytes
                files_percent = bytes_percent = 100.0
                scattered = 0
            elif folder_coverage:
                def coverage_score(item: tuple[int, list[int]]) -> tuple[float, float, int]:
                    target, values = item
                    return (
                        (_percent(values[0], total_files) or 0.0) + (_percent(values[1], total_bytes) or 0.0),
                        _percent(values[0], total_files) or 0.0,
                        -target,
                    )

                best_target, best_values = max(folder_coverage.items(), key=coverage_score)
                matched_files, matched_bytes = best_values
                files_percent = _percent(matched_files, total_files)
                bytes_percent = _percent(matched_bytes, total_bytes)
                status = "possible"
                other_ids = sorted(folder_coverage)
                scattered = int(union[0] >= total_files and matched_files < total_files)
                evidence = (
                    "Candidate descendant filename-and-size matches were counted on the best single other drive; the folder structure is not complete."
                )
            elif (
                fingerprints[folder_id] in ambiguous_structure_fingerprints
                or folder_id in ambiguous_structure_folders
                or ambiguous_descendants > 0
            ):
                status = "ambiguous"
                evidence = (
                    "One or more descendant candidates are too common or compete with "
                    "repeated records, so the saved metadata cannot identify a useful "
                    "folder copy without creating misleading links."
                )
                other_ids = []
                best_target = None
                matched_files = matched_bytes = 0
                files_percent = bytes_percent = None
                scattered = 0
            else:
                status = "single"
                evidence = "No descendant metadata match was found on another drive in this catalogue."
                other_ids = []
                best_target = None
                matched_files = matched_bytes = 0
                files_percent = bytes_percent = 0.0
                scattered = 0

            stage_rows.append(
                (
                    folder_id,
                    volume_id,
                    status,
                    _json_ids(other_ids),
                    evidence,
                    best_target,
                    matched_files,
                    total_files,
                    matched_bytes,
                    total_bytes,
                    files_percent,
                    bytes_percent,
                    scattered,
                )
            )
            for target in sorted(
                set(folder_coverage) | set(all_exact_targets) | set(renamed_targets)
            ):
                values = folder_coverage.get(target, [0, 0])
                target_status = (
                    "likely" if target in trusted_exact_targets else "possible"
                )
                full_structure = target in all_exact_targets or target in renamed_targets
                target_evidence = (
                    "Complete subtree metadata matches: descendant names, exact "
                    "sizes, and folder layout."
                    if target_status == "likely"
                    else (
                        "The saved structure and folder name match, but scan health is "
                        "not trustworthy enough to label it complete."
                        if target in untrusted_exact_targets
                        else (
                            "The content-bearing subtree matches, but the folder name differs."
                            if target in renamed_targets
                            else "Some descendant filename-and-size metadata appears on "
                            "this drive, but a complete matching folder structure was not found."
                        )
                    )
                )
                stage_matches.append(
                    (
                        folder_id,
                        target,
                        target_status,
                        total_files if full_structure else values[0],
                        total_files,
                        total_bytes if full_structure else values[1],
                        total_bytes,
                        target_evidence,
                    )
                )

            parent_id = folder_info.parent_id
            if parent_id is not None:
                parent_map = coverage.setdefault(parent_id, {})
                for target, values in folder_coverage.items():
                    aggregate = parent_map.setdefault(target, [0, 0])
                    aggregate[0] += values[0]
                    aggregate[1] += values[1]
                parent_union = union_counts.setdefault(parent_id, [0, 0])
                parent_union[0] += union[0]
                parent_union[1] += union[1]
                ambiguous_counts[parent_id] = (
                    ambiguous_counts.get(parent_id, 0) + ambiguous_descendants
                )
            coverage.pop(folder_id, None)
            union_counts.pop(folder_id, None)
            ambiguous_counts.pop(folder_id, None)

            if (
                len(stage_rows) >= self.options.batch_size
                or len(stage_matches) >= self.options.batch_size * 4
            ):
                self.connection.executemany(
                    "INSERT INTO backup_stage_folder_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    stage_rows,
                )
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_folder_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    stage_matches,
                )
                stage_rows.clear()
                stage_matches.clear()
                self._emit(
                    progress_callback,
                    "compare_folders",
                    total + processed,
                    progress_total,
                    f"Calculating folder coverage… {processed:,}/{total:,}",
                )
        if stage_rows:
            self.connection.executemany(
                "INSERT INTO backup_stage_folder_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stage_rows,
            )
        if stage_matches:
            self.connection.executemany(
                "INSERT OR REPLACE INTO backup_stage_folder_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                stage_matches,
            )

        root_fingerprints = {
            volume_id: fingerprints[folder_id]
            for volume_id, folder_id in root_folder_ids.items()
        }
        self._emit(
            progress_callback,
            "compare_folders",
            progress_total,
            progress_total,
            "Saved folder structures compared",
        )
        return root_fingerprints

    def _build_volume_results(
        self,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None,
    ) -> None:
        volumes = list(self.connection.execute("SELECT id FROM volumes ORDER BY id"))
        self._emit(progress_callback, "volume_coverage", 0, len(volumes), "Calculating volume coverage…")
        scan_by_volume = {record.volume_id: record for record in self.scan_records()}
        for index, volume_row in enumerate(volumes, start=1):
            self._check_cancel(cancel_callback)
            volume_id = int(volume_row["id"])
            totals = self.connection.execute(
                """
                SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes
                     , COALESCE(SUM(evidence_eligible), 0) AS coverage_files
                     , COALESCE(SUM(CASE WHEN evidence_eligible = 1 THEN size_bytes ELSE 0 END), 0)
                       AS coverage_bytes
                FROM backup_work_files WHERE volume_id = ?
                """,
                (volume_id,),
            ).fetchone()
            matched = self.connection.execute(
                """
                SELECT
                    COALESCE(SUM(r.status='likely'), 0) AS likely_files,
                    COALESCE(SUM(CASE WHEN r.status='likely' THEN wf.size_bytes ELSE 0 END), 0) AS likely_bytes,
                    COALESCE(SUM(r.status='possible'), 0) AS possible_files,
                    COALESCE(SUM(CASE WHEN r.status='possible' THEN wf.size_bytes ELSE 0 END), 0) AS possible_bytes,
                    COALESCE(SUM(r.status='ambiguous'), 0) AS ambiguous_files,
                    COALESCE(SUM(CASE WHEN r.status='ambiguous' THEN wf.size_bytes ELSE 0 END), 0) AS ambiguous_bytes
                FROM backup_stage_file_results r
                JOIN backup_work_files wf ON wf.file_id = r.file_id
                WHERE r.volume_id = ?
                """,
                (volume_id,),
            ).fetchone()
            total_files = int(totals["files"] or 0)
            total_bytes = int(totals["bytes"] or 0)
            coverage_files = int(totals["coverage_files"] or 0)
            coverage_bytes = int(totals["coverage_bytes"] or 0)
            likely_files = int(matched["likely_files"] or 0)
            likely_bytes = int(matched["likely_bytes"] or 0)
            possible_files = int(matched["possible_files"] or 0)
            possible_bytes = int(matched["possible_bytes"] or 0)
            ambiguous_files = int(matched["ambiguous_files"] or 0)
            ambiguous_bytes = int(matched["ambiguous_bytes"] or 0)
            excluded_files = max(0, total_files - coverage_files)
            excluded_bytes = max(0, total_bytes - coverage_bytes)
            single_files = max(
                0,
                coverage_files - likely_files - possible_files - ambiguous_files,
            )
            single_bytes = max(
                0,
                coverage_bytes - likely_bytes - possible_bytes - ambiguous_bytes,
            )
            scan = scan_by_volume.get(volume_id)
            health = scan.health_status if scan is not None else "not_scanned"
            # Percentages need a trustworthy denominator. A completed scan that
            # reported access errors may have omitted files/folders, so retain
            # its counts for diagnosis but do not present them as coverage.
            coverage_eligible = bool(
                coverage_files > 0
                and scan is not None
                and scan.last_applied_at
                and health == "completed"
            )
            if coverage_files == 0:
                if total_files > 0:
                    status = "excluded"
                else:
                    status = "empty" if health == "empty" else "unknown"
            elif not coverage_eligible:
                status = "unknown"
            elif likely_files == coverage_files:
                status = "likely"
            elif likely_files or possible_files:
                status = "possible"
            elif ambiguous_files:
                status = "ambiguous"
            else:
                status = "single"
            self.connection.execute(
                """
                INSERT INTO backup_stage_volume_results (
                    volume_id, status, health_status, coverage_eligible,
                    total_files, total_bytes, coverage_files, coverage_bytes,
                    likely_files, likely_bytes, possible_files, possible_bytes,
                    ambiguous_files, ambiguous_bytes, excluded_files, excluded_bytes,
                    single_files, single_bytes, likely_files_percent,
                    likely_bytes_percent, latest_scan_status, latest_scan_errors
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    volume_id,
                    status,
                    health,
                    int(coverage_eligible),
                    total_files,
                    total_bytes,
                    coverage_files,
                    coverage_bytes,
                    likely_files,
                    likely_bytes,
                    possible_files,
                    possible_bytes,
                    ambiguous_files,
                    ambiguous_bytes,
                    excluded_files,
                    excluded_bytes,
                    single_files,
                    single_bytes,
                    _percent(likely_files, coverage_files),
                    _percent(likely_bytes, coverage_bytes),
                    scan.latest_attempt_status if scan else None,
                    scan.latest_attempt_errors if scan else None,
                ),
            )
            self._emit(
                progress_callback,
                "volume_coverage",
                index,
                len(volumes),
                f"Calculating volume coverage… {index}/{len(volumes)}",
            )

    def _build_mirror_candidates(
        self,
        pair_counts: dict[tuple[int, int], list[int]],
        root_fingerprints: dict[int, bytes],
    ) -> None:
        metrics = {
            int(row["volume_id"]): (
                int(row["coverage_files"]),
                bool(row["coverage_eligible"]),
            )
            for row in self.connection.execute(
                """
                SELECT volume_id, coverage_files, coverage_eligible
                FROM backup_stage_volume_results
                """
            )
        }
        totals = {volume_id: values[0] for volume_id, values in metrics.items()}
        root_group_sizes: dict[bytes, int] = defaultdict(int)
        for volume_id, fingerprint in root_fingerprints.items():
            if totals.get(volume_id, 0) >= 2:
                root_group_sizes[fingerprint] += 1
        register = {
            int(row["volume_id"]): (bool(row["is_mirror"]), row["master_volume_id"])
            for row in self.connection.execute(
                "SELECT volume_id, is_mirror, master_volume_id FROM volume_register"
            )
        }
        volume_ids = sorted(totals)
        for source_index, source in enumerate(volume_ids):
            for target in volume_ids[source_index + 1 :]:
                matched_forward = pair_counts.get((source, target), [0, 0, 0])[0]
                matched_reverse = pair_counts.get((target, source), [0, 0, 0])[0]
                source_percent = _percent(matched_forward, totals[source]) or 0.0
                target_percent = _percent(matched_reverse, totals[target]) or 0.0
                same_root_structure = bool(
                    totals[source] >= 2
                    and totals[target] >= 2
                    and root_fingerprints.get(source) == root_fingerprints.get(target)
                    and root_group_sizes.get(root_fingerprints.get(source), 0)
                    <= self.options.max_folder_volumes_per_fingerprint
                )
                complete = bool(
                    same_root_structure
                    and metrics[source][1]
                    and metrics[target][1]
                )
                manual = bool(
                    (register.get(source, (False, None))[0] and register.get(source, (False, None))[1] == target)
                    or (register.get(target, (False, None))[0] and register.get(target, (False, None))[1] == source)
                )
                if (
                    not complete
                    and not manual
                    and max(source_percent, target_percent)
                    < self.options.mirror_candidate_threshold_percent
                ):
                    continue
                if complete:
                    evidence = "Complete root folder metadata structure matches."
                elif same_root_structure:
                    evidence = (
                        "The saved root structures appear to match, but at least one "
                        "applied scan has access errors or no trustworthy denominator, "
                        "so this is not labelled a complete drive copy."
                    )
                elif max(source_percent, target_percent) >= self.options.mirror_candidate_threshold_percent:
                    evidence = (
                        "A large share of normalized filename-and-size records overlaps; "
                        "review paths and timestamps before declaring a mirror."
                    )
                else:
                    evidence = (
                        "This pair is listed because it has a manual mirror relationship, "
                        "but its saved metadata overlap is below the suggestion threshold."
                    )
                self.connection.execute(
                    "INSERT INTO backup_stage_mirrors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        source,
                        target,
                        source_percent,
                        target_percent,
                        min(matched_forward, matched_reverse),
                        int(complete),
                        evidence,
                        int(manual),
                    ),
                )

    def _publish(
        self,
        started_at: str,
        completed_at: str,
        source_signature: str,
        volume_rows: Sequence[sqlite3.Row],
        total_files: int,
        total_folders: int,
        likely_files: int,
        possible_files: int,
        ambiguous_files: int,
        excluded_files: int,
        single_files: int,
        invalidation_watermark: int,
    ) -> int:
        with self.db.transaction(immediate=True) as conn:
            changed_during_analysis = (
                self._source_signature() != source_signature
                or conn.execute(
                    """
                    SELECT 1
                    FROM backup_analysis_invalidations
                    WHERE id > ?
                    LIMIT 1
                    """,
                    (invalidation_watermark,),
                ).fetchone()
                is not None
            )
            if changed_during_analysis:
                raise _AnalysisSourceChanged
            run_id = int(
                conn.execute(
                    """
                    INSERT INTO backup_analysis_runs (
                        started_at, completed_at, status, rules_version, source_signature,
                        files_analyzed, folders_analyzed, likely_files, possible_files,
                        ambiguous_files, excluded_files, single_files, message
                    ) VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        started_at,
                        completed_at,
                        RULES_VERSION,
                        source_signature,
                        total_files,
                        total_folders,
                        likely_files,
                        possible_files,
                        ambiguous_files,
                        excluded_files,
                        single_files,
                        "Saved catalogue metadata only; no drive contents or checksums were read.",
                    ),
                ).lastrowid
            )
            # Retire the previous generation before copying the new one. Other
            # connections continue to see the old committed generation until
            # this transaction commits, while SQLite can reuse its freed pages.
            conn.execute("DELETE FROM backup_analysis_runs WHERE id != ?", (run_id,))
            conn.executemany(
                """
                INSERT INTO backup_analysis_volume_snapshots (
                    run_id, volume_id, drive_id, last_scan_at,
                    indexed_file_count, indexed_folder_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        int(row["id"]),
                        str(row["drive_id"] or ""),
                        row["last_scan_at"],
                        int(row["indexed_file_count"] or 0),
                        int(row["indexed_folder_count"] or 0),
                    )
                    for row in volume_rows
                ],
            )
            conn.execute(
                """
                INSERT INTO backup_file_results
                SELECT ?, file_id, volume_id, status, other_volume_ids, evidence_text,
                       strong_volume_ids, possible_volume_ids
                FROM backup_stage_file_results
                """,
                (run_id,),
            )
            conn.execute(
                """
                INSERT INTO backup_folder_results
                SELECT ?, folder_id, volume_id, status, other_volume_ids, evidence_text,
                       best_target_volume_id, matched_files, total_files, matched_bytes,
                       total_bytes, best_coverage_files_percent,
                       best_coverage_bytes_percent, scattered
                FROM backup_stage_folder_results
                """,
                (run_id,),
            )
            conn.execute(
                """
                INSERT INTO backup_folder_drive_matches
                SELECT ?, folder_id, target_volume_id, status, matched_files, total_files,
                       matched_bytes, total_bytes, evidence_text
                FROM backup_stage_folder_matches
                """,
                (run_id,),
            )
            conn.execute(
                """
                INSERT INTO backup_volume_results
                SELECT ?, volume_id, status, health_status, coverage_eligible,
                       total_files, total_bytes, coverage_files, coverage_bytes,
                       likely_files, likely_bytes, possible_files, possible_bytes,
                       ambiguous_files, ambiguous_bytes, excluded_files, excluded_bytes,
                       single_files, single_bytes,
                       likely_files_percent, likely_bytes_percent,
                       latest_scan_status, latest_scan_errors
                FROM backup_stage_volume_results
                """,
                (run_id,),
            )
            conn.execute(
                """
                INSERT INTO backup_mirror_candidates
                SELECT ?, source_volume_id, target_volume_id, source_coverage_percent,
                       target_coverage_percent, matched_files, complete_structure,
                       evidence_text, manual_mirror_link
                FROM backup_stage_mirrors
                """,
                (run_id,),
            )
            conn.execute(
                """
                UPDATE backup_analysis_state
                SET active_run_id = ?, forced_stale = 0, stale_reason = '', updated_at = ?
                WHERE id = 1
                """,
                (run_id, completed_at),
            )
            conn.execute("DELETE FROM backup_analysis_invalidations")
        return run_id

    def item_statuses(
        self,
        item_type: str,
        item_ids: Sequence[int],
    ) -> dict[int, ItemBackupStatus]:
        ids = [int(value) for value in dict.fromkeys(item_ids)]
        if not ids:
            return {}
        if item_type not in {"file", "folder"}:
            raise ValueError(f"Unsupported catalogue item type: {item_type}")
        state = self.state()
        result: dict[int, ItemBackupStatus] = {}
        table = "files" if item_type == "file" else "folders"
        result_table = "backup_file_results" if item_type == "file" else "backup_folder_results"
        id_column = "file_id" if item_type == "file" else "folder_id"
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            existing = {
                int(row["id"]): row
                for row in self.connection.execute(
                    f"SELECT id, volume_id, missing FROM {table} WHERE id IN ({placeholders})",
                    chunk,
                )
            }
            stored: dict[int, sqlite3.Row] = {}
            folder_match_statuses: dict[int, dict[str, list[int]]] = defaultdict(
                lambda: {"likely": [], "possible": []}
            )
            if state.active_run_id is not None:
                stored = {
                    int(row[id_column]): row
                    for row in self.connection.execute(
                        f"SELECT * FROM {result_table} WHERE run_id = ? AND {id_column} IN ({placeholders})",
                        (state.active_run_id, *chunk),
                    )
                }
                if item_type == "folder":
                    for match in self.connection.execute(
                        f"""
                        SELECT folder_id, target_volume_id, status
                        FROM backup_folder_drive_matches
                        WHERE run_id = ? AND folder_id IN ({placeholders})
                        ORDER BY folder_id, target_volume_id
                        """,
                        (state.active_run_id, *chunk),
                    ):
                        match_status = str(match["status"])
                        if match_status in {"likely", "possible"}:
                            folder_match_statuses[int(match["folder_id"])][
                                match_status
                            ].append(int(match["target_volume_id"]))
            for item_id in chunk:
                item = existing.get(item_id)
                if item is None:
                    continue
                row = stored.get(item_id)
                if state.active_run_id is None:
                    status = "unknown"
                    evidence = "Backup evidence has not been analysed for this catalogue."
                    other_ids: tuple[int, ...] = ()
                    strong_ids: tuple[int, ...] = ()
                    possible_ids: tuple[int, ...] = ()
                elif bool(item["missing"]):
                    status = "unknown"
                    evidence = "This catalogue record is marked missing."
                    other_ids = ()
                    strong_ids = ()
                    possible_ids = ()
                elif row is None:
                    status = "single"
                    evidence = "No normalized filename-and-size match was found on another drive in the saved catalogue."
                    other_ids = ()
                    strong_ids = ()
                    possible_ids = ()
                else:
                    status = str(row["status"])
                    evidence = str(row["evidence_text"] or "")
                    other_ids = _parse_ids(row["other_volume_ids"])
                    strong_ids = (
                        _parse_ids(row["strong_volume_ids"])
                        if item_type == "file"
                        else ()
                    )
                    possible_ids = (
                        _parse_ids(row["possible_volume_ids"])
                        if item_type == "file"
                        else ()
                    )
                kwargs: dict[str, Any] = {}
                if item_type == "folder" and row is not None and not bool(item["missing"]):
                    target_statuses = folder_match_statuses[item_id]
                    kwargs = {
                        "strong_volume_ids": tuple(target_statuses["likely"]),
                        "possible_volume_ids": tuple(target_statuses["possible"]),
                        "best_target_volume_id": row["best_target_volume_id"],
                        "matched_files": int(row["matched_files"] or 0),
                        "total_files": int(row["total_files"] or 0),
                        "matched_bytes": int(row["matched_bytes"] or 0),
                        "total_bytes": int(row["total_bytes"] or 0),
                        "best_coverage_files_percent": row["best_coverage_files_percent"],
                        "best_coverage_bytes_percent": row["best_coverage_bytes_percent"],
                        "scattered": bool(row["scattered"]),
                    }
                elif row is not None:
                    kwargs = {
                        "strong_volume_ids": strong_ids,
                        "possible_volume_ids": possible_ids,
                    }
                result[item_id] = ItemBackupStatus(
                    item_type=item_type,
                    item_id=item_id,
                    status=status,
                    other_volume_ids=other_ids,
                    other_drive_count=len(other_ids),
                    evidence_text=evidence,
                    analysed_at=state.analysed_at,
                    is_stale=state.is_stale,
                    stale_reason=state.stale_reason,
                    **kwargs,
                )
        return result

    def file_status(self, file_id: int) -> ItemBackupStatus:
        result = self.item_statuses("file", [file_id]).get(int(file_id))
        if result is None:
            return ItemBackupStatus("file", int(file_id), "unknown", evidence_text="File is not in the catalogue.")
        return result

    def folder_status(self, folder_id: int) -> ItemBackupStatus:
        result = self.item_statuses("folder", [folder_id]).get(int(folder_id))
        if result is None:
            return ItemBackupStatus("folder", int(folder_id), "unknown", evidence_text="Folder is not in the catalogue.")
        return result

    def file_matches(self, file_id: int) -> list[MatchLocation]:
        status = self.file_status(file_id)
        matches = [
            MatchLocation(
                volume_id,
                "likely",
                "Normalized filename, exact size, modified time, and parent path match.",
                is_stale=status.is_stale,
                analysed_at=status.analysed_at,
            )
            for volume_id in status.strong_volume_ids
        ]
        matches.extend(
            MatchLocation(
                volume_id,
                "possible",
                "Normalized filename and exact size match; path or modified time differs.",
                is_stale=status.is_stale,
                analysed_at=status.analysed_at,
            )
            for volume_id in status.possible_volume_ids
        )
        return sorted(matches, key=lambda match: (match.target_volume_id, match.status))

    def folder_matches(self, folder_id: int) -> list[MatchLocation]:
        state = self.state()
        if state.active_run_id is None:
            return []
        current = self.connection.execute(
            "SELECT missing FROM folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
        if current is None or bool(current["missing"]):
            return []
        return [
            MatchLocation(
                int(row["target_volume_id"]),
                str(row["status"]),
                str(row["evidence_text"] or ""),
                int(row["matched_files"]),
                int(row["total_files"]),
                int(row["matched_bytes"]),
                int(row["total_bytes"]),
                state.is_stale,
                state.analysed_at,
            )
            for row in self.connection.execute(
                """
                SELECT * FROM backup_folder_drive_matches
                WHERE run_id = ? AND folder_id = ?
                ORDER BY target_volume_id
                """,
                (state.active_run_id, int(folder_id)),
            )
        ]

    def volume_summaries(self) -> list[VolumeBackupSummary]:
        state = self.state()
        if state.active_run_id is None:
            return []
        return [
            VolumeBackupSummary(
                volume_id=int(row["volume_id"]),
                status=str(row["status"]),
                health_status=str(row["health_status"]),
                coverage_eligible=bool(row["coverage_eligible"]),
                total_files=int(row["total_files"]),
                total_bytes=int(row["total_bytes"]),
                coverage_files=int(row["coverage_files"]),
                coverage_bytes=int(row["coverage_bytes"]),
                likely_files=int(row["likely_files"]),
                likely_bytes=int(row["likely_bytes"]),
                possible_files=int(row["possible_files"]),
                possible_bytes=int(row["possible_bytes"]),
                ambiguous_files=int(row["ambiguous_files"]),
                ambiguous_bytes=int(row["ambiguous_bytes"]),
                excluded_files=int(row["excluded_files"]),
                excluded_bytes=int(row["excluded_bytes"]),
                single_files=int(row["single_files"]),
                single_bytes=int(row["single_bytes"]),
                likely_files_percent=row["likely_files_percent"],
                likely_bytes_percent=row["likely_bytes_percent"],
                latest_scan_status=row["latest_scan_status"],
                latest_scan_errors=row["latest_scan_errors"],
                analysed_at=state.analysed_at,
                is_stale=state.is_stale,
                stale_reason=state.stale_reason,
            )
            for row in self.connection.execute(
                "SELECT * FROM backup_volume_results WHERE run_id = ? ORDER BY volume_id",
                (state.active_run_id,),
            )
        ]

    def mirror_candidates(self) -> list[MirrorCandidate]:
        state = self.state()
        if state.active_run_id is None:
            return []
        rows = list(
            self.connection.execute(
                """
                SELECT * FROM backup_mirror_candidates
                WHERE run_id = ?
                """,
                (state.active_run_id,),
            )
        )
        volume_ids = {
            int(row["id"])
            for row in self.connection.execute("SELECT id FROM volumes")
        }
        manual_pairs = {
            tuple(sorted((int(row["volume_id"]), int(row["master_volume_id"]))))
            for row in self.connection.execute(
                """
                SELECT volume_id, master_volume_id
                FROM volume_register
                WHERE is_mirror = 1 AND master_volume_id IS NOT NULL
                """
            )
            if int(row["volume_id"]) != int(row["master_volume_id"])
            and int(row["volume_id"]) in volume_ids
            and int(row["master_volume_id"]) in volume_ids
        }
        candidates: list[MirrorCandidate] = []
        stored_pairs: set[tuple[int, int]] = set()
        for row in rows:
            pair = (
                int(row["source_volume_id"]),
                int(row["target_volume_id"]),
            )
            stored_pairs.add(pair)
            manual = pair in manual_pairs
            source_percent = float(row["source_coverage_percent"])
            target_percent = float(row["target_coverage_percent"])
            complete = bool(row["complete_structure"])
            overlap = max(source_percent, target_percent)
            if (
                not complete
                and not manual
                and overlap < self.options.mirror_candidate_threshold_percent
            ):
                # This row existed only because the pair was manually linked
                # when the analysis ran. Do not leave a ghost suggestion after
                # that relationship is removed or changed.
                continue
            if complete:
                evidence = "Complete root folder metadata structure matches."
            elif overlap >= self.options.mirror_candidate_threshold_percent:
                stored_evidence = str(row["evidence_text"] or "")
                evidence = (
                    stored_evidence
                    if "manual mirror relationship" not in stored_evidence.casefold()
                    else (
                        "A large share of normalized filename-and-size records overlaps; "
                        "review paths and timestamps before declaring a mirror."
                    )
                )
            else:
                evidence = (
                    "The current manual mirror relationship is shown even though its "
                    "saved metadata overlap is below the suggestion threshold."
                )
            candidates.append(
                MirrorCandidate(
                    pair[0],
                    pair[1],
                    source_percent,
                    target_percent,
                    int(row["matched_files"]),
                    complete,
                    evidence,
                    manual,
                )
            )
        for source, target in sorted(manual_pairs - stored_pairs):
            candidates.append(
                MirrorCandidate(
                    source,
                    target,
                    None,
                    None,
                    0,
                    False,
                    (
                        "This manual mirror relationship was added after the saved-metadata "
                        "analysis. Update the analysis to calculate its current metadata overlap."
                    ),
                    True,
                )
            )
        return sorted(
            candidates,
            key=lambda candidate: (
                not candidate.complete_structure,
                not candidate.manual_mirror_link,
                -max(
                    candidate.source_coverage_percent or 0.0,
                    candidate.target_coverage_percent or 0.0,
                ),
                candidate.source_volume_id,
                candidate.target_volume_id,
            ),
        )

    def scan_records(self) -> list[ScanRecord]:
        rows = list(self.connection.execute(
            """
            WITH attempts AS (
                SELECT sh.*,
                       ROW_NUMBER() OVER (PARTITION BY volume_id ORDER BY id DESC) AS rank
                FROM scan_history sh
            ),
            applied AS (
                SELECT sh.*,
                       ROW_NUMBER() OVER (PARTITION BY volume_id ORDER BY id DESC) AS rank
                FROM scan_history sh
                WHERE status = 'completed'
            )
            SELECT v.id AS volume_id, COALESCE(r.drive_id, '') AS drive_id,
                   v.name AS volume_name, v.indexed_file_count, v.indexed_folder_count,
                   a.id AS latest_attempt_scan_id,
                   a.status AS latest_attempt_status,
                   COALESCE(a.finished_at, a.started_at) AS latest_attempt_at,
                   a.errors_count AS latest_attempt_errors,
                   a.files_seen AS latest_attempt_files,
                   a.folders_seen AS latest_attempt_folders,
                   a.message AS latest_attempt_message,
                   p.id AS last_applied_scan_id,
                   COALESCE(p.finished_at, p.started_at) AS last_applied_at,
                   p.errors_count AS last_applied_errors
            FROM volumes v
            LEFT JOIN volume_register r ON r.volume_id = v.id
            LEFT JOIN attempts a ON a.volume_id = v.id AND a.rank = 1
            LEFT JOIN applied p ON p.volume_id = v.id AND p.rank = 1
            ORDER BY v.id
            """
        ))
        scan_ids = sorted(
            {
                int(scan_id)
                for row in rows
                for scan_id in (
                    row["latest_attempt_scan_id"],
                    row["last_applied_scan_id"],
                )
                if scan_id is not None
            }
        )
        ignored_by_scan: dict[int, int] = {}
        for start in range(0, len(scan_ids), 500):
            chunk = scan_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for error in self.connection.execute(
                f"SELECT scan_id, path FROM scan_errors WHERE scan_id IN ({placeholders})",
                chunk,
            ):
                if error["scan_id"] is not None and _is_ignored_system_scan_path(
                    error["path"]
                ):
                    scan_id = int(error["scan_id"])
                    ignored_by_scan[scan_id] = ignored_by_scan.get(scan_id, 0) + 1

        records: list[ScanRecord] = []
        for row in rows:
            applied_at = row["last_applied_at"]
            applied_errors = row["last_applied_errors"]
            latest_error_count = int(row["latest_attempt_errors"] or 0)
            applied_error_count = int(applied_errors or 0)
            latest_scan_id = row["latest_attempt_scan_id"]
            applied_scan_id = row["last_applied_scan_id"]
            latest_ignored = min(
                latest_error_count,
                ignored_by_scan.get(int(latest_scan_id), 0)
                if latest_scan_id is not None
                else 0,
            )
            applied_ignored = min(
                applied_error_count,
                ignored_by_scan.get(int(applied_scan_id), 0)
                if applied_scan_id is not None
                else 0,
            )
            applied_relevant_errors = max(0, applied_error_count - applied_ignored)
            file_count = int(row["indexed_file_count"] or 0)
            # Scan health describes the catalogue snapshot currently being
            # analysed. Failed/cancelled/discarded later attempts did not apply
            # content and are reported separately in latest_attempt_* fields.
            if applied_at is None:
                health = "not_scanned"
            elif applied_relevant_errors > 0:
                health = "completed_with_errors"
            elif file_count == 0:
                health = "empty"
            else:
                health = "completed"
            records.append(
                ScanRecord(
                    volume_id=int(row["volume_id"]),
                    drive_id=str(row["drive_id"] or ""),
                    volume_name=row["volume_name"],
                    indexed_file_count=file_count,
                    indexed_folder_count=int(row["indexed_folder_count"] or 0),
                    latest_attempt_status=row["latest_attempt_status"],
                    latest_attempt_at=row["latest_attempt_at"],
                    latest_attempt_errors=row["latest_attempt_errors"],
                    latest_attempt_files=row["latest_attempt_files"],
                    latest_attempt_folders=row["latest_attempt_folders"],
                    latest_attempt_message=row["latest_attempt_message"],
                    last_applied_at=applied_at,
                    last_applied_errors=applied_errors,
                    health_status=health,
                    latest_attempt_ignored_errors=latest_ignored,
                    last_applied_ignored_errors=applied_ignored,
                )
            )
        return records
