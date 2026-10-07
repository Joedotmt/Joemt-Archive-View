from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import heapq
from itertools import chain, groupby
import json
from operator import itemgetter
import re
import sqlite3
import unicodedata
from typing import Any, Callable, Iterable, Iterator, NamedTuple, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database
from .content_hash import HASH_LABELS, HASH_LENGTHS


RULES_VERSION = 4
# Two records are the "same file" by metadata when name and size agree and the
# saved modified times are within this window, or differ by exactly one hour
# (FAT/exFAT drives store local time, so DST copies land an hour apart).
MODIFIED_TIME_TOLERANCE_SECONDS = 2
MODIFIED_TIME_HOUR_OFFSET_SECONDS = 3600
# Huge identical-content or generic-name groups store at most this many drive
# ids per record; the true drive count travels in the evidence text.
MAX_STORED_OTHER_VOLUME_IDS = 64
# A folder persists at most this many per-drive match rows: structure matches
# and drives holding strong evidence first. Weak-only drives are listed only
# when no drive holds strong evidence, and then only the best one.
MAX_STORED_FOLDER_DRIVE_MATCHES = 32
CONTENT_HASH_LENGTHS = HASH_LENGTHS
_CONTENT_HASH_ALIASES = {key.replace("_", ""): key for key in CONTENT_HASH_LENGTHS}
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

# Evidence prose is persisted per analysis run. Keep these strings stable:
# the UI derives a short reason label from the exact text (see
# folder_reason_key / file_reason_key), so rewording here would orphan every
# saved analysis until it is re-run.
FOLDER_EVIDENCE_EXCLUDED = (
    "This folder contains only known operating-system metadata, "
    "which is excluded from copy coverage."
)
FOLDER_EVIDENCE_EMPTY = "The saved catalogue contains no files in this folder."
FOLDER_EVIDENCE_UNKNOWN_SCAN = (
    "The folder is empty in the catalogue, but the latest successful scan is "
    "missing or reported access errors."
)
FOLDER_EVIDENCE_TRUSTED_HASHED = (
    "A complete hash-aware structure matches on another drive: exact "
    "content hashes, descendant filenames, and the content-bearing "
    "folder layout all match. The folder name also matches. Known "
    "operating-system metadata is excluded."
)
FOLDER_EVIDENCE_TRUSTED_LEGACY = (
    "A complete metadata structure matches on another drive: at least "
    "two content-file names and exact sizes plus the content-bearing "
    "folder layout. These legacy records do not contain saved hashes. "
    "The folder name also matches. Known operating-system metadata is "
    "excluded."
)
FOLDER_EVIDENCE_SUPPRESSED_SUFFIX = (
    " Additional repeated structures were suppressed because they "
    "could not be paired without reusing a target folder."
)
FOLDER_EVIDENCE_MIXED_HASH = (
    "The content-bearing structure matches, but only some descendant "
    "records have comparable saved hashes. Mixed hash and legacy "
    "evidence remains possible rather than being labelled complete."
)
FOLDER_EVIDENCE_UNTRUSTED_AND_RENAMED = (
    "A content-bearing subtree matches structurally on other drives, "
    "but some folder names differ and at least one exact-name pair has "
    "an incomplete or error-bearing scan. This remains possible evidence."
)
FOLDER_EVIDENCE_UNTRUSTED_SCAN = (
    "The saved folder name and content-bearing structure match, but at "
    "least one applied scan has access errors or no trustworthy "
    "denominator, so the result is not labelled complete."
)
FOLDER_EVIDENCE_RENAMED = (
    "The content-bearing subtree has the same descendant names, exact "
    "sizes, and layout, but the folder's own name differs. This is "
    "possible evidence, not a complete folder match. Known "
    "operating-system metadata is excluded."
)
FOLDER_EVIDENCE_COVERAGE = (
    "Candidate descendant hash or filename-and-size matches were counted "
    "on the best single other drive; a complete hash-aware folder structure "
    "was not found."
)
FOLDER_EVIDENCE_AMBIGUOUS = (
    "One or more descendant candidates are too common or compete with "
    "repeated records, so the saved metadata cannot identify a useful "
    "folder copy without creating misleading links."
)
FOLDER_EVIDENCE_SINGLE = (
    "No descendant metadata match was found on another drive in this catalogue."
)

FILE_EVIDENCE_HASH_VERIFIED = "An exact content hash matches on another drive."
FILE_EVIDENCE_HASH_VERIFIED_EXTRA_DRIVES = (
    " Additional drives have metadata-only evidence because a "
    "comparable saved hash was unavailable there."
)
FILE_EVIDENCE_PAIRED_SUFFIX = " Repeated records are paired one-to-one per drive."
FILE_EVIDENCE_METADATA_LIKELY = (
    "A comparable saved content hash was unavailable for at least one "
    "record. Normalized filename, exact size, modified time, and parent "
    "path match; repeated records are paired one-to-one per drive."
)
FILE_EVIDENCE_NAME_SIZE_ONLY = (
    "A file with the same name and size exists on another drive, but its modified "
    "time differs or is missing, so it is a lead rather than a confirmed copy."
)
FILE_EVIDENCE_HASH_CONFLICT = (
    "Comparable content hashes differ even though the filename "
    "and size metadata match. The records are not treated as copies."
)
FILE_EVIDENCE_COMPETING = (
    "Several records on these drives share the same normalized filename "
    "and exact size. The saved metadata cannot identify which repeated "
    "item corresponds, so no arbitrary one-to-one copy is claimed."
)
FILE_EVIDENCE_REPETITIVE_HASH = (
    "Exact hashed content occurs too repetitively to map safely to an "
    "individual catalogue record. No arbitrary target is claimed."
)
FILE_EVIDENCE_TOO_COMMON_PREFIX = (
    "The normalized filename-and-size combination is too common to "
    "identify a useful copy:"
)
ITEM_EVIDENCE_MISSING = "This catalogue record is marked missing."

# Rules version 4: an item's label describes what exists on other drives, by
# strength of evidence. One-to-one pairing is kept only for coverage arithmetic.
FOLDER_EVIDENCE_FULL_MATCH_HASHED = (
    "The whole folder matches on another drive: same folder name, same layout, "
    "and every file's name, size and saved content hash."
)
FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED = (
    "The whole folder matches on another drive by folder name, layout, file names "
    "and sizes; the files that have saved hashes match too, but not every file "
    "has one."
)
FOLDER_EVIDENCE_FULL_MATCH_LEGACY = (
    "The whole folder matches on another drive by folder name, layout, file names "
    "and sizes. These records have no saved hashes, so contents were not compared."
)
FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS = (
    " A scan of one of the two drives reported access errors, so its listing may "
    "be incomplete."
)
FOLDER_EVIDENCE_SUFFIX_RENAMED = " The folder is named differently on that drive."
FOLDER_EVIDENCE_ALL_FILES_HASHED = (
    "Every file in this folder has a hash-verified copy on one other drive; the "
    "folder layout there differs or could not be matched as a unit."
)
FOLDER_EVIDENCE_ALL_FILES_FOUND = (
    "Every file in this folder was found on one other drive by name, size and "
    "modified time; the folder layout there differs or could not be matched as a "
    "unit. Contents were not compared."
)
FOLDER_EVIDENCE_ALL_FILES_WEAK = (
    "Every file in this folder has a same-name, same-size file on one other drive, "
    "but modified times differ or are missing, so these are leads rather than "
    "confirmed copies."
)
FOLDER_EVIDENCE_PARTIAL = (
    "Only part of this folder was found on the best single other drive, counting "
    "files that match by name, size and modified time."
)
FOLDER_EVIDENCE_STRUCTURE_REPEATS_PREFIX = (
    "This folder structure repeats too often to pair copies safely:"
)
# Per-drive folder match rows whose evidence starts with this text hold the
# whole folder as a structure (exact or renamed). Drives that merely hold every
# file loose, by name, size and time, never start with it, so a "Full match"
# folder counts only real whole-folder copies as strong drives.
FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX = "Whole folder matches here"
_FULL_MATCH_REASON_KEYS = frozenset(
    {"full_match_hashed", "full_match_partly_hashed", "full_match_legacy"}
)
FILE_EVIDENCE_TIME_MATCH = (
    "A file with the same name, size and modified time (within 2 seconds, or an "
    "exact one-hour clock offset) exists on another drive. Contents were not "
    "compared."
)
FILE_EVIDENCE_GENERIC_PREFIX = "This name and size is generic across the catalogue:"
FILE_EVIDENCE_CONFLICT_NOTE = (
    " A same-name, same-size file with different content (hash mismatch) exists "
    "on another drive."
)
FILE_EVIDENCE_REPEATED_CONTENT_NOTE = (
    " This exact content occurs on many drives, so individual copies were not "
    "paired for coverage."
)

_FOLDER_SUFFIXES = (
    FOLDER_EVIDENCE_SUPPRESSED_SUFFIX,
    FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS,
    FOLDER_EVIDENCE_SUFFIX_RENAMED,
)
_FOLDER_SUFFIX_FLAGS = {
    FOLDER_EVIDENCE_SUPPRESSED_SUFFIX: "suppressed",
    FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS: "scan_errors",
    FOLDER_EVIDENCE_SUFFIX_RENAMED: "renamed",
}
_FILE_NOTES = (
    FILE_EVIDENCE_PAIRED_SUFFIX,
    FILE_EVIDENCE_HASH_VERIFIED_EXTRA_DRIVES,
    FILE_EVIDENCE_CONFLICT_NOTE,
    FILE_EVIDENCE_REPEATED_CONTENT_NOTE,
)
_FILE_NOTE_FLAGS = {
    FILE_EVIDENCE_HASH_VERIFIED_EXTRA_DRIVES: "extra_drives",
    FILE_EVIDENCE_CONFLICT_NOTE: "conflict",
    FILE_EVIDENCE_REPEATED_CONTENT_NOTE: "repeated_content",
}
_FOLDER_REASON_KEYS = {
    FOLDER_EVIDENCE_EXCLUDED: "excluded",
    FOLDER_EVIDENCE_EMPTY: "empty",
    FOLDER_EVIDENCE_UNKNOWN_SCAN: "unknown_scan",
    FOLDER_EVIDENCE_FULL_MATCH_HASHED: "full_match_hashed",
    FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED: "full_match_partly_hashed",
    FOLDER_EVIDENCE_FULL_MATCH_LEGACY: "full_match_legacy",
    FOLDER_EVIDENCE_ALL_FILES_HASHED: "all_files_hashed",
    FOLDER_EVIDENCE_ALL_FILES_FOUND: "all_files_found",
    FOLDER_EVIDENCE_ALL_FILES_WEAK: "all_files_weak",
    FOLDER_EVIDENCE_PARTIAL: "partial",
    FOLDER_EVIDENCE_SINGLE: "single",
    # Rules-version 3 texts; still recognised so an outdated run reads sanely.
    FOLDER_EVIDENCE_TRUSTED_HASHED: "trusted_hashed",
    FOLDER_EVIDENCE_TRUSTED_LEGACY: "trusted_legacy",
    FOLDER_EVIDENCE_MIXED_HASH: "mixed_hash",
    FOLDER_EVIDENCE_UNTRUSTED_AND_RENAMED: "untrusted_and_renamed",
    FOLDER_EVIDENCE_UNTRUSTED_SCAN: "untrusted_scan",
    FOLDER_EVIDENCE_RENAMED: "renamed",
    FOLDER_EVIDENCE_COVERAGE: "coverage",
    FOLDER_EVIDENCE_AMBIGUOUS: "ambiguous",
}
_FILE_REASON_KEYS = {
    FILE_EVIDENCE_TIME_MATCH: "time_match",
    FILE_EVIDENCE_NAME_SIZE_ONLY: "name_size_only",
    FILE_EVIDENCE_HASH_CONFLICT: "hash_conflict",
    # Rules-version 3 texts.
    FILE_EVIDENCE_METADATA_LIKELY: "metadata_likely",
    FILE_EVIDENCE_COMPETING: "competing",
    FILE_EVIDENCE_REPETITIVE_HASH: "repetitive_hash",
}
_COUNTS_PATTERN = re.compile(r"([\d,]+) (?:records|folders) across ([\d,]+) drives")


def _strip_known_suffixes(text: str, suffixes: tuple[str, ...]) -> tuple[str, frozenset[str]]:
    flags: set[str] = set()
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                flags.add(suffix)
                changed = True
    return text, frozenset(flags)


def folder_reason_key(evidence_text: str | None) -> str:
    """Map saved folder evidence prose to a stable short key ('' if unknown)."""
    text = str(evidence_text or "").strip()
    if not text:
        return ""
    # Existing saved analyses used SHA-256-specific prose.
    text = text.replace("SHA-256 content hash", "content hash").replace("saved SHA-256 hash", "saved content hash")
    text, _ = _strip_known_suffixes(text, _FOLDER_SUFFIXES)
    if text.startswith(FOLDER_EVIDENCE_STRUCTURE_REPEATS_PREFIX):
        return "structure_repeats"
    return _FOLDER_REASON_KEYS.get(text, "")


def folder_evidence_flags(evidence_text: str | None) -> frozenset[str]:
    """Return the qualifier flags appended to folder evidence: scan_errors, renamed, suppressed."""
    text = str(evidence_text or "").strip()
    _, suffixes = _strip_known_suffixes(text, _FOLDER_SUFFIXES)
    return frozenset(_FOLDER_SUFFIX_FLAGS[suffix] for suffix in suffixes)


def file_reason_key(evidence_text: str | None) -> str:
    """Map saved file evidence prose to a stable short key ('' if unknown)."""
    text = str(evidence_text or "").strip()
    if not text:
        return ""
    text = text.replace("SHA-256 content hash", "content hash")
    text, _ = _strip_known_suffixes(text, _FILE_NOTES)
    if text.startswith(FILE_EVIDENCE_HASH_VERIFIED):
        return "hash_verified"
    if text.startswith(FILE_EVIDENCE_GENERIC_PREFIX):
        return "generic"
    if text.startswith(FILE_EVIDENCE_TOO_COMMON_PREFIX):
        return "too_common"
    return _FILE_REASON_KEYS.get(text, "")


def file_evidence_flags(evidence_text: str | None) -> frozenset[str]:
    """Return the note flags appended to file evidence: conflict, extra_drives, repeated_content.

    Notes are matched anywhere in the text: the repeated-content note is
    followed by its record/drive counts sentence, so it is not always the tail.
    """
    text = str(evidence_text or "")
    return frozenset(flag for note, flag in _FILE_NOTE_FLAGS.items() if note in text)


def evidence_counts(evidence_text: str | None) -> tuple[int, int] | None:
    """Return (records, drives) embedded in generic/repeating evidence, if any."""
    match = _COUNTS_PATTERN.search(str(evidence_text or ""))
    if match is None:
        return None
    return int(match.group(1).replace(",", "")), int(match.group(2).replace(",", ""))


def _parse_modified_time(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_MODIFIED_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
# The scanner saves "2026-08-19T09:00:00.000000+0000": 31 characters, UTC.
_CANONICAL_MODIFIED_TIME_LENGTH = 31


def _canonical_modified_time(value: Any) -> str | None:
    """Rewrite a saved modified time as UTC in the scanner's own format.

    Generic groups are swept in saved-text order, so equal instants written
    with different offsets or formats ('+00:00', 'Z', no microseconds) must
    sort together. Text the scanner already wrote passes straight through;
    unparseable text is kept as it is and never matches by time.
    """
    if value is None:
        return None
    text = str(value)
    if (
        len(text) == _CANONICAL_MODIFIED_TIME_LENGTH
        and text.endswith("+0000")
        and text[10] == "T"
        and text[19] == "."
    ):
        return text
    parsed = _parse_modified_time(text)
    if parsed is None:
        return text
    return parsed.astimezone(timezone.utc).strftime(_MODIFIED_TIME_FORMAT)


def _instants_match(
    first_text: str,
    first_instant: datetime | None,
    second_text: str,
    second_instant: datetime | None,
) -> bool:
    """modified_times_match on already-parsed values, so groups parse each row once."""
    if first_instant is None or second_instant is None:
        # Two empty or unreadable values never match just by being equal text.
        return False
    if first_text == second_text:
        return True
    delta = abs((first_instant - second_instant).total_seconds())
    return (
        delta <= MODIFIED_TIME_TOLERANCE_SECONDS
        or abs(delta - MODIFIED_TIME_HOUR_OFFSET_SECONDS) <= MODIFIED_TIME_TOLERANCE_SECONDS
    )


def modified_times_match(first: Any, second: Any) -> bool:
    """Same-file test for saved modified times: within 2 s, or exactly 1 h apart."""
    if first is None or second is None:
        return False
    return _instants_match(
        str(first),
        _parse_modified_time(first),
        str(second),
        _parse_modified_time(second),
    )


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


@dataclass(frozen=True)
class AnalysisOptions:
    batch_size: int = 10_000
    mirror_candidate_threshold_percent: float = 50.0
    # A project file kept on up to 32 of the catalogue's drives is still judged
    # with full tiers (time tolerance, conflicts); only groups beyond these
    # bounds take the cheaper "generic name" path.
    max_candidate_volumes_per_key: int = 32
    max_candidate_records_per_key: int = 256
    max_candidate_edges_per_key: int = 4_096
    max_strong_volumes_per_signature: int = 32
    max_strong_records_per_signature: int = 512
    max_strong_edges_per_signature: int = 4_096
    max_hash_volumes_per_signature: int = 32
    max_hash_records_per_signature: int = 512
    max_hash_edges_per_signature: int = 4_096
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
    verified_volume_ids: tuple[int, ...] = ()
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
    latest_attempt_hash_errors: int = 0
    last_applied_hash_errors: int = 0


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


class _FileRecord(NamedTuple):
    """One same-name, same-size candidate, parsed once for pairwise comparison."""

    file_id: int
    volume_id: int
    parent_path_key: bytes
    modified_at: str
    modified_instant: datetime | None
    content_hash: tuple[str, bytes] | None
    size_bytes: int


# Per-(folder, target drive) coverage slots, accumulated up the folder tree.
_ALL_FILES, _STRONG_FILES, _STRONG_BYTES, _VERIFIED_FILES = range(4)

# Every (file, other drive) existence row, whichever pass recorded it: per
# record for bounded groups, per (key, saved time text, own drive) for generic
# groups without hashes, per (hash, own drive) for over-bound hash groups. The
# three sources are disjoint by construction: the metadata pass never repeats a
# drive the hash pass already wrote, and a generic record that carries a hash
# falls back to per-record rows. No file is therefore counted twice per drive.
def _file_target_rows_sql(*, with_depth: bool) -> str:
    """The three existence sources as one row set.

    with_depth adds the file's folder depth (looked up by primary key inside
    each arm, which is far cheaper than joining the grouped result), so the
    folder pass can read coverage deepest-first straight from the cursor.
    """
    depth_column = ", wfo.depth" if with_depth else ""
    depth_join = (
        "\n    JOIN backup_work_folders wfo ON wfo.folder_id = wf.folder_id" if with_depth else ""
    )
    return f"""
    SELECT wf.folder_id, wf.file_id, wf.size_bytes, t.target_volume_id,
           (t.status = 'likely') AS likely, t.verified AS verified{depth_column}
    FROM backup_stage_file_targets t
    JOIN backup_work_files wf ON wf.file_id = t.file_id{depth_join}
    UNION ALL
    SELECT wf.folder_id, wf.file_id, wf.size_bytes, kt.target_volume_id,
           (kt.status = 'likely') AS likely, kt.verified AS verified{depth_column}
    FROM backup_stage_key_targets kt
    JOIN backup_work_files wf
      ON wf.file_key = kt.file_key
     AND wf.volume_id = kt.source_volume_id
     AND COALESCE(wf.modified_at, '') = kt.time_signature{depth_join}
    WHERE wf.evidence_eligible = 1 AND wf.content_hash IS NULL
    UNION ALL
    SELECT wf.folder_id, wf.file_id, wf.size_bytes, ht.target_volume_id,
           1 AS likely, 1 AS verified{depth_column}
    FROM backup_stage_hash_targets ht
    JOIN backup_work_files wf
      ON wf.content_hash_algorithm = ht.content_hash_algorithm
     AND wf.content_hash = ht.content_hash
     AND wf.volume_id = ht.source_volume_id{depth_join}
    WHERE wf.evidence_eligible = 1 AND wf.content_hash IS NOT NULL
"""


_FILE_TARGET_ROWS_SQL = _file_target_rows_sql(with_depth=False)
_FILE_TARGET_ROWS_WITH_DEPTH_SQL = _file_target_rows_sql(with_depth=True)


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


def _normalized_content_hash(
    algorithm: str | None,
    digest: Any,
) -> tuple[str, bytes] | None:
    """Return a canonical, comparable saved content hash.

    A malformed or unsupported value is deliberately treated as unavailable,
    so legacy and partially hashed catalogues retain the metadata fallback.
    """
    normalized_algorithm = re.sub(r"[^a-z0-9]", "", str(algorithm or "").casefold())
    normalized_algorithm = _CONTENT_HASH_ALIASES.get(normalized_algorithm, normalized_algorithm)
    expected_length = CONTENT_HASH_LENGTHS.get(normalized_algorithm)
    if expected_length is None or digest is None:
        return None
    try:
        normalized_digest = bytes(digest)
    except (TypeError, ValueError):
        return None
    if len(normalized_digest) != expected_length:
        return None
    return normalized_algorithm, normalized_digest


def _file_structure_key(
    name: str,
    size_bytes: int,
    content_hash: tuple[str, bytes] | None,
) -> bytes:
    """Build a folder token that never hides a known content conflict."""
    normalized_name = _normalized_text(name).encode("utf-8")
    normalized_size = str(int(size_bytes)).encode("ascii")
    if content_hash is not None:
        algorithm, digest = content_hash
        return _digest(
            b"jvvv-file-content-v1",
            normalized_name,
            normalized_size,
            algorithm.encode("ascii"),
            digest,
        )
    return _digest(b"jvvv-file-metadata-v1", normalized_name, normalized_size)


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


def _capped_ids(values: Iterable[int]) -> list[int]:
    """Sorted unique drive ids, truncated to the stored cap for huge groups."""
    return sorted({int(value) for value in values})[:MAX_STORED_OTHER_VOLUME_IDS]


def _capped_ids_preferring(preferred: Iterable[int], others: Iterable[int]) -> list[int]:
    """Sorted unique drive ids within the cap, keeping every preferred id first.

    When a huge group must be truncated, the drives holding stronger evidence
    survive and the weaker ones are dropped.
    """
    kept = _capped_ids(preferred)
    kept.extend(
        _capped_ids(set(others) - set(kept))[: MAX_STORED_OTHER_VOLUME_IDS - len(kept)]
    )
    return sorted(kept)


def _saved_hashes_equal(
    first: tuple[str, bytes] | None,
    second: tuple[str, bytes] | None,
) -> bool | None:
    """True/False when both records carry comparable hashes; None when they cannot be compared."""
    if first is None or second is None or first[0] != second[0]:
        return None
    return first[1] == second[1]


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


def _other_drive_count(item_type: str, evidence: str, other_ids: Sequence[int]) -> int:
    """True count of other drives; huge groups store capped ids but keep counts in the text."""
    counted = (
        file_reason_key(evidence) == "generic"
        or FILE_EVIDENCE_REPEATED_CONTENT_NOTE in evidence
        if item_type == "file"
        else folder_reason_key(evidence) == "structure_repeats"
    )
    counts = evidence_counts(evidence) if counted else None
    if counts is None:
        return len(other_ids)
    # The counts sentence includes the item's own drive.
    return max(len(other_ids), counts[1] - 1)


def _add_pair_count(
    pair_counts: dict[tuple[int, int], list[int]],
    first: int,
    second: int,
    size_bytes: int,
) -> None:
    pair_counts[(first, second)][0] += 1
    pair_counts[(first, second)][1] += size_bytes


class BackupAnalysisEngine:
    """Compare records already stored in a catalogue; never access source paths."""

    def __init__(self, db: Database, options: AnalysisOptions | None = None) -> None:
        self.db = db
        self.connection = db.connection
        self.options = options or AnalysisOptions()

    def ensure_schema(self, *, defer_persistent_indexes: bool = False) -> None:
        with self.db.transaction():
            for statement in ANALYSIS_SCHEMA_SQL:
                normalized = statement.lstrip().casefold()
                if defer_persistent_indexes and (
                    normalized.startswith("create index")
                    or normalized.startswith("create unique index")
                ):
                    continue
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO backup_analysis_state (
                    id, active_run_id, forced_stale, stale_reason, updated_at
                ) VALUES (1, NULL, 0, '', ?)
                """,
                (_utc_now(),),
            )

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
            "backup_stage_key_targets",
            "backup_stage_hash_targets",
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
                structure_key BLOB NOT NULL,
                parent_path_key BLOB NOT NULL,
                modified_at TEXT,
                size_bytes INTEGER NOT NULL,
                evidence_eligible INTEGER NOT NULL,
                content_hash BLOB,
                content_hash_algorithm TEXT NOT NULL
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
                possible_volume_ids TEXT NOT NULL,
                verified_volume_ids TEXT NOT NULL
            )
            """,
            """
            CREATE TEMP TABLE backup_stage_file_targets (
                file_id INTEGER NOT NULL,
                target_volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                verified INTEGER NOT NULL,
                PRIMARY KEY (file_id, target_volume_id)
            ) WITHOUT ROWID
            """,
            # Generic name+size groups record existence once per (key, saved
            # modified-time text, own drive) instead of once per record; the
            # rows join back to backup_work_files for folder coverage.
            """
            CREATE TEMP TABLE backup_stage_key_targets (
                file_key BLOB NOT NULL,
                time_signature TEXT NOT NULL,
                source_volume_id INTEGER NOT NULL,
                target_volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                verified INTEGER NOT NULL,
                PRIMARY KEY (file_key, time_signature, source_volume_id, target_volume_id)
            ) WITHOUT ROWID
            """,
            # Identical content on more drives than the hash pairing bounds
            # allow: one existence row per (hash, own drive, other drive).
            """
            CREATE TEMP TABLE backup_stage_hash_targets (
                content_hash_algorithm TEXT NOT NULL,
                content_hash BLOB NOT NULL,
                source_volume_id INTEGER NOT NULL,
                target_volume_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                verified INTEGER NOT NULL,
                PRIMARY KEY (
                    content_hash_algorithm, content_hash, source_volume_id, target_volume_id
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TEMP TABLE backup_work_folders (
                folder_id INTEGER PRIMARY KEY,
                volume_id INTEGER NOT NULL,
                parent_id INTEGER,
                fingerprint BLOB NOT NULL,
                recursive_file_count INTEGER NOT NULL,
                recursive_size_bytes INTEGER NOT NULL,
                depth INTEGER NOT NULL
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
        *,
        defer_persistent_indexes: bool = False,
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
            self.ensure_schema(defer_persistent_indexes=defer_persistent_indexes)
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
                "CREATE INDEX temp.idx_backup_work_file_hash "
                "ON backup_work_files(content_hash_algorithm, content_hash, volume_id) "
                "WHERE content_hash IS NOT NULL"
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
                "Saved catalogue hashes and metadata analysed. No source drive or file content was read.",
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
            SELECT id, volume_id, folder_id, name, relative_path, size_bytes, modified_at,
                   content_hash, content_hash_algorithm
            FROM files
            WHERE missing = 0
            ORDER BY id
            """
        ):
            evidence_eligible = not _is_copy_metadata_noise(
                row["name"], row["relative_path"]
            )
            saved_hash = _normalized_content_hash(
                row["content_hash_algorithm"], row["content_hash"]
            )
            batch.append(
                (
                    int(row["id"]),
                    int(row["volume_id"]),
                    row["folder_id"],
                    _file_key(row["name"], int(row["size_bytes"] or 0)),
                    _file_structure_key(
                        row["name"], int(row["size_bytes"] or 0), saved_hash
                    ),
                    _path_key(row["relative_path"]),
                    _canonical_modified_time(row["modified_at"]),
                    int(row["size_bytes"] or 0),
                    int(evidence_eligible),
                    saved_hash[1] if saved_hash is not None else None,
                    saved_hash[0] if saved_hash is not None else "",
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
                        "[]",
                    )
                )
            processed += 1
            if len(batch) >= self.options.batch_size:
                self.connection.executemany(
                    "INSERT INTO backup_work_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch
                )
                if excluded_batch:
                    self.connection.executemany(
                        "INSERT INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                "INSERT INTO backup_work_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch
            )
        if excluded_batch:
            self.connection.executemany(
                "INSERT INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                excluded_batch,
            )
        self._emit(progress_callback, "index_files", total, total, "Saved file metadata indexed")

    def _group_volume_ids(self, where: str, params: tuple[Any, ...]) -> set[int]:
        """Distinct drives holding one candidate group; bounded by the drive count, not by records."""
        return {
            int(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT volume_id FROM backup_work_files "
                f"WHERE evidence_eligible = 1 AND {where}",
                params,
            )
        }

    def _compare_hashes(
        self,
        total: int,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None,
    ) -> dict[tuple[int, int], list[int]]:
        """Record exact-content existence per drive; pair one-to-one only for coverage arithmetic."""
        pair_counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        result_batch: list[tuple[Any, ...]] = []
        target_batch: list[tuple[Any, ...]] = []
        hash_target_batch: list[tuple[Any, ...]] = []
        processed = 0

        def flush(force: bool = False) -> None:
            if not force and (
                len(result_batch) < self.options.batch_size
                and len(target_batch) < self.options.batch_size * 4
                and len(hash_target_batch) < self.options.batch_size * 4
            ):
                return
            if hash_target_batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_hash_targets VALUES (?, ?, ?, ?, 'likely', 1)",
                    hash_target_batch,
                )
                hash_target_batch.clear()
            if result_batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    result_batch,
                )
                result_batch.clear()
            if target_batch:
                self.connection.executemany(
                    """
                    INSERT INTO backup_stage_file_targets (
                        file_id, target_volume_id, status, verified
                    ) VALUES (?, ?, 'likely', 1)
                    ON CONFLICT(file_id, target_volume_id) DO UPDATE SET
                        status = 'likely', verified = 1
                    """,
                    target_batch,
                )
                target_batch.clear()

        def count_pairs(rows: list[sqlite3.Row]) -> None:
            """Pair identical-content records one-to-one per drive pair.

            Same name and parent path pair first, then whatever is left, so
            drive-pair percentages (mirror candidates) never count one target
            record twice. Pairing decides nothing else: item and folder
            verdicts follow existence, which stage() records separately.
            """
            by_volume: dict[int, list[sqlite3.Row]] = defaultdict(list)
            for row in rows:
                by_volume[int(row["volume_id"])].append(row)
            volume_ids = sorted(by_volume)

            def assign(
                source: int,
                target: int,
                source_row: sqlite3.Row,
                target_row: sqlite3.Row,
            ) -> None:
                _add_pair_count(pair_counts, source, target, int(source_row["size_bytes"] or 0))
                _add_pair_count(pair_counts, target, source, int(target_row["size_bytes"] or 0))

            for source_index, source in enumerate(volume_ids):
                for target in volume_ids[source_index + 1 :]:
                    source_rows = by_volume[source]
                    target_rows = by_volume[target]
                    source_by_context: dict[tuple[bytes, bytes], list[sqlite3.Row]] = defaultdict(list)
                    target_by_context: dict[tuple[bytes, bytes], list[sqlite3.Row]] = defaultdict(list)
                    for row in source_rows:
                        source_by_context[
                            (bytes(row["file_key"]), bytes(row["parent_path_key"]))
                        ].append(row)
                    for row in target_rows:
                        target_by_context[
                            (bytes(row["file_key"]), bytes(row["parent_path_key"]))
                        ].append(row)

                    used_source: set[int] = set()
                    used_target: set[int] = set()
                    for context in source_by_context.keys() & target_by_context.keys():
                        for source_row, target_row in zip(
                            source_by_context[context], target_by_context[context]
                        ):
                            used_source.add(int(source_row["file_id"]))
                            used_target.add(int(target_row["file_id"]))
                            assign(source, target, source_row, target_row)

                    remaining_source = [
                        row for row in source_rows if int(row["file_id"]) not in used_source
                    ]
                    remaining_target = [
                        row for row in target_rows if int(row["file_id"]) not in used_target
                    ]
                    for source_row, target_row in zip(remaining_source, remaining_target):
                        assign(source, target, source_row, target_row)

        def stage(
            rows: Iterable[sqlite3.Row],
            group_volume_ids: set[int],
            *,
            over_bound: bool,
            group_records: int,
            group_volumes: int,
        ) -> None:
            nonlocal processed
            # Over-bound groups write existence once per own drive, not per
            # record; the rows join back to every record of that hash there.
            staged_sources: set[int] = set()
            for member in rows:
                file_id = int(member["file_id"])
                source_volume = int(member["volume_id"])
                other_volumes = _capped_ids(group_volume_ids - {source_volume})
                evidence = FILE_EVIDENCE_HASH_VERIFIED
                if over_bound:
                    evidence += (
                        FILE_EVIDENCE_REPEATED_CONTENT_NOTE
                        + f" {group_records:,} records across {group_volumes:,} drives."
                    )
                ids_json = _json_ids(other_volumes)
                result_batch.append(
                    (
                        file_id,
                        source_volume,
                        "likely",
                        ids_json,
                        evidence,
                        ids_json,
                        "[]",
                        ids_json,
                    )
                )
                # Every drive holding this content is a verified target, so
                # folder coverage follows existence rather than pairing.
                if over_bound:
                    if source_volume not in staged_sources:
                        staged_sources.add(source_volume)
                        algorithm = str(member["content_hash_algorithm"])
                        digest = bytes(member["content_hash"])
                        hash_target_batch.extend(
                            (algorithm, digest, source_volume, target)
                            for target in sorted(group_volume_ids - {source_volume})
                        )
                else:
                    target_batch.extend((file_id, target) for target in other_volumes)
                processed += 1
                flush()

        members_cursor = self.connection.execute(
            """
            WITH candidate_hashes AS (
                SELECT content_hash_algorithm, content_hash,
                       COUNT(*) AS record_count,
                       COUNT(DISTINCT volume_id) AS volume_count
                FROM backup_work_files
                WHERE evidence_eligible = 1 AND content_hash IS NOT NULL
                GROUP BY content_hash_algorithm, content_hash
                HAVING COUNT(DISTINCT volume_id) > 1
            )
            SELECT wf.file_key, wf.file_id, wf.volume_id, wf.parent_path_key,
                   wf.size_bytes, wf.content_hash, wf.content_hash_algorithm,
                   candidates.record_count, candidates.volume_count
            FROM backup_work_files wf
            JOIN candidate_hashes candidates
              ON candidates.content_hash_algorithm = wf.content_hash_algorithm
             AND candidates.content_hash = wf.content_hash
            WHERE wf.evidence_eligible = 1
            ORDER BY wf.content_hash_algorithm, wf.content_hash,
                     wf.file_key, wf.parent_path_key, wf.volume_id, wf.file_id
            """
        )
        for key, hash_rows in groupby(
            members_cursor,
            key=lambda row: (
                str(row["content_hash_algorithm"]),
                bytes(row["content_hash"]),
            ),
        ):
            self._check_cancel(cancel_callback)
            first = next(hash_rows)
            group_records = int(first["record_count"])
            group_volumes = int(first["volume_count"])
            candidate_edges = group_records * (group_volumes - 1)
            over_bound = (
                group_volumes > self.options.max_hash_volumes_per_signature
                or group_records > self.options.max_hash_records_per_signature
                or candidate_edges > self.options.max_hash_edges_per_signature
            )
            all_rows = chain((first,), hash_rows)
            if over_bound:
                # Existence only: the drive set is small even when the record
                # list is enormous, and pairing is skipped for performance.
                group_volume_ids = self._group_volume_ids(
                    "content_hash_algorithm = ? AND content_hash = ?", key
                )
                stage(
                    all_rows,
                    group_volume_ids,
                    over_bound=True,
                    group_records=group_records,
                    group_volumes=group_volumes,
                )
            else:
                rows = list(all_rows)
                count_pairs(rows)
                stage(
                    rows,
                    {int(row["volume_id"]) for row in rows},
                    over_bound=False,
                    group_records=group_records,
                    group_volumes=group_volumes,
                )
            self._emit(
                progress_callback,
                "compare_files",
                min(processed, total),
                total,
                f"Comparing saved content hashes... {processed:,} candidate files",
            )
        flush(force=True)
        return pair_counts

    def _compare_files(
        self,
        total: int,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None,
    ) -> dict[tuple[int, int], list[int]]:
        self._emit(progress_callback, "compare_files", 0, total, "Comparing saved file metadata…")
        pair_counts = self._compare_hashes(total, progress_callback, cancel_callback)
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
            SELECT wf.file_key, wf.file_id, wf.volume_id, wf.parent_path_key,
                   wf.modified_at, wf.size_bytes,
                   wf.content_hash, wf.content_hash_algorithm,
                   existing.verified_volume_ids AS existing_verified_volume_ids,
                   existing.evidence_text AS existing_evidence,
                   candidates.record_count, candidates.volume_count
            FROM backup_work_files wf
            JOIN candidate_keys candidates ON candidates.file_key = wf.file_key
            LEFT JOIN backup_stage_file_results existing ON existing.file_id = wf.file_id
            WHERE wf.evidence_eligible = 1
            ORDER BY wf.file_key,
                     CASE WHEN wf.modified_at IS NULL THEN 1 ELSE 0 END,
                     wf.modified_at, wf.parent_path_key, wf.volume_id, wf.file_id
            """
        )
        result_batch: list[tuple[Any, ...]] = []
        target_batch: list[tuple[Any, ...]] = []
        key_target_batch: list[tuple[Any, ...]] = []
        processed = 0

        def flush_batches(force: bool = False) -> None:
            if not force and (
                len(result_batch) < self.options.batch_size
                and len(target_batch) < self.options.batch_size * 4
                and len(key_target_batch) < self.options.batch_size * 4
            ):
                return
            if key_target_batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_key_targets VALUES (?, ?, ?, ?, ?, ?)",
                    key_target_batch,
                )
                key_target_batch.clear()
            if result_batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO backup_stage_file_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    result_batch,
                )
                result_batch.clear()
            if target_batch:
                self.connection.executemany(
                    """
                    INSERT INTO backup_stage_file_targets (
                        file_id, target_volume_id, status, verified
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(file_id, target_volume_id) DO UPDATE SET
                        status = CASE
                            WHEN backup_stage_file_targets.status = 'likely'
                              OR excluded.status = 'likely'
                            THEN 'likely'
                            ELSE 'possible'
                        END,
                        verified = MAX(backup_stage_file_targets.verified, excluded.verified)
                    """,
                    target_batch,
                )
                target_batch.clear()

        def as_record(row: sqlite3.Row) -> _FileRecord:
            modified_at = str(row["modified_at"] or "")
            saved_hash = (
                (str(row["content_hash_algorithm"]), bytes(row["content_hash"]))
                if row["content_hash"] is not None
                else None
            )
            return _FileRecord(
                int(row["file_id"]),
                int(row["volume_id"]),
                bytes(row["parent_path_key"]),
                modified_at,
                _parse_modified_time(modified_at) if modified_at else None,
                saved_hash,
                int(row["size_bytes"] or 0),
            )

        def times_match(first: _FileRecord, second: _FileRecord) -> bool:
            if not first.modified_at or not second.modified_at:
                return False
            return _instants_match(
                first.modified_at,
                first.modified_instant,
                second.modified_at,
                second.modified_instant,
            )

        def time_delta(first: _FileRecord, second: _FileRecord) -> float:
            if (
                first.modified_at == second.modified_at
                or first.modified_instant is None
                or second.modified_instant is None
            ):
                return 0.0
            return abs((first.modified_instant - second.modified_instant).total_seconds())

        def within_tolerance(first: _FileRecord, second: _FileRecord) -> bool:
            """Generic groups: within 2 s only; the one-hour rule would chain unrelated copies."""
            if first.modified_instant is None or second.modified_instant is None:
                return False
            if first.modified_at == second.modified_at:
                return True
            return (
                abs((first.modified_instant - second.modified_instant).total_seconds())
                <= MODIFIED_TIME_TOLERANCE_SECONDS
            )

        def existence(
            records: list[_FileRecord],
        ) -> dict[int, tuple[set[int], set[int], set[int]]]:
            """Per record: other drives with an equal hash, a time match, or only differing hashes."""
            by_volume: dict[int, list[_FileRecord]] = defaultdict(list)
            for record in records:
                by_volume[record.volume_id].append(record)
            found: dict[int, tuple[set[int], set[int], set[int]]] = {}
            for record in records:
                verified: set[int] = set()
                strong: set[int] = set()
                conflict: set[int] = set()
                for volume, others in by_volume.items():
                    if volume == record.volume_id:
                        continue
                    best = 0
                    for other in others:
                        equal = _saved_hashes_equal(record.content_hash, other.content_hash)
                        if equal is True:
                            best = 3
                            break
                        if equal is False:
                            continue
                        best = max(best, 2 if times_match(record, other) else 1)
                    if best == 3:
                        verified.add(volume)
                    elif best == 2:
                        strong.add(volume)
                    elif best == 0:
                        conflict.add(volume)
                found[record.file_id] = (verified, strong, conflict)
            return found

        def pair_drive(
            source_records: list[_FileRecord],
            target_records: list[_FileRecord],
            match: Callable[[_FileRecord, _FileRecord], bool],
        ) -> list[tuple[_FileRecord, _FileRecord, str]]:
            """One-to-one coverage pairs between two drives.

            Time matches (per `match`) pair first (closest times, then same
            parent path); leftovers pair arbitrarily as 'possible'. Differing
            comparable hashes never pair. Equal hashes are consumed as 'hash'
            because the hash pass already staged and counted them.
            """
            candidates: list[tuple[int, float, int, int, int, _FileRecord, _FileRecord]] = []
            for source in source_records:
                for target in target_records:
                    equal = _saved_hashes_equal(source.content_hash, target.content_hash)
                    if equal is False:
                        continue
                    if equal is True:
                        candidates.append((0, 0.0, 0, source.file_id, target.file_id, source, target))
                    elif match(source, target):
                        candidates.append(
                            (
                                1,
                                time_delta(source, target),
                                int(source.parent_path_key != target.parent_path_key),
                                source.file_id,
                                target.file_id,
                                source,
                                target,
                            )
                        )
            candidates.sort(key=lambda item: item[:5])
            used_source: set[int] = set()
            used_target: set[int] = set()
            pairs: list[tuple[_FileRecord, _FileRecord, str]] = []
            for rank, _delta, _moved, _source_id, _target_id, source, target in candidates:
                if source.file_id in used_source or target.file_id in used_target:
                    continue
                used_source.add(source.file_id)
                used_target.add(target.file_id)
                pairs.append((source, target, "hash" if rank == 0 else "likely"))
            remaining_target = [
                target for target in target_records if target.file_id not in used_target
            ]
            for source in source_records:
                if source.file_id in used_source:
                    continue
                for index, target in enumerate(remaining_target):
                    equal = _saved_hashes_equal(source.content_hash, target.content_hash)
                    if equal is False:
                        continue
                    pairs.append((source, remaining_target.pop(index), "hash" if equal else "possible"))
                    break
            return pairs

        def count_pairs(
            records: list[_FileRecord],
            match: Callable[[_FileRecord, _FileRecord], bool] = times_match,
        ) -> None:
            """Drive-pair overlap for mirror candidates; decides no item or folder verdict.

            Only strong pairs count: time-matched here, hash-equal in the hash
            pass (skipped here so they are not counted twice). Records that
            agree on name and size alone never make two drives look alike.
            """
            by_volume: dict[int, list[_FileRecord]] = defaultdict(list)
            for record in records:
                by_volume[record.volume_id].append(record)
            volume_ids = sorted(by_volume)
            for source_index, source in enumerate(volume_ids):
                for target in volume_ids[source_index + 1 :]:
                    for source_record, target_record, status in pair_drive(
                        by_volume[source], by_volume[target], match
                    ):
                        if status != "likely":
                            continue
                        _add_pair_count(pair_counts, source, target, source_record.size_bytes)
                        _add_pair_count(pair_counts, target, source, target_record.size_bytes)

        def count_bucket_pairs(records: list[_FileRecord]) -> None:
            """Drive-pair overlap for one generic time bucket.

            Every record here shares the name and size; with no saved hashes
            two copies pair whenever their times lie within the tolerance, so
            a two-pointer walk over each drive's time-sorted records finds the
            one-to-one pairing without comparing every record with every
            other (500 copies on two drives would otherwise mean 250,000
            comparisons). A bucket holding a saved hash takes the full pairing
            so conflicting content never pairs.
            """
            if any(record.content_hash is not None for record in records):
                count_pairs(records, within_tolerance)
                return
            by_volume: dict[int, list[_FileRecord]] = defaultdict(list)
            for record in records:
                if record.modified_instant is not None:
                    by_volume[record.volume_id].append(record)
            for volume_records in by_volume.values():
                volume_records.sort(key=lambda record: record.modified_instant)
            volume_ids = sorted(by_volume)
            for source_index, source in enumerate(volume_ids):
                source_records = by_volume[source]
                for target in volume_ids[source_index + 1 :]:
                    target_records = by_volume[target]
                    source_at = target_at = 0
                    while source_at < len(source_records) and target_at < len(target_records):
                        first = source_records[source_at]
                        second = target_records[target_at]
                        delta = (first.modified_instant - second.modified_instant).total_seconds()
                        if abs(delta) <= MODIFIED_TIME_TOLERANCE_SECONDS:
                            _add_pair_count(pair_counts, source, target, first.size_bytes)
                            _add_pair_count(pair_counts, target, source, second.size_bytes)
                            source_at += 1
                            target_at += 1
                        elif delta < 0:
                            source_at += 1
                        else:
                            target_at += 1

        hash_covered_cache: dict[tuple[str, bytes, int], frozenset[int]] = {}

        def hash_covered_targets(algorithm: str, digest: bytes, own_volume: int) -> frozenset[int]:
            """Drives the hash pass already wrote for this (hash, own drive) as existence rows."""
            cache_key = (algorithm, digest, own_volume)
            covered = hash_covered_cache.get(cache_key)
            if covered is None:
                covered = frozenset(
                    int(row[0])
                    for row in self.connection.execute(
                        """
                        SELECT target_volume_id FROM backup_stage_hash_targets
                        WHERE content_hash_algorithm = ? AND content_hash = ?
                          AND source_volume_id = ?
                        """,
                        cache_key,
                    )
                )
                hash_covered_cache[cache_key] = covered
            return covered

        def stage_member(
            member: sqlite3.Row,
            *,
            group_volume_ids: set[int],
            verified: set[int],
            strong: set[int],
            conflict: set[int],
            generic_group: bool,
            group_records: int,
            group_volumes: int,
            key_targets: set[tuple[str, int]] | None = None,
        ) -> None:
            """Stage one record's verdict and its folder-coverage targets.

            key_targets (generic groups only) collects the (modified-time text,
            own drive) combinations already written to backup_stage_key_targets,
            so existence is recorded once per combination rather than per record.
            """
            nonlocal processed
            file_id = int(member["file_id"])
            own_volume = int(member["volume_id"])
            verified = (verified | set(_parse_ids(member["existing_verified_volume_ids"]))) - {own_volume}
            strong = (strong | verified) - {own_volume}
            # A drive whose same-name records all differ by hash is never a
            # lead, but it is still noted even when it holds a verified copy
            # under another name. The group spans more than one drive, so
            # every other drive is strong, possible or a conflict.
            possible = group_volume_ids - strong - conflict - {own_volume}
            existing_evidence = str(member["existing_evidence"] or "")
            if verified:
                status = "likely"
                evidence = FILE_EVIDENCE_HASH_VERIFIED
                if (strong - verified) or possible:
                    evidence += FILE_EVIDENCE_HASH_VERIFIED_EXTRA_DRIVES
                if FILE_EVIDENCE_REPEATED_CONTENT_NOTE in existing_evidence:
                    evidence += existing_evidence[
                        existing_evidence.index(FILE_EVIDENCE_REPEATED_CONTENT_NOTE) :
                    ]
            elif generic_group:
                status = "likely" if strong else "possible"
                evidence = (
                    FILE_EVIDENCE_GENERIC_PREFIX
                    + f" {group_records:,} records across {group_volumes:,} drives."
                )
            elif strong:
                status = "likely"
                evidence = FILE_EVIDENCE_TIME_MATCH
            elif possible:
                status = "possible"
                evidence = FILE_EVIDENCE_NAME_SIZE_ONLY
            else:
                status = "ambiguous"
                evidence = FILE_EVIDENCE_HASH_CONFLICT
            if conflict and status != "ambiguous":
                evidence += FILE_EVIDENCE_CONFLICT_NOTE
            result_batch.append(
                (
                    file_id,
                    own_volume,
                    status,
                    _json_ids(_capped_ids_preferring(strong, possible)),
                    evidence,
                    _json_ids(_capped_ids(strong)),
                    _json_ids(_capped_ids(possible)),
                    _json_ids(_capped_ids(verified)),
                )
            )
            # Folder coverage follows existence: one target row per other drive
            # that holds this file, strongest evidence first when a huge group
            # has to be capped. Verified drives were already written by the
            # hash pass (per record, or per hash for over-bound groups), so
            # they are not repeated here and no file is counted twice.
            likely_targets = _capped_ids(strong - verified)
            possible_targets = _capped_ids(possible)[
                : MAX_STORED_OTHER_VOLUME_IDS - len(likely_targets)
            ]
            if (
                member["content_hash"] is not None
                and FILE_EVIDENCE_REPEATED_CONTENT_NOTE in existing_evidence
            ):
                # An over-bound hash group stored its verified drive ids capped,
                # but wrote an existence row per (hash, own drive) for EVERY
                # drive holding the content. A drive beyond the cap therefore
                # looks unverified here while already being covered; writing
                # it again would count this file twice on that drive.
                hash_covered = hash_covered_targets(
                    str(member["content_hash_algorithm"]),
                    bytes(member["content_hash"]),
                    own_volume,
                )
                likely_targets = [target for target in likely_targets if target not in hash_covered]
                possible_targets = [
                    target for target in possible_targets if target not in hash_covered
                ]
            if key_targets is not None and member["content_hash"] is None:
                # Generic group without a saved hash: every record sharing this
                # key, time text and drive has the same other drives, so the
                # existence rows are written once and joined back by key.
                signature = str(member["modified_at"] or "")
                if (signature, own_volume) not in key_targets:
                    key_targets.add((signature, own_volume))
                    file_key = bytes(member["file_key"])
                    key_target_batch.extend(
                        (file_key, signature, own_volume, target, "likely", 0)
                        for target in likely_targets
                    )
                    key_target_batch.extend(
                        (file_key, signature, own_volume, target, "possible", 0)
                        for target in possible_targets
                    )
            else:
                target_batch.extend(
                    (file_id, target, "likely", 0) for target in likely_targets
                )
                target_batch.extend(
                    (file_id, target, "possible", 0) for target in possible_targets
                )
            processed += 1
            flush_batches()

        def stage_generic(
            rows: list[sqlite3.Row],
            strong_per_row: list[set[int]],
            group_volume_ids: set[int],
            group_records: int,
            group_volumes: int,
            key_targets: set[tuple[str, int]],
        ) -> None:
            for row, strong_volumes in zip(rows, strong_per_row):
                stage_member(
                    row,
                    group_volume_ids=group_volume_ids,
                    verified=set(),
                    strong=strong_volumes,
                    conflict=set(),
                    generic_group=True,
                    group_records=group_records,
                    group_volumes=group_volumes,
                    key_targets=key_targets,
                )

        def bucket_strong_per_row(
            bucket: list[sqlite3.Row],
            instants: list[datetime],
        ) -> list[set[int]]:
            """Per record: other drives holding a copy within the tolerance window.

            A chained bucket only bounds the work; two records ten seconds
            apart in one chain are not the same file. The bucket is walked in
            time order with a sliding window, so each record sees exactly the
            drives whose copies lie within MODIFIED_TIME_TOLERANCE_SECONDS.
            """
            order = sorted(range(len(bucket)), key=instants.__getitem__)
            volumes = [int(bucket[index]["volume_id"]) for index in order]
            times = [instants[index] for index in order]
            strong: list[set[int]] = [set() for _ in bucket]
            window: Counter[int] = Counter()
            window_volumes: set[int] = set()
            left = right = 0
            for position, own_index in enumerate(order):
                instant = times[position]
                while (instant - times[left]).total_seconds() > MODIFIED_TIME_TOLERANCE_SECONDS:
                    volume = volumes[left]
                    window[volume] -= 1
                    if window[volume] == 0:
                        del window[volume]
                        window_volumes = set(window)
                    left += 1
                while (
                    right < len(times)
                    and (times[right] - instant).total_seconds() <= MODIFIED_TIME_TOLERANCE_SECONDS
                ):
                    volume = volumes[right]
                    if volume not in window:
                        window_volumes = set(window) | {volume}
                    window[volume] += 1
                    right += 1
                strong[own_index] = window_volumes - {volumes[position]}
            return strong

        def time_buckets(
            rows: Iterable[sqlite3.Row],
        ) -> Iterator[tuple[list[sqlite3.Row], list[set[int]]]]:
            """Sweep a generic group, in saved-time order, into tolerance buckets.

            Consecutive records whose modified times are within
            MODIFIED_TIME_TOLERANCE_SECONDS of each other chain into one bucket
            (FAT drives round to 2 s); the chain only bounds the work. Yields
            (rows, strong_per_row): for each record the other drives holding a
            copy within the tolerance of THAT record, and an empty set for a
            record with no readable time or when too many copies share the
            time for any of them to count as strong. The record bound applies
            per drive, so a two-drive mirror of many identical generic files
            keeps its evidence while the drive and edge bounds still cap the
            work. The one-hour FAT/DST rule is NOT applied in generic groups:
            for a name this common it would chain unrelated copies across an
            hour.
            """
            bucket: list[sqlite3.Row] = []
            bucket_instants: list[datetime] = []
            bucket_volumes: Counter[int] = Counter()
            last_instant: datetime | None = None
            too_common = False

            def bucket_strong() -> list[set[int]]:
                if len(bucket_volumes) > 1:
                    return bucket_strong_per_row(bucket, bucket_instants)
                return [set() for _ in bucket]

            for row in rows:
                modified_at = str(row["modified_at"] or "")
                instant = _parse_modified_time(modified_at) if modified_at else None
                if instant is None:
                    # An empty or unreadable time is never strong evidence.
                    yield [row], [set()]
                    continue
                if (
                    last_instant is not None
                    and abs((instant - last_instant).total_seconds())
                    > MODIFIED_TIME_TOLERANCE_SECONDS
                ):
                    if bucket:
                        yield bucket, bucket_strong()
                        bucket = []
                        bucket_instants = []
                        bucket_volumes = Counter()
                    too_common = False
                last_instant = instant
                if too_common:
                    yield [row], [set()]
                    continue
                volume = int(row["volume_id"])
                bucket.append(row)
                bucket_instants.append(instant)
                bucket_volumes[volume] += 1
                bucket_edges = len(bucket) * max(0, len(bucket_volumes) - 1)
                if (
                    len(bucket_volumes) > self.options.max_strong_volumes_per_signature
                    or bucket_volumes[volume] > self.options.max_strong_records_per_signature
                    or bucket_edges > self.options.max_strong_edges_per_signature
                ):
                    # Too many copies share this time to call any of them
                    # strong; the rest of the chain is treated the same way.
                    yield bucket, [set() for _ in bucket]
                    bucket = []
                    bucket_instants = []
                    bucket_volumes = Counter()
                    too_common = True
            if bucket:
                yield bucket, bucket_strong()

        for file_key, member_rows in groupby(
            candidate_members,
            key=lambda row: bytes(row["file_key"]),
        ):
            self._check_cancel(cancel_callback)
            first_member = next(member_rows)
            group_records = int(first_member["record_count"])
            group_volumes = int(first_member["volume_count"])
            candidate_edges = group_records * (group_volumes - 1)
            generic_group = (
                group_volumes > self.options.max_candidate_volumes_per_key
                or group_records > self.options.max_candidate_records_per_key
                or candidate_edges > self.options.max_candidate_edges_per_key
            )
            all_members = chain((first_member,), member_rows)
            if not generic_group:
                members = list(all_members)
                records = [as_record(row) for row in members]
                group_volume_ids = {record.volume_id for record in records}
                count_pairs(records)
                found = existence(records)
                for member, record in zip(members, records):
                    verified, strong, conflict = found[record.file_id]
                    stage_member(
                        member,
                        group_volume_ids=group_volume_ids,
                        verified=verified,
                        strong=strong,
                        conflict=conflict,
                        generic_group=False,
                        group_records=group_records,
                        group_volumes=group_volumes,
                    )
            else:
                # A generic name and size is never paired for coverage
                # (performance). The cursor is ordered by saved modified time,
                # so a bounded time bucket can be swept out to supply the strong
                # drives; a huge common key never becomes one huge Python list.
                group_volume_ids = self._group_volume_ids("file_key = ?", (file_key,))
                key_targets: set[tuple[str, int]] = set()
                for bucket_rows, strong_per_row in time_buckets(all_members):
                    stage_generic(
                        bucket_rows,
                        strong_per_row,
                        group_volume_ids,
                        group_records,
                        group_volumes,
                        key_targets,
                    )
                    if any(strong_per_row):
                        # The bounded strong bucket is paired one-to-one purely
                        # for drive-pair overlap (mirror candidates); it never
                        # adds targets, which existence above already covers.
                        # Only records within the tolerance of each other pair.
                        count_bucket_pairs([as_record(row) for row in bucket_rows])
            hash_covered_cache.clear()
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

    @staticmethod
    def _pending_coverage_map() -> dict[int, dict[int, list[int]]]:
        """Folder coverage awaiting children, keyed by parent folder id.

        Only parents whose subtrees are still being judged live here; tests
        replace this hook with a recording dict to pin that bound.
        """
        return {}

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

        direct_evidence: dict[int, tuple[int, int, int]] = {
            int(row["folder_id"]): (
                int(row["files"]),
                int(row["bytes"]),
                int(row["hashed_files"]),
            )
            for row in self.connection.execute(
                """
                SELECT folder_id, COUNT(*) AS files,
                       COALESCE(SUM(size_bytes), 0) AS bytes,
                       COALESCE(SUM(content_hash IS NOT NULL), 0) AS hashed_files
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
            SELECT folder_id, structure_key
            FROM backup_work_files
            WHERE folder_id IS NOT NULL AND evidence_eligible = 1
            ORDER BY folder_id, structure_key
            """
        ):
            folder_id = int(file_row["folder_id"])
            if folder_id != current_folder:
                if current_folder is not None and hasher is not None:
                    direct_digest[current_folder] = hasher.digest()
                current_folder = folder_id
                hasher = hashlib.blake2b(digest_size=16)
            hasher.update(file_row["structure_key"])
        if current_folder is not None and hasher is not None:
            direct_digest[current_folder] = hasher.digest()

        fingerprints: dict[int, bytes] = {}
        evidence_totals: dict[int, tuple[int, int, int]] = {}
        # Deepest first so children are judged before their parents; ties by
        # folder id so the coverage cursor below can be read in lockstep.
        ordered_ids = sorted(
            info,
            key=lambda folder_id: (-info[folder_id].depth, folder_id),
        )
        work_batch: list[tuple[Any, ...]] = []
        for processed, folder_id in enumerate(ordered_ids, start=1):
            self._check_cancel(cancel_callback)
            hasher = hashlib.blake2b(digest_size=16)
            hasher.update(b"jvvv-folder-hybrid-v2")
            hasher.update(direct_digest.get(folder_id, b"\x00" * 16))
            child_tokens = []
            evidence_files, evidence_bytes, hashed_files = direct_evidence.get(
                folder_id, (0, 0, 0)
            )
            for child_id in children.get(folder_id, ()):
                child_files, child_bytes, child_hashed_files = evidence_totals[child_id]
                evidence_files += child_files
                evidence_bytes += child_bytes
                hashed_files += child_hashed_files
                if child_files <= 0:
                    continue
                child_name = info[child_id].normalized_name.encode("utf-8")
                child_tokens.append(_digest(child_name, fingerprints[child_id]))
            for token in sorted(child_tokens):
                hasher.update(token)
            fingerprint = hasher.digest()
            fingerprints[folder_id] = fingerprint
            evidence_totals[folder_id] = (
                evidence_files,
                evidence_bytes,
                hashed_files,
            )
            folder_info = info[folder_id]
            work_batch.append(
                (
                    folder_id,
                    folder_info.volume_id,
                    folder_info.parent_id,
                    fingerprint,
                    evidence_files,
                    evidence_bytes,
                    folder_info.depth,
                )
            )
            if len(work_batch) >= self.options.batch_size:
                self.connection.executemany(
                    "INSERT INTO backup_work_folders VALUES (?, ?, ?, ?, ?, ?, ?)", work_batch
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
                "INSERT INTO backup_work_folders VALUES (?, ?, ?, ?, ?, ?, ?)", work_batch
            )
        self.connection.execute(
            "CREATE INDEX temp.idx_backup_work_folder_fingerprint "
            "ON backup_work_folders(fingerprint, volume_id)"
        )

        exact_matches: dict[int, set[int]] = defaultdict(set)
        renamed_structure_matches: dict[int, set[int]] = defaultdict(set)
        # Fingerprint groups that cannot be paired as a unit: over the bounds,
        # or a folder name repeated unequally between two drives. Their
        # (records, drives, drive ids) feed the "structure repeats" verdict.
        structure_groups: dict[bytes, tuple[int, int, frozenset[int]]] = {}
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
                # enormous membership list; only the small drive set is kept.
                group_volume_ids = {int(first_member["volume_id"])}
                group_volume_ids.update(int(row["volume_id"]) for row in member_rows)
                structure_groups[fingerprint] = (
                    folder_records,
                    folder_volumes,
                    frozenset(group_volume_ids),
                )
                continue
            group = list(chain((first_member,), member_rows))
            by_volume: dict[int, list[sqlite3.Row]] = defaultdict(list)
            for row in group:
                by_volume[int(row["volume_id"])].append(row)
            volume_ids = sorted(by_volume)
            unpaired_folders: set[int] = set()
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
                            unpaired_folders.update(named_ids)
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
                        unpaired_folders.update(
                            int(row["folder_id"])
                            for row in (*remaining_source, *remaining_target)
                        )
            if unpaired_folders:
                ambiguous_structure_folders.update(unpaired_folders)
                structure_groups[fingerprint] = (
                    folder_records,
                    folder_volumes,
                    frozenset(volume_ids),
                )

        # Fingerprints intentionally omit the folder's own display name, so a
        # complete project tree is still recognised after its top-level folder
        # is renamed; that shows up as an explicit "renamed" qualifier.
        #
        # Coverage below comes from the staged existence rows (one per other
        # drive that holds each file, not pairing), so a folder can never be
        # red while its files are green. Each (folder, target drive) slot counts
        # every file found there, the files found by hash or time ("strong")
        # with their bytes, and the hash-verified ones; parents accumulate all
        # four values from their children.
        #
        # The cursor is read lazily, in the same (depth desc, folder id) order
        # as the folder loop, so `coverage` only ever holds parents awaiting
        # their remaining children instead of every folder of the catalogue.
        # Every folder in the cursor is in ordered_ids (the depth join only
        # succeeds for indexed folders) and both are sorted the same way, so
        # the two are read in lockstep; the check after the loop makes any
        # desynchronisation fail loudly instead of publishing wrong verdicts.
        direct_coverage_groups = groupby(
            self.connection.execute(
                f"""
                SELECT folder_id, target_volume_id,
                       COUNT(*) AS all_files,
                       COALESCE(SUM(likely), 0) AS strong_files,
                       COALESCE(SUM(CASE WHEN likely THEN size_bytes ELSE 0 END), 0)
                           AS strong_bytes,
                       COALESCE(SUM(verified), 0) AS verified_files
                FROM ({_FILE_TARGET_ROWS_WITH_DEPTH_SQL})
                GROUP BY depth, folder_id, target_volume_id
                ORDER BY depth DESC, folder_id, target_volume_id
                """
            ),
            key=itemgetter(0),
        )
        pending_direct = next(direct_coverage_groups, None)
        coverage: dict[int, dict[int, list[int]]] = self._pending_coverage_map()

        def take_direct_coverage(folder_id: int, into: dict[int, list[int]]) -> None:
            """Merge this folder's own (folder, drive) rows from the cursor into `into`."""
            nonlocal pending_direct
            if pending_direct is None or pending_direct[0] != folder_id:
                return
            for row in pending_direct[1]:
                # Columns: folder_id, target_volume_id, then the four slots.
                # COUNT/SUM over integer columns come back as ints.
                target = row[1]
                values = list(row[2:])
                aggregate = into.get(target)
                if aggregate is None:
                    into[target] = values
                else:
                    for index, value in enumerate(values):
                        aggregate[index] += value
            pending_direct = next(direct_coverage_groups, None)

        # Files with a strong match on at least one drive, for "scattered".
        union_strong: dict[int, int] = {
            int(row["folder_id"]): int(row["matched_files"])
            for row in self.connection.execute(
                f"""
                SELECT folder_id, COUNT(*) AS matched_files
                FROM (
                    SELECT DISTINCT folder_id, file_id
                    FROM ({_FILE_TARGET_ROWS_SQL})
                    WHERE likely AND folder_id IS NOT NULL
                )
                GROUP BY folder_id
                """
            )
        }

        health_by_volume = {record.volume_id: record.health_status for record in self.scan_records()}

        def clean_scan_first(target: int) -> tuple[bool, int]:
            return health_by_volume.get(target) != "completed", target

        def reported_access_errors(*volume_ids: int | None) -> bool:
            """Errors stay explicit: an incomplete listing on either side is flagged, never hidden."""
            return any(
                health_by_volume.get(candidate) == "completed_with_errors"
                for candidate in volume_ids
                if candidate is not None
            )

        stage_rows: list[tuple[Any, ...]] = []
        stage_matches: list[tuple[Any, ...]] = []
        for processed, folder_id in enumerate(ordered_ids, start=1):
            folder_info = info[folder_id]
            volume_id = folder_info.volume_id
            total_files, total_bytes, hashed_files = evidence_totals[folder_id]
            indexed_files = folder_info.indexed_files
            # Children (deeper folders) have already accumulated into this
            # slot; the folder's own file rows are merged in from the cursor.
            folder_coverage = coverage.pop(folder_id, {})
            take_direct_coverage(folder_id, folder_coverage)
            strong_coverage_targets = {
                target
                for target, values in folder_coverage.items()
                if values[_STRONG_FILES] > 0
            }
            union = union_strong.get(folder_id, 0)
            all_exact_targets = sorted(exact_matches.get(folder_id, ()))
            renamed_targets = sorted(renamed_structure_matches.get(folder_id, ()))
            structure_targets = set(all_exact_targets) | set(renamed_targets)
            fingerprint = fingerprints[folder_id]
            scattered = 0
            if total_files == 0:
                if indexed_files > 0:
                    status = "excluded"
                    evidence = FOLDER_EVIDENCE_EXCLUDED
                elif health_by_volume.get(volume_id) in {"completed", "empty"}:
                    status = "empty"
                    evidence = FOLDER_EVIDENCE_EMPTY
                else:
                    status = "unknown"
                    evidence = FOLDER_EVIDENCE_UNKNOWN_SCAN
                other_ids: list[int] = []
                best_target = None
                matched_files = matched_bytes = 0
                files_percent = bytes_percent = None
            elif all_exact_targets or renamed_targets:
                # The whole structure exists elsewhere. Qualifiers stay explicit
                # instead of downgrading the verdict.
                status = "likely"
                if hashed_files == total_files:
                    evidence = FOLDER_EVIDENCE_FULL_MATCH_HASHED
                elif hashed_files > 0:
                    evidence = FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED
                else:
                    evidence = FOLDER_EVIDENCE_FULL_MATCH_LEGACY
                if all_exact_targets:
                    best_target = min(all_exact_targets, key=clean_scan_first)
                else:
                    best_target = min(renamed_targets, key=clean_scan_first)
                if reported_access_errors(volume_id, best_target):
                    evidence += FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS
                if not all_exact_targets:
                    evidence += FOLDER_EVIDENCE_SUFFIX_RENAMED
                if folder_id in ambiguous_structure_folders:
                    evidence += FOLDER_EVIDENCE_SUPPRESSED_SUFFIX
                other_ids = _capped_ids_preferring(
                    structure_targets | strong_coverage_targets, folder_coverage
                )
                matched_files = total_files
                matched_bytes = total_bytes
                files_percent = bytes_percent = 100.0
            elif folder_coverage:
                best_target, best = max(
                    folder_coverage.items(),
                    key=lambda item: (
                        item[1][_STRONG_FILES],
                        item[1][_VERIFIED_FILES],
                        item[1][_ALL_FILES],
                        item[1][_STRONG_BYTES],
                        -item[0],
                    ),
                )
                matched_files = best[_STRONG_FILES]
                matched_bytes = best[_STRONG_BYTES]
                if best[_VERIFIED_FILES] >= total_files:
                    status = "likely"
                    evidence = FOLDER_EVIDENCE_ALL_FILES_HASHED
                elif best[_STRONG_FILES] >= total_files:
                    status = "likely"
                    evidence = FOLDER_EVIDENCE_ALL_FILES_FOUND
                elif best[_ALL_FILES] >= total_files:
                    status = "possible"
                    evidence = FOLDER_EVIDENCE_ALL_FILES_WEAK
                else:
                    status = "possible"
                    evidence = FOLDER_EVIDENCE_PARTIAL
                    scattered = int(union >= total_files and matched_files < total_files)
                if reported_access_errors(volume_id, best_target):
                    evidence += FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS
                files_percent = _percent(matched_files, total_files)
                bytes_percent = _percent(matched_bytes, total_bytes)
                other_ids = _capped_ids_preferring(strong_coverage_targets, folder_coverage)
            elif (
                fingerprint in ambiguous_structure_fingerprints
                or folder_id in ambiguous_structure_folders
            ):
                status = "possible"
                records, drives, group_volume_ids = structure_groups.get(
                    fingerprint, (0, 0, frozenset())
                )
                evidence = (
                    FOLDER_EVIDENCE_STRUCTURE_REPEATS_PREFIX
                    + f" {records:,} folders across {drives:,} drives."
                )
                other_ids = _capped_ids(group_volume_ids - {volume_id})
                best_target = None
                matched_files = matched_bytes = 0
                files_percent = bytes_percent = None
            else:
                status = "single"
                evidence = FOLDER_EVIDENCE_SINGLE
                other_ids = []
                best_target = None
                matched_files = matched_bytes = 0
                files_percent = bytes_percent = 0.0

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
            # Persisted per-drive rows: every structure match and every drive
            # holding strong evidence. Weak-only drives are listed only when no
            # drive holds strong evidence, and then only the best one; the cap
            # keeps the drives with the most strong coverage.
            persisted_targets = structure_targets | strong_coverage_targets
            if best_target is not None:
                persisted_targets.add(best_target)
            no_coverage = [0] * 4
            ranked_targets = heapq.nsmallest(
                MAX_STORED_FOLDER_DRIVE_MATCHES,
                (
                    (target, folder_coverage.get(target, no_coverage))
                    for target in persisted_targets
                ),
                key=lambda item: (
                    item[0] not in structure_targets,
                    -item[1][_STRONG_FILES],
                    -item[1][_VERIFIED_FILES],
                    -item[1][_ALL_FILES],
                    -item[1][_STRONG_BYTES],
                    item[0],
                ),
            )
            # Structure matches without a hash for every file carry the
            # same hedge as the folder verdict.
            structure_hedge = "" if hashed_files == total_files else " (contents not compared)"
            for target, values in ranked_targets:
                target_files = values[_STRONG_FILES]
                target_bytes = values[_STRONG_BYTES]
                if target in all_exact_targets:
                    target_status = "likely"
                    target_files, target_bytes = total_files, total_bytes
                    target_evidence = f"{FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX}{structure_hedge}."
                elif target in renamed_targets:
                    target_status = "likely"
                    target_files, target_bytes = total_files, total_bytes
                    target_evidence = (
                        f"{FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX} under a different folder name"
                        f"{structure_hedge}."
                    )
                elif values[_VERIFIED_FILES] >= total_files:
                    target_status = "likely"
                    target_evidence = "Every file here is hash-verified."
                elif values[_STRONG_FILES] >= total_files:
                    target_status = "likely"
                    target_evidence = "Every file found here by name, size and time."
                elif values[_ALL_FILES] >= total_files:
                    target_status = "possible"
                    target_evidence = (
                        "Files found here by name and size only."
                        if target_files == 0
                        else f"Every file is here by name and size; {target_files:,} of "
                        f"{total_files:,} also match by modified time."
                    )
                else:
                    target_status = "possible"
                    target_evidence = (
                        f"Part of this folder is here ({target_files:,} of {total_files:,} "
                        "files by name, size and time"
                    )
                    if values[_ALL_FILES] > target_files:
                        # Weak matches (name and size only) are not coverage,
                        # but the drive row says how many more files it holds.
                        target_evidence += f"; {values[_ALL_FILES]:,} by name and size"
                    target_evidence += ")."
                stage_matches.append(
                    (
                        folder_id,
                        target,
                        target_status,
                        target_files,
                        total_files,
                        target_bytes,
                        total_bytes,
                        target_evidence,
                    )
                )

            parent_id = folder_info.parent_id
            if parent_id is not None:
                if folder_coverage:
                    parent_map = coverage.setdefault(parent_id, {})
                    for target, values in folder_coverage.items():
                        aggregate = parent_map.get(target)
                        if aggregate is None:
                            # The child's slot is never read again; hand the
                            # list itself up instead of copying it.
                            parent_map[target] = values
                        else:
                            for index, value in enumerate(values):
                                aggregate[index] += value
                union_strong[parent_id] = union_strong.get(parent_id, 0) + union
            union_strong.pop(folder_id, None)

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
        if pending_direct is not None:
            raise RuntimeError(
                "Folder coverage rows were left unread; the coverage cursor and the "
                "folder order disagree."
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
            if evidence_totals[folder_id][2] in {0, evidence_totals[folder_id][0]}
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
        self._check_cancel(cancel_callback)
        totals_by_volume = {
            int(row["volume_id"]): row
            for row in self.connection.execute(
                """
                SELECT volume_id,
                       COUNT(*) AS files,
                       COALESCE(SUM(size_bytes), 0) AS bytes,
                       COALESCE(SUM(evidence_eligible), 0) AS coverage_files,
                       COALESCE(
                           SUM(CASE WHEN evidence_eligible = 1 THEN size_bytes ELSE 0 END),
                           0
                       ) AS coverage_bytes
                FROM backup_work_files
                GROUP BY volume_id
                """
            )
        }
        self._check_cancel(cancel_callback)
        matched_by_volume = {
            int(row["volume_id"]): row
            for row in self.connection.execute(
                """
                SELECT
                    r.volume_id,
                    COALESCE(SUM(r.status='likely'), 0) AS likely_files,
                    COALESCE(
                        SUM(CASE WHEN r.status='likely' THEN wf.size_bytes ELSE 0 END),
                        0
                    ) AS likely_bytes,
                    COALESCE(SUM(r.status='possible'), 0) AS possible_files,
                    COALESCE(
                        SUM(CASE WHEN r.status='possible' THEN wf.size_bytes ELSE 0 END),
                        0
                    ) AS possible_bytes,
                    COALESCE(SUM(r.status='ambiguous'), 0) AS ambiguous_files,
                    COALESCE(
                        SUM(CASE WHEN r.status='ambiguous' THEN wf.size_bytes ELSE 0 END),
                        0
                    ) AS ambiguous_bytes
                FROM backup_stage_file_results r
                JOIN backup_work_files wf ON wf.file_id = r.file_id
                GROUP BY r.volume_id
                """
            )
        }
        for index, volume_row in enumerate(volumes, start=1):
            self._check_cancel(cancel_callback)
            volume_id = int(volume_row["id"])
            totals = totals_by_volume.get(volume_id)
            matched = matched_by_volume.get(volume_id)
            total_files = int(totals["files"] or 0) if totals is not None else 0
            total_bytes = int(totals["bytes"] or 0) if totals is not None else 0
            coverage_files = (
                int(totals["coverage_files"] or 0) if totals is not None else 0
            )
            coverage_bytes = (
                int(totals["coverage_bytes"] or 0) if totals is not None else 0
            )
            likely_files = (
                int(matched["likely_files"] or 0) if matched is not None else 0
            )
            likely_bytes = (
                int(matched["likely_bytes"] or 0) if matched is not None else 0
            )
            possible_files = (
                int(matched["possible_files"] or 0) if matched is not None else 0
            )
            possible_bytes = (
                int(matched["possible_bytes"] or 0) if matched is not None else 0
            )
            ambiguous_files = (
                int(matched["ambiguous_files"] or 0) if matched is not None else 0
            )
            ambiguous_bytes = (
                int(matched["ambiguous_bytes"] or 0) if matched is not None else 0
            )
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
                        "A large share of files matches by name, size and modified time "
                        "(or by saved content hash); review paths before declaring a mirror."
                    )
                else:
                    evidence = (
                        "This pair is listed because it has a manual mirror relationship, "
                        "but its saved evidence overlap is below the suggestion threshold."
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
                        "Saved catalogue hashes and metadata only; no drive contents were read.",
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
                INSERT INTO backup_file_results (
                    run_id, file_id, volume_id, status, other_volume_ids,
                    evidence_text, strong_volume_ids, possible_volume_ids,
                    verified_volume_ids
                )
                SELECT ?, file_id, volume_id, status, other_volume_ids, evidence_text,
                       strong_volume_ids, possible_volume_ids, verified_volume_ids
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
            # Per folder: (target drive, row status, row evidence), in drive order.
            folder_match_rows: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
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
                        SELECT folder_id, target_volume_id, status, evidence_text
                        FROM backup_folder_drive_matches
                        WHERE run_id = ? AND folder_id IN ({placeholders})
                        ORDER BY folder_id, target_volume_id
                        """,
                        (state.active_run_id, *chunk),
                    ):
                        match_status = str(match["status"])
                        if match_status in {"likely", "possible"}:
                            folder_match_rows[int(match["folder_id"])].append(
                                (
                                    int(match["target_volume_id"]),
                                    match_status,
                                    str(match["evidence_text"] or ""),
                                )
                            )
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
                    verified_ids: tuple[int, ...] = ()
                elif bool(item["missing"]):
                    status = "unknown"
                    evidence = ITEM_EVIDENCE_MISSING
                    other_ids = ()
                    strong_ids = ()
                    possible_ids = ()
                    verified_ids = ()
                elif row is None:
                    status = "single"
                    evidence = (
                        "No exact saved content hash or normalized filename-and-size match "
                        "was found on another drive in the catalogue."
                    )
                    other_ids = ()
                    strong_ids = ()
                    possible_ids = ()
                    verified_ids = ()
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
                    verified_ids = (
                        _parse_ids(row["verified_volume_ids"])
                        if item_type == "file"
                        else ()
                    )
                kwargs: dict[str, Any] = {}
                if item_type == "folder" and row is not None and not bool(item["missing"]):
                    match_rows = folder_match_rows.get(item_id, ())
                    # A "Full match" folder counts as strong only the drives
                    # holding the whole folder as a structure. A drive that
                    # merely holds every file loose stays a coverage lead in
                    # other_volume_ids rather than inflating the full-match count.
                    whole_only = folder_reason_key(evidence) in _FULL_MATCH_REASON_KEYS
                    kwargs = {
                        "strong_volume_ids": tuple(
                            target
                            for target, match_status, match_evidence in match_rows
                            if match_status == "likely"
                            and (
                                not whole_only
                                or match_evidence.startswith(FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX)
                            )
                        ),
                        "possible_volume_ids": tuple(
                            target
                            for target, match_status, _match_evidence in match_rows
                            if match_status == "possible"
                        ),
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
                        "verified_volume_ids": verified_ids,
                    }
                result[item_id] = ItemBackupStatus(
                    item_type=item_type,
                    item_id=item_id,
                    status=status,
                    other_volume_ids=other_ids,
                    other_drive_count=_other_drive_count(item_type, evidence, other_ids),
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
                "Exact content hash match from the saved scan.",
                is_stale=status.is_stale,
                analysed_at=status.analysed_at,
            )
            for volume_id in status.verified_volume_ids
        ]
        matches.extend(
            MatchLocation(
                volume_id,
                "likely",
                "Same name, size and modified time; contents were not compared.",
                is_stale=status.is_stale,
                analysed_at=status.analysed_at,
            )
            for volume_id in status.strong_volume_ids
            if volume_id not in status.verified_volume_ids
        )
        matches.extend(
            MatchLocation(
                volume_id,
                "possible",
                "Same name and size only; modified time differs or is unavailable, "
                "and no comparable saved hash matched.",
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
                        "A large share of files matches by name, size and modified time "
                        "(or by saved content hash); review paths before declaring a mirror."
                    )
                )
            else:
                evidence = (
                    "The current manual mirror relationship is shown even though its "
                    "saved evidence overlap is below the suggestion threshold."
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
                        "This manual mirror relationship was added after the saved-evidence "
                        "analysis. Update the analysis to calculate its current evidence overlap."
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
                WHERE status IN ('completed', 'completed_with_warnings')
            )
            SELECT v.id AS volume_id, COALESCE(r.drive_id, '') AS drive_id,
                   v.name AS volume_name, v.indexed_file_count, v.indexed_folder_count,
                   a.id AS latest_attempt_scan_id,
                   a.status AS latest_attempt_status,
                   COALESCE(a.finished_at, a.started_at) AS latest_attempt_at,
                   a.errors_count AS latest_attempt_errors,
                   a.hash_errors AS latest_attempt_hash_errors,
                   a.files_seen AS latest_attempt_files,
                   a.folders_seen AS latest_attempt_folders,
                   a.message AS latest_attempt_message,
                   p.id AS last_applied_scan_id,
                   COALESCE(p.finished_at, p.started_at) AS last_applied_at,
                   p.errors_count AS last_applied_errors,
                   p.hash_errors AS last_applied_hash_errors
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
                f"SELECT scan_id, path, message FROM scan_errors WHERE scan_id IN ({placeholders})",
                chunk,
            ):
                if error["scan_id"] is not None and _is_ignored_system_scan_path(
                    error["path"]
                ) and not str(error["message"] or "").startswith(
                    tuple(f"{label} hash unavailable:" for label in HASH_LABELS.values())
                ):
                    scan_id = int(error["scan_id"])
                    ignored_by_scan[scan_id] = ignored_by_scan.get(scan_id, 0) + 1

        records: list[ScanRecord] = []
        for row in rows:
            applied_at = row["last_applied_at"]
            applied_errors = row["last_applied_errors"]
            latest_error_count = int(row["latest_attempt_errors"] or 0)
            applied_error_count = int(applied_errors or 0)
            latest_hash_errors = min(
                latest_error_count,
                int(row["latest_attempt_hash_errors"] or 0),
            )
            applied_hash_errors = min(
                applied_error_count,
                int(row["last_applied_hash_errors"] or 0),
            )
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
            applied_relevant_errors = max(
                0,
                applied_error_count - applied_ignored - applied_hash_errors,
            )
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
                    latest_attempt_hash_errors=latest_hash_errors,
                    last_applied_hash_errors=applied_hash_errors,
                )
            )
        return records
