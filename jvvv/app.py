from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import re
import sqlite3
import sys
import traceback
from threading import Event
from time import monotonic
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QDate,
    QEvent,
    QEventLoop,
    QFileInfo,
    QItemSelectionModel,
    QLockFile,
    QModelIndex,
    QObject,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QLocale,
    QProcess,
    QSettings,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPalette, QPixmap, QPolygon, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileIconProvider,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import APP_NAME
from .backup_analysis import (
    AnalysisState,
    BackupAnalysisEngine,
    ItemBackupStatus,
    MirrorCandidate,
    VolumeBackupSummary,
)
from .catalogue_backup import (
    BACKUP_FILE_FILTER,
    BackupCancelled,
    BackupProgress,
    BackupResult,
    RestoreResult,
    create_catalogue_backup,
    restore_catalogue_backup,
)
from .database import (
    ARCHIVE_STATUSES,
    CATALOGUE_EXTENSION,
    CONNECTOR_OPTIONS,
    CatalogueError,
    CatalogueInUseError,
    Database,
    VOLUME_CONDITIONS,
    catalogue_path_with_extension,
    create_catalogue,
    drive_id_sort_key,
    is_valid_drive_id,
    open_catalogue,
    parse_db_time,
)
from .scanner import VolumeScanner
from .media_metadata import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .theme import (
    ADOBE_THEME,
    DARK_MODE,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_COLOR_MODE,
    DEFAULT_THEME_STYLE,
    FUSION_THEME,
    LIGHT_MODE,
    apply_application_theme,
    contrasting_text_color,
    normalize_accent_color,
    normalize_color_mode,
    normalize_theme_style,
    theme_default_accent,
)
from .utils import (
    ConnectedVolumeResolver,
    VolumeSnapshot,
    capture_volume_snapshot,
    format_size,
    list_connected_volume_snapshots,
    open_in_file_manager,
    percentage_full,
    rename_volume_label,
    relative_path_for_display,
    resolve_volume_source_path,
)


ROLE_VOLUME_ID = Qt.ItemDataRole.UserRole
ROLE_FOLDER_ID = Qt.ItemDataRole.UserRole + 1
ROLE_RELATIVE_PATH = Qt.ItemDataRole.UserRole + 2
ROLE_ITEM_TYPE = Qt.ItemDataRole.UserRole + 3
ROLE_ITEM_ID = Qt.ItemDataRole.UserRole + 4
ROLE_PERCENT_FULL = Qt.ItemDataRole.UserRole + 5
VOLUME_FULL_COLUMN = 18
LAST_CATALOGUE_PATH_SETTING = "catalogues/lastPath"
SEARCH_INCLUDE_PATHS_SETTING = "search/includePaths"
THEME_STYLE_SETTING = "appearance/themeStyle"
COLOR_MODE_SETTING = "appearance/colorMode"
ACCENT_COLOR_SETTING = "appearance/accentColor"
CATALOGUE_FILE_FILTER = "Joemt Archive View Files (*.jvvv)"
CATALOGUE_PROBE_ARGUMENT = "--catalogue-location-probe"
CATALOGUE_PROBE_TIMEOUT_MS = 8000
CATALOGUE_PROBE_OK = 0
CATALOGUE_PROBE_UNAVAILABLE = 2
CATALOGUE_PROBE_INVALID = 3


def format_exception_diagnostics(exc: Exception) -> str:
    sections = []
    diagnostic_details = getattr(exc, "diagnostic_details", "")
    if diagnostic_details:
        sections.append(str(diagnostic_details).strip())

    traceback_details = "".join(
        traceback.TracebackException.from_exception(exc).format(chain=True)
    ).strip()
    if traceback_details:
        sections.append(f"Traceback:\n{traceback_details}")
    return "\n\n".join(sections)


def acquire_catalogue_lock(path: Path) -> QLockFile:
    lock = QLockFile(f"{path}.lock")
    if not lock.tryLock(100):
        raise CatalogueInUseError(
            "This catalogue appears to be open in another JVVV window or process."
        )
    return lock


def probe_catalogue_location(path: str | Path) -> int:
    """Read a catalogue without modifying it to test whether its location responds."""
    catalogue_path = catalogue_path_with_extension(path)
    try:
        with catalogue_path.open("rb") as catalogue_file:
            header = catalogue_file.read(100)
        if len(header) < 16 or header[:16] != b"SQLite format 3\x00":
            return CATALOGUE_PROBE_INVALID

        connection = sqlite3.connect(
            Database._sqlite_uri(catalogue_path, mode="ro"),
            timeout=0.25,
            uri=True,
        )
        try:
            connection.execute("PRAGMA schema_version").fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return CATALOGUE_PROBE_UNAVAILABLE
    return CATALOGUE_PROBE_OK


def catalogue_probe_command(path: Path) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, [CATALOGUE_PROBE_ARGUMENT, str(path)]
    return sys.executable, ["-m", "jvvv", CATALOGUE_PROBE_ARGUMENT, str(path)]


PROGRESS_BAR_HEIGHT = 16
UI_ZOOM_STEP = 0.1
MIN_UI_ZOOM = 0.8
MAX_UI_ZOOM = 1.6
CONTENT_DATE_GUESS_ITEM_BUDGET = 500
CONTENT_DATE_GUESS_TIME_BUDGET_SECONDS = 0.025
VOLUME_CONNECTION_POLL_INTERVAL_MS = 1500
VOLUME_CONNECTION_REFRESH_DELAY_MS = 250
SEARCH_RESULT_BATCH_SIZE = 500
AID_VOLUME_LABEL_RE = re.compile(r"^AID-\d{3,}$", re.IGNORECASE)


def progress_bar_style(height: int, radius: int, chunk_radius: int) -> str:
    return f"""
QProgressBar {{
    min-height: {height}px;
    max-height: {height}px;
    border: 1px solid palette(mid);
    border-radius: {radius}px;
    background: palette(base);
    text-align: center;
}}
QProgressBar::chunk {{
    border-radius: {chunk_radius}px;
    background: palette(highlight);
}}
"""


def configure_progress_bar(progress_bar: QProgressBar, zoom: float = 1.0) -> None:
    height = max(1, round(PROGRESS_BAR_HEIGHT * zoom))
    progress_bar.setMinimumHeight(height + 2)
    progress_bar.setStyleSheet(
        progress_bar_style(
            height,
            max(2, round(5 * zoom)),
            max(1, round(4 * zoom)),
        )
    )


@dataclass(frozen=True)
class TableColumn:
    title: str
    display: Callable[[Any], Any]
    sort_key: Callable[[Any], Any] | None = None
    alignment: Qt.AlignmentFlag | None = None
    decoration: Callable[[Any], QIcon | None] | None = None
    tooltip: Callable[[Any], str | None] | None = None
    header_tooltip: str | None = None


class StandardTableModel(QAbstractTableModel):
    def __init__(self, columns: list[TableColumn], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.columns = columns
        self.items: list[Any] = []
        self.sort_column = 0
        self.sort_order = Qt.SortOrder.AscendingOrder

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self.columns)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:  # type: ignore[override]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].title
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole:
            return self.columns[section].header_tooltip
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid():
            return None

        item = self.items[index.row()]
        column = self.columns[index.column()]

        if role == Qt.ItemDataRole.DisplayRole:
            return column.display(item)

        if role == Qt.ItemDataRole.DecorationRole and column.decoration is not None:
            return column.decoration(item)

        if role == Qt.ItemDataRole.TextAlignmentRole and column.alignment is not None:
            return column.alignment | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.ToolTipRole and column.tooltip is not None:
            return column.tooltip(item)

        return self.role_value(item, role)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # type: ignore[override]
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_items(self, items: list[Any]) -> None:
        self.beginResetModel()
        self.items = items
        self._sort_items()
        self.endResetModel()

    def append_items(self, items: list[Any]) -> None:
        if not items:
            return
        first_row = len(self.items)
        last_row = first_row + len(items) - 1
        self.beginInsertRows(QModelIndex(), first_row, last_row)
        self.items.extend(items)
        self.endInsertRows()

    def item_at(self, index: QModelIndex) -> Any | None:
        if not index.isValid() or index.row() < 0 or index.row() >= len(self.items):
            return None
        return self.items[index.row()]

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:  # type: ignore[override]
        self.sort_column = column
        self.sort_order = order
        self.layoutAboutToBeChanged.emit()
        self._sort_items()
        self.layoutChanged.emit()

    def role_value(self, item: Any, role: int) -> Any:
        return None

    def group_key(self, item: Any) -> Any:
        return 0

    def _sort_items(self) -> None:
        reverse = self.sort_order == Qt.SortOrder.DescendingOrder
        column = self.columns[self.sort_column]
        sort_key = column.sort_key or column.display
        sorted_items: list[Any] = []
        group_keys = sorted({self.group_key(item) for item in self.items})
        for group_key in group_keys:
            group_items = [item for item in self.items if self.group_key(item) == group_key]
            sorted_items.extend(
                sorted(
                    group_items,
                    key=lambda item: self._normalized_sort_value(sort_key(item)),
                    reverse=reverse,
                )
            )
        self.items = sorted_items

    def _normalized_sort_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.casefold()
        if value is None:
            return ""
        return value


TEXT_EXTENSIONS = {"txt", "md", "markdown", "rst", "log", "csv", "json", "xml", "yaml", "yml"}
PDF_EXTENSIONS = {"pdf"}
OFFICE_EXTENSIONS = {
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "odt",
    "ods",
    "odp",
    "rtf",
}
ARCHIVE_EXTENSIONS = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"}
EXECUTABLE_EXTENSIONS = {"exe", "msi", "app", "bat", "cmd", "com", "sh", "run", "deb", "rpm", "dmg"}

BACKUP_METADATA_DISCLAIMER = (
    "This analysis reads the saved catalogue only, not the connected drives. "
    "Hash-verified means full-file SHA-256 values recorded during scans match; "
    "metadata-only matches are not byte-for-byte verification."
)
BACKUP_COLUMN_HEADER_TOOLTIP = (
    "Evidence that another catalogue volume contains the same item.\n"
    "Green: matching SHA-256 or strong metadata evidence. Amber: possible or partial match. "
    "Red: none found. Grey: not analysed, outdated, or unknown.\n\n"
    f"{BACKUP_METADATA_DISCLAIMER}"
)
BACKUP_FILTER_OPTIONS = (
    ("All items", "all"),
    ("Needs attention", "attention"),
    ("No other copy found", "none"),
    ("Possible or partial", "possible"),
    ("Hash verified, strong, or complete", "strong"),
    ("Unknown / not analysed", "unknown"),
)


def object_value(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dataclass, mapping, or sqlite row."""
    if value is None:
        return default
    if hasattr(value, name):
        return getattr(value, name)
    try:
        return value[name]
    except (KeyError, IndexError, TypeError):
        return default


def first_object_value(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        candidate = object_value(value, name, None)
        if candidate is not None:
            return candidate
    return default


def enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold().replace("-", "_").replace(" ", "_")


@dataclass(frozen=True)
class BackupDisplay:
    state: str
    text: str
    tooltip: str
    sort_rank: int


NOT_ANALYSED_BACKUP_DISPLAY = BackupDisplay(
    state="unknown",
    text="Not analysed",
    tooltip=(
        "Backup evidence has not been analysed for this catalogue.\n\n"
        f"{BACKUP_METADATA_DISCLAIMER}"
    ),
    sort_rank=2,
)


def plural_other_drives(count: int) -> str:
    return f"{count} other drive" + ("" if count == 1 else "s")


def backup_drive_references(
    volume_ids: Any,
    volume_references: dict[int, str],
) -> list[str]:
    references = []
    for raw_volume_id in volume_ids or ():
        try:
            volume_id = int(raw_volume_id)
        except (TypeError, ValueError):
            continue
        references.append(volume_references.get(volume_id, f"Volume {volume_id}"))
    return references


def summarized_backup_drive_references(references: list[str], limit: int = 5) -> str:
    shown = references[:limit]
    remaining = len(references) - len(shown)
    suffix = f" (+{remaining} more)" if remaining else ""
    return f"{', '.join(shown)}{suffix}"


def backup_evidence_label(label: str, stale: bool) -> str:
    if not stale:
        return label
    return f"Last-analysed {label[:1].lower()}{label[1:]}"


def item_backup_display(
    status: Any,
    volume_references: dict[int, str] | None = None,
    *,
    item_type: str | None = None,
) -> BackupDisplay:
    if status is None:
        return NOT_ANALYSED_BACKUP_DISPLAY

    references = volume_references or {}
    stale = bool(object_value(status, "is_stale", False))
    stale_reason = str(object_value(status, "stale_reason", "") or "").strip()
    raw_state = enum_value(object_value(status, "status", "unknown"))
    resolved_type = item_type or str(object_value(status, "item_type", "") or "")
    other_ids = object_value(status, "other_volume_ids", ()) or ()
    other_count = int(object_value(status, "other_drive_count", len(other_ids)) or 0)
    drive_refs = backup_drive_references(other_ids, references)
    strong_ids = object_value(status, "strong_volume_ids", None)
    possible_ids = object_value(status, "possible_volume_ids", None)
    verified_ids = object_value(status, "verified_volume_ids", None)
    has_target_breakdown = (
        strong_ids is not None or possible_ids is not None or verified_ids is not None
    )
    verified_id_set = {
        int(value)
        for value in (verified_ids or ())
        if str(value).strip().lstrip("-").isdigit()
    }
    verified_refs = backup_drive_references(verified_ids or (), references)
    metadata_strong_ids = [
        value
        for value in (strong_ids or ())
        if int(value) not in verified_id_set
    ]
    strong_refs = backup_drive_references(metadata_strong_ids, references)
    possible_refs = backup_drive_references(possible_ids or (), references)
    evidence = str(object_value(status, "evidence_text", "") or "").strip()
    analysed_at = object_value(status, "analysed_at")

    if stale:
        lines = [
            "The saved backup analysis is out of date for this item.",
            stale_reason or "The catalogue changed after this item was analysed.",
        ]
        state = "unknown"
        text = "Outdated"
        rank = 2
    elif raw_state == "empty":
        state = "unknown"
        text = "N/A · empty"
        rank = 2
        lines = [
            "This folder contains no indexed files, so other-copy coverage is not applicable."
        ]
    elif raw_state == "excluded":
        state = "unknown"
        text = "N/A · system metadata"
        rank = 2
        lines = [
            "Known operating-system bookkeeping is deliberately excluded from copy coverage."
        ]
    elif raw_state == "ambiguous":
        state = "unknown"
        text = "Too common"
        rank = 2
        lines = [
            (
                "This folder structure occurs too often to identify a useful copy."
                if resolved_type == "folder"
                else "This filename-and-size combination is too common to identify a useful copy."
            )
        ]
    elif raw_state in {"strong", "complete", "likely", "matched"}:
        state = "strong"
        complete_count = (
            len(strong_ids or ())
            if has_target_breakdown
            else other_count
        )
        if resolved_type == "folder":
            text = f"Complete · {plural_other_drives(complete_count)}"
        elif verified_refs:
            text = f"Hash verified · {plural_other_drives(len(verified_refs))}"
        else:
            text = f"Strong metadata · {plural_other_drives(complete_count)}"
        rank = 3
        if resolved_type == "folder":
            lines = ["Complete structural match on another drive."]
        elif verified_refs:
            lines = [
                "The full-file SHA-256 recorded during scanning matches on another drive."
            ]
        else:
            lines = ["Strong file metadata match on another drive; no comparable hash was available."]
    elif raw_state in {"possible", "partial", "probable", "scattered"}:
        state = "possible"
        files_percent = object_value(status, "best_coverage_files_percent")
        bytes_percent = object_value(status, "best_coverage_bytes_percent")
        if resolved_type == "folder" and (
            files_percent is not None or bytes_percent is not None
        ):
            parts = []
            if files_percent is not None:
                parts.append(f"{float(files_percent):.0f}% files")
            if bytes_percent is not None:
                parts.append(f"{float(bytes_percent):.0f}% data")
            text = "Possible · " + " · ".join(parts)
        else:
            text = f"Possible · {plural_other_drives(other_count)}"
        rank = 1
        lines = [
            "This folder has possible or partial metadata evidence on another drive."
            if resolved_type == "folder"
            else "A possible metadata match is recorded on another drive."
        ]
        if bool(object_value(status, "scattered", False)):
            lines.append(
                "Matching files are spread across drives; no single drive has a complete folder copy."
            )
        best_target_id = object_value(status, "best_target_volume_id")
        if best_target_id is not None:
            try:
                best_target = references.get(
                    int(best_target_id), f"Volume {int(best_target_id)}"
                )
                lines.append(f"Best single matching drive: {best_target}.")
            except (TypeError, ValueError):
                pass
        if files_percent is not None:
            lines.append(f"Best single-drive file coverage: {float(files_percent):.0f}%.")
        if bytes_percent is not None:
            lines.append(f"Best single-drive byte coverage: {float(bytes_percent):.0f}%.")
    elif raw_state in {"none", "no_match", "unprotected", "single", "single_copy"}:
        state = "none"
        text = "None found"
        rank = 0
        lines = [
            "No matching copy was found on another volume in this catalogue."
        ]
    else:
        state = "unknown"
        text = (
            "Not analysed"
            if raw_state in {"not_analysed", "not_analyzed", ""}
            or (raw_state == "unknown" and analysed_at is None)
            else "Unknown"
        )
        rank = 2
        lines = [
            stale_reason
            or "There is not enough current catalogue information to assess another copy."
        ]

    if has_target_breakdown:
        if resolved_type == "folder":
            if strong_refs:
                lines.append(
                    f"{backup_evidence_label('Complete structure drives', stale)}: "
                    f"{summarized_backup_drive_references(strong_refs)}."
                )
            if possible_refs:
                lines.append(
                    f"{backup_evidence_label('Possible or partial drives', stale)}: "
                    f"{summarized_backup_drive_references(possible_refs)}."
                )
        else:
            if verified_refs:
                lines.append(
                    f"{backup_evidence_label('Hash-verified drives', stale)}: "
                    f"{summarized_backup_drive_references(verified_refs)}."
                )
            if strong_refs:
                lines.append(
                    f"{backup_evidence_label('Strong metadata-only drives', stale)}: "
                    f"{summarized_backup_drive_references(strong_refs)}."
                )
            if possible_refs:
                lines.append(
                    f"{backup_evidence_label('Possible-only drives', stale)}: "
                    f"{summarized_backup_drive_references(possible_refs)}."
                )
    elif drive_refs:
        lines.append(
            f"{backup_evidence_label('Other catalogue drives', stale)}: "
            f"{summarized_backup_drive_references(drive_refs)}."
        )
    if evidence:
        evidence_label = "Last-analysed evidence" if stale else "Evidence"
        lines.append(f"{evidence_label}: {evidence}")
    if analysed_at:
        lines.append(f"Analysed: {display_db_time(str(analysed_at))}.")
    lines.extend(["", BACKUP_METADATA_DISCLAIMER])
    return BackupDisplay(state, text, "\n".join(lines), rank)


def backup_filter_matches(display: BackupDisplay, filter_key: str) -> bool:
    if filter_key == "all":
        return True
    if display.text.startswith("N/A"):
        return False
    if filter_key == "attention":
        return display.state != "strong"
    if filter_key == "possible":
        return display.state == "possible"
    if filter_key == "strong":
        return display.state == "strong"
    if filter_key == "none":
        return display.state == "none"
    if filter_key == "unknown":
        return display.state == "unknown"
    return True


def volume_backup_display(
    summary: Any,
    indexed_file_count: int,
    scan_record: Any = None,
    analysis_state: Any = None,
) -> BackupDisplay:
    current_scan_health = enum_value(
        first_object_value(scan_record, "health_status", default="")
    )
    analysed_health = enum_value(
        first_object_value(summary, "health_status", "scan_health", default="")
    )
    # The report row belongs to the last backup-analysis run, while scan_record
    # describes the catalogue now.  An empty drive's label must follow the live
    # scan record so it cannot repeat an obsolete clean/error classification.
    health = current_scan_health or analysed_health
    if indexed_file_count == 0 and health in {"empty", "healthy_empty", "completed_empty"}:
        return BackupDisplay(
            "unknown",
            "N/A · empty",
            "The last applied scan found no user-content access errors and the catalogue "
            "contains no files. Protected system-metadata warnings, if any, remain in "
            "the scan report. Empty drives are excluded from other-copy coverage.\n\n"
            f"{BACKUP_METADATA_DISCLAIMER}",
            2,
        )
    if indexed_file_count == 0 and health in {
        "unknown",
        "check_scan",
        "completed_with_errors",
        "scan_errors",
        "incomplete",
    }:
        return BackupDisplay(
            "unknown",
            "Check scan",
            "The applied scan recorded access errors or incomplete information, so this "
            "cannot be treated as a confirmed empty drive.\n\n"
            f"{BACKUP_METADATA_DISCLAIMER}",
            2,
        )
    if indexed_file_count == 0 and health in {"not_scanned", "no_applied_scan"}:
        return BackupDisplay(
            "unknown",
            "Not scanned",
            "No successfully applied scan establishes whether this drive is empty.\n\n"
            f"{BACKUP_METADATA_DISCLAIMER}",
            2,
        )
    latest_status = enum_value(
        first_object_value(scan_record, "latest_attempt_status", "status", default="")
    )
    latest_errors = int(
        first_object_value(
            scan_record,
            "latest_attempt_errors",
            "errors_count",
            "access_errors",
            default=0,
        )
        or 0
    )
    latest_ignored_errors = int(
        object_value(scan_record, "latest_attempt_ignored_errors", 0) or 0
    )
    latest_hash_errors = int(
        object_value(scan_record, "latest_attempt_hash_errors", 0) or 0
    )
    actionable_latest_errors = max(
        0,
        latest_errors - latest_ignored_errors - latest_hash_errors,
    )
    if indexed_file_count == 0 and latest_status == "completed" and actionable_latest_errors > 0:
        return BackupDisplay(
            "unknown",
            "Check scan",
            "The completed scan recorded access errors, so an empty catalogue cannot be "
            f"treated as a confirmed empty drive.\n\n{BACKUP_METADATA_DISCLAIMER}",
            2,
        )
    if indexed_file_count == 0 and latest_status == "completed":
        return BackupDisplay(
            "unknown",
            "N/A · empty",
            "The last applied scan completed and the catalogue contains no files. "
            "Empty drives are excluded from other-copy coverage.\n\n"
            f"{BACKUP_METADATA_DISCLAIMER}",
            2,
        )
    if summary is None:
        return NOT_ANALYSED_BACKUP_DISPLAY
    if bool(object_value(summary, "is_stale", False)) or bool(
        object_value(analysis_state, "is_stale", False)
    ):
        reason = str(
            object_value(summary, "stale_reason", "")
            or object_value(analysis_state, "stale_reason", "")
            or ""
        )
        return BackupDisplay(
            "unknown",
            "Outdated",
            (reason or "The catalogue changed after this volume was analysed.")
            + f"\n\n{BACKUP_METADATA_DISCLAIMER}",
            2,
        )

    summary_status = enum_value(object_value(summary, "status", ""))
    excluded_files = int(object_value(summary, "excluded_files", 0) or 0)
    coverage_files = int(
        object_value(
            summary,
            "coverage_files",
            max(0, indexed_file_count - excluded_files),
        )
        or 0
    )
    if summary_status == "excluded" or (
        indexed_file_count > 0 and coverage_files == 0 and excluded_files > 0
    ):
        return BackupDisplay(
            "unknown",
            "N/A · system metadata",
            (
                f"All {excluded_files:,} indexed files are known operating-system "
                "bookkeeping and are excluded from copy coverage.\n\n"
                f"{BACKUP_METADATA_DISCLAIMER}"
            ),
            2,
        )

    coverage_eligible = bool(object_value(summary, "coverage_eligible", True))
    if indexed_file_count > 0 and not coverage_eligible:
        if health in {"not_scanned", "no_applied_scan"}:
            text = "Not scanned"
            reason = "No successfully applied scan provides a trustworthy coverage denominator."
        else:
            text = "Check scan"
            reason = (
                "The latest scan record is incomplete or contains access errors, so "
                "other-copy percentages are withheld."
            )
        return BackupDisplay(
            "unknown",
            text,
            f"{reason}\n\n{BACKUP_METADATA_DISCLAIMER}",
            2,
        )

    total_files = int(
        first_object_value(summary, "total_files", "indexed_files", "file_count", default=indexed_file_count)
        or 0
    )
    strong_files = int(
        first_object_value(
            summary,
            "likely_files",
            "strong_files",
            "matched_files",
            "protected_files",
            default=0,
        )
        or 0
    )
    total_bytes = int(first_object_value(summary, "total_bytes", "indexed_bytes", default=0) or 0)
    strong_bytes = int(
        first_object_value(
            summary,
            "likely_bytes",
            "strong_bytes",
            "matched_bytes",
            "protected_bytes",
            default=0,
        )
        or 0
    )
    files_percent = first_object_value(
        summary,
        "likely_files_percent",
        "strong_files_percent",
        "file_coverage_percent",
        "coverage_files_percent",
    )
    bytes_percent = first_object_value(
        summary,
        "likely_bytes_percent",
        "strong_bytes_percent",
        "byte_coverage_percent",
        "coverage_bytes_percent",
    )
    coverage_bytes = int(
        object_value(summary, "coverage_bytes", max(0, total_bytes)) or 0
    )
    if files_percent is None and coverage_files:
        files_percent = strong_files * 100.0 / coverage_files
    if bytes_percent is None and coverage_bytes:
        bytes_percent = strong_bytes * 100.0 / coverage_bytes
    if files_percent is None:
        return NOT_ANALYSED_BACKUP_DISPLAY

    files_value = float(files_percent)
    bytes_value = float(bytes_percent) if bytes_percent is not None else None
    if summary_status in {"likely", "strong", "complete"}:
        state, rank = "strong", 3
    elif summary_status in {"possible", "partial"}:
        state, rank = "possible", 1
    elif summary_status in {"single", "none", "no_match"}:
        state, rank = "none", 0
    elif summary_status in {"ambiguous", "unknown"}:
        state, rank = "unknown", 2
    elif files_value >= 100 and (bytes_value is None or bytes_value >= 100):
        state, rank = "strong", 3
    elif files_value > 0 or (bytes_value is not None and bytes_value > 0):
        state, rank = "possible", 1
    else:
        state, rank = "none", 0
    possible_files = int(object_value(summary, "possible_files", 0) or 0)
    ambiguous_files = int(object_value(summary, "ambiguous_files", 0) or 0)
    if state == "unknown" and ambiguous_files:
        text = f"Too common · {ambiguous_files:,} files"
    elif state == "possible" and files_value <= 0 and possible_files:
        text = f"Possible · {possible_files:,} files"
    else:
        text = f"{files_value:.0f}% files"
        if bytes_value is not None:
            text += f" · {bytes_value:.0f}% data"
    tooltip = (
        f"Hash-verified or strong metadata matches on another drive: {strong_files:,} of "
        f"{coverage_files:,} coverage-eligible files"
    )
    if coverage_bytes:
        tooltip += f", {format_size(strong_bytes)} of {format_size(coverage_bytes)}"
    if possible_files:
        tooltip += f". Possible-only matches not counted as coverage: {possible_files:,} files"
    if ambiguous_files:
        tooltip += f". Too-common metadata excluded from matching: {ambiguous_files:,} files"
    if excluded_files:
        tooltip += f". Operating-system metadata excluded: {excluded_files:,} files"
    tooltip += (
        ". Coverage is aggregated across all other drives; 100% here does not by "
        "itself prove that one complete mirror drive exists"
    )
    tooltip += f".\n\n{BACKUP_METADATA_DISCLAIMER}"
    return BackupDisplay(state, text, tooltip, rank)


def file_type_label(extension: str) -> str:
    ext = extension.lower().lstrip(".")
    if not ext:
        return "File"
    if ext in VIDEO_EXTENSIONS:
        return "Video"
    if ext in AUDIO_EXTENSIONS:
        return "Audio"
    if ext in IMAGE_EXTENSIONS:
        return "Image"
    if ext in TEXT_EXTENSIONS:
        return "Text"
    if ext in PDF_EXTENSIONS:
        return "PDF"
    if ext in OFFICE_EXTENSIONS:
        return "Office Document"
    if ext in ARCHIVE_EXTENSIONS:
        return "Archive"
    if ext in EXECUTABLE_EXTENSIONS:
        return "Executable"
    return ext.upper()


def file_category(extension: str) -> str:
    ext = extension.lower().lstrip(".")
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in OFFICE_EXTENSIONS:
        return "office"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in EXECUTABLE_EXTENSIONS:
        return "executable"
    return "unknown"


@dataclass(frozen=True)
class BrowserItem:
    item_type: str
    item_id: int
    name: str
    relative_path: str
    type_label: str
    extension: str = ""
    size_bytes: int | None = 0
    modified_at: str | None = None
    missing: bool = False
    parent_id: int | None = None
    is_parent_entry: bool = False
    backup: BackupDisplay = NOT_ANALYSED_BACKUP_DISPLAY

    @property
    def is_folder(self) -> bool:
        return self.item_type == "folder"


@dataclass(frozen=True)
class CatalogueItemRef:
    item_type: str
    item_id: int
    volume_id: int
    relative_path: str
    missing: bool = False

    @property
    def is_folder(self) -> bool:
        return self.item_type == "folder"


class CatalogueIconProvider:
    CATEGORY_STYLES = {
        "video": ("VID", "#7c3aed"),
        "audio": ("AUD", "#0f766e"),
        "image": ("IMG", "#15803d"),
        "text": ("TXT", "#475569"),
        "pdf": ("PDF", "#dc2626"),
        "office": ("DOC", "#2563eb"),
        "archive": ("ZIP", "#b45309"),
        "executable": ("EXE", "#334155"),
        "unknown": ("?", "#64748b"),
    }

    def __init__(self) -> None:
        self.native = QFileIconProvider()
        self.generic_file_icon = self.native.icon(QFileIconProvider.IconType.File)
        self.folder_icon = self.native.icon(QFileIconProvider.IconType.Folder)
        if self.folder_icon.isNull():
            self.folder_icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        if self.generic_file_icon.isNull():
            self.generic_file_icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.fallback_cache: dict[str, QIcon] = {}
        self.native_cache: dict[str, QIcon] = {}

    def icon_for(self, item: BrowserItem) -> QIcon:
        if item.is_folder:
            return self.folder_icon

        ext = item.extension.lower().lstrip(".")
        native_icon = self._native_icon_for_extension(ext)
        if native_icon is not None:
            return native_icon
        return self._fallback_icon(file_category(ext))

    def _native_icon_for_extension(self, extension: str) -> QIcon | None:
        if not extension:
            return None
        if extension in self.native_cache:
            return self.native_cache[extension]

        icon = self.native.icon(QFileInfo(f"jvvv-placeholder.{extension}"))
        if not icon.isNull() and icon.cacheKey() != self.generic_file_icon.cacheKey():
            self.native_cache[extension] = icon
            return icon
        return None

    def _fallback_icon(self, category: str) -> QIcon:
        if category in self.fallback_cache:
            return self.fallback_cache[category]

        label, color = self.CATEGORY_STYLES.get(category, self.CATEGORY_STYLES["unknown"])
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f8fafc"))
        painter.drawRoundedRect(QRectF(6, 3, 20, 26), 3, 3)
        painter.setBrush(QColor("#e2e8f0"))
        painter.drawPolygon(QPolygon([QPoint(21, 3), QPoint(26, 8), QPoint(21, 8)]))
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(8, 16, 16, 9), 2, 2)
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPointSize(6 if len(label) > 2 else 8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(8, 16, 16, 9), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()

        icon = QIcon(pixmap)
        self.fallback_cache[category] = icon
        return icon


class BackupStatusIconProvider:
    COLORS = {
        "strong": "#2e9b4b",
        "possible": "#d18a00",
        "none": "#cf3f3f",
        "unknown": "#7b8794",
    }

    def __init__(self) -> None:
        self.cache: dict[str, QIcon] = {}

    def icon_for(self, display: BackupDisplay) -> QIcon:
        state = display.state if display.state in self.COLORS else "unknown"
        if state in self.cache:
            return self.cache[state]
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#39424c"))
        painter.setBrush(QColor(self.COLORS[state]))
        painter.drawEllipse(QRectF(3, 3, 10, 10))
        painter.end()
        icon = QIcon(pixmap)
        self.cache[state] = icon
        return icon


@dataclass(frozen=True)
class VolumeItem:
    id: int
    drive_id: str
    name: str | None
    source_path: str
    register_status: str
    condition: str
    description: str
    connector: str
    is_mirror: bool
    master_volume_id: int | None
    master_drive_id: str | None
    master_name: str | None
    date_added: str
    earliest_content_date: str | None
    latest_content_date: str | None
    retired_date: str | None
    mirror_date: str | None
    capacity_bytes: int
    used_bytes: int
    free_bytes: int
    indexed_file_count: int
    indexed_folder_count: int
    last_scan_at: str | None
    connected: bool
    percent_full: int
    backup: BackupDisplay = NOT_ANALYSED_BACKUP_DISPLAY


def volume_item_from_record(
    volume: Any,
    connected: bool,
    backup_summary: Any = None,
    scan_record: Any = None,
    analysis_state: Any = None,
) -> VolumeItem:
    return VolumeItem(
        id=volume["id"],
        drive_id=volume["drive_id"] or "",
        name=volume["name"],
        source_path=volume["source_path"],
        register_status=volume["register_status"],
        condition=volume["condition"],
        description=volume["description"] or "",
        connector=volume["connector"],
        is_mirror=bool(volume["is_mirror"]),
        master_volume_id=volume["master_volume_id"],
        master_drive_id=volume["master_drive_id"],
        master_name=volume["master_name"],
        date_added=volume["date_added"],
        earliest_content_date=volume["earliest_content_date"],
        latest_content_date=volume["latest_content_date"],
        retired_date=volume["retired_date"],
        mirror_date=volume["mirror_date"],
        capacity_bytes=volume["capacity_bytes"],
        used_bytes=volume["used_bytes"],
        free_bytes=volume["free_bytes"],
        indexed_file_count=volume["indexed_file_count"],
        indexed_folder_count=volume["indexed_folder_count"],
        last_scan_at=volume["last_scan_at"],
        connected=connected,
        percent_full=percentage_full(volume["used_bytes"], volume["capacity_bytes"]),
        backup=volume_backup_display(
            backup_summary,
            int(volume["indexed_file_count"] or 0),
            scan_record,
            analysis_state,
        ),
    )


@dataclass(frozen=True)
class SearchResultItem:
    item_type: str
    item_id: int
    name: str
    volume_id: int
    drive_id: str | None
    volume_name: str | None
    relative_path: str
    size_bytes: int | None
    modified_at: str | None
    missing: bool
    source_path: str
    connected: bool
    backup: BackupDisplay = NOT_ANALYSED_BACKUP_DISPLAY

    @property
    def is_folder(self) -> bool:
        return self.item_type == "folder"


class BrowserTableModel(StandardTableModel):
    def __init__(
        self,
        icons: CatalogueIconProvider,
        parent: QObject | None = None,
        backup_icons: BackupStatusIconProvider | None = None,
    ) -> None:
        self.icons = icons
        self.backup_icons = backup_icons or BackupStatusIconProvider()
        super().__init__(
            [
                TableColumn("Name", lambda item: item.name, decoration=self.icons.icon_for),
                TableColumn(
                    "Other copies",
                    lambda item: "N/A" if item.is_parent_entry else item.backup.text,
                    sort_key=lambda item: item.backup.sort_rank,
                    decoration=lambda item: None
                    if item.is_parent_entry
                    else self.backup_icons.icon_for(item.backup),
                    tooltip=lambda item: None if item.is_parent_entry else item.backup.tooltip,
                    header_tooltip=BACKUP_COLUMN_HEADER_TOOLTIP,
                ),
                TableColumn("Type", lambda item: item.type_label),
                TableColumn(
                    "Size",
                    lambda item: display_indexed_size(item.size_bytes),
                    sort_key=lambda item: size_sort_key(item.size_bytes),
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn("Modified", lambda item: display_db_time(item.modified_at), sort_key=lambda item: item.modified_at or ""),
                TableColumn("Relative Path", lambda item: relative_path_for_display(item.relative_path)),
                TableColumn("Status", lambda item: "Missing" if item.missing else "Indexed"),
            ],
            parent,
        )

    def role_value(self, item: BrowserItem, role: int) -> Any:
        if role == ROLE_ITEM_ID:
            return item.item_id
        if role == ROLE_ITEM_TYPE:
            return item.item_type
        if role == ROLE_RELATIVE_PATH:
            return item.relative_path
        if role == ROLE_FOLDER_ID and item.is_folder:
            return item.item_id
        return None

    def group_key(self, item: BrowserItem) -> int:
        if item.is_parent_entry:
            return -1
        return 0 if item.is_folder else 1


class VolumeTableModel(StandardTableModel):
    def __init__(
        self,
        parent: QObject | None = None,
        backup_icons: BackupStatusIconProvider | None = None,
    ) -> None:
        self.backup_icons = backup_icons or BackupStatusIconProvider()
        super().__init__(
            [
                TableColumn(
                    "Drive ID",
                    lambda item: item.drive_id or "-",
                    sort_key=lambda item: drive_id_sort_key(item.drive_id),
                ),
                TableColumn("Name", lambda item: display_volume_name(item.name)),
                TableColumn("Source Path", lambda item: item.source_path or "-"),
                TableColumn("Status", lambda item: item.register_status),
                TableColumn("Condition", lambda item: item.condition),
                TableColumn("Description", lambda item: item.description or "-"),
                TableColumn("Connector", lambda item: item.connector),
                TableColumn("Connection", lambda item: "Connected" if item.connected else "Offline"),
                TableColumn("Mirror", lambda item: "Yes" if item.is_mirror else "No", sort_key=lambda item: item.is_mirror),
                TableColumn(
                    "Master Drive",
                    lambda item: volume_reference(item.master_drive_id, item.master_name)
                    if item.master_volume_id is not None
                    else "-",
                ),
                TableColumn("Date Added", lambda item: display_db_date(item.date_added), sort_key=lambda item: item.date_added or ""),
                TableColumn(
                    "Earliest Content",
                    lambda item: display_db_date(item.earliest_content_date),
                    sort_key=lambda item: item.earliest_content_date or "",
                ),
                TableColumn(
                    "Latest Content",
                    lambda item: display_db_date(item.latest_content_date),
                    sort_key=lambda item: item.latest_content_date or "",
                ),
                TableColumn("Retired Date", lambda item: display_db_date(item.retired_date), sort_key=lambda item: item.retired_date or ""),
                TableColumn("Mirror Date", lambda item: display_db_date(item.mirror_date), sort_key=lambda item: item.mirror_date or ""),
                TableColumn(
                    "Capacity",
                    lambda item: format_size(item.capacity_bytes),
                    sort_key=lambda item: item.capacity_bytes,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn(
                    "Used",
                    lambda item: format_size(item.used_bytes),
                    sort_key=lambda item: item.used_bytes,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn(
                    "Free",
                    lambda item: format_size(item.free_bytes),
                    sort_key=lambda item: item.free_bytes,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn("Full", lambda item: f"{item.percent_full}%", sort_key=lambda item: item.percent_full),
                TableColumn(
                    "Files",
                    lambda item: f"{item.indexed_file_count:,}",
                    sort_key=lambda item: item.indexed_file_count,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn(
                    "Folders",
                    lambda item: f"{item.indexed_folder_count:,}",
                    sort_key=lambda item: item.indexed_folder_count,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn("Last Scan", lambda item: display_db_time(item.last_scan_at), sort_key=lambda item: item.last_scan_at or ""),
                TableColumn(
                    "Other-copy coverage",
                    lambda item: item.backup.text,
                    sort_key=lambda item: item.backup.sort_rank,
                    decoration=lambda item: self.backup_icons.icon_for(item.backup),
                    tooltip=lambda item: item.backup.tooltip,
                    header_tooltip=(
                        "The share of this volume's indexed files and bytes with a hash-verified "
                        "or strong metadata match on another catalogue drive. Empty volumes are N/A.\n\n"
                        f"{BACKUP_METADATA_DISCLAIMER}"
                    ),
                ),
            ],
            parent,
        )

    def role_value(self, item: VolumeItem, role: int) -> Any:
        if role == ROLE_VOLUME_ID:
            return item.id
        if role == ROLE_PERCENT_FULL:
            return item.percent_full
        return None


class VolumeTableView(QTableView):
    """A table that can scroll half a viewport beyond its final row."""

    def updateGeometries(self) -> None:  # type: ignore[override]
        super().updateGeometries()
        scroll_bar = self.verticalScrollBar()
        trailing_space = max(0, self.viewport().height() // 2)
        scroll_bar.setMaximum(scroll_bar.maximum() + trailing_space)


class SearchResultsTableModel(StandardTableModel):
    def __init__(
        self,
        icons: CatalogueIconProvider,
        parent: QObject | None = None,
        backup_icons: BackupStatusIconProvider | None = None,
    ) -> None:
        self.icons = icons
        self.backup_icons = backup_icons or BackupStatusIconProvider()
        super().__init__(
            [
                TableColumn("Name", lambda item: item.name, decoration=self.icon_for),
                TableColumn(
                    "Other copies",
                    lambda item: item.backup.text,
                    sort_key=lambda item: item.backup.sort_rank,
                    decoration=lambda item: self.backup_icons.icon_for(item.backup),
                    tooltip=lambda item: item.backup.tooltip,
                    header_tooltip=BACKUP_COLUMN_HEADER_TOOLTIP,
                ),
                TableColumn("Kind", lambda item: item.item_type.title()),
                TableColumn("Volume", lambda item: display_volume_name(item.volume_name)),
                TableColumn(
                    "Drive ID",
                    lambda item: item.drive_id or "-",
                    sort_key=lambda item: drive_id_sort_key(item.drive_id),
                ),
                TableColumn("Relative Path", lambda item: relative_path_for_display(item.relative_path)),
                TableColumn(
                    "Size",
                    lambda item: display_indexed_size(item.size_bytes),
                    sort_key=lambda item: size_sort_key(item.size_bytes),
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn("Modified", lambda item: display_db_time(item.modified_at), sort_key=lambda item: item.modified_at or ""),
                TableColumn("Volume Status", lambda item: "Connected" if item.connected else "Offline"),
            ],
            parent,
        )

    def icon_for(self, item: SearchResultItem) -> QIcon:
        extension = "" if item.is_folder else PurePosixPath(item.name).suffix.lstrip(".")
        return self.icons.icon_for(
            BrowserItem(
                item_type=item.item_type,
                item_id=item.item_id,
                name=item.name,
                relative_path=item.relative_path,
                type_label="Folder" if item.is_folder else file_type_label(extension),
                extension=extension,
                size_bytes=item.size_bytes,
                modified_at=item.modified_at,
                missing=item.missing,
            )
        )

    def role_value(self, item: SearchResultItem, role: int) -> Any:
        if role == ROLE_VOLUME_ID:
            return item.volume_id
        if role == ROLE_ITEM_ID:
            return item.item_id
        if role == ROLE_ITEM_TYPE:
            return item.item_type
        if role == ROLE_RELATIVE_PATH:
            return item.relative_path
        return None

    def group_key(self, item: SearchResultItem) -> int:
        return 0 if item.is_folder else 1


class VolumeFullDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:  # type: ignore[override]
        percent = int(index.data(ROLE_PERCENT_FULL) or 0)
        percent = max(0, min(100, percent))

        painter.save()
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)

        content_rect = option.rect.adjusted(6, 4, -6, -4)
        text = f"{percent}%"
        text_width = option.fontMetrics.horizontalAdvance("100%")
        bar_rect = content_rect.adjusted(0, 0, -(text_width + 8), 0)

        if bar_rect.width() > 0 and bar_rect.height() > 0:
            radius = 3.0
            base_color = option.palette.color(QPalette.ColorRole.Base)
            border_color = option.palette.color(QPalette.ColorRole.Mid)
            chunk_color = option.palette.color(QPalette.ColorRole.Highlight)

            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(border_color)
            painter.setBrush(base_color)
            painter.drawRoundedRect(QRectF(bar_rect), radius, radius)

            if percent > 0:
                fill_rect = QRectF(bar_rect)
                fill_rect.setWidth(max(2.0, fill_rect.width() * (percent / 100)))
                fill_rect = fill_rect.adjusted(1, 1, -1, -1)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(chunk_color)
                painter.drawRoundedRect(fill_rect, radius - 1, radius - 1)

        text_color = option.palette.color(
            QPalette.ColorRole.HighlightedText
            if option.state & QStyle.StateFlag.State_Selected
            else QPalette.ColorRole.Text
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(text_color)
        painter.drawText(content_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


def display_db_time(value: str | None) -> str:
    parsed = parse_db_time(value)
    if parsed is None:
        return "-"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def display_indexed_size(value: int | None) -> str:
    if value is None:
        return "Unknown"
    return format_size(value)


def display_duration_ms(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    milliseconds = max(0, int(value))
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:d}:{seconds:02d}.{millis:03d}"


def size_sort_key(value: int | None) -> int:
    return -1 if value is None else int(value)


def source_path_exists(source_path: str | None) -> bool:
    return bool(source_path and Path(source_path).exists())


def display_db_date(value: str | None) -> str:
    if not value:
        return "-"
    qdate = QDate.fromString(value, Qt.DateFormat.ISODate)
    if not qdate.isValid():
        return value
    return QLocale.system().toString(qdate, QLocale.FormatType.ShortFormat)


def volume_reference(drive_id: str | None, name: str | None) -> str:
    if drive_id and name:
        return f"{drive_id} - {name}"
    return drive_id or name or "-"


def display_volume_name(name: str | None) -> str:
    return name or "-"


def suggested_new_volume_drive_id(volume_label: str | None, fallback_drive_id: str) -> str:
    label = (volume_label or "").strip()
    if AID_VOLUME_LABEL_RE.fullmatch(label):
        return label.upper()
    return fallback_drive_id


def volume_matches_filter(item: VolumeItem, query: str) -> bool:
    text = query.strip().casefold()
    if not text:
        return True
    haystack = " ".join(
        [
            item.drive_id or "",
            item.name or "",
            item.source_path or "",
            item.register_status,
            item.condition,
            item.description,
            item.connector,
            "mirror" if item.is_mirror else "",
            volume_reference(item.master_drive_id, item.master_name)
            if item.master_volume_id is not None
            else "",
            item.date_added or "",
            item.earliest_content_date or "",
            item.latest_content_date or "",
            item.retired_date or "",
            item.mirror_date or "",
            str(item.indexed_file_count),
            str(item.indexed_folder_count),
            item.backup.text,
        ]
    ).casefold()
    return all(term in haystack for term in text.split())


def connected_volume_signature(snapshots: list[VolumeSnapshot]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                snapshot.identity_kind.casefold(),
                snapshot.identity_token.casefold(),
                snapshot.mount_root.casefold(),
            )
            for snapshot in snapshots
            if snapshot.identity_kind and snapshot.identity_token
        )
    )


def include_content_timestamp(
    earliest: str | None,
    latest: str | None,
    timestamp: float,
) -> tuple[str | None, str | None]:
    try:
        value = datetime.fromtimestamp(timestamp).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return earliest, latest
    earliest = value if earliest is None or value < earliest else earliest
    latest = value if latest is None or value > latest else latest
    return earliest, latest


def iter_content_date_timestamps(root: Path) -> Iterator[float]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            yield current.stat().st_mtime
        except OSError:
            pass
        try:
            if not current.is_dir():
                continue
        except OSError:
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                        yield stat_result.st_mtime
                    except OSError:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue


def guess_content_dates_from_path(source_path: str) -> tuple[str | None, str | None]:
    if not source_path:
        return None, None
    root = Path(source_path)
    if not root.exists():
        return None, None

    earliest: str | None = None
    latest: str | None = None
    for timestamp in iter_content_date_timestamps(root):
        earliest, latest = include_content_timestamp(earliest, latest, timestamp)
    return earliest, latest


def set_combo_value(combo: QComboBox, value: str) -> None:
    index = combo.findText(value)
    if index < 0:
        combo.addItem(value)
        index = combo.findText(value)
    combo.setCurrentIndex(index)


class OptionalDateEdit(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.known_check = QCheckBox("Known")
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat(QLocale.system().dateFormat(QLocale.FormatType.ShortFormat))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.known_check)
        layout.addWidget(self.date_edit, 1)

        self.known_check.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        self.date_edit.setEnabled(self.isEnabled() and self.known_check.isChecked())

    def setEnabled(self, enabled: bool) -> None:  # type: ignore[override]
        super().setEnabled(enabled)
        self._sync_enabled()

    def value(self) -> str | None:
        if not self.known_check.isChecked():
            return None
        return self.date_edit.date().toString(Qt.DateFormat.ISODate)

    def set_value(self, value: str | None) -> None:
        if value:
            qdate = QDate.fromString(value, Qt.DateFormat.ISODate)
            self.date_edit.setDate(qdate if qdate.isValid() else QDate.currentDate())
            self.known_check.setChecked(True)
        else:
            self.known_check.setChecked(False)
        self._sync_enabled()

    def set_value_if_empty(self, value: str | None) -> None:
        if value and not self.value():
            self.set_value(value)

    def clear(self) -> None:
        self.set_value(None)


class DriveIdDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        suggested_drive_id: str,
        source_path: str,
        volume_label: str = "",
        existing_volumes: list[Any] | None = None,
        allow_volume_label_rename: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm Drive ID")
        self.setMinimumWidth(480)
        self.existing_drive_ids = {
            str(row["drive_id"]).casefold()
            for row in existing_volumes or []
            if row["drive_id"]
        }

        self.drive_id_edit = QLineEdit(suggested_drive_id)
        self.drive_id_edit.selectAll()
        self.validation_label = QLabel("")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b91c1c;")

        path_label = QLabel(source_path)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        form = QFormLayout()
        form.addRow("Drive or folder", path_label)
        form.addRow("Drive ID", self.drive_id_edit)

        self._custom_volume_label = volume_label
        self.volume_label_edit = QLineEdit(volume_label)
        self.rename_volume_label_check = QCheckBox(
            "Rename volume label to match Drive ID"
        )
        self.rename_volume_label_check.setEnabled(allow_volume_label_rename)
        self.rename_volume_label_check.setChecked(allow_volume_label_rename)
        if not allow_volume_label_rename:
            self.rename_volume_label_check.setToolTip(
                "Volume-label renaming is available for connected Windows volumes."
            )
        form.addRow("Volume Label", self.volume_label_edit)
        form.addRow("", self.rename_volume_label_check)

        self.rename_volume_label_check.toggled.connect(
            self.on_rename_volume_label_toggled
        )
        self.drive_id_edit.textChanged.connect(self.on_drive_id_changed)
        self.volume_label_edit.textEdited.connect(self.remember_custom_volume_label)
        self.sync_volume_label_controls()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.validation_label)
        layout.addWidget(buttons)

    def value(self) -> str:
        return self.drive_id_edit.text().strip()

    def should_rename_volume_label(self) -> bool:
        return self.rename_volume_label_check.isChecked()

    def volume_label_value(self) -> str:
        return self.volume_label_edit.text().strip()

    def remember_custom_volume_label(self, label: str) -> None:
        self._custom_volume_label = label

    def on_rename_volume_label_toggled(self, checked: bool) -> None:
        if checked:
            self._custom_volume_label = self.volume_label_edit.text()
        self.sync_volume_label_controls()

    def on_drive_id_changed(self, drive_id: str) -> None:
        if self.rename_volume_label_check.isChecked():
            self.volume_label_edit.setText(drive_id)

    def sync_volume_label_controls(self) -> None:
        rename_to_drive_id = self.rename_volume_label_check.isChecked()
        if rename_to_drive_id:
            self.volume_label_edit.setText(self.drive_id_edit.text())
        else:
            self.volume_label_edit.setText(self._custom_volume_label)
        self.volume_label_edit.setEnabled(
            self.rename_volume_label_check.isEnabled() and not rename_to_drive_id
        )

    def validate_form(self) -> str | None:
        drive_id = self.value()
        if not is_valid_drive_id(drive_id):
            return "Enter a Drive ID."
        if drive_id.casefold() in self.existing_drive_ids:
            return "Drive IDs must be unique within the catalogue."
        return None

    def accept(self) -> None:
        message = self.validate_form()
        if message is not None:
            self.validation_label.setText(message)
            return
        super().accept()


class VolumeDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "New Volume",
        volume=None,
        suggested_drive_id: str = "",
        master_options: list[Any] | None = None,
        mirror_dependents: list[Any] | None = None,
        existing_volumes: list[Any] | None = None,
        show_source_path: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(760)
        self.resize(820, 620)
        self.current_volume_id = int(volume["id"]) if volume is not None else None
        self.show_source_path = show_source_path
        self.master_options = master_options or []
        self.mirror_dependents = mirror_dependents or []
        self.existing_names = {
            str(row["name"]).casefold(): int(row["id"])
            for row in existing_volumes or []
            if row["name"]
        }
        self.existing_drive_ids = {
            str(row["drive_id"]).casefold(): int(row["id"])
            for row in existing_volumes or []
            if row["drive_id"]
        }

        self.drive_id_edit = QLineEdit(volume["drive_id"] if volume is not None else suggested_drive_id)
        self.name_edit = QLineEdit((volume["name"] or "") if volume is not None else "")
        self.path_edit = QLineEdit(volume["source_path"] if volume is not None else "")
        self.path_display_label = self._read_only_value_label(self.path_edit.text() or "-")
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse)
        self.date_guess_progress = QProgressBar()
        self.date_guess_progress.setRange(0, 0)
        self.date_guess_progress.setFormat("Checking content dates...")
        self.date_guess_progress.setTextVisible(True)
        self.date_guess_progress.setVisible(False)
        configure_progress_bar(self.date_guess_progress)
        self.date_guess_timer = QTimer(self)
        self.date_guess_timer.timeout.connect(self.process_content_date_guess)
        self.date_guess_iterator: Iterator[float] | None = None
        self.date_guess_source_path: str | None = None
        self.date_guess_earliest: str | None = None
        self.date_guess_latest: str | None = None
        self.date_guess_items_seen = 0

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)

        self.status_combo = QComboBox()
        self.status_combo.addItems(ARCHIVE_STATUSES)
        set_combo_value(
            self.status_combo,
            volume["register_status"] if volume is not None else ARCHIVE_STATUSES[0],
        )

        self.condition_combo = QComboBox()
        self.condition_combo.addItems(VOLUME_CONDITIONS)
        set_combo_value(
            self.condition_combo,
            volume["condition"] if volume is not None else "Unknown",
        )

        self.connector_combo = QComboBox()
        self.connector_combo.setEditable(True)
        self.connector_combo.addItems(CONNECTOR_OPTIONS)
        self.connector_combo.setCurrentText(volume["connector"] if volume is not None else "Unknown")

        self.date_added_edit = QDateEdit(QDate.currentDate())
        self.date_added_edit.setCalendarPopup(True)
        self.date_added_edit.setDisplayFormat(QLocale.system().dateFormat(QLocale.FormatType.ShortFormat))
        if volume is not None and volume["date_added"]:
            qdate = QDate.fromString(volume["date_added"], Qt.DateFormat.ISODate)
            if qdate.isValid():
                self.date_added_edit.setDate(qdate)

        self.earliest_date_edit = OptionalDateEdit()
        self.latest_date_edit = OptionalDateEdit()
        self.retired_date_edit = OptionalDateEdit()
        self.mirror_date_edit = OptionalDateEdit()
        if volume is not None:
            self.earliest_date_edit.set_value(volume["earliest_content_date"])
            self.latest_date_edit.set_value(volume["latest_content_date"])
            self.retired_date_edit.set_value(volume["retired_date"])
            self.mirror_date_edit.set_value(volume["mirror_date"])

        self.mirror_check = QCheckBox("This is a mirror drive")
        self.mirror_check.setChecked(bool(volume is not None and volume["is_mirror"]))
        self.master_combo = QComboBox()
        self.master_combo.addItem("Select master drive...", None)
        for row in sorted(
            self.master_options,
            key=lambda item: (drive_id_sort_key(item["drive_id"]), (item["name"] or "").casefold()),
        ):
            self.master_combo.addItem(volume_reference(row["drive_id"], row["name"]), int(row["id"]))
        if volume is not None and volume["master_volume_id"] is not None:
            self.set_master_volume_id(int(volume["master_volume_id"]))

        self.description_edit = QPlainTextEdit(volume["description"] if volume is not None else "")
        self.description_edit.setMinimumHeight(90)

        self.validation_label = QLabel("")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b91c1c;")

        identity_box, identity_form = self._dialog_section("Identity")
        identity_form.addRow("Drive ID", self.drive_id_edit)
        identity_form.addRow("Name", self.name_edit)
        if self.show_source_path:
            identity_form.addRow("Drive or folder", path_row)
            identity_form.addRow("", self.date_guess_progress)
        else:
            identity_form.addRow("Scan Path", self.path_display_label)

        register_box, register_form = self._dialog_section("Register")
        register_form.addRow("Status", self.status_combo)
        register_form.addRow("Condition", self.condition_combo)
        register_form.addRow("Connector", self.connector_combo)

        dates_box, dates_form = self._dialog_section("Dates")
        dates_form.addRow("Date Added", self.date_added_edit)
        dates_form.addRow("Earliest Content", self.earliest_date_edit)
        dates_form.addRow("Latest Content", self.latest_date_edit)
        dates_form.addRow("Retired Date", self.retired_date_edit)

        mirror_box, mirror_form = self._dialog_section("Mirror")
        mirror_form.addRow("", self.mirror_check)
        mirror_form.addRow("Master Drive", self.master_combo)
        mirror_form.addRow("Mirror Date", self.mirror_date_edit)

        notes_box = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_box)
        notes_layout.addWidget(self.description_edit)

        content = QWidget()
        content_layout = QGridLayout(content)
        content_layout.setColumnStretch(0, 1)
        content_layout.setColumnStretch(1, 1)
        content_layout.addWidget(identity_box, 0, 0)
        content_layout.addWidget(register_box, 0, 1)
        content_layout.addWidget(dates_box, 1, 0)
        content_layout.addWidget(mirror_box, 1, 1)
        content_layout.addWidget(notes_box, 2, 0, 1, 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.validation_label)
        layout.addWidget(buttons)

        self.status_combo.currentTextChanged.connect(self.on_status_changed)
        self.mirror_check.toggled.connect(self.on_mirror_toggled)
        self.path_edit.editingFinished.connect(self.on_path_editing_finished)
        self.on_status_changed(self.status_combo.currentText())
        self.on_mirror_toggled(self.mirror_check.isChecked())

    def _read_only_value_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        return label

    def _dialog_section(self, title: str) -> tuple[QGroupBox, QFormLayout]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return box, form

    def browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose Drive or Folder", self.path_edit.text())
        if directory:
            self.path_edit.setText(directory)
            self.start_content_date_guess(directory)

    def on_path_editing_finished(self) -> None:
        if self.show_source_path:
            self.start_content_date_guess(self.path_edit.text().strip())

    def start_content_date_guess(self, source_path: str) -> None:
        self.stop_content_date_guess()
        if not source_path:
            return
        root = Path(source_path)
        if not root.exists():
            return
        if self.earliest_date_edit.value() and self.latest_date_edit.value():
            return

        self.date_guess_source_path = source_path
        self.date_guess_iterator = iter_content_date_timestamps(root)
        self.date_guess_earliest = None
        self.date_guess_latest = None
        self.date_guess_items_seen = 0
        self.date_guess_progress.setRange(0, 0)
        self.date_guess_progress.setValue(0)
        self.date_guess_progress.setFormat("Checking content dates...")
        self.date_guess_progress.setVisible(True)
        self.date_guess_timer.start(0)

    def stop_content_date_guess(self, hide_progress: bool = True) -> None:
        self.date_guess_timer.stop()
        self.date_guess_iterator = None
        self.date_guess_source_path = None
        self.date_guess_earliest = None
        self.date_guess_latest = None
        self.date_guess_items_seen = 0
        if hide_progress:
            self.date_guess_progress.setVisible(False)

    def process_content_date_guess(self) -> None:
        if self.date_guess_iterator is None:
            self.date_guess_timer.stop()
            return

        deadline = monotonic() + CONTENT_DATE_GUESS_TIME_BUDGET_SECONDS
        processed = 0
        while processed < CONTENT_DATE_GUESS_ITEM_BUDGET and monotonic() < deadline:
            try:
                timestamp = next(self.date_guess_iterator)
            except StopIteration:
                self.finish_content_date_guess()
                return
            self.date_guess_earliest, self.date_guess_latest = include_content_timestamp(
                self.date_guess_earliest,
                self.date_guess_latest,
                timestamp,
            )
            processed += 1
            self.date_guess_items_seen += 1

        if processed:
            self.date_guess_progress.setFormat(
                f"Checking content dates... {self.date_guess_items_seen} items"
            )

    def finish_content_date_guess(self) -> None:
        source_path = self.date_guess_source_path
        earliest = self.date_guess_earliest
        latest = self.date_guess_latest
        items_seen = self.date_guess_items_seen
        self.stop_content_date_guess(hide_progress=False)

        if source_path != self.path_edit.text().strip():
            self.date_guess_progress.setVisible(False)
            return

        self.earliest_date_edit.set_value_if_empty(earliest)
        self.latest_date_edit.set_value_if_empty(latest)
        if items_seen:
            self.date_guess_progress.setRange(0, 1)
            self.date_guess_progress.setValue(1)
            self.date_guess_progress.setFormat("Content dates checked")
        else:
            self.date_guess_progress.setVisible(False)

    def set_master_volume_id(self, volume_id: int) -> None:
        for index in range(self.master_combo.count()):
            if self.master_combo.itemData(index) == volume_id:
                self.master_combo.setCurrentIndex(index)
                return

    def on_status_changed(self, status: str) -> None:
        if status == "Retired" and not self.retired_date_edit.value():
            self.retired_date_edit.set_value(QDate.currentDate().toString(Qt.DateFormat.ISODate))

    def on_mirror_toggled(self, checked: bool) -> None:
        self.master_combo.setEnabled(checked)
        self.mirror_date_edit.setEnabled(checked)
        if not checked:
            self.master_combo.setCurrentIndex(0)
            self.mirror_date_edit.clear()

    def values(self) -> tuple[str, str, dict[str, Any]]:
        master_volume_id = self.master_combo.currentData()
        register = {
            "drive_id": self.drive_id_edit.text().strip(),
            "is_mirror": self.mirror_check.isChecked(),
            "status": self.status_combo.currentText().strip(),
            "condition": self.condition_combo.currentText().strip(),
            "description": self.description_edit.toPlainText(),
            "earliest_content_date": self.earliest_date_edit.value(),
            "latest_content_date": self.latest_date_edit.value(),
            "connector": self.connector_combo.currentText().strip(),
            "date_added": self.date_added_edit.date().toString(Qt.DateFormat.ISODate),
            "retired_date": self.retired_date_edit.value(),
            "mirror_date": self.mirror_date_edit.value() if self.mirror_check.isChecked() else None,
            "master_volume_id": int(master_volume_id)
            if self.mirror_check.isChecked() and master_volume_id is not None
            else None,
        }
        return self.name_edit.text().strip(), self.path_edit.text().strip(), register

    def validate_form(self) -> str | None:
        name, _source_path, register = self.values()
        existing_name_id = self.existing_names.get(name.casefold()) if name else None
        if name and existing_name_id is not None and existing_name_id != self.current_volume_id:
            return "Volume names must be unique within the catalogue."

        drive_id = register["drive_id"]
        if not is_valid_drive_id(drive_id):
            return "Enter a Drive ID."

        existing_drive_id = self.existing_drive_ids.get(str(drive_id).casefold())
        if existing_drive_id is not None and existing_drive_id != self.current_volume_id:
            return "Drive IDs must be unique within the catalogue."

        earliest = register["earliest_content_date"]
        latest = register["latest_content_date"]
        if earliest and latest and earliest > latest:
            return "Earliest Content Date cannot be after Latest Content Date."

        date_added = register["date_added"]
        retired_date = register["retired_date"]
        if retired_date and retired_date < date_added:
            return "Retired Date cannot be before Date Added."

        mirror_date = register["mirror_date"]
        if mirror_date and mirror_date < date_added:
            return "Mirror Date cannot be before Date Added."

        if self.mirror_check.isChecked():
            master_volume_id = register["master_volume_id"]
            if master_volume_id is None:
                return "Select the non-mirror master drive."
            if master_volume_id == self.current_volume_id:
                return "A volume cannot mirror itself."
            if self.mirror_dependents:
                return "This volume is already a master drive. Remove its mirror relationships before marking it as a mirror."

        return None

    def accept(self) -> None:
        message = self.validate_form()
        if message is not None:
            self.validation_label.setText(message)
            return
        super().accept()

    def done(self, result: int) -> None:
        self.stop_content_date_guess()
        super().done(result)


class ItemPropertiesDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        icon: QIcon,
        name: str,
        subtitle: str,
        properties: list[tuple[str, str]],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Properties - {name}")
        self.resize(760, 520)
        self.setMinimumSize(560, 380)
        self.copy_text = "\n".join(f"{label}: {value}" for label, value in properties)

        icon_label = QLabel()
        icon_label.setPixmap(icon.pixmap(QSize(48, 48)))
        icon_label.setFixedSize(QSize(56, 56))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        name_label = QLabel(name)
        name_label.setWordWrap(True)
        name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        name_font = name_label.font()
        name_font.setPointSize(name_font.pointSize() + 3)
        name_font.setBold(True)
        name_label.setFont(name_font)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        heading_layout = QVBoxLayout()
        heading_layout.addWidget(name_label)
        heading_layout.addWidget(subtitle_label)

        top_layout = QHBoxLayout()
        top_layout.addWidget(icon_label)
        top_layout.addLayout(heading_layout, 1)

        self.details_edit = QPlainTextEdit()
        self.details_edit.setReadOnly(True)
        self.details_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details_edit.setPlainText(self.copy_text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_button = buttons.addButton("Copy All", QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(self.copy_all)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top_layout)
        layout.addWidget(self.details_edit, 1)
        layout.addWidget(buttons)

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.copy_text)


class BackupEvidenceDialog(QDialog):
    analyse_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Backup Evidence - saved catalogue evidence")
        self.resize(980, 680)
        self.setMinimumSize(760, 520)

        explanation = QLabel(
            "This compares records already saved in the catalogue. It does not reconnect "
            "or rescan drives or read file contents. Hash-verified means SHA-256 values "
            "recorded during scans match; other strong matches use metadata only."
        )
        explanation.setObjectName("offlineNotice")
        explanation.setWordWrap(True)

        self.tabs = QTabWidget()
        self.overview_tab = QWidget()
        overview_layout = QVBoxLayout(self.overview_tab)
        self.analysis_state_label = QLabel("Not analysed")
        self.analysis_state_label.setObjectName("emptyStateTitle")
        self.analysis_state_label.setWordWrap(True)
        self.analysis_summary_label = QLabel("")
        self.analysis_summary_label.setWordWrap(True)
        self.analysis_summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        legend = QLabel(
            "Green — SHA-256 match, strong metadata match, or complete folder structure\n"
            "Amber — possible file match or partial folder on the best single drive\n"
            "Red — no other catalogue match found\n"
            "Grey — not analysed, outdated, too common, excluded system metadata, "
            "or insufficient scan information"
        )
        legend.setWordWrap(True)
        rules = QLabel(
            "What the labels mean:\n"
            "• Hash verified: the full-file SHA-256 values recorded during scans are "
            "identical. Names, paths, and timestamps may differ.\n"
            "• Strong metadata: when a comparable hash is unavailable, normalized "
            "filename, exact byte size, non-empty exact modified time, and the same "
            "normalized parent path match.\n"
            "• Possible file: normalized filename and exact byte size on another "
            "drive, but path or modified time differs or is unavailable.\n"
            "• Too common: repetitive hashes or weak name-and-size evidence are suppressed "
            "when they span "
            "more than 8 drives, 64 records, or 256 projected file-to-drive links. "
            "Exact path-and-time subgroups use separate bounded limits (32 drives, "
            "512 records, or 4,096 links), so even strong evidence cannot expand "
            "without limit.\n"
            "• Complete folder: the folder name and a content-bearing subtree with at "
            "least two files match one other drive by descendant names, layout, and "
            "SHA-256 wherever hashes are present, and both applied scans have trustworthy "
            "denominators. Mixed hashed/legacy, one-file, renamed, overly common, "
            "error-bearing, or partial subtrees stay amber or grey.\n"
            "• Known OS bookkeeping such as .DS_Store, Thumbs.db, desktop.ini, "
            "$RECYCLE.BIN, and System Volume Information is shown but excluded from "
            "coverage."
        )
        rules.setWordWrap(True)
        scale_note = QLabel(
            "Large catalogues can take several minutes and may use substantial "
            "temporary disk space while indexes are built. The operation is cancellable; "
            "the previous completed analysis remains active until the new one is saved."
        )
        scale_note.setWordWrap(True)
        overview_layout.addWidget(self.analysis_state_label)
        overview_layout.addWidget(self.analysis_summary_label)
        overview_layout.addSpacing(10)
        overview_layout.addWidget(legend)
        overview_layout.addSpacing(10)
        overview_layout.addWidget(rules)
        overview_layout.addSpacing(10)
        overview_layout.addWidget(scale_note)
        overview_layout.addStretch(1)

        self.volume_table = self._report_table(
            [
                "Drive",
                "Indexed files",
                "Hash verified / strong metadata",
                "Possible",
                "Too common",
                "OS metadata",
                "File coverage",
                "Byte coverage",
                "Status",
            ]
        )
        self.mirror_table = self._report_table(
            [
                "Drive A",
                "Drive B",
                "A found on B",
                "B found on A",
                "Structure",
                "Why suggested",
            ]
        )
        self.scan_table = self._report_table(
            [
                "Drive",
                "Latest attempt",
                "Outcome",
                "Last applied",
                "Files",
                "Folders",
                "Incomplete / access",
                "Hash unavailable",
                "Meaning",
            ]
        )

        volume_page = QWidget()
        volume_layout = QVBoxLayout(volume_page)
        volume_note = QLabel(
            "Coverage counts hash-verified or strong metadata matches on a different drive. "
            "Empty successfully scanned drives are N/A."
        )
        volume_note.setWordWrap(True)
        volume_layout.addWidget(volume_note)
        volume_layout.addWidget(self.volume_table, 1)

        mirror_page = QWidget()
        mirror_layout = QVBoxLayout(mirror_page)
        mirror_note = QLabel(
            "These are saved-evidence overlap suggestions only. They do not create or change the "
            "manual mirror relationship in the volume register."
        )
        mirror_note.setWordWrap(True)
        mirror_layout.addWidget(mirror_note)
        mirror_layout.addWidget(self.mirror_table, 1)

        scan_page = QWidget()
        scan_layout = QVBoxLayout(scan_page)
        scan_note = QLabel(
            "The latest scan attempt is reported separately from the last successfully "
            "applied catalogue data. A failed or cancelled attempt does not invalidate a "
            "previous successful backup analysis. Hash-unavailable files remain indexed "
            "and use labelled metadata fallback; inaccessible or unstable paths can mean "
            "catalogue gaps."
        )
        scan_note.setWordWrap(True)
        scan_layout.addWidget(scan_note)
        scan_layout.addWidget(self.scan_table, 1)

        self.tabs.addTab(self.overview_tab, "Overview")
        self.tabs.addTab(volume_page, "Volumes")
        self.tabs.addTab(mirror_page, "Potential drive copies")
        self.tabs.addTab(scan_page, "Scan records")

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        configure_progress_bar(self.progress)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.analyse_button = buttons.addButton(
            "Analyse saved evidence",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.cancel_button = buttons.addButton(
            "Cancel analysis",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.cancel_button.setEnabled(False)
        self.analyse_button.clicked.connect(self.analyse_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.progress)
        layout.addWidget(buttons)

    def _report_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSortingEnabled(True)
        return table

    def set_analysis_running(self, running: bool) -> None:
        self.analyse_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        if running:
            self.analysis_state_label.setText(
                "Analysis running · the last completed results remain visible"
            )
            self.progress.setRange(0, 0)
            self.progress.setFormat("Preparing saved catalogue evidence…")

    def set_analysis_progress(self, completed: int, total: int, message: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(max(completed, 0), total))
            self.progress.setFormat(f"{message} — %p%")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(message)

    def set_records(
        self,
        state: Any,
        summaries: list[Any],
        mirrors: list[Any],
        scans: list[Any],
        volume_references: dict[int, str],
    ) -> None:
        status = enum_value(object_value(state, "status"))
        stale = bool(object_value(state, "is_stale", False))
        analysed_at = object_value(state, "analysed_at")
        stale_reason = str(object_value(state, "stale_reason", "") or "")
        if stale:
            state_text = "Analysis outdated"
        elif status in {"complete", "completed", "current", "ready"}:
            state_text = "Analysis current"
        elif status in {"running", "analysing", "analyzing"}:
            state_text = "Analysis running"
        else:
            state_text = "Not analysed"
        if analysed_at:
            state_text += f" · {display_db_time(str(analysed_at))}"
        self.analysis_state_label.setText(state_text)

        total_files = sum(
            int(first_object_value(row, "total_files", "indexed_files", "file_count", default=0) or 0)
            for row in summaries
        )
        strong_files = sum(
            int(
                first_object_value(
                    row,
                    "likely_files",
                    "strong_files",
                    "matched_files",
                    "protected_files",
                    default=0,
                )
                or 0
            )
            for row in summaries
        )
        possible_files = sum(
            int(object_value(row, "possible_files", 0) or 0) for row in summaries
        )
        ambiguous_files = sum(
            int(object_value(row, "ambiguous_files", 0) or 0) for row in summaries
        )
        excluded_files = sum(
            int(object_value(row, "excluded_files", 0) or 0) for row in summaries
        )
        if not analysed_at and not summaries:
            indexed_records = sum(
                int(object_value(scan, "indexed_file_count", 0) or 0)
                for scan in scans
            )
            drive_word = "drive" if len(scans) == 1 else "drives"
            summary_parts = [
                "No backup evidence has been analysed yet. Choose Analyse saved evidence "
                f"to compare {indexed_records:,} indexed file records across "
                f"{len(scans):,} catalogue {drive_word}."
            ]
        else:
            summary_parts = [
                f"The last completed analysis found hash-verified or strong metadata "
                f"evidence on another "
                f"drive for {strong_files:,} of {total_files:,} indexed file records.",
                f"Possible: {possible_files:,}. Too common to use: "
                f"{ambiguous_files:,}. Excluded OS metadata: {excluded_files:,}.",
            ]
        if stale_reason:
            summary_parts.append(stale_reason)
        summary_parts.append(BACKUP_METADATA_DISCLAIMER)
        self.analysis_summary_label.setText("\n\n".join(summary_parts))
        self.analyse_button.setText(
            "Update saved evidence" if analysed_at else "Analyse saved evidence"
        )

        self._populate_volumes(summaries, volume_references, stale)
        self._populate_mirrors(mirrors, volume_references, stale)
        self._populate_scans(scans, volume_references)

    def _set_table_rows(self, table: QTableWidget, rows: list[list[str]]) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row_index, column_index, item)
        table.resizeColumnsToContents()
        table.setSortingEnabled(True)

    def _populate_volumes(
        self,
        summaries: list[Any],
        volume_references: dict[int, str],
        analysis_stale: bool = False,
    ) -> None:
        rows = []
        for summary in summaries:
            volume_id = int(object_value(summary, "volume_id", 0) or 0)
            total_files = int(first_object_value(summary, "total_files", "indexed_files", "file_count", default=0) or 0)
            strong_files = int(
                first_object_value(
                    summary,
                    "likely_files",
                    "strong_files",
                    "matched_files",
                    "protected_files",
                    default=0,
                )
                or 0
            )
            possible_files = int(object_value(summary, "possible_files", 0) or 0)
            ambiguous_files = int(object_value(summary, "ambiguous_files", 0) or 0)
            excluded_files = int(object_value(summary, "excluded_files", 0) or 0)
            summary_status = enum_value(object_value(summary, "status", ""))
            files_percent = first_object_value(
                summary,
                "likely_files_percent",
                "strong_files_percent",
                "file_coverage_percent",
                "coverage_files_percent",
            )
            bytes_percent = first_object_value(
                summary,
                "likely_bytes_percent",
                "strong_bytes_percent",
                "byte_coverage_percent",
                "coverage_bytes_percent",
            )
            stale = analysis_stale or bool(object_value(summary, "is_stale", False))
            health = enum_value(
                first_object_value(summary, "health_status", "scan_health", default="")
            )
            if stale:
                status_text = "Outdated"
                file_text = "Outdated"
                byte_text = "Outdated"
            elif total_files == 0 and health in {"empty", "healthy_empty", "completed_empty"}:
                status_text = "N/A · empty"
                file_text = "N/A"
                byte_text = "N/A"
            elif total_files == 0 and health in {
                "unknown",
                "check_scan",
                "completed_with_errors",
                "scan_errors",
                "incomplete",
            }:
                status_text = "Check scan"
                file_text = "Unknown"
                byte_text = "Unknown"
            elif total_files == 0 and health in {"not_scanned", "no_applied_scan"}:
                status_text = "Not scanned"
                file_text = "Unknown"
                byte_text = "Unknown"
            elif summary_status == "excluded":
                status_text = "N/A · system metadata"
                file_text = "N/A"
                byte_text = "N/A"
            elif total_files > 0 and not bool(
                object_value(summary, "coverage_eligible", True)
            ):
                status_text = "Not scanned" if health in {"not_scanned", "no_applied_scan"} else "Check scan"
                file_text = "Unknown"
                byte_text = "Unknown"
            elif files_percent is None:
                status_text = "Not analysed"
                file_text = "Not analysed"
                byte_text = "Not analysed"
            else:
                status_text = "Current"
                file_text = f"{float(files_percent):.0f}%"
                byte_text = f"{float(bytes_percent):.0f}%" if bytes_percent is not None else "N/A"
            rows.append(
                [
                    volume_references.get(volume_id, f"Volume {volume_id}"),
                    f"{total_files:,}",
                    f"{strong_files:,}",
                    f"{possible_files:,}",
                    f"{ambiguous_files:,}",
                    f"{excluded_files:,}",
                    file_text,
                    byte_text,
                    status_text,
                ]
            )
        self._set_table_rows(self.volume_table, rows)

    def _populate_mirrors(
        self,
        mirrors: list[Any],
        volume_references: dict[int, str],
        analysis_stale: bool = False,
    ) -> None:
        rows = []
        for candidate in mirrors:
            first_id = int(first_object_value(candidate, "volume_a_id", "first_volume_id", "source_volume_id", default=0) or 0)
            second_id = int(first_object_value(candidate, "volume_b_id", "second_volume_id", "target_volume_id", default=0) or 0)
            a_on_b = first_object_value(
                candidate,
                "a_on_b_percent",
                "first_on_second_percent",
                "source_coverage_percent",
            )
            b_on_a = first_object_value(
                candidate,
                "b_on_a_percent",
                "second_on_first_percent",
                "target_coverage_percent",
            )
            complete = bool(
                first_object_value(
                    candidate,
                    "complete_structure",
                    "complete",
                    "is_complete",
                    "exact",
                    default=False,
                )
            )
            reason = str(first_object_value(candidate, "evidence_text", "reason", default="Metadata overlap") or "Metadata overlap")
            if (
                bool(object_value(candidate, "manual_mirror_link", False))
                and "manual mirror relationship" not in reason.casefold()
            ):
                reason = f"Existing manual mirror relationship. {reason}"
            if analysis_stale:
                reason = f"OUTDATED — {reason}"
            rows.append(
                [
                    volume_references.get(first_id, f"Volume {first_id}"),
                    volume_references.get(second_id, f"Volume {second_id}"),
                    f"{float(a_on_b):.0f}%" if a_on_b is not None else "N/A",
                    f"{float(b_on_a):.0f}%" if b_on_a is not None else "N/A",
                    "Outdated"
                    if analysis_stale
                    else ("Complete structural match" if complete else "Partial overlap"),
                    reason,
                ]
            )
        self._set_table_rows(self.mirror_table, rows)

    def _populate_scans(
        self,
        scans: list[Any],
        volume_references: dict[int, str],
    ) -> None:
        rows = []
        for scan in scans:
            volume_id = int(object_value(scan, "volume_id", 0) or 0)
            status = enum_value(first_object_value(scan, "latest_attempt_status", "status", default="not_scanned"))
            files = int(
                first_object_value(
                    scan,
                    "latest_attempt_files",
                    "files_seen",
                    "indexed_files",
                    default=0,
                )
                or 0
            )
            folders = int(
                first_object_value(
                    scan,
                    "latest_attempt_folders",
                    "folders_seen",
                    "indexed_folders",
                    default=0,
                )
                or 0
            )
            errors = int(
                first_object_value(
                    scan,
                    "latest_attempt_errors",
                    "errors_count",
                    "access_errors",
                    default=0,
                )
                or 0
            )
            ignored_errors = int(
                object_value(scan, "latest_attempt_ignored_errors", 0) or 0
            )
            hash_errors = int(
                object_value(scan, "latest_attempt_hash_errors", 0) or 0
            )
            access_errors = max(0, errors - hash_errors)
            actionable_access_errors = max(0, access_errors - ignored_errors)
            health = enum_value(object_value(scan, "health_status", ""))
            attempted_at = first_object_value(scan, "started_at", "latest_attempt_at", "last_scan_at")
            applied_at = first_object_value(
                scan,
                "last_applied_at",
                "applied_scan_at",
                "catalogue_scan_at",
            )
            applied = bool(first_object_value(scan, "applied", "is_applied", default=status == "completed"))
            if (
                status == "completed"
                and access_errors
                and ignored_errors >= access_errors
            ):
                outcome = "Completed with system warning" + (
                    "s" if access_errors != 1 else ""
                )
                if health in {"empty", "healthy_empty", "completed_empty"}:
                    outcome += " · empty"
                    meaning = (
                        "Only protected drive metadata was skipped; the empty drive remains "
                        "confirmed and coverage is N/A"
                    )
                else:
                    meaning = (
                        "Only protected drive metadata was skipped; copy coverage remains usable"
                    )
                if hash_errors:
                    outcome += " · hash gaps"
                    meaning += (
                        f"; {hash_errors:,} file"
                        + ("s" if hash_errors != 1 else "")
                        + " will use metadata fallback"
                    )
            elif status == "completed" and health in {"empty", "healthy_empty", "completed_empty"}:
                outcome = "Completed · empty"
                meaning = "Healthy empty catalogue; coverage N/A"
            elif status == "completed" and actionable_access_errors:
                outcome = "Completed with incomplete areas"
                meaning = "Check scan; some paths were inaccessible or unstable"
                if hash_errors:
                    meaning += (
                        f"; {hash_errors:,} additional file"
                        + ("s" if hash_errors != 1 else "")
                        + " use metadata fallback"
                    )
            elif status == "completed" and hash_errors:
                outcome = "Completed · hash gaps"
                meaning = (
                    f"{hash_errors:,} file"
                    + ("s" if hash_errors != 1 else "")
                    + " could not be hashed; those files use metadata fallback"
                )
            elif status == "completed" and files == 0:
                outcome = "Completed · empty"
                meaning = "Healthy empty catalogue; coverage N/A"
            elif status == "completed":
                outcome = "Completed"
                meaning = "Latest attempt was applied"
            elif status in {"failed", "cancelled", "discarded"}:
                outcome = status.title()
                meaning = "Not applied; prior catalogue evidence remains in effect"
            else:
                outcome = "Not scanned" if status in {"", "not_scanned", "none"} else status.title()
                meaning = "No applied scan data" if not applied else "See scan log"
            rows.append(
                [
                    volume_references.get(volume_id, f"Volume {volume_id}"),
                    display_db_time(str(attempted_at)) if attempted_at else "Never",
                    outcome,
                    display_db_time(str(applied_at)) if applied_at else "Never",
                    f"{files:,}",
                    f"{folders:,}",
                    f"{access_errors:,}",
                    f"{hash_errors:,}",
                    meaning,
                ]
            )
        self._set_table_rows(self.scan_table, rows)


class PreferencesDialog(QDialog):
    appearance_changed = Signal(str, str, str)

    def __init__(
        self,
        include_paths: bool,
        theme_style: str,
        color_mode: str,
        accent_color: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preferences - {APP_NAME}")
        self.setMinimumWidth(440)

        search_group = QGroupBox("Search")
        self.include_paths_check = QCheckBox(
            "Include file and folder paths in searches"
        )
        self.include_paths_check.setChecked(include_paths)
        explanation = QLabel(
            "When enabled, a folder-name match can also return every file and "
            "subfolder beneath it. Leave this disabled for concise name-based results."
        )
        explanation.setWordWrap(True)

        search_layout = QVBoxLayout(search_group)
        search_layout.addWidget(self.include_paths_check)
        search_layout.addWidget(explanation)

        appearance_group = QGroupBox("Appearance")
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Default", ADOBE_THEME)
        self.theme_combo.addItem("Fusion (Qt)", FUSION_THEME)
        normalized_theme_style = normalize_theme_style(theme_style)
        theme_index = self.theme_combo.findData(normalized_theme_style)
        self.theme_combo.setCurrentIndex(max(0, theme_index))
        self._last_theme_style = normalized_theme_style

        self.color_mode_combo = QComboBox()
        self.color_mode_combo.addItem("Dark", DARK_MODE)
        self.color_mode_combo.addItem("Light", LIGHT_MODE)
        mode_index = self.color_mode_combo.findData(normalize_color_mode(color_mode))
        self.color_mode_combo.setCurrentIndex(max(0, mode_index))

        self._accent_color = normalize_accent_color(accent_color)
        self.accent_button = QPushButton()
        self.accent_button.setToolTip("Choose a custom accent color")
        self.accent_button.clicked.connect(self.choose_accent_color)
        self.update_accent_button()

        appearance_form = QFormLayout(appearance_group)
        appearance_form.addRow("Theme:", self.theme_combo)
        appearance_form.addRow("Mode:", self.color_mode_combo)
        appearance_form.addRow("Accent color:", self.accent_button)

        self.reset_theme_button = QPushButton("Reset Theme")
        self.reset_theme_button.setObjectName("resetThemeButton")
        self.reset_theme_button.setToolTip(
            "Restore the default Adobe dark theme and its blue accent"
        )
        self.reset_theme_button.clicked.connect(self.reset_theme)
        appearance_form.addRow("", self.reset_theme_button)

        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        self.color_mode_combo.currentIndexChanged.connect(self.emit_appearance_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(appearance_group)
        layout.addWidget(search_group)
        layout.addWidget(buttons)

    def choose_accent_color(self) -> None:
        starting_color = self._accent_color
        picker = QColorDialog(QColor(starting_color), self)
        picker.setWindowTitle("Select Accent Color")
        picker.setOption(
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
            True,
        )
        picker.currentColorChanged.connect(self.preview_accent_color)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.preview_accent_color(picker.currentColor())
            return

        self._accent_color = starting_color
        self.update_accent_button()
        self.emit_appearance_changed()

    def preview_accent_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        self._accent_color = normalize_accent_color(color)
        self.update_accent_button()
        self.emit_appearance_changed()

    def reset_theme(self) -> None:
        theme_was_blocked = self.theme_combo.blockSignals(True)
        mode_was_blocked = self.color_mode_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(DEFAULT_THEME_STYLE))
        self.color_mode_combo.setCurrentIndex(
            self.color_mode_combo.findData(DEFAULT_COLOR_MODE)
        )
        self.theme_combo.blockSignals(theme_was_blocked)
        self.color_mode_combo.blockSignals(mode_was_blocked)
        self._accent_color = DEFAULT_ACCENT_COLOR
        self._last_theme_style = DEFAULT_THEME_STYLE
        self.update_accent_button()
        self.emit_appearance_changed()

    def on_theme_changed(self, *_args: object) -> None:
        theme_style = self.theme_style()
        if theme_style != self._last_theme_style:
            self._last_theme_style = theme_style
            self._accent_color = theme_default_accent(theme_style)
            self.update_accent_button()
        self.emit_appearance_changed()

    def emit_appearance_changed(self, *_args: object) -> None:
        self.appearance_changed.emit(
            self.theme_style(),
            self.color_mode(),
            self.accent_color(),
        )

    def update_accent_button(self) -> None:
        text_color = contrasting_text_color(self._accent_color)
        border_color = QColor(self._accent_color).darker(135).name()
        self.accent_button.setText(self._accent_color.upper())
        self.accent_button.setStyleSheet(
            "QPushButton {"
            f"background-color: {self._accent_color}; color: {text_color}; "
            f"border: 1px solid {border_color}; font-weight: 600;"
            "}"
        )

    def include_paths(self) -> bool:
        return self.include_paths_check.isChecked()

    def theme_style(self) -> str:
        return normalize_theme_style(self.theme_combo.currentData())

    def color_mode(self) -> str:
        return normalize_color_mode(self.color_mode_combo.currentData())

    def accent_color(self) -> str:
        return self._accent_color


class HelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Help - {APP_NAME}")
        self.resize(700, 560)
        self.setStyleSheet("""
            QDialog { background: palette(window); }
            QLabel#subtitle { font-size: 13px; }
            QTextBrowser {
                background: palette(base); border: 1px solid palette(mid);
                border-radius: 8px; padding: 8px;
            }
            QPushButton { min-width: 88px; padding: 6px 14px; }
        """)

        title = QLabel(f"<b style='font-size:24px'>{APP_NAME}</b>")
        subtitle = QLabel("Offline catalogues for removable drives and folders")
        subtitle.setObjectName("subtitle")

        content = QTextBrowser()
        content.setOpenExternalLinks(True)
        content.setHtml(self._help_html())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setDefault(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(6)
        layout.addWidget(content, 1)
        layout.addWidget(buttons)

    def _help_html(self) -> str:
        sections = [
            ("1. Create a catalogue",
             "Choose <b>File → New Catalogue</b>, select a location, and save the "
             "<code>.jvvv</code> file. It is a portable SQLite catalogue."),
            ("2. Add a volume",
             "Choose <b>New Volume</b>, select a connected drive or folder, "
             "and it scans automatically. Scans read every file to record a full "
             "SHA-256 content hash, so large drives take longer. "
             "Scan it again later to review and apply catalogue changes."),
            ("3. Browse offline",
             "Select a saved volume to explore its folder tree, even when the "
             "original drive is disconnected."),
            ("4. Search",
             "Find files and folders by name or extension across the stored catalogue. "
             "Path matching can be enabled under <b>Settings &gt; Preferences</b>."),
            ("5. Review copy evidence",
             "Choose <b>Catalogue &gt; Backup Evidence</b> to compare saved evidence. "
             "The analysis does not reconnect, rescan, or reread file contents. It first "
             "uses SHA-256 values recorded by scans, then clearly labels metadata-only "
             "fallbacks when a comparable hash is unavailable."),
            ("6. Back up or restore a catalogue",
             "Choose <b>File &gt; Create Catalogue Backup</b> for a compact, lossless "
             "<code>.zip</code>. Choose <b>File &gt; Restore Catalogue from Backup</b> "
             "to validate it, rebuild omitted indexes and other derived data, and save "
             "the result as a normal <code>.jvvv</code> file."),
        ]
        section_html = "".join(
            f"<h2>{title}</h2><p>{body}</p>" for title, body in sections
        )
        return f"""
        <style>
            body {{ font-size: 14px; }}
            h1 {{ margin-bottom: 4px; }}
            h2 {{ margin-top: 20px; font-size: 17px; }}
            p {{ margin: 5px 0 10px; }}
            a {{ text-decoration: none; }}
        </style>
        <h1>Quick start</h1>
        <p><b>New Catalogue → New Volume → Browse or Search</b></p>
        {section_html}
        <hr>
        <p><b>About</b><br>{APP_NAME} is GPLv3 open-source software by Joemt.<br>
        <a href="https://github.com/joedotmt/Joemt-Archive-View">Source code</a>
        &nbsp;·&nbsp; <a href="https://joe.mt">joe.mt</a></p>
        """


class ScanWorker(QObject):
    progress = Signal(int, int, str)
    stats_progress = Signal(int, int, str, int, int)
    review_requested = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: Path, volume_id: int) -> None:
        super().__init__()
        self.db_path = db_path
        self.volume_id = volume_id
        self.cancel_requested = False
        self._review_event = Event()
        self._apply_reviewed_changes = False
        self._windows_thread_handle: Any | None = None

    def _start_cancellable_io(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenThread.restype = wintypes.HANDLE
            self._windows_thread_handle = kernel32.OpenThread(
                0x0001,
                False,
                kernel32.GetCurrentThreadId(),
            )
        except Exception:
            self._windows_thread_handle = None

    def _stop_cancellable_io(self) -> None:
        handle = self._windows_thread_handle
        self._windows_thread_handle = None
        if os.name != "nt" or not handle:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
        except Exception:
            pass

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        try:
            self._start_cancellable_io()
            db = Database(self.db_path)
            scanner = VolumeScanner(
                db,
                progress_callback=lambda files, folders, path: self.progress.emit(files, folders, path),
                stats_progress_callback=lambda files, folders, message, done, total: self.stats_progress.emit(
                    files,
                    folders,
                    message,
                    done,
                    total,
                ),
                cancel_callback=lambda: self.cancel_requested,
                preview_callback=self.request_review,
            )
            result = scanner.scan(self.volume_id)
            self.finished.emit(
                {
                    "status": result.status,
                    "files_seen": result.files_seen,
                    "folders_seen": result.folders_seen,
                    "errors_count": result.errors_count,
                    "message": result.message or "",
                    "changes": result.changes.as_dict() if result.changes is not None else {},
                    "files_hashed": result.files_hashed,
                    "bytes_hashed": result.bytes_hashed,
                    "hash_errors": result.hash_errors,
                    "media_files": result.media_files,
                    "media_metadata_collected": result.media_metadata_collected,
                }
            )
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            if db is not None:
                db.close()
            self._stop_cancellable_io()

    def request_review(self, changes) -> bool:
        self._apply_reviewed_changes = False
        self._review_event.clear()
        self.review_requested.emit(changes.as_dict())
        self._review_event.wait()
        return self._apply_reviewed_changes and not self.cancel_requested

    def resolve_review(self, apply_changes: bool) -> None:
        self._apply_reviewed_changes = bool(apply_changes)
        self._review_event.set()

    @Slot()
    def cancel(self) -> None:
        self.cancel_requested = True
        self._review_event.set()
        handle = self._windows_thread_handle
        if os.name != "nt" or not handle:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
            kernel32.CancelSynchronousIo.restype = wintypes.BOOL
            kernel32.CancelSynchronousIo(handle)
        except Exception:
            pass


class DeleteVolumeWorker(QObject):
    progress = Signal(str)
    finished = Signal(int)
    failed = Signal(str)

    def __init__(self, db_path: Path, volume_id: int) -> None:
        super().__init__()
        self.db_path = db_path
        self.volume_id = volume_id

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        error_details: str | None = None
        try:
            db = Database(self.db_path)
            self.progress.emit("Deleting indexed records...")
            db.delete_volume(self.volume_id)
        except Exception:
            error_details = traceback.format_exc()
        finally:
            if db is not None:
                db.close()

        if error_details is None:
            self.finished.emit(self.volume_id)
        else:
            self.failed.emit(error_details)


class CatalogueBackupWorker(QObject):
    progress = Signal(object, object, str)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(object, str)

    def __init__(self, source_path: Path, backup_path: Path, *, overwrite: bool) -> None:
        super().__init__()
        self.source_path = source_path
        self.backup_path = backup_path
        self.overwrite = overwrite
        self.cancel_requested = False

    @Slot()
    def run(self) -> None:
        result: BackupResult | None = None
        error: Exception | None = None
        details = ""
        try:
            result = create_catalogue_backup(
                self.source_path,
                self.backup_path,
                overwrite=self.overwrite,
                progress_callback=self._report_progress,
                cancel_callback=lambda: self.cancel_requested,
            )
        except BackupCancelled:
            self.cancel_requested = True
        except Exception as exc:
            error = exc
            details = traceback.format_exc()

        if self.cancel_requested:
            self.cancelled.emit()
        elif error is not None:
            self.failed.emit(error, details)
        elif result is not None:
            self.finished.emit(result)

    def _report_progress(self, progress: BackupProgress) -> None:
        self.progress.emit(progress.completed, progress.total, progress.message)

    def cancel(self) -> None:
        self.cancel_requested = True


class CatalogueRestoreWorker(QObject):
    progress = Signal(object, object, str)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(object, str)

    def __init__(self, backup_path: Path, catalogue_path: Path, *, overwrite: bool) -> None:
        super().__init__()
        self.backup_path = backup_path
        self.catalogue_path = catalogue_path
        self.overwrite = overwrite
        self.cancel_requested = False

    @Slot()
    def run(self) -> None:
        result: RestoreResult | None = None
        error: Exception | None = None
        details = ""
        try:
            result = restore_catalogue_backup(
                self.backup_path,
                self.catalogue_path,
                overwrite=self.overwrite,
                progress_callback=self._report_progress,
                cancel_callback=lambda: self.cancel_requested,
            )
        except BackupCancelled:
            self.cancel_requested = True
        except Exception as exc:
            error = exc
            details = traceback.format_exc()

        if self.cancel_requested:
            self.cancelled.emit()
        elif error is not None:
            self.failed.emit(error, details)
        elif result is not None:
            self.finished.emit(result)

    def _report_progress(self, progress: BackupProgress) -> None:
        self.progress.emit(progress.completed, progress.total, progress.message)

    def cancel(self) -> None:
        self.cancel_requested = True


class CatalogueInfoWorker(QObject):
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = db_path
        self.cancel_requested = False
        self._active_db: Database | None = None

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        info = None
        error_details: str | None = None
        try:
            db = Database(
                self.db_path,
                initialize=False,
                create=False,
                read_only=True,
            )
            self._active_db = db
            db.connection.set_progress_handler(
                lambda: 1 if self.cancel_requested else 0,
                1000,
            )
            if not self.cancel_requested:
                info = db.get_catalogue_info()
        except Exception:
            if not self.cancel_requested:
                error_details = traceback.format_exc()
        finally:
            self._active_db = None
            if db is not None:
                db.connection.set_progress_handler(None, 0)
                db.close()

        if self.cancel_requested:
            self.cancelled.emit()
        elif error_details is not None:
            self.failed.emit(error_details)
        else:
            self.finished.emit(info)

    def cancel(self) -> None:
        self.cancel_requested = True
        db = self._active_db
        if db is not None:
            try:
                db.connection.interrupt()
            except Exception:
                pass


class BackupAnalysisWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = db_path
        self.cancel_requested = False
        self._active_db: Database | None = None

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        summary: Any = None
        error_details: str | None = None
        try:
            if BackupAnalysisEngine is None:
                raise RuntimeError("Backup analysis support is unavailable in this build.")
            db = Database(
                self.db_path,
                initialize=False,
                create=False,
                read_only=False,
            )
            self._active_db = db
            db.connection.set_progress_handler(
                lambda: 1 if self.cancel_requested else 0,
                1000,
            )
            engine = BackupAnalysisEngine(db)

            def report(value: Any) -> None:
                self.progress.emit(
                    int(object_value(value, "completed", 0) or 0),
                    int(object_value(value, "total", 0) or 0),
                    str(
                        object_value(value, "message", "")
                        or object_value(value, "phase", "Analysing saved evidence")
                    ),
                )

            summary = engine.analyse(
                progress_callback=report,
                cancel_callback=lambda: self.cancel_requested,
            )
        except Exception:
            if not self.cancel_requested:
                error_details = traceback.format_exc()
        finally:
            if db is not None:
                try:
                    db.connection.set_progress_handler(None, 0)
                except Exception:
                    pass
                db.close()
            self._active_db = None

        summary_status = enum_value(object_value(summary, "status"))
        cancelled = summary_status == "cancelled" or (
            summary is None and self.cancel_requested
        )
        if cancelled:
            self.cancelled.emit()
        elif error_details is not None:
            self.failed.emit(error_details)
        else:
            self.finished.emit(summary)

    def cancel(self) -> None:
        self.cancel_requested = True
        db = self._active_db
        if db is not None:
            try:
                db.connection.interrupt()
            except Exception:
                pass


class SearchWorker(QObject):
    batch_ready = Signal(int, list)
    finished = Signal(int, int)
    cancelled = Signal(int)
    failed = Signal(int, str)

    def __init__(
        self,
        db_path: Path,
        query: str,
        request_id: int,
        connected_volume_snapshots: list[VolumeSnapshot] | None = None,
        *,
        include_paths: bool = False,
        backup_filter_key: str = "all",
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.query = query
        self.request_id = request_id
        self.connected_volume_snapshots = connected_volume_snapshots
        self.include_paths = include_paths
        self.backup_filter_key = backup_filter_key
        self.cancel_requested = False

    def _build_batch(
        self,
        rows: list[Any],
        engine: Any,
        resolver: ConnectedVolumeResolver,
        connected_by_volume: dict[int, bool],
        volume_references: dict[int, str],
    ) -> list[SearchResultItem]:
        statuses: dict[tuple[str, int], Any] = {}
        if engine is not None:
            for item_type in ("folder", "file"):
                ids = [int(row["item_id"]) for row in rows if row["item_type"] == item_type]
                if not ids:
                    continue
                try:
                    for item_id, status in engine.item_statuses(item_type, ids).items():
                        statuses[(item_type, int(item_id))] = status
                except Exception:
                    # Search remains usable if an older catalogue has not had its
                    # auxiliary analysis schema initialized yet.
                    pass

        items: list[SearchResultItem] = []
        for result in rows:
            volume_id = int(result["volume_id"])
            connected = connected_by_volume.get(volume_id)
            if connected is None:
                connected = resolver.resolve(result) is not None
                connected_by_volume[volume_id] = connected
            item_type = str(result["item_type"])
            item_id = int(result["item_id"])
            backup = item_backup_display(
                statuses.get((item_type, item_id)),
                volume_references,
                item_type=item_type,
            )
            if not backup_filter_matches(backup, self.backup_filter_key):
                continue
            items.append(
                SearchResultItem(
                    item_type=item_type,
                    item_id=item_id,
                    name=result["name"],
                    volume_id=volume_id,
                    drive_id=result["drive_id"],
                    volume_name=result["volume_name"],
                    relative_path=result["relative_path"],
                    size_bytes=result["size_bytes"],
                    modified_at=result["modified_at"],
                    missing=bool(result["missing"]),
                    source_path=result["source_path"],
                    connected=connected,
                    backup=backup,
                )
            )
        return items

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        result_count = 0
        error_details: str | None = None
        try:
            db = Database(
                self.db_path,
                initialize=False,
                create=False,
                read_only=True,
            )
            db.connection.set_progress_handler(
                lambda: 1 if self.cancel_requested else 0,
                1000,
            )
            resolver = ConnectedVolumeResolver(
                self.connected_volume_snapshots,
                check_source_path=self.connected_volume_snapshots is None,
            )
            try:
                volume_references = {
                    int(volume["id"]): volume_reference(volume["drive_id"], volume["name"])
                    for volume in db.list_volumes()
                }
            except (AttributeError, TypeError):
                volume_references = {}
            engine = BackupAnalysisEngine(db) if BackupAnalysisEngine is not None else None
            connected_by_volume: dict[int, bool] = {}
            raw_batch: list[Any] = []
            for result in db.iter_search(
                self.query,
                include_paths=self.include_paths,
            ):
                if self.cancel_requested:
                    break
                raw_batch.append(result)
                if len(raw_batch) >= SEARCH_RESULT_BATCH_SIZE:
                    batch = self._build_batch(
                        raw_batch,
                        engine,
                        resolver,
                        connected_by_volume,
                        volume_references,
                    )
                    if batch:
                        self.batch_ready.emit(self.request_id, batch)
                        result_count += len(batch)
                    raw_batch = []
            if raw_batch and not self.cancel_requested:
                batch = self._build_batch(
                    raw_batch,
                    engine,
                    resolver,
                    connected_by_volume,
                    volume_references,
                )
                if batch:
                    self.batch_ready.emit(self.request_id, batch)
                    result_count += len(batch)
        except Exception:
            if not self.cancel_requested:
                error_details = traceback.format_exc()
        finally:
            if db is not None:
                db.connection.set_progress_handler(None, 0)
                db.close()

        if self.cancel_requested:
            self.cancelled.emit(self.request_id)
        elif error_details is not None:
            self.failed.emit(self.request_id, error_details)
        else:
            self.finished.emit(self.request_id, result_count)

    def cancel(self) -> None:
        self.cancel_requested = True


class _CatalogueOpenCancelled(Exception):
    pass


class CatalogueOpenWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object, list, object, object)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.cancel_requested = False
        self._active_db: Database | None = None
        self._windows_thread_handle: Any | None = None

    def _start_cancellable_io(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenThread.restype = wintypes.HANDLE
            # CancelSynchronousIo requires a handle with THREAD_TERMINATE access.
            self._windows_thread_handle = kernel32.OpenThread(
                0x0001,
                False,
                kernel32.GetCurrentThreadId(),
            )
        except Exception:
            self._windows_thread_handle = None

    def _stop_cancellable_io(self) -> None:
        handle = self._windows_thread_handle
        self._windows_thread_handle = None
        if os.name != "nt" or not handle:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
        except Exception:
            pass

    def _check_cancelled(self) -> None:
        if self.cancel_requested:
            raise _CatalogueOpenCancelled

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        lock: QLockFile | None = None
        result: tuple[Database, list[VolumeItem], list[VolumeSnapshot], QLockFile] | None = None
        error: Exception | None = None
        was_cancelled = False
        self._start_cancellable_io()
        try:
            self._check_cancelled()
            self.progress.emit(0, 0, "Acquiring catalogue lock...")
            lock = acquire_catalogue_lock(self.path)
            self._check_cancelled()
            self.progress.emit(0, 0, "Opening and checking catalogue...")
            # This connection is created in the worker and, once this method is
            # finished with it, becomes the main-window connection.
            db = open_catalogue(self.path, check_same_thread=False)
            self._active_db = db
            self._check_cancelled()
            volumes = db.list_volumes()
            self._check_cancelled()
            snapshots = list_connected_volume_snapshots()
            self._check_cancelled()
            resolver = ConnectedVolumeResolver(snapshots)
            total = len(volumes)
            self.progress.emit(0, max(total, 1), "Loading volumes...")
            items: list[VolumeItem] = []
            for index, volume in enumerate(volumes, start=1):
                self._check_cancelled()
                items.append(
                    volume_item_from_record(volume, resolver.resolve(volume) is not None)
                )
                self.progress.emit(
                    index,
                    max(total, 1),
                    f"Loading volumes... {index}/{total}",
                )
            if not volumes:
                self.progress.emit(1, 1, "Catalogue ready")
            self._check_cancelled()
            result = (db, items, snapshots, lock)
            db = None
            lock = None
        except _CatalogueOpenCancelled:
            was_cancelled = True
        except Exception as exc:
            if self.cancel_requested:
                was_cancelled = True
            else:
                error = exc
        finally:
            self._active_db = None
            if db is not None:
                db.close()
            if lock is not None:
                lock.unlock()
            self._stop_cancellable_io()

        if result is not None:
            self.finished.emit(*result)
        elif was_cancelled:
            self.cancelled.emit()
        elif error is not None:
            self.failed.emit(error)

    def cancel(self) -> None:
        self.cancel_requested = True
        db = self._active_db
        if db is not None:
            try:
                db.connection.interrupt()
            except Exception:
                pass

        handle = self._windows_thread_handle
        if os.name != "nt" or not handle:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
            kernel32.CancelSynchronousIo.restype = wintypes.BOOL
            kernel32.CancelSynchronousIo(handle)
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("JVVV", APP_NAME)
        self.search_include_paths = self.settings.value(
            SEARCH_INCLUDE_PATHS_SETTING,
            False,
            type=bool,
        )
        self.theme_style = normalize_theme_style(
            self.settings.value(THEME_STYLE_SETTING, DEFAULT_THEME_STYLE)
        )
        self.color_mode = normalize_color_mode(
            self.settings.value(COLOR_MODE_SETTING, DEFAULT_COLOR_MODE)
        )
        self.accent_color = normalize_accent_color(
            self.settings.value(ACCENT_COLOR_SETTING, DEFAULT_ACCENT_COLOR)
        )
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_application_theme(
                application,
                theme_style=self.theme_style,
                color_mode=self.color_mode,
                accent_color=self.accent_color,
            )
        self.db: Database | None = None
        self.catalogue_path: Path | None = None
        self.catalogue_lock: QLockFile | None = None
        self.current_volume_id: int | None = None
        self.current_folder_id: int | None = None
        self.scan_thread: QThread | None = None
        self.scan_worker: ScanWorker | None = None
        self.scan_cancel_requested = False
        self.post_scan_edit_volume_id: int | None = None
        self.delete_thread: QThread | None = None
        self.delete_worker: DeleteVolumeWorker | None = None
        self.catalogue_info_thread: QThread | None = None
        self.catalogue_info_worker: CatalogueInfoWorker | None = None
        self.catalogue_archive_thread: QThread | None = None
        self.catalogue_archive_worker: CatalogueBackupWorker | CatalogueRestoreWorker | None = None
        self.catalogue_archive_operation = ""
        self.pending_restored_catalogue_path: Path | None = None
        self.backup_analysis_thread: QThread | None = None
        self.backup_analysis_worker: BackupAnalysisWorker | None = None
        self.backup_evidence_dialog: BackupEvidenceDialog | None = None
        self.search_thread: QThread | None = None
        self.search_worker: SearchWorker | None = None
        self.catalogue_probe_process: QProcess | None = None
        self.catalogue_probe_timed_out = False
        self.catalogue_open_thread: QThread | None = None
        self.catalogue_open_worker: CatalogueOpenWorker | None = None
        self.catalogue_open_path: Path | None = None
        self.catalogue_open_status_message = "Catalogue opened."
        self.catalogue_open_cancel_requested = False
        self.pending_search_request: tuple[int, Path, str, bool] | None = None
        self.search_request_id = 0
        self.browser_shortcuts: list[QShortcut] = []
        self.browser_icons = CatalogueIconProvider()
        self.backup_status_icons = BackupStatusIconProvider()
        self.volume_model = VolumeTableModel(self, self.backup_status_icons)
        self.browser_model = BrowserTableModel(
            self.browser_icons,
            self,
            self.backup_status_icons,
        )
        self.search_model = SearchResultsTableModel(
            self.browser_icons,
            self,
            self.backup_status_icons,
        )
        self.current_directory_items: list[BrowserItem] = []
        self.backup_engine: Any = None
        self.backup_volume_references: dict[int, str] = {}
        self.backup_volume_summaries: dict[int, Any] = {}
        self.backup_scan_records: dict[int, Any] = {}
        self.backup_analysis_state: Any = None
        self.volume_full_delegate = VolumeFullDelegate(self)
        self.catalogue_actions: list[QAction] = []
        self.catalogue_widgets: list[QWidget] = []
        self.scan_blocked_actions: list[QAction] = []
        self.scan_blocked_widgets: list[QWidget] = []
        self.base_ui_font = QFont(QApplication.font())
        self.ui_zoom = 1.0
        self._connected_volume_snapshots: list[VolumeSnapshot] = []
        self._connected_volume_signature: tuple[tuple[str, str, str], ...] = ()
        self.volume_connection_timer = QTimer(self)
        self.volume_connection_timer.setInterval(VOLUME_CONNECTION_POLL_INTERVAL_MS)
        self.volume_connection_timer.timeout.connect(self.check_connected_volumes)
        self.volume_connection_refresh_timer = QTimer(self)
        self.volume_connection_refresh_timer.setSingleShot(True)
        self.volume_connection_refresh_timer.setInterval(VOLUME_CONNECTION_REFRESH_DELAY_MS)
        self.volume_connection_refresh_timer.timeout.connect(self.refresh_after_connected_volumes_changed)
        self.catalogue_probe_timer = QTimer(self)
        self.catalogue_probe_timer.setSingleShot(True)
        self.catalogue_probe_timer.setInterval(CATALOGUE_PROBE_TIMEOUT_MS)
        self.catalogue_probe_timer.timeout.connect(self.on_catalogue_probe_timeout)

        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)
        self.setStatusBar(QStatusBar())
        self.statusBar().setSizeGripEnabled(False)

        self._build_menu_bar()
        self._build_ui()
        self._connect_signals()
        self._set_catalogue_open(False)
        QTimer.singleShot(0, self.open_last_catalogue)

    def eventFilter(self, source: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if (
            source is self.volume_table.viewport()
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            point = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if not self.volume_table.indexAt(point).isValid():
                self.clear_volume_selection()
        return super().eventFilter(source, event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.save_all_table_header_states()
        if self._catalogue_open_in_progress() and self.db is None:
            QMessageBox.information(
                self,
                "Catalogue Loading",
                "Wait for the catalogue to finish loading before closing the application.",
            )
            event.ignore()
            return
        if not self.close_catalogue(show_status=False):
            event.ignore()
            return
        self.settings.sync()
        super().closeEvent(event)

    def _build_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        self.new_catalogue_action = QAction("New Catalogue\u2026", self)
        self.new_catalogue_action.setShortcut(QKeySequence(QKeySequence.StandardKey.New))
        self.new_catalogue_action.triggered.connect(self.new_catalogue)
        file_menu.addAction(self.new_catalogue_action)

        self.open_catalogue_action = QAction("Open Catalogue\u2026", self)
        self.open_catalogue_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Open))
        self.open_catalogue_action.triggered.connect(self.open_catalogue_from_dialog)
        file_menu.addAction(self.open_catalogue_action)

        file_menu.addSeparator()

        self.create_catalogue_backup_action = QAction(
            "Create Catalogue Backup\u2026",
            self,
        )
        self.create_catalogue_backup_action.triggered.connect(
            self.create_catalogue_backup_from_dialog
        )
        file_menu.addAction(self.create_catalogue_backup_action)

        self.restore_catalogue_backup_action = QAction(
            "Restore Catalogue from Backup\u2026",
            self,
        )
        self.restore_catalogue_backup_action.triggered.connect(
            self.restore_catalogue_backup_from_dialog
        )
        file_menu.addAction(self.restore_catalogue_backup_action)

        file_menu.addSeparator()

        self.open_catalogue_location_action = QAction("Open Catalogue Location", self)
        self.open_catalogue_location_action.triggered.connect(self.open_catalogue_location)
        file_menu.addAction(self.open_catalogue_location_action)

        self.close_catalogue_action = QAction("Close Catalogue", self)
        self.close_catalogue_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Close))
        self.close_catalogue_action.triggered.connect(lambda: self.close_catalogue())
        file_menu.addAction(self.close_catalogue_action)

        file_menu.addSeparator()

        self.exit_action = QAction("Exit", self)
        self.exit_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Quit))
        self.exit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.exit_action.triggered.connect(QApplication.instance().quit)
        file_menu.addAction(self.exit_action)

        catalogue_menu = self.menuBar().addMenu("&Catalogue")

        self.new_volume_action = QAction("New Volume\u2026", self)
        self.new_volume_action.triggered.connect(self.add_volume)
        catalogue_menu.addAction(self.new_volume_action)

        catalogue_menu.addSeparator()

        self.backup_evidence_action = QAction("Backup Evidence\u2026", self)
        self.backup_evidence_action.triggered.connect(self.show_backup_evidence)
        self.backup_evidence_action.setToolTip(
            "Compare metadata already saved in this catalogue; no drives are rescanned"
        )
        catalogue_menu.addAction(self.backup_evidence_action)

        self.catalogue_info_action = QAction("Catalogue Info\u2026", self)
        self.catalogue_info_action.triggered.connect(self.show_catalogue_info)
        catalogue_menu.addAction(self.catalogue_info_action)

        view_menu = self.menuBar().addMenu("&View")
        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_in_action.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        self.zoom_in_action.triggered.connect(self.zoom_in)
        view_menu.addAction(self.zoom_in_action)

        self.zoom_out_action = QAction("Zoom Out", self)
        self.zoom_out_action.setShortcut("Ctrl + -")
        self.zoom_out_action.triggered.connect(self.zoom_out)
        view_menu.addAction(self.zoom_out_action)

        settings_menu = self.menuBar().addMenu("&Settings")
        self.preferences_action = QAction("Preferences\u2026", self)
        self.preferences_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.preferences_action.triggered.connect(self.show_preferences)
        settings_menu.addAction(self.preferences_action)

        help_menu = self.menuBar().addMenu("&Help")
        self.help_action = QAction("Help", self)
        self.help_action.setShortcut(QKeySequence(QKeySequence.StandardKey.HelpContents))
        self.help_action.triggered.connect(self.show_help)
        help_menu.addAction(self.help_action)

    def zoom_in(self) -> None:
        self.set_ui_zoom(self.ui_zoom + UI_ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_ui_zoom(self.ui_zoom - UI_ZOOM_STEP)

    def set_ui_zoom(self, zoom: float) -> None:
        zoom = round(max(MIN_UI_ZOOM, min(MAX_UI_ZOOM, zoom)), 2)
        if zoom == self.ui_zoom:
            return

        self.ui_zoom = zoom
        font = QFont(self.base_ui_font)
        point_size = self.base_ui_font.pointSizeF()
        if point_size > 0:
            font.setPointSizeF(max(6.0, point_size * zoom))
        elif self.base_ui_font.pixelSize() > 0:
            font.setPixelSize(max(6, round(self.base_ui_font.pixelSize() * zoom)))
        QApplication.setFont(font)

        self.apply_ui_zoom()
        self.statusBar().showMessage(f"UI zoom {round(zoom * 100)}%", 3000)

    def apply_ui_zoom(self) -> None:
        self.zoom_in_action.setEnabled(self.ui_zoom < MAX_UI_ZOOM)
        self.zoom_out_action.setEnabled(self.ui_zoom > MIN_UI_ZOOM)

        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_application_theme(
                application,
                self.ui_zoom,
                self.theme_style,
                self.color_mode,
                self.accent_color,
            )

        if hasattr(self, "welcome_title_label"):
            title_font = QFont(QApplication.font())
            point_size = title_font.pointSizeF()
            if point_size > 0:
                title_font.setPointSizeF(point_size + (8 * self.ui_zoom))
            elif title_font.pixelSize() > 0:
                title_font.setPixelSize(title_font.pixelSize() + self.scaled_ui_value(8))
            title_font.setBold(True)
            self.welcome_title_label.setFont(title_font)

        for table_name in ("volume_table", "file_table", "search_table"):
            table = getattr(self, table_name, None)
            if table is not None:
                table.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
                table.verticalHeader().setDefaultSectionSize(self.scaled_ui_value(24))

        if hasattr(self, "folder_tree"):
            self.folder_tree.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
            self.folder_tree.setIndentation(self.scaled_ui_value(16))
        if hasattr(self, "up_button"):
            navigation_size = self.scaled_ui_value(26)
            self.up_button.setFixedSize(navigation_size, navigation_size)
            self.up_button.setIconSize(
                QSize(self.scaled_ui_value(16), self.scaled_ui_value(16))
            )

        if hasattr(self, "scan_progress"):
            configure_progress_bar(self.scan_progress, self.ui_zoom)
        if hasattr(self, "catalogue_loading_progress"):
            configure_progress_bar(self.catalogue_loading_progress, self.ui_zoom)
        if hasattr(self, "detail_full"):
            configure_progress_bar(self.detail_full, self.ui_zoom)
        if hasattr(self, "details_box"):
            self.details_box.setMaximumHeight(self.scaled_ui_value(150))
        if hasattr(self, "workspace_splitter"):
            self.workspace_splitter.setHandleWidth(self.scaled_ui_value(3))
        if hasattr(self, "browser_splitter"):
            self.browser_splitter.setHandleWidth(self.scaled_ui_value(3))
        if hasattr(self, "welcome_new_button"):
            self.welcome_new_button.setMinimumWidth(self.scaled_ui_value(240))
            self.welcome_open_button.setMinimumWidth(self.scaled_ui_value(240))
        if hasattr(self, "catalogue_loading_progress"):
            self.catalogue_loading_path_label.setMaximumWidth(self.scaled_ui_value(700))
            self.catalogue_loading_progress.setMinimumWidth(self.scaled_ui_value(420))
            self.catalogue_loading_cancel_button.setMinimumWidth(self.scaled_ui_value(120))

        self.apply_scaled_layout_metrics()

    def apply_scaled_layout_metrics(self) -> None:
        layout_metrics = (
            ("volume_pane_layout", (6, 6, 3, 6), 5),
            ("content_pane_layout", (3, 6, 6, 6), 5),
            ("browser_tab_layout", (6, 6, 6, 6), 5),
            ("search_tab_layout", (6, 6, 6, 6), 5),
            ("log_tab_layout", (6, 6, 6, 6), None),
            ("browser_path_layout", (0, 0, 0, 0), 4),
            ("search_controls_layout", (0, 0, 0, 0), 4),
            ("search_empty_layout", (0, 0, 0, 0), 4),
        )
        for name, margins, spacing in layout_metrics:
            layout = getattr(self, name, None)
            if layout is None:
                continue
            layout.setContentsMargins(*(self.scaled_ui_value(value) if value else 0 for value in margins))
            if spacing is not None:
                layout.setSpacing(self.scaled_ui_value(spacing))

        details_layout = getattr(self, "details_layout", None)
        if details_layout is not None:
            details_layout.setHorizontalSpacing(self.scaled_ui_value(12))
            details_layout.setVerticalSpacing(self.scaled_ui_value(4))

        for name in ("welcome_layout", "loading_layout"):
            layout = getattr(self, name, None)
            if layout is not None:
                layout.setSpacing(self.scaled_ui_value(14))

    def scaled_ui_value(self, value: int) -> int:
        return max(1, round(value * self.ui_zoom))

    def _build_ui(self) -> None:
        self.stack = QStackedWidget()
        self.welcome_page = self._build_welcome_page()
        self.loading_page = self._build_loading_page()
        self.catalogue_page = self._build_catalogue_workspace()
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.loading_page)
        self.stack.addWidget(self.catalogue_page)
        self.setCentralWidget(self.stack)

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("welcomePage")
        layout = QVBoxLayout(page)
        self.welcome_layout = layout
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(14)

        title = QLabel("No Catalogue Open")
        title.setObjectName("welcomeTitle")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 8)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome_title_label = title

        description = QLabel("Create a new catalogue file or open an existing .jvvv catalogue.")
        description.setObjectName("mutedLabel")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description.setWordWrap(True)

        self.welcome_new_button = QPushButton("Create New Catalogue")
        self.welcome_new_button.setObjectName("primaryButton")
        self.welcome_open_button = QPushButton("Open Existing Catalogue")
        self.welcome_new_button.setMinimumWidth(240)
        self.welcome_open_button.setMinimumWidth(240)

        layout.addWidget(title)
        layout.addWidget(description)
        layout.addSpacing(8)
        layout.addWidget(self.welcome_new_button, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.welcome_open_button, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def _build_loading_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("loadingPage")
        layout = QVBoxLayout(page)
        self.loading_layout = layout
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(14)

        title = QLabel("Opening Catalogue")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 6)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.catalogue_loading_path_label = QLabel()
        self.catalogue_loading_path_label.setObjectName("loadingPath")
        self.catalogue_loading_path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.catalogue_loading_path_label.setWordWrap(True)
        self.catalogue_loading_path_label.setMaximumWidth(700)

        self.catalogue_loading_progress = QProgressBar()
        self.catalogue_loading_progress.setMinimumWidth(420)
        self.catalogue_loading_progress.setTextVisible(True)
        configure_progress_bar(self.catalogue_loading_progress)

        self.catalogue_loading_cancel_button = QPushButton("Cancel")
        self.catalogue_loading_cancel_button.setMinimumWidth(120)

        layout.addWidget(title)
        layout.addWidget(self.catalogue_loading_path_label)
        layout.addWidget(self.catalogue_loading_progress)
        layout.addWidget(
            self.catalogue_loading_cancel_button,
            0,
            Qt.AlignmentFlag.AlignCenter,
        )
        return page

    def _build_catalogue_workspace(self) -> QWidget:
        self.volume_table = VolumeTableView()
        self.volume_table.setObjectName("volumeTable")
        self.volume_table.setModel(self.volume_model)
        self.configure_table_view(self.volume_table)
        self.volume_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.volume_table.setItemDelegateForColumn(VOLUME_FULL_COLUMN, self.volume_full_delegate)
        self.volume_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.volume_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.volume_table.viewport().installEventFilter(self)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.volume_table,
                {
                    0: 110,
                    1: 145,
                    3: 105,
                    4: 100,
                    5: 220,
                    6: 110,
                    7: 100,
                    8: 80,
                    9: 160,
                    10: 100,
                    11: 120,
                    12: 120,
                    13: 105,
                    14: 105,
                    15: 95,
                    16: 95,
                    17: 95,
                    VOLUME_FULL_COLUMN: 80,
                    19: 80,
                    20: 85,
                    21: 145,
                    22: 165,
                },
                stretch_column=2,
            ),
        )

        self.volume_filter_edit = QLineEdit()
        self.volume_filter_edit.setObjectName("filterField")
        self.volume_filter_edit.setPlaceholderText("Filter volumes by any visible field")

        left = QWidget()
        left.setObjectName("volumePane")
        left_layout = QVBoxLayout(left)
        self.volume_pane_layout = left_layout
        left_layout.setContentsMargins(6, 6, 3, 6)
        left_layout.setSpacing(5)
        left_layout.addWidget(self.volume_filter_edit)
        left_layout.addWidget(self.volume_table, 1)

        self.details_box = self._build_details_box()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workspaceTabs")
        self.browser_tab = self._build_browser_tab()
        self.search_tab = self._build_search_tab()
        self.log_tab = self._build_log_tab()
        self.tabs.addTab(self.browser_tab, "Catalogue")
        self.tabs.addTab(self.search_tab, "Search")
        self.tabs.addTab(self.log_tab, "Scan Log")

        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Idle")
        self.scan_progress.setTextVisible(True)
        configure_progress_bar(self.scan_progress)

        self.stop_scan_button = QPushButton("Cancel Scan")
        self.stop_scan_button.setEnabled(False)
        self.stop_scan_button.setToolTip("Cancel the active scan and discard its partial results")

        scan_status_layout = QHBoxLayout()
        self.scan_status_layout = scan_status_layout
        scan_status_layout.setContentsMargins(0, 0, 0, 0)
        scan_status_layout.setSpacing(5)
        scan_status_layout.addWidget(self.stop_scan_button)
        scan_status_layout.addWidget(self.scan_progress, 1)

        right = QWidget()
        right.setObjectName("contentPane")
        right_layout = QVBoxLayout(right)
        self.content_pane_layout = right_layout
        right_layout.setContentsMargins(3, 6, 6, 6)
        right_layout.setSpacing(5)
        right_layout.addWidget(self.details_box)
        right_layout.addWidget(self.tabs, 1)
        right_layout.addLayout(scan_status_layout)

        splitter = QSplitter()
        self.workspace_splitter = splitter
        splitter.setObjectName("workspaceSplitter")
        splitter.setHandleWidth(3)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _detail_value_label(self, key: str, word_wrap: bool = False) -> QLabel:
        widget = QLabel("-")
        widget.setObjectName("detailValue")
        widget.setWordWrap(word_wrap)
        widget.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.detail_labels[key] = widget
        return widget

    def _build_details_box(self) -> QGroupBox:
        box = QGroupBox("Selected Volume")
        box.setObjectName("detailsPanel")
        box.setMaximumHeight(self.scaled_ui_value(180))
        self.detail_labels: dict[str, QLabel] = {}
        self.detail_full = QProgressBar()
        self.detail_full.setRange(0, 100)
        self.detail_full.setFormat("%p% full")
        configure_progress_bar(self.detail_full)

        grid = QGridLayout(box)
        self.details_layout = grid
        grid.setHorizontalSpacing(self.scaled_ui_value(12))
        grid.setVerticalSpacing(self.scaled_ui_value(4))
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(5, 1)

        summary_rows = [
            (0, 0, "Drive ID", "drive_id"),
            (0, 2, "Name", "name"),
            (0, 4, "Connection", "connection"),
            (1, 0, "Status", "register_status"),
            (1, 2, "Condition", "condition"),
            (1, 4, "Last Scan", "last_scan"),
        ]
        for row, column, label, key in summary_rows:
            key_label = QLabel(label)
            key_label.setObjectName("detailKey")
            grid.addWidget(key_label, row, column)
            grid.addWidget(self._detail_value_label(key), row, column + 1)

        path_row = 2
        scan_path_label = QLabel("Scan Path")
        scan_path_label.setObjectName("detailKey")
        grid.addWidget(scan_path_label, path_row, 0)
        grid.addWidget(self._detail_value_label("path"), path_row, 1, 1, 5)

        coverage_row = 3
        coverage_label = QLabel("Other-copy coverage")
        coverage_label.setObjectName("detailKey")
        coverage_label.setToolTip(BACKUP_COLUMN_HEADER_TOOLTIP)
        grid.addWidget(coverage_label, coverage_row, 0)
        coverage_value = self._detail_value_label("backup_coverage")
        coverage_value.setToolTip(BACKUP_COLUMN_HEADER_TOOLTIP)
        grid.addWidget(coverage_value, coverage_row, 1, 1, 5)

        full_row = 4
        full_label = QLabel("Full")
        full_label.setObjectName("detailKey")
        grid.addWidget(full_label, full_row, 0)
        grid.addWidget(self.detail_full, full_row, 1, 1, 5)
        return box

    def _build_browser_tab(self) -> QWidget:
        self.offline_label = QLabel("")
        self.offline_label.setObjectName("offlineNotice")
        self.offline_label.setVisible(False)
        self.folder_tree = QTreeWidget()
        self.folder_tree.setObjectName("folderTree")
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
        self.folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.setIndentation(self.scaled_ui_value(16))

        self.up_button = QToolButton()
        self.up_button.setObjectName("navigationButton")
        self.up_button.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp)
        )
        self.up_button.setToolTip("Up one folder (Alt+Up)")
        self.up_button.setAccessibleName("Up one folder")
        self.up_button.setFixedSize(
            self.scaled_ui_value(26), self.scaled_ui_value(26)
        )
        self.up_button.setIconSize(
            QSize(self.scaled_ui_value(16), self.scaled_ui_value(16))
        )
        self.up_button.setEnabled(False)
        self.current_path_label = QLineEdit("/")
        self.current_path_label.setObjectName("pathField")
        self.current_path_label.setReadOnly(True)
        self.current_path_label.setToolTip("Current catalogue path")
        self.current_path_label.addAction(
            self.browser_icons.folder_icon,
            QLineEdit.ActionPosition.LeadingPosition,
        )

        path_row = QHBoxLayout()
        self.browser_path_layout = path_row
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(4)
        path_row.addWidget(self.up_button)
        path_row.addWidget(self.current_path_label, 1)

        backup_filter_label = QLabel("Other-copy status")
        backup_filter_label.setObjectName("detailKey")
        self.browser_backup_filter_combo = QComboBox()
        self.browser_backup_filter_combo.setObjectName("browserBackupFilter")
        for label, key in BACKUP_FILTER_OPTIONS:
            self.browser_backup_filter_combo.addItem(label, key)
        self.browser_backup_filter_combo.setToolTip(
            "Filter by catalogue-metadata copy evidence. 'Needs attention' includes "
            "possible, partial, none found, outdated, and unknown items."
        )
        self.browser_backup_help_button = QPushButton("How matching works")
        self.browser_backup_help_button.setToolTip(
            "Explain what the coloured copy-evidence indicators mean"
        )

        backup_filter_row = QHBoxLayout()
        backup_filter_row.setContentsMargins(0, 0, 0, 0)
        backup_filter_row.setSpacing(4)
        backup_filter_row.addWidget(backup_filter_label)
        backup_filter_row.addWidget(self.browser_backup_filter_combo)
        backup_filter_row.addStretch(1)
        backup_filter_row.addWidget(self.browser_backup_help_button)

        self.file_table = QTableView()
        self.file_table.setObjectName("fileTable")
        self.file_table.setModel(self.browser_model)
        self.configure_table_view(self.file_table)
        self.file_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.file_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.file_table,
                {1: 165, 2: 140, 3: 95, 4: 155, 5: 280, 6: 90},
            ),
        )

        browser_splitter = QSplitter()
        self.browser_splitter = browser_splitter
        browser_splitter.setObjectName("browserSplitter")
        browser_splitter.setHandleWidth(3)
        browser_splitter.addWidget(self.folder_tree)
        browser_splitter.addWidget(self.file_table)
        browser_splitter.setStretchFactor(0, 1)
        browser_splitter.setStretchFactor(1, 2)

        widget = QWidget()
        widget.setObjectName("browserTab")
        layout = QVBoxLayout(widget)
        self.browser_tab_layout = layout
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)
        layout.addWidget(self.offline_label)
        layout.addLayout(path_row)
        layout.addLayout(backup_filter_row)
        layout.addWidget(browser_splitter, 1)
        return widget

    def configure_table_view(self, table: QTableView) -> None:
        table.setSortingEnabled(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        self.configure_table_palette(table)
        table.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
        table.setWordWrap(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(self.scaled_ui_value(24))

        header = table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(True)

    def configure_table_palette(self, table: QTableView) -> None:
        palette = QPalette(QApplication.palette())
        base_color = palette.color(QPalette.ColorRole.Base)
        alternate_color = base_color.lighter(112) if base_color.lightness() < 128 else base_color.darker(104)
        palette.setColor(QPalette.ColorRole.AlternateBase, alternate_color)
        table.setPalette(palette)

    def apply_table_default_columns(
        self,
        table: QTableView,
        default_widths: dict[int, int],
        stretch_column: int = 0,
        only_if_empty: bool = False,
    ) -> None:
        if table.property("headerLayoutApplied"):
            return

        if self.restore_table_header_state(table):
            table.setProperty("headerLayoutApplied", True)
            self.install_table_header_persistence(table)
            return

        if only_if_empty and table.columnWidth(stretch_column) > 0:
            table.setProperty("headerLayoutApplied", True)
            self.install_table_header_persistence(table)
            return

        table.setProperty("suppressHeaderSave", True)
        try:
            remaining = table.viewport().width() - sum(default_widths.values()) - 24
            table.setColumnWidth(stretch_column, max(220, remaining))
            for column, width in default_widths.items():
                table.setColumnWidth(column, width)
        finally:
            table.setProperty("suppressHeaderSave", False)

        table.setProperty("headerLayoutApplied", True)
        self.install_table_header_persistence(table)

    def table_header_settings_key(self, table: QTableView, suffix: str) -> str | None:
        name = table.objectName()
        if not name:
            return None
        return f"tableHeaders/{name}/{suffix}"

    def restore_table_header_state(self, table: QTableView) -> bool:
        state_key = self.table_header_settings_key(table, "state")
        count_key = self.table_header_settings_key(table, "columnCount")
        if state_key is None or count_key is None or table.model() is None:
            return False

        expected_columns = table.model().columnCount()
        saved_columns = self.settings.value(count_key, -1, type=int)
        state = self.settings.value(state_key)
        if saved_columns != expected_columns or state is None:
            return False

        if not isinstance(state, QByteArray):
            try:
                state = QByteArray(state)
            except TypeError:
                return False

        table.setProperty("suppressHeaderSave", True)
        try:
            return table.horizontalHeader().restoreState(state)
        finally:
            table.setProperty("suppressHeaderSave", False)

    def install_table_header_persistence(self, table: QTableView) -> None:
        if table.property("headerPersistenceInstalled"):
            return

        header = table.horizontalHeader()
        header.sectionMoved.connect(lambda *_args, table=table: self.save_table_header_state(table))
        header.sectionResized.connect(lambda *_args, table=table: self.save_table_header_state(table))
        header.sortIndicatorChanged.connect(lambda *_args, table=table: self.save_table_header_state(table))
        table.setProperty("headerPersistenceInstalled", True)

    def save_table_header_state(self, table: QTableView) -> None:
        if table.property("suppressHeaderSave") or table.model() is None:
            return

        state_key = self.table_header_settings_key(table, "state")
        count_key = self.table_header_settings_key(table, "columnCount")
        if state_key is None or count_key is None:
            return

        self.settings.setValue(state_key, table.horizontalHeader().saveState())
        self.settings.setValue(count_key, table.model().columnCount())

    def save_all_table_header_states(self) -> None:
        for table_name in ("volume_table", "file_table", "search_table"):
            table = getattr(self, table_name, None)
            if table is not None:
                self.save_table_header_state(table)

    def _build_search_tab(self) -> QWidget:
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(self.search_placeholder_text())
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("searchButton")
        self.open_file_button = QPushButton("Open")
        self.reveal_file_button = QPushButton("Reveal")
        self.open_file_button.setEnabled(False)
        self.reveal_file_button.setEnabled(False)
        self.search_backup_filter_combo = QComboBox()
        self.search_backup_filter_combo.setObjectName("searchBackupFilter")
        for label, key in BACKUP_FILTER_OPTIONS:
            self.search_backup_filter_combo.addItem(label, key)
        self.search_backup_filter_combo.setToolTip(
            "Limit results by catalogue-metadata copy evidence"
        )

        search_row = QHBoxLayout()
        self.search_controls_layout = search_row
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(4)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(QLabel("Other-copy status"))
        search_row.addWidget(self.search_backup_filter_combo)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.open_file_button)
        search_row.addWidget(self.reveal_file_button)

        self.search_table = QTableView()
        self.search_table.setObjectName("searchTable")
        self.search_table.setModel(self.search_model)
        self.configure_table_view(self.search_table)
        self.search_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.search_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.search_table,
                {1: 165, 2: 80, 3: 150, 4: 85, 5: 280, 6: 95, 7: 155, 8: 120},
            ),
        )

        self.search_empty_state = QWidget()
        empty_layout = QVBoxLayout(self.search_empty_state)
        self.search_empty_layout = empty_layout
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setSpacing(4)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("No Results")
        empty_title.setObjectName("emptyStateTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_description = QLabel("Try a different name, extension, or folder.")
        empty_description.setObjectName("emptyStateDescription")
        empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_description)

        self.search_results_stack = QStackedWidget()
        self.search_results_stack.setObjectName("searchResultsStack")
        self.search_results_stack.addWidget(self.search_table)
        self.search_results_stack.addWidget(self.search_empty_state)
        self.search_results_stack.setCurrentWidget(self.search_table)

        widget = QWidget()
        widget.setObjectName("searchTab")
        layout = QVBoxLayout(widget)
        self.search_tab_layout = layout
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)
        layout.addLayout(search_row)
        layout.addWidget(self.search_results_stack, 1)
        return widget

    def _build_log_tab(self) -> QWidget:
        self.scan_log = QPlainTextEdit()
        self.scan_log.setObjectName("scanLog")
        self.scan_log.setReadOnly(True)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.log_tab_layout = layout
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.scan_log)
        return widget

    def _connect_signals(self) -> None:
        self.welcome_new_button.clicked.connect(self.new_catalogue)
        self.welcome_open_button.clicked.connect(self.open_catalogue_from_dialog)
        self.catalogue_loading_cancel_button.clicked.connect(self.cancel_catalogue_open)
        self.volume_filter_edit.textChanged.connect(lambda _text: self.refresh_volumes())
        self.volume_table.selectionModel().selectionChanged.connect(self.on_volume_selection_changed)
        self.volume_table.customContextMenuRequested.connect(self.show_volume_context_menu)
        self.volume_table.doubleClicked.connect(self.edit_volume_index)
        self.folder_tree.itemExpanded.connect(self.load_tree_children)
        self.folder_tree.currentItemChanged.connect(self.on_folder_changed)
        self.folder_tree.customContextMenuRequested.connect(self.show_folder_tree_context_menu)
        self.up_button.clicked.connect(self.navigate_parent_folder)
        self.browser_backup_filter_combo.currentIndexChanged.connect(
            lambda _index: self.apply_browser_backup_filter()
        )
        self.browser_backup_help_button.clicked.connect(self.show_backup_evidence)
        self.file_table.doubleClicked.connect(self.open_browser_index)
        self.file_table.customContextMenuRequested.connect(self.show_browser_context_menu)
        self.search_button.clicked.connect(self.perform_search)
        self.stop_scan_button.clicked.connect(self.cancel_scan)
        self.search_edit.returnPressed.connect(self.perform_search)
        self.search_backup_filter_combo.currentIndexChanged.connect(
            lambda _index: self.perform_search()
        )
        self.search_table.selectionModel().selectionChanged.connect(self.on_search_selection_changed)
        self.search_table.doubleClicked.connect(self.open_search_index)
        self.search_table.customContextMenuRequested.connect(self.show_search_context_menu)
        self.open_file_button.clicked.connect(self.open_selected_search_item)
        self.reveal_file_button.clicked.connect(lambda: self.open_selected_real_item(reveal=True))

        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.setShortcut("F5")
        self.refresh_action.triggered.connect(self.refresh_volumes)
        self.addAction(self.refresh_action)
        self.catalogue_actions = [
            self.close_catalogue_action,
            self.open_catalogue_location_action,
            self.create_catalogue_backup_action,
            self.new_volume_action,
            self.backup_evidence_action,
            self.catalogue_info_action,
            self.refresh_action,
        ]
        self.catalogue_widgets = [
            self.volume_filter_edit,
            self.search_edit,
            self.search_button,
            self.browser_backup_filter_combo,
            self.search_backup_filter_combo,
        ]
        self.scan_blocked_actions = [
            self.new_volume_action,
            self.create_catalogue_backup_action,
            self.restore_catalogue_backup_action,
        ]
        self.scan_blocked_widgets = []

        self.add_browser_shortcut(QKeySequence("Backspace"), self.navigate_parent_folder)
        self.add_browser_shortcut(QKeySequence("Alt+Up"), self.navigate_parent_folder)
        self.add_browser_shortcut(QKeySequence("Return"), self.open_selected_browser_item)
        self.add_browser_shortcut(QKeySequence("Enter"), self.open_selected_browser_item)
        self.add_browser_shortcut(
            QKeySequence(QKeySequence.StandardKey.Copy),
            self.copy_selected_browser_path,
        )

    def add_browser_shortcut(self, sequence: QKeySequence, callback) -> None:
        shortcut = QShortcut(sequence, self.file_table)
        shortcut.activated.connect(callback)
        self.browser_shortcuts.append(shortcut)

    def show_help(self) -> None:
        dialog = HelpDialog(self)
        dialog.exec()

    def apply_appearance(
        self,
        theme_style: str,
        color_mode: str,
        accent_color: str,
    ) -> None:
        """Apply an appearance choice without persisting it."""
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_application_theme(
                application,
                getattr(self, "ui_zoom", 1.0),
                theme_style,
                color_mode,
                accent_color,
            )
        for table_name in ("volume_table", "file_table", "search_table"):
            table = getattr(self, table_name, None)
            if table is not None:
                MainWindow.configure_table_palette(self, table)

    def show_preferences(self) -> None:
        original_appearance = (
            self.theme_style,
            self.color_mode,
            self.accent_color,
        )
        dialog = PreferencesDialog(
            self.search_include_paths,
            original_appearance[0],
            original_appearance[1],
            original_appearance[2],
            self,
        )
        preview_signal = getattr(dialog, "appearance_changed", None)
        if preview_signal is not None:
            preview_signal.connect(
                lambda theme_style, color_mode, accent_color: MainWindow.apply_appearance(
                    self,
                    theme_style,
                    color_mode,
                    accent_color,
                )
            )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            MainWindow.apply_appearance(self, *original_appearance)
            return

        include_paths = dialog.include_paths()
        theme_style = dialog.theme_style()
        color_mode = dialog.color_mode()
        accent_color = dialog.accent_color()
        search_changed = include_paths != self.search_include_paths
        appearance_changed = (
            theme_style != self.theme_style
            or color_mode != self.color_mode
            or accent_color != self.accent_color
        )
        if not search_changed and not appearance_changed:
            return

        self.search_include_paths = include_paths
        self.theme_style = theme_style
        self.color_mode = color_mode
        self.accent_color = accent_color
        self.settings.setValue(SEARCH_INCLUDE_PATHS_SETTING, include_paths)
        self.settings.setValue(THEME_STYLE_SETTING, theme_style)
        self.settings.setValue(COLOR_MODE_SETTING, color_mode)
        self.settings.setValue(ACCENT_COLOR_SETTING, accent_color)
        self.settings.sync()

        if appearance_changed:
            MainWindow.apply_appearance(
                self,
                self.theme_style,
                self.color_mode,
                self.accent_color,
            )

        if search_changed:
            self.search_edit.setPlaceholderText(self.search_placeholder_text())
            if self.db is not None and self.search_edit.text().strip():
                self.perform_search()
        self.statusBar().showMessage("Preferences saved.", 3000)

    def search_placeholder_text(self) -> str:
        if self.search_include_paths:
            return "Search filename, extension, folder, or relative path"
        return "Search filename, extension, or folder"

    def open_catalogue_location(self) -> None:
        if self.catalogue_path is None:
            return
        try:
            open_in_file_manager(self.catalogue_path, reveal=True)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Open Catalogue Location Failed",
                str(exc),
            )

    def show_backup_evidence(self) -> None:
        if self.db is None:
            return
        if self.backup_evidence_dialog is None:
            dialog = BackupEvidenceDialog(self)
            dialog.analyse_requested.connect(self.start_backup_analysis)
            dialog.cancel_requested.connect(self.cancel_backup_analysis)
            self.backup_evidence_dialog = dialog
        self.refresh_backup_evidence_dialog()
        self.backup_evidence_dialog.show()
        self.backup_evidence_dialog.raise_()
        self.backup_evidence_dialog.activateWindow()

    def refresh_backup_evidence_dialog(self) -> None:
        dialog = self.backup_evidence_dialog
        if dialog is None or self.db is None:
            return
        engine = getattr(self, "backup_engine", None)
        if engine is None and BackupAnalysisEngine is not None:
            try:
                engine = BackupAnalysisEngine(self.db)
                engine.ensure_schema()
                self.backup_engine = engine
            except Exception:
                engine = None
        volumes = self.db.list_volumes()
        self.backup_volume_references = {
            int(volume["id"]): volume_reference(volume["drive_id"], volume["name"])
            for volume in volumes
        }
        if engine is None:
            dialog.set_records(None, [], [], [], self.backup_volume_references)
            dialog.analysis_state_label.setText("Backup analysis support is unavailable")
            dialog.analyse_button.setEnabled(False)
            return
        try:
            state = engine.state()
            summaries = list(engine.volume_summaries())
            mirrors = list(engine.mirror_candidates())
            scans = list(engine.scan_records())
        except Exception as exc:
            dialog.set_records(None, [], [], [], self.backup_volume_references)
            dialog.analysis_state_label.setText("Backup evidence could not be loaded")
            dialog.analysis_summary_label.setText(
                f"{exc}\n\n{BACKUP_METADATA_DISCLAIMER}"
            )
            return
        dialog.set_records(
            state,
            summaries,
            mirrors,
            scans,
            self.backup_volume_references,
        )
        running = self.backup_analysis_worker is not None
        dialog.set_analysis_running(running)
        if not running:
            dialog.analyse_button.setEnabled(
                self.scan_worker is None and self.delete_worker is None
            )

    def start_backup_analysis(self) -> None:
        if self.db is None or self.backup_analysis_worker is not None:
            return
        if self.scan_worker is not None or self.delete_worker is not None:
            self._show_catalogue_job_running_message()
            return
        if BackupAnalysisEngine is None:
            QMessageBox.warning(
                self,
                "Backup Analysis Unavailable",
                "Backup analysis support is unavailable in this build.",
            )
            return

        if self.backup_evidence_dialog is not None:
            self.backup_evidence_dialog.set_analysis_running(True)
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Analysing saved catalogue evidence…")
        self.statusBar().showMessage(
            "Analysing saved catalogue evidence; no drives are being read…"
        )

        self.backup_analysis_thread = QThread(self)
        self.backup_analysis_worker = BackupAnalysisWorker(self.db.path)
        self.backup_analysis_worker.moveToThread(self.backup_analysis_thread)
        self.backup_analysis_thread.started.connect(self.backup_analysis_worker.run)
        self.backup_analysis_worker.progress.connect(self.on_backup_analysis_progress)
        self.backup_analysis_worker.finished.connect(self.on_backup_analysis_finished)
        self.backup_analysis_worker.cancelled.connect(self.on_backup_analysis_cancelled)
        self.backup_analysis_worker.failed.connect(self.on_backup_analysis_failed)
        self.backup_analysis_worker.finished.connect(self.backup_analysis_thread.quit)
        self.backup_analysis_worker.cancelled.connect(self.backup_analysis_thread.quit)
        self.backup_analysis_worker.failed.connect(self.backup_analysis_thread.quit)
        self.backup_analysis_worker.finished.connect(self.backup_analysis_worker.deleteLater)
        self.backup_analysis_worker.cancelled.connect(self.backup_analysis_worker.deleteLater)
        self.backup_analysis_worker.failed.connect(self.backup_analysis_worker.deleteLater)
        self.backup_analysis_thread.finished.connect(self.backup_analysis_thread.deleteLater)
        self.backup_analysis_thread.finished.connect(self.clear_backup_analysis_worker)
        self.backup_analysis_thread.start()

    @Slot(int, int, str)
    def on_backup_analysis_progress(self, completed: int, total: int, message: str) -> None:
        label = message or "Analysing saved catalogue evidence"
        if total > 0:
            self.scan_progress.setRange(0, total)
            self.scan_progress.setValue(min(max(completed, 0), total))
            self.scan_progress.setFormat(f"{label} — %p%")
        else:
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(label)
        if self.backup_evidence_dialog is not None:
            self.backup_evidence_dialog.set_analysis_progress(completed, total, label)
        self.statusBar().showMessage(f"{label}; no drives are being read.")

    def cancel_backup_analysis(self) -> None:
        worker = self.backup_analysis_worker
        if worker is None:
            return
        worker.cancel()
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Cancelling saved-evidence analysis…")
        if self.backup_evidence_dialog is not None:
            self.backup_evidence_dialog.cancel_button.setEnabled(False)
            self.backup_evidence_dialog.progress.setRange(0, 0)
            self.backup_evidence_dialog.progress.setFormat("Cancelling…")
        self.statusBar().showMessage("Cancelling backup analysis…")

    @Slot(object)
    def on_backup_analysis_finished(self, summary: Any) -> None:
        if enum_value(object_value(summary, "status")) == "discarded":
            self.scan_progress.setRange(0, 1)
            self.scan_progress.setValue(0)
            self.scan_progress.setFormat("Backup analysis not applied")
            self.statusBar().showMessage(
                str(
                    object_value(summary, "message", "")
                    or "The catalogue changed during analysis; results were not applied."
                ),
                8000,
            )
            self.refresh_backup_evidence_views()
            return
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_progress.setFormat("Backup evidence updated")
        self.statusBar().showMessage(
            "Backup evidence updated from saved catalogue hashes and metadata.",
            6000,
        )
        self.refresh_backup_evidence_views()

    @Slot()
    def on_backup_analysis_cancelled(self) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Backup analysis cancelled")
        self.statusBar().showMessage(
            "Backup analysis cancelled; incomplete results were not applied.",
            5000,
        )
        self.refresh_backup_evidence_dialog()

    @Slot(str)
    def on_backup_analysis_failed(self, details: str) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Backup analysis failed")
        self.statusBar().showMessage("Backup analysis failed.", 5000)
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("Backup Analysis Failed")
        dialog.setText("Saved catalogue evidence could not be analysed.")
        dialog.setInformativeText(
            "No source drive was scanned, and incomplete analysis results were not applied."
        )
        dialog.setDetailedText(details)
        dialog.exec()
        self.refresh_backup_evidence_dialog()

    def refresh_backup_evidence_views(self) -> None:
        if self.db is None:
            return
        self.refresh_volumes()
        if self.current_volume_id is not None and self.current_folder_id is not None:
            self.load_directory_items(self.current_volume_id, self.current_folder_id)
        if self.search_edit.text().strip():
            self.perform_search()
        self.refresh_backup_evidence_dialog()

    @Slot()
    def clear_backup_analysis_worker(self) -> None:
        self.backup_analysis_worker = None
        self.backup_analysis_thread = None
        if getattr(self, "backup_evidence_dialog", None) is not None:
            self.refresh_backup_evidence_dialog()

    def show_catalogue_info(self) -> None:
        if self.db is None or self.catalogue_info_thread is not None:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return

        self._start_catalogue_info(self.db.path)

    def _start_catalogue_info(self, db_path: Path) -> None:
        self._set_catalogue_busy(True)
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Loading catalogue info...")
        self.statusBar().showMessage("Loading catalogue info...")

        self.catalogue_info_thread = QThread(self)
        self.catalogue_info_worker = CatalogueInfoWorker(db_path)
        self.catalogue_info_worker.moveToThread(self.catalogue_info_thread)
        self.catalogue_info_thread.started.connect(self.catalogue_info_worker.run)
        self.catalogue_info_worker.finished.connect(self.on_catalogue_info_finished)
        self.catalogue_info_worker.cancelled.connect(self.on_catalogue_info_cancelled)
        self.catalogue_info_worker.failed.connect(self.on_catalogue_info_failed)
        self.catalogue_info_worker.finished.connect(self.catalogue_info_thread.quit)
        self.catalogue_info_worker.cancelled.connect(self.catalogue_info_thread.quit)
        self.catalogue_info_worker.failed.connect(self.catalogue_info_thread.quit)
        self.catalogue_info_worker.finished.connect(self.catalogue_info_worker.deleteLater)
        self.catalogue_info_worker.cancelled.connect(self.catalogue_info_worker.deleteLater)
        self.catalogue_info_worker.failed.connect(self.catalogue_info_worker.deleteLater)
        self.catalogue_info_thread.finished.connect(self.catalogue_info_thread.deleteLater)
        self.catalogue_info_thread.finished.connect(self.clear_catalogue_info_worker)
        self.catalogue_info_thread.start()

    @Slot(object)
    def on_catalogue_info_finished(self, info) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_progress.setFormat("Catalogue info loaded")
        self.statusBar().showMessage("Catalogue info loaded.", 3000)
        self._show_catalogue_info_dialog(info)

    def _show_catalogue_info_dialog(self, info) -> None:

        dialog = QDialog(self)
        dialog.setWindowTitle("Catalogue Info")
        dialog.setMinimumWidth(self.scaled_ui_value(460))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        def add_row(label: str, value: str, *, wrap: bool = False) -> None:
            value_label = QLabel(value)
            value_label.setWordWrap(wrap)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            form.addRow(label, value_label)

        add_row("Catalogue", self.catalogue_path.name if self.catalogue_path is not None else "-")
        add_row("Location", str(self.catalogue_path) if self.catalogue_path is not None else "-", wrap=True)
        add_row("Volumes", self._display_optional_count(info["volume_count"]))
        add_row("Total Data Used", format_size(info["total_used_bytes"]))
        add_row("Total Capacity", format_size(info["total_capacity_bytes"]))
        add_row("Total Free", format_size(info["total_free_bytes"]))
        add_row("Total Indexed Size", format_size(info["indexed_size_bytes"]))
        add_row("Files", self._display_optional_count(info["file_count"]))
        add_row("Folders", self._display_optional_count(info["folder_count"]))
        add_row("Missing Files", self._display_optional_count(info["missing_file_count"]))
        add_row("Missing Folders", self._display_optional_count(info["missing_folder_count"]))
        add_row("Scans", self._display_optional_count(info["scan_count"]))
        add_row("Latest Scan", self._display_time(info["latest_scan_at"]))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dialog.accept)

        layout = QVBoxLayout(dialog)
        layout.addLayout(form)
        layout.addWidget(buttons)
        dialog.exec()

    @Slot()
    def on_catalogue_info_cancelled(self) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Idle")

    @Slot(str)
    def on_catalogue_info_failed(self, details: str) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Catalogue info failed")
        self.statusBar().showMessage("Catalogue info failed.", 4000)
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("Catalogue Info Failed")
        dialog.setText("The catalogue information could not be loaded.")
        dialog.setDetailedText(details)
        dialog.exec()

    @Slot()
    def clear_catalogue_info_worker(self) -> None:
        self.catalogue_info_worker = None
        self.catalogue_info_thread = None
        self._set_catalogue_busy(False)

    def create_catalogue_backup_from_dialog(self) -> None:
        if self.db is None or self.catalogue_path is None:
            return
        if self._catalogue_job_running() or self._catalogue_open_in_progress():
            self._show_catalogue_job_running_message()
            return
        selected = self._choose_catalogue_backup_path()
        if selected is None:
            return
        path, overwrite = selected
        worker = CatalogueBackupWorker(self.catalogue_path, path, overwrite=overwrite)
        self._start_catalogue_archive_worker(worker, "backup")

    def restore_catalogue_backup_from_dialog(self) -> None:
        if self._catalogue_job_running() or self._catalogue_open_in_progress():
            self._show_catalogue_job_running_message()
            return
        backup_text, _ = QFileDialog.getOpenFileName(
            self,
            "Restore Catalogue from Backup",
            str(self.catalogue_path.parent if self.catalogue_path else Path.home()),
            BACKUP_FILE_FILTER,
        )
        if not backup_text:
            return
        backup_path = Path(backup_text).expanduser()
        selected = self._choose_restored_catalogue_path(backup_path)
        if selected is None:
            return
        target, overwrite = selected
        if (
            self.catalogue_path is not None
            and target.resolve(strict=False) == self.catalogue_path.resolve(strict=False)
        ):
            QMessageBox.warning(
                self,
                "Catalogue Is Open",
                "Choose a different destination, or close the current catalogue before "
                "replacing it from a backup.",
            )
            return
        worker = CatalogueRestoreWorker(backup_path, target, overwrite=overwrite)
        self._start_catalogue_archive_worker(worker, "restore")

    def _choose_catalogue_backup_path(self) -> tuple[Path, bool] | None:
        if self.catalogue_path is None:
            return None
        dialog = QFileDialog(self, "Create Catalogue Backup")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(BACKUP_FILE_FILTER)
        dialog.setDefaultSuffix("zip")
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.setDirectory(str(self.catalogue_path.parent))
        dialog.selectFile(f"{self.catalogue_path.stem}.backup.zip")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected = dialog.selectedFiles()
        if not selected:
            return None
        path = Path(selected[0]).expanduser()
        if path.suffix.casefold() != ".zip":
            path = Path(f"{path}.zip")
        overwrite = path.exists()
        if overwrite:
            answer = QMessageBox.question(
                self,
                "Overwrite Catalogue Backup",
                f"Replace the existing backup file?\n\n{path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
        return path, overwrite

    def _choose_restored_catalogue_path(
        self,
        backup_path: Path,
    ) -> tuple[Path, bool] | None:
        dialog = QFileDialog(self, "Save Restored Catalogue")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(CATALOGUE_FILE_FILTER)
        dialog.setDefaultSuffix(CATALOGUE_EXTENSION.lstrip("."))
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.setDirectory(str(backup_path.parent))
        suggested_stem = backup_path.stem
        if suggested_stem.casefold().endswith(".backup"):
            suggested_stem = suggested_stem[:-7]
        dialog.selectFile(f"{suggested_stem}{CATALOGUE_EXTENSION}")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected = dialog.selectedFiles()
        if not selected:
            return None
        path = catalogue_path_with_extension(selected[0])
        overwrite = path.exists()
        if overwrite:
            answer = QMessageBox.question(
                self,
                "Overwrite Catalogue",
                f"Replace the existing catalogue file?\n\n{path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
        return path, overwrite

    def _start_catalogue_archive_worker(
        self,
        worker: CatalogueBackupWorker | CatalogueRestoreWorker,
        operation: str,
    ) -> None:
        self.catalogue_archive_operation = operation
        self.pending_restored_catalogue_path = None
        self.catalogue_archive_thread = QThread(self)
        self.catalogue_archive_worker = worker
        worker.moveToThread(self.catalogue_archive_thread)
        self.catalogue_archive_thread.started.connect(worker.run)
        worker.progress.connect(self.on_catalogue_archive_progress)
        if operation == "backup":
            worker.finished.connect(self.on_catalogue_backup_finished)
        else:
            worker.finished.connect(self.on_catalogue_restore_finished)
        worker.cancelled.connect(self.on_catalogue_archive_cancelled)
        worker.failed.connect(self.on_catalogue_archive_failed)
        worker.finished.connect(self.catalogue_archive_thread.quit)
        worker.cancelled.connect(self.catalogue_archive_thread.quit)
        worker.failed.connect(self.catalogue_archive_thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self.catalogue_archive_thread.finished.connect(
            self.catalogue_archive_thread.deleteLater
        )
        self.catalogue_archive_thread.finished.connect(
            self.clear_catalogue_archive_worker
        )
        self._set_catalogue_archive_running(True)
        self.catalogue_archive_thread.start()

    @Slot(object, object, str)
    def on_catalogue_archive_progress(
        self,
        completed: object,
        total: object,
        message: str,
    ) -> None:
        completed_value = max(0, int(completed or 0))
        total_value = max(0, int(total or 0))
        if total_value:
            maximum = 1000
            value = min(maximum, (completed_value * maximum) // total_value)
            self.scan_progress.setRange(0, maximum)
            self.scan_progress.setValue(value)
        else:
            self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat(message or "Processing catalogue archive…")
        self.statusBar().showMessage(message or "Processing catalogue archive…")

    @Slot(object)
    def on_catalogue_backup_finished(self, result: BackupResult) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_progress.setFormat("Catalogue backup created")
        if result.savings_bytes >= 0:
            saving_text = (
                f"Saved {format_size(result.savings_bytes)} "
                f"({result.savings_percent:.1f}%)."
            )
        else:
            saving_text = f"The ZIP is {format_size(-result.savings_bytes)} larger."
        QMessageBox.information(
            self,
            "Catalogue Backup Created",
            "The lossless catalogue backup was created successfully.\n\n"
            f"Original catalogue: {format_size(result.original_size)}\n"
            f"Backup ZIP: {format_size(result.backup_size)}\n"
            f"{saving_text}\n\n{result.backup_path}",
        )
        self.statusBar().showMessage("Catalogue backup created.", 5000)

    @Slot(object)
    def on_catalogue_restore_finished(self, result: RestoreResult) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_progress.setFormat("Catalogue restored")
        regenerated = ", ".join(result.regenerated_components)
        QMessageBox.information(
            self,
            "Catalogue Restored",
            "The backup passed validation and was restored atomically.\n\n"
            f"Catalogue: {result.catalogue_path}\n"
            f"Restored size: {format_size(result.catalogue_size)}\n"
            f"Regenerated: {regenerated}",
        )
        self.pending_restored_catalogue_path = result.catalogue_path
        self.statusBar().showMessage("Catalogue restored.", 5000)

    @Slot()
    def on_catalogue_archive_cancelled(self) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Catalogue archive operation cancelled")
        self.statusBar().showMessage("Catalogue archive operation cancelled.", 4000)

    @Slot(object, str)
    def on_catalogue_archive_failed(self, exc: Exception, details: str) -> None:
        operation = "Restore" if self.catalogue_archive_operation == "restore" else "Backup"
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat(f"Catalogue {operation.casefold()} failed")
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle(f"Catalogue {operation} Failed")
        dialog.setText(str(exc) or f"The catalogue {operation.casefold()} failed.")
        diagnostic = format_exception_diagnostics(exc)
        dialog.setDetailedText(diagnostic or details)
        dialog.exec()
        self.statusBar().showMessage(f"Catalogue {operation.casefold()} failed.", 5000)

    @Slot()
    def clear_catalogue_archive_worker(self) -> None:
        restored_path = self.pending_restored_catalogue_path
        self.pending_restored_catalogue_path = None
        self.catalogue_archive_worker = None
        self.catalogue_archive_thread = None
        self.catalogue_archive_operation = ""
        self._set_catalogue_archive_running(False)
        if restored_path is not None:
            self.open_catalogue_path(restored_path, status_message="Catalogue restored and opened.")

    def _set_catalogue_archive_running(self, running: bool) -> None:
        self._set_catalogue_busy(running)
        enabled = not running and not self._catalogue_open_in_progress()
        self.new_catalogue_action.setEnabled(enabled)
        self.open_catalogue_action.setEnabled(enabled)
        self.restore_catalogue_backup_action.setEnabled(enabled)
        self.welcome_new_button.setEnabled(enabled)
        self.welcome_open_button.setEnabled(enabled)
        if running:
            label = "Creating catalogue backup…" if self.catalogue_archive_operation == "backup" else "Restoring catalogue…"
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(label)
            self.statusBar().showMessage(label)

    def new_catalogue(self) -> None:
        if self._catalogue_open_in_progress() or self.catalogue_archive_worker is not None:
            return
        path = self._choose_new_catalogue_path()
        if path is None:
            return
        if self.db is not None and not self.close_catalogue(show_status=False):
            return

        lock: QLockFile | None = None
        db: Database | None = None
        try:
            lock = self._acquire_catalogue_lock(path)
            db = create_catalogue(path, overwrite=path.exists())
        except Exception as exc:
            if db is not None:
                db.close()
            if lock is not None:
                lock.unlock()
            self._show_catalogue_error("New Catalogue Failed", exc)
            return

        self._open_catalogue_in_window(db, path, lock)
        self.statusBar().showMessage("Catalogue created.", 4000)

    def open_catalogue_from_dialog(self) -> None:
        if self._catalogue_open_in_progress() or self.catalogue_archive_worker is not None:
            return
        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "Open Catalogue",
            str(self.catalogue_path.parent if self.catalogue_path else Path.home()),
            CATALOGUE_FILE_FILTER,
        )
        if not path_text:
            return
        self.open_catalogue_path(catalogue_path_with_extension(path_text))

    def open_last_catalogue(self) -> None:
        if (
            self.db is not None
            or self._catalogue_open_in_progress()
            or self.catalogue_archive_worker is not None
        ):
            return
        path_text = self.settings.value(LAST_CATALOGUE_PATH_SETTING, "", type=str)
        if not path_text:
            return

        path = catalogue_path_with_extension(path_text)
        self.open_catalogue_path(path, status_message="Last catalogue opened.")

    def open_catalogue_path(self, path: str | Path, status_message: str = "Catalogue opened.") -> None:
        if self._catalogue_open_in_progress() or self.catalogue_archive_worker is not None:
            return
        path = catalogue_path_with_extension(path)
        if self.db is not None and not self.close_catalogue(show_status=False):
            return

        self.catalogue_open_cancel_requested = False
        self.catalogue_open_path = path
        self.catalogue_open_status_message = status_message
        self._set_catalogue_loading(True, path)

        self.catalogue_probe_timed_out = False
        self.catalogue_probe_process = QProcess(self)
        program, arguments = catalogue_probe_command(path)
        self.catalogue_probe_process.setProgram(program)
        self.catalogue_probe_process.setArguments(arguments)
        if not getattr(sys, "frozen", False):
            self.catalogue_probe_process.setWorkingDirectory(
                str(Path(__file__).resolve().parent.parent)
            )
        self.catalogue_probe_process.finished.connect(self.on_catalogue_probe_finished)
        self.catalogue_probe_process.errorOccurred.connect(self.on_catalogue_probe_error)
        self.catalogue_loading_progress.setFormat("Checking catalogue location...")
        self.statusBar().showMessage("Checking catalogue location...")
        self.catalogue_probe_timer.start()
        self.catalogue_probe_process.start()

    @Slot(int, QProcess.ExitStatus)
    def on_catalogue_probe_finished(
        self,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        process = self.catalogue_probe_process
        if process is None:
            return
        self.catalogue_probe_timer.stop()
        self.catalogue_probe_process = None
        process.deleteLater()

        if self.catalogue_open_cancel_requested:
            self.on_catalogue_open_cancelled()
            self.catalogue_open_cancel_requested = False
            self._set_catalogue_loading(False)
            return

        if self.catalogue_probe_timed_out:
            timeout_seconds = CATALOGUE_PROBE_TIMEOUT_MS // 1000
            self._fail_catalogue_probe(
                f"The catalogue location did not respond within {timeout_seconds} seconds. "
                "Check that the network drive is connected and try again."
            )
            return
        if exit_code == CATALOGUE_PROBE_INVALID:
            self._fail_catalogue_probe(
                "The selected file is not a valid SQLite catalogue database."
            )
            return
        if exit_code != CATALOGUE_PROBE_OK:
            self._fail_catalogue_probe(
                "The catalogue location is unavailable. Check that the network drive "
                "is connected and try again."
            )
            return

        self._start_catalogue_open_worker()

    @Slot(QProcess.ProcessError)
    def on_catalogue_probe_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        process = self.catalogue_probe_process
        if process is None:
            return
        self.catalogue_probe_timer.stop()
        self.catalogue_probe_process = None
        process.deleteLater()
        if self.catalogue_open_cancel_requested:
            self.on_catalogue_open_cancelled()
            self.catalogue_open_cancel_requested = False
            self._set_catalogue_loading(False)
            return
        self._fail_catalogue_probe("The catalogue accessibility check could not be started.")

    @Slot()
    def on_catalogue_probe_timeout(self) -> None:
        process = self.catalogue_probe_process
        if process is None:
            return
        self.catalogue_probe_timed_out = True
        self.catalogue_loading_progress.setFormat("Catalogue location is not responding...")
        self.statusBar().showMessage("Catalogue location is not responding...")
        process.kill()

    def _fail_catalogue_probe(self, message: str) -> None:
        self.catalogue_open_path = None
        self.catalogue_probe_timed_out = False
        self._set_catalogue_loading(False)
        self._set_catalogue_open(False)
        QMessageBox.critical(self, "Open Catalogue Failed", message)

    def _start_catalogue_open_worker(self) -> None:
        path = self.catalogue_open_path
        if path is None:
            return
        self.catalogue_loading_progress.setRange(0, 0)
        self.catalogue_loading_progress.setFormat("Acquiring catalogue lock...")
        self.statusBar().showMessage("Opening catalogue...")

        self.catalogue_open_thread = QThread(self)
        self.catalogue_open_worker = CatalogueOpenWorker(path)
        self.catalogue_open_worker.moveToThread(self.catalogue_open_thread)
        self.catalogue_open_thread.started.connect(self.catalogue_open_worker.run)
        self.catalogue_open_worker.progress.connect(self.on_catalogue_open_progress)
        self.catalogue_open_worker.finished.connect(self.on_catalogue_open_finished)
        self.catalogue_open_worker.failed.connect(self.on_catalogue_open_failed)
        self.catalogue_open_worker.cancelled.connect(self.on_catalogue_open_cancelled)
        self.catalogue_open_worker.finished.connect(self.catalogue_open_thread.quit)
        self.catalogue_open_worker.failed.connect(self.catalogue_open_thread.quit)
        self.catalogue_open_worker.cancelled.connect(self.catalogue_open_thread.quit)
        self.catalogue_open_worker.finished.connect(self.catalogue_open_worker.deleteLater)
        self.catalogue_open_worker.failed.connect(self.catalogue_open_worker.deleteLater)
        self.catalogue_open_worker.cancelled.connect(self.catalogue_open_worker.deleteLater)
        self.catalogue_open_thread.finished.connect(self.catalogue_open_thread.deleteLater)
        self.catalogue_open_thread.finished.connect(self.clear_catalogue_open_worker)
        self.catalogue_open_thread.start()

    @Slot()
    def cancel_catalogue_open(self) -> None:
        if not self._catalogue_open_in_progress() or self.catalogue_open_cancel_requested:
            return
        self.catalogue_open_cancel_requested = True
        self.catalogue_loading_cancel_button.setEnabled(False)
        self.catalogue_loading_progress.setRange(0, 0)
        self.catalogue_loading_progress.setFormat("Cancelling...")
        self.statusBar().showMessage("Cancelling catalogue open...")
        process = self.catalogue_probe_process
        if process is not None:
            process.kill()
            return
        worker = self.catalogue_open_worker
        if worker is not None:
            worker.cancel()

    @Slot(int, int, str)
    def on_catalogue_open_progress(self, value: int, maximum: int, message: str) -> None:
        if self.catalogue_open_cancel_requested:
            return
        if maximum <= 0:
            self.catalogue_loading_progress.setRange(0, 0)
            self.catalogue_loading_progress.setFormat(message)
        else:
            self.catalogue_loading_progress.setRange(0, maximum)
            self.catalogue_loading_progress.setValue(min(max(value, 0), maximum))
            self.catalogue_loading_progress.setFormat(f"{message} — %p%")
        self.statusBar().showMessage(message)

    @Slot(object, list, object, object)
    def on_catalogue_open_finished(
        self,
        db: Database,
        items: list[VolumeItem],
        connected_volume_snapshots: list[VolumeSnapshot],
        lock: QLockFile,
    ) -> None:
        path = self.catalogue_open_path
        if self.catalogue_open_cancel_requested or path is None:
            db.close()
            lock.unlock()
            self.catalogue_open_path = None
            self._set_catalogue_loading(False)
            self._set_catalogue_open(False)
            self.statusBar().showMessage("Catalogue opening cancelled.", 3000)
            return

        self.catalogue_open_path = None
        self._open_catalogue_in_window(
            db,
            path,
            lock,
            initial_volume_items=items,
            connected_volume_snapshots=connected_volume_snapshots,
        )
        self._set_catalogue_loading(False)
        self.statusBar().showMessage(self.catalogue_open_status_message, 4000)

    @Slot(object)
    def on_catalogue_open_failed(self, exc: Exception) -> None:
        self.catalogue_open_path = None
        self._set_catalogue_loading(False)
        self._set_catalogue_open(False)
        self._show_catalogue_error("Open Catalogue Failed", exc)

    @Slot()
    def on_catalogue_open_cancelled(self) -> None:
        self.catalogue_open_path = None
        self._set_catalogue_loading(False)
        self._set_catalogue_open(False)
        self.statusBar().showMessage("Catalogue opening cancelled.", 3000)

    @Slot()
    def clear_catalogue_open_worker(self) -> None:
        self.catalogue_open_worker = None
        self.catalogue_open_thread = None
        self.catalogue_open_cancel_requested = False
        self._set_catalogue_loading(False)

    def _catalogue_open_in_progress(self) -> bool:
        return (
            getattr(self, "catalogue_probe_process", None) is not None
            or getattr(self, "catalogue_open_worker", None) is not None
        )

    def _set_catalogue_loading(self, loading: bool, path: Path | None = None) -> None:
        enabled = (
            not loading
            and not self._catalogue_open_in_progress()
            and getattr(self, "catalogue_archive_worker", None) is None
        )
        self.new_catalogue_action.setEnabled(enabled)
        self.open_catalogue_action.setEnabled(enabled)
        self.restore_catalogue_backup_action.setEnabled(enabled)
        self.welcome_new_button.setEnabled(enabled)
        self.welcome_open_button.setEnabled(enabled)
        self.catalogue_loading_cancel_button.setEnabled(
            loading and not self.catalogue_open_cancel_requested
        )
        if loading:
            self.catalogue_loading_path_label.setText(str(path) if path is not None else "")
            self.catalogue_loading_progress.setRange(0, 0)
            self.catalogue_loading_progress.setFormat("Opening and checking catalogue...")
            self.stack.setCurrentWidget(self.loading_page)
            self.statusBar().showMessage("Opening catalogue...")

    def close_catalogue(self, show_status: bool = True) -> bool:
        if not self._stop_catalogue_archive_for_close():
            return False
        if self.db is None:
            self._set_catalogue_open(False)
            return True

        if self.delete_worker is not None:
            QMessageBox.information(
                self,
                "Volume Deleting",
                "Wait for the current volume delete to finish before closing the catalogue.",
            )
            return False

        if not self._stop_backup_analysis_for_close():
            return False
        if not self._stop_catalogue_info_for_close():
            return False
        if not self._stop_scan_for_catalogue_close():
            return False
        if not self._stop_search_for_catalogue_close():
            return False

        db = self.db
        lock = self.catalogue_lock
        self.db = None
        self.catalogue_path = None
        self.catalogue_lock = None
        self.backup_engine = None
        self.backup_volume_references = {}
        if self.backup_evidence_dialog is not None:
            self.backup_evidence_dialog.close()
            self.backup_evidence_dialog = None
        self._set_catalogue_open(False)

        try:
            db.close()
        finally:
            if lock is not None:
                lock.unlock()

        if show_status:
            self.statusBar().showMessage("Catalogue closed.", 4000)
        return True

    def _choose_new_catalogue_path(self) -> Path | None:
        dialog = QFileDialog(self, "New Catalogue")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(CATALOGUE_FILE_FILTER)
        dialog.setDefaultSuffix(CATALOGUE_EXTENSION.lstrip("."))
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.setDirectory(str(self.catalogue_path.parent if self.catalogue_path else Path.home()))

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        selected = dialog.selectedFiles()
        if not selected:
            return None
        path = catalogue_path_with_extension(selected[0])
        if path.exists():
            answer = QMessageBox.question(
                self,
                "Overwrite Catalogue",
                f"Replace the existing catalogue file?\n\n{path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
        return path

    def _acquire_catalogue_lock(self, path: Path) -> QLockFile:
        return acquire_catalogue_lock(path)

    def _open_catalogue_in_window(
        self,
        db: Database,
        path: Path,
        lock: QLockFile,
        *,
        initial_volume_items: list[VolumeItem] | None = None,
        connected_volume_snapshots: list[VolumeSnapshot] | None = None,
    ) -> None:
        self.db = db
        if BackupAnalysisEngine is not None:
            try:
                self.backup_engine = BackupAnalysisEngine(db)
                self.backup_engine.ensure_schema()
            except Exception:
                self.backup_engine = None
        self.catalogue_path = path
        self.catalogue_lock = lock
        self.settings.setValue(LAST_CATALOGUE_PATH_SETTING, str(path.resolve(strict=False)))
        self._set_catalogue_open(True)
        self.start_connected_volume_monitor(connected_volume_snapshots)
        if initial_volume_items is None or self.backup_engine is not None:
            self.refresh_volumes()
        else:
            self._apply_volume_items(initial_volume_items)

    def current_connected_volume_signature(self) -> tuple[tuple[str, str, str], ...]:
        return connected_volume_signature(list_connected_volume_snapshots())

    def start_connected_volume_monitor(
        self,
        connected_volume_snapshots: list[VolumeSnapshot] | None = None,
    ) -> None:
        self._connected_volume_snapshots = (
            list_connected_volume_snapshots()
            if connected_volume_snapshots is None
            else connected_volume_snapshots
        )
        self._connected_volume_signature = connected_volume_signature(
            self._connected_volume_snapshots
        )
        self.volume_connection_refresh_timer.stop()
        self.volume_connection_timer.start()

    def stop_connected_volume_monitor(self) -> None:
        self.volume_connection_timer.stop()
        self.volume_connection_refresh_timer.stop()
        self._connected_volume_snapshots = []
        self._connected_volume_signature = ()

    @Slot()
    def check_connected_volumes(self) -> None:
        if self.db is None:
            return
        snapshots = list_connected_volume_snapshots()
        signature = connected_volume_signature(snapshots)
        self._connected_volume_snapshots = snapshots
        if signature == self._connected_volume_signature:
            return
        self._connected_volume_signature = signature
        self.volume_connection_refresh_timer.start()

    @Slot()
    def refresh_after_connected_volumes_changed(self) -> None:
        if self.db is None:
            return
        self.refresh_volumes()
        if self.search_edit.text().strip():
            self.perform_search()
        else:
            self.on_search_selection_changed()
        self.statusBar().showMessage("Connected volumes updated.", 3000)

    def _stop_catalogue_archive_for_close(self) -> bool:
        worker = getattr(self, "catalogue_archive_worker", None)
        thread = getattr(self, "catalogue_archive_thread", None)
        if worker is None or thread is None or not thread.isRunning():
            return True
        operation = (
            "restore"
            if getattr(self, "catalogue_archive_operation", "") == "restore"
            else "backup"
        )
        answer = QMessageBox.question(
            self,
            f"Catalogue {operation.title()} Running",
            f"A catalogue {operation} is still running. Cancel it before closing?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        worker.cancel()
        self.scan_progress.setFormat("Cancelling…")
        self.statusBar().showMessage(f"Cancelling catalogue {operation}…")
        for _ in range(50):
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
            if self.catalogue_archive_thread is None or not self.catalogue_archive_thread.isRunning():
                return True
            self.catalogue_archive_thread.wait(100)
        QMessageBox.information(
            self,
            f"Catalogue {operation.title()} Cancelling",
            "Cancellation has been requested. Close the application after the operation stops.",
        )
        return False

    def _stop_scan_for_catalogue_close(self) -> bool:
        if self.scan_worker is None:
            return True

        answer = QMessageBox.question(
            self,
            "Scan Running",
            "A scan is still running. Cancel it before closing the catalogue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False

        self.cancel_scan()

        for _ in range(50):
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
            if self.scan_thread is None or not self.scan_thread.isRunning():
                return True
            self.scan_thread.wait(100)

        QMessageBox.information(
            self,
            "Scan Cancelling",
            "Cancellation has been requested. Close the catalogue after the scan stops.",
        )
        return False

    def _stop_search_for_catalogue_close(self) -> bool:
        self.pending_search_request = None
        self.search_request_id += 1
        if (
            self.search_worker is None
            or self.search_thread is None
            or not self.search_thread.isRunning()
        ):
            return True

        self.search_worker.cancel()
        self.statusBar().showMessage("Cancelling search...")

        for _ in range(50):
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
            if self.search_thread is None or not self.search_thread.isRunning():
                return True
            self.search_thread.wait(100)

        QMessageBox.information(
            self,
            "Search Cancelling",
            "Cancellation has been requested. Close the catalogue after the search stops.",
        )
        return False

    def _stop_catalogue_info_for_close(self) -> bool:
        if (
            self.catalogue_info_worker is None
            or self.catalogue_info_thread is None
            or not self.catalogue_info_thread.isRunning()
        ):
            return True

        self.catalogue_info_worker.cancel()
        self.scan_progress.setFormat("Cancelling...")
        self.statusBar().showMessage("Cancelling catalogue info...")

        for _ in range(50):
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
            if self.catalogue_info_thread is None or not self.catalogue_info_thread.isRunning():
                return True
            self.catalogue_info_thread.wait(100)

        QMessageBox.information(
            self,
            "Catalogue Info Cancelling",
            "Cancellation has been requested. Close the catalogue after loading stops.",
        )
        return False

    def _stop_backup_analysis_for_close(self) -> bool:
        worker = getattr(self, "backup_analysis_worker", None)
        thread = getattr(self, "backup_analysis_thread", None)
        if worker is None or thread is None or not thread.isRunning():
            return True

        answer = QMessageBox.question(
            self,
            "Backup Analysis Running",
            "Saved catalogue evidence is still being analysed. Cancel it before closing?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        worker.cancel()
        self.statusBar().showMessage("Cancelling backup analysis…")
        for _ in range(50):
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
            if self.backup_analysis_thread is None or not self.backup_analysis_thread.isRunning():
                return True
            self.backup_analysis_thread.wait(100)
        QMessageBox.information(
            self,
            "Backup Analysis Cancelling",
            "Cancellation has been requested. Close the catalogue after analysis stops.",
        )
        return False

    def _catalogue_job_running(self) -> bool:
        return (
            self.scan_worker is not None
            or self.delete_worker is not None
            or self.catalogue_info_worker is not None
            or getattr(self, "catalogue_archive_worker", None) is not None
            or getattr(self, "backup_analysis_worker", None) is not None
        )

    def _show_catalogue_job_running_message(self) -> None:
        if self.delete_worker is not None:
            QMessageBox.information(
                self,
                "Volume Deleting",
                "Wait for the current volume delete to finish.",
            )
        elif self.catalogue_info_worker is not None:
            QMessageBox.information(
                self,
                "Catalogue Info Loading",
                "Wait for catalogue information to finish loading.",
            )
        elif getattr(self, "catalogue_archive_worker", None) is not None:
            operation = getattr(self, "catalogue_archive_operation", "backup")
            QMessageBox.information(
                self,
                f"Catalogue {operation.title()} Running",
                f"Wait for the catalogue {operation} to finish before starting another job.",
            )
        elif getattr(self, "backup_analysis_worker", None) is not None:
            QMessageBox.information(
                self,
                "Backup Analysis Running",
                "Wait for the saved-evidence analysis to finish or cancel it in Backup Evidence.",
            )
        else:
            QMessageBox.information(
                self,
                "Scan Running",
                "Wait for the current scan to finish or cancel it.",
            )

    def _set_catalogue_busy(self, busy: bool) -> None:
        enabled = self.db is not None and not busy
        for action in self.catalogue_actions:
            action.setEnabled(enabled)
        for widget in self.catalogue_widgets:
            widget.setEnabled(enabled)
        for shortcut in self.browser_shortcuts:
            shortcut.setEnabled(enabled)
        if hasattr(self, "restore_catalogue_backup_action"):
            self.restore_catalogue_backup_action.setEnabled(
                not busy
                and not self._catalogue_open_in_progress()
                and getattr(self, "catalogue_archive_worker", None) is None
            )

        if hasattr(self, "volume_table"):
            self.volume_table.setEnabled(enabled)
        if hasattr(self, "tabs"):
            self.tabs.setEnabled(enabled)

    def _set_scan_running_ui(self, running: bool) -> None:
        enabled = self.db is not None and not running
        for action in self.scan_blocked_actions:
            action.setEnabled(enabled)
        for widget in self.scan_blocked_widgets:
            widget.setEnabled(enabled)
        if hasattr(self, "stop_scan_button"):
            self.stop_scan_button.setEnabled(running and not self.scan_cancel_requested)

    def _set_catalogue_open(self, is_open: bool) -> None:
        if hasattr(self, "stack"):
            self.stack.setCurrentWidget(self.catalogue_page if is_open else self.welcome_page)
        self.close_catalogue_action.setEnabled(is_open)
        for action in self.catalogue_actions:
            action.setEnabled(is_open)
        for widget in self.catalogue_widgets:
            widget.setEnabled(is_open)
        for shortcut in self.browser_shortcuts:
            shortcut.setEnabled(is_open)

        if not is_open:
            self.stop_connected_volume_monitor()
            self._clear_catalogue_views()
        self._update_window_title()

    def _clear_catalogue_views(self) -> None:
        self.current_volume_id = None
        self.current_folder_id = None
        self.post_scan_edit_volume_id = None
        self.current_directory_items = []
        self.backup_volume_summaries = {}
        self.backup_scan_records = {}
        self.backup_analysis_state = None
        self.volume_model.set_items([])
        self.browser_model.set_items([])
        self.search_model.set_items([])
        self.set_search_empty_state(False)
        self.volume_filter_edit.clear()
        self.search_edit.clear()
        self.scan_log.clear()
        self.show_volume_details(None)
        self.clear_browser()
        self.on_search_selection_changed()
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Idle")
        self.statusBar().clearMessage()

    def _update_window_title(self) -> None:
        if self.catalogue_path is None:
            self.setWindowTitle(APP_NAME)
        else:
            self.setWindowTitle(f"{APP_NAME} - {self.catalogue_path.name}")

    def _show_catalogue_error(self, title: str, exc: Exception) -> None:
        message = str(exc) or "The catalogue could not be opened."
        details = format_exception_diagnostics(exc)
        dialog = QMessageBox(self)
        if isinstance(exc, CatalogueInUseError):
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Catalogue In Use")
        else:
            dialog.setIcon(QMessageBox.Icon.Critical)
            dialog.setWindowTitle(title)
        dialog.setText(message)
        dialog.setInformativeText("Select Show Details… for diagnostic information.")
        dialog.setDetailedText(details)
        print(f"{title}: {message}\n\n{details}", file=sys.stderr)
        dialog.exec()

    def refresh_volumes(self) -> None:
        if self.db is None:
            self._clear_catalogue_views()
            return
        volumes = self.db.list_volumes()
        self.backup_volume_references = {
            int(volume["id"]): volume_reference(volume["drive_id"], volume["name"])
            for volume in volumes
        }
        summaries: dict[int, Any] = {}
        scans: dict[int, Any] = {}
        analysis_state: Any = None
        engine = getattr(self, "backup_engine", None)
        if engine is not None:
            try:
                analysis_state = engine.state()
                summaries = {
                    int(object_value(summary, "volume_id")): summary
                    for summary in engine.volume_summaries()
                }
                scans = {
                    int(object_value(scan, "volume_id")): scan
                    for scan in engine.scan_records()
                }
            except Exception:
                summaries = {}
                scans = {}
                analysis_state = None
        self.backup_volume_summaries = summaries
        self.backup_scan_records = scans
        self.backup_analysis_state = analysis_state
        resolver = ConnectedVolumeResolver(
            self._connected_volume_snapshots,
            check_source_path=False,
        )
        all_items = [
            volume_item_from_record(
                volume,
                self.current_source_path_for_volume(volume, resolver) is not None,
                summaries.get(int(volume["id"])),
                scans.get(int(volume["id"])),
                analysis_state,
            )
            for volume in volumes
        ]
        self._apply_volume_items(all_items)

    def _apply_volume_items(self, all_items: list[VolumeItem]) -> None:
        selected_id = self.selected_volume_id() or self.current_volume_id
        filter_text = self.volume_filter_edit.text() if hasattr(self, "volume_filter_edit") else ""
        items = [item for item in all_items if volume_matches_filter(item, filter_text)]
        self.volume_model.set_items(items)

        visible_ids = {item.id for item in self.volume_model.items}
        if selected_id in visible_ids and self.select_volume(selected_id):
            self.show_selected_volume(selected_id)
        else:
            self.clear_volume_selection()

    def selected_volume_id(self) -> int | None:
        item = self.volume_model.item_at(self.volume_table.currentIndex())
        return item.id if item is not None else None

    def selected_volume(self):
        if self.db is None:
            return None
        volume_id = self.selected_volume_id()
        return self.db.get_volume(volume_id) if volume_id is not None else None

    def current_source_path_for_volume(self, volume, resolver: ConnectedVolumeResolver | None = None) -> str | None:
        if volume is None:
            return None
        if resolver is not None:
            return resolver.resolve(volume)
        return resolve_volume_source_path(volume)

    def show_volume_context_menu(self, point: QPoint) -> None:
        if self.db is None:
            return
        index = self.volume_table.indexAt(point)
        menu = QMenu(self)
        busy = self._catalogue_job_running()

        if not index.isValid():
            new_action = menu.addAction("New Volume")
            new_action.triggered.connect(self.add_volume)
            new_action.setEnabled(not busy)
            menu.exec(self.volume_table.viewport().mapToGlobal(point))
            return

        self.volume_table.selectRow(index.row())
        self.volume_table.setCurrentIndex(self.volume_model.index(index.row(), 0))
        scan_running = self.scan_worker is not None

        new_action = menu.addAction("New Volume")
        new_action.triggered.connect(self.add_volume)
        new_action.setEnabled(not busy)
        menu.addSeparator()

        edit_action = menu.addAction("Edit Volume")
        edit_action.triggered.connect(self.edit_volume)
        edit_action.setEnabled(not busy)

        delete_action = menu.addAction("Delete Volume")
        delete_action.triggered.connect(self.delete_volume)
        delete_action.setEnabled(not busy)

        menu.addSeparator()
        scan_action = menu.addAction("Scan")
        scan_action.triggered.connect(lambda: self.start_scan())
        scan_action.setEnabled(not busy)

        cancel_action = menu.addAction("Cancel Scan")
        cancel_action.triggered.connect(self.cancel_scan)
        cancel_action.setEnabled(scan_running)

        menu.exec(self.volume_table.viewport().mapToGlobal(point))

    def on_volume_selection_changed(self, selected=None, deselected=None) -> None:
        if self.db is None:
            return
        volume_id = self.selected_volume_id()
        self.show_selected_volume(volume_id)

    def clear_volume_selection(self) -> None:
        selection_model = self.volume_table.selectionModel()
        if selection_model is not None:
            selection_model.setCurrentIndex(QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate)
            selection_model.clearSelection()
        else:
            self.volume_table.setCurrentIndex(QModelIndex())
        self.show_selected_volume(None)

    def show_selected_volume(self, volume_id: int | None) -> None:
        self.current_volume_id = volume_id
        self.clear_browser()
        # Resolving a volume and loading its root directory can block the UI.
        # Paint the empty model now so rows from the previous volume are never
        # displayed alongside the newly selected volume's details.
        self.folder_tree.viewport().repaint()
        self.file_table.viewport().repaint()
        volume = self.db.get_volume(volume_id) if self.db is not None and volume_id is not None else None
        self.show_volume_details(volume)
        self.load_volume_browser(volume_id)
        self.load_scan_log(volume_id)

    def show_volume_details(self, volume) -> None:
        if volume is None:
            for widget in self.detail_labels.values():
                widget.setText("-")
            self.detail_full.setValue(0)
            return

        current_source_path = self.current_source_path_for_volume(volume)
        connected = current_source_path is not None
        full = percentage_full(volume["used_bytes"], volume["capacity_bytes"])
        volume_id = int(volume["id"])
        backup = volume_backup_display(
            getattr(self, "backup_volume_summaries", {}).get(volume_id),
            int(volume["indexed_file_count"] or 0),
            getattr(self, "backup_scan_records", {}).get(volume_id),
            getattr(self, "backup_analysis_state", None),
        )
        values = {
            "drive_id": volume["drive_id"] or "-",
            "name": display_volume_name(volume["name"]),
            "path": current_source_path or volume["source_path"] or "-",
            "connection": "Connected" if connected else "Offline",
            "register_status": volume["register_status"],
            "condition": volume["condition"],
            "last_scan": self._display_time(volume["last_scan_at"]),
            "backup_coverage": backup.text,
        }
        for key, value in values.items():
            self.detail_labels[key].setText(value)
        self.detail_labels["backup_coverage"].setToolTip(backup.tooltip)
        self.detail_full.setValue(full)

    def add_volume(self) -> None:
        if self.db is None:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        source_path = self.choose_new_volume_location()
        if source_path is None:
            return
        try:
            snapshot = capture_volume_snapshot(source_path) if source_path else None
            if snapshot is not None:
                source_path = snapshot.source_path
            drive_id = self.choose_new_volume_drive_id(source_path, snapshot)
            if drive_id is None:
                return
            refreshed_snapshot = capture_volume_snapshot(source_path) if source_path else None
            if refreshed_snapshot is not None:
                snapshot = refreshed_snapshot
            volume_id = self.db.create_volume(
                "",
                source_path,
                {"drive_id": drive_id},
                location=snapshot.as_db_fields() if snapshot is not None else None,
            )
            self.current_volume_id = volume_id
            self.refresh_volumes()
            self.start_scan(
                volume_id=volume_id,
                source_path=source_path,
                edit_after_success=True,
            )
        except Exception as exc:
            QMessageBox.critical(self, "New Volume Failed", str(exc))

    def choose_new_volume_location(self) -> str | None:
        directory = QFileDialog.getExistingDirectory(self, "Choose Volume to Scan", str(Path.home()))
        return directory or None

    def choose_new_volume_drive_id(self, source_path: str, snapshot: VolumeSnapshot | None) -> str | None:
        if self.db is None:
            return None
        volume_label = snapshot.identity_label if snapshot is not None else ""
        suggested_drive_id = suggested_new_volume_drive_id(volume_label, self.db.next_drive_id())
        dialog = DriveIdDialog(
            self,
            suggested_drive_id=suggested_drive_id,
            source_path=source_path,
            volume_label=volume_label,
            existing_volumes=self.db.list_volumes(),
            allow_volume_label_rename=snapshot is not None and sys.platform == "win32",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        drive_id = dialog.value()
        target_volume_label = (
            drive_id
            if dialog.should_rename_volume_label()
            else dialog.volume_label_value()
        )
        if snapshot is not None and target_volume_label != volume_label:
            rename_volume_label(source_path, target_volume_label)
        return drive_id

    def edit_volume(self) -> None:
        if self.db is None:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        volume = self.selected_volume()
        if volume is None:
            return
        self.edit_volume_record(volume)

    def edit_volume_record(self, volume) -> None:
        if self.db is None:
            return
        dialog = VolumeDialog(
            self,
            "Edit Volume",
            volume=volume,
            master_options=self.db.list_master_volume_options(volume["id"]),
            mirror_dependents=self.db.list_mirror_dependents(volume["id"]),
            existing_volumes=self.db.list_volumes(),
            show_source_path=False,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source_path, register = dialog.values()
        try:
            self.db.update_volume(volume["id"], name, source_path, register)
            self.refresh_volumes()
            self.statusBar().showMessage("Volume updated.", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Edit Volume Failed", str(exc))

    def edit_volume_by_id(self, volume_id: int) -> None:
        if self.db is None:
            return
        volume = self.db.get_volume(volume_id)
        if volume is None:
            return
        self.select_volume(volume_id)
        self.edit_volume_record(volume)

    def edit_volume_index(self, index: QModelIndex) -> None:
        if self.db is None or not index.isValid():
            return
        item = self.volume_model.item_at(index)
        if item is None:
            return
        if self.select_volume(item.id):
            self.edit_volume()

    def delete_volume(self) -> None:
        if self.db is None:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        volume = self.selected_volume()
        if volume is None:
            return
        dependents = self.db.list_mirror_dependents(volume["id"])
        if dependents:
            names = "\n".join(f"- {volume_reference(row['drive_id'], row['name'])}" for row in dependents)
            QMessageBox.warning(
                self,
                "Cannot Delete Master Drive",
                "This volume is selected as the master drive for:\n\n"
                f"{names}\n\nRemove those mirror relationships before deleting it.",
            )
            return
        display_name = volume["drive_id"] or volume["name"]
        answer = QMessageBox.question(
            self,
            "Delete Volume",
            f"Delete {display_name}?\n\nThis will delete the volume and all indexed records.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.start_delete_volume(volume["id"], display_name or "volume")

    def start_delete_volume(self, volume_id: int, display_name: str) -> None:
        if self.db is None:
            return

        self._set_catalogue_busy(True)
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat(f"Deleting {display_name}...")
        self.statusBar().showMessage(f"Deleting {display_name}...")

        self.delete_thread = QThread(self)
        self.delete_worker = DeleteVolumeWorker(self.db.path, volume_id)
        self.delete_worker.moveToThread(self.delete_thread)
        self.delete_thread.started.connect(self.delete_worker.run)
        self.delete_worker.progress.connect(self.on_delete_progress)
        self.delete_worker.finished.connect(self.on_delete_finished)
        self.delete_worker.failed.connect(self.on_delete_failed)
        self.delete_worker.finished.connect(self.delete_thread.quit)
        self.delete_worker.failed.connect(self.delete_thread.quit)
        self.delete_worker.finished.connect(self.delete_worker.deleteLater)
        self.delete_worker.failed.connect(self.delete_worker.deleteLater)
        self.delete_thread.finished.connect(self.delete_thread.deleteLater)
        self.delete_thread.finished.connect(self.clear_delete_worker)
        self.delete_thread.start()

    def start_scan(
        self,
        volume_id: int | None = None,
        source_path: str | None = None,
        edit_after_success: bool = False,
    ) -> None:
        if self.db is None:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        volume = self.db.get_volume(volume_id) if volume_id is not None else self.selected_volume()
        if volume is None:
            return

        if source_path is None:
            source_path = self.choose_scan_location(volume)
            if source_path is None:
                return
        try:
            snapshot = capture_volume_snapshot(source_path)
            if snapshot is not None:
                source_path = snapshot.source_path
            self.db.update_volume_location(
                volume["id"],
                source_path,
                snapshot.as_db_fields() if snapshot is not None else None,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Scan Location Failed", str(exc))
            return

        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Starting scan · SHA-256 hashing enabled...")
        self.scan_progress.setToolTip(
            "Scans read every regular file to record a full SHA-256 content hash. "
            "Media details are collected when supported."
        )
        self.statusBar().showMessage("Scanning and hashing file contents...")
        self.scan_cancel_requested = False
        self.post_scan_edit_volume_id = volume["id"] if edit_after_success else None
        self._set_scan_running_ui(True)

        self.scan_thread = QThread(self)
        self.scan_worker = ScanWorker(self.db.path, volume["id"])
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.stats_progress.connect(self.on_scan_stats_progress)
        self.scan_worker.review_requested.connect(self.on_scan_review_requested)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.failed.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_worker.failed.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.finished.connect(self.clear_scan_worker)
        self.scan_thread.start()

    def choose_scan_location(self, volume) -> str | None:
        current_path = self.current_source_path_for_volume(volume) or volume["source_path"] or ""
        initial_dir = current_path if source_path_exists(current_path) else str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "Choose Scan Location", initial_dir)
        return directory or None

    def cancel_scan(self) -> None:
        if self.scan_worker is None or self.scan_cancel_requested:
            return
        self.scan_cancel_requested = True
        self.scan_worker.cancel()
        self.stop_scan_button.setEnabled(False)
        self.scan_progress.setFormat("Cancelling scan...")
        self.statusBar().showMessage("Cancelling scan...")

    @Slot(int, int, str)
    def on_scan_progress(self, files_seen: int, folders_seen: int, current_path: str) -> None:
        if self.scan_cancel_requested:
            return
        if self.scan_progress.maximum() != 0:
            self.scan_progress.setRange(0, 0)
        if current_path.startswith(("Hashing SHA-256", "Reading media details")):
            self.scan_progress.setFormat(
                f"{current_path} · {files_seen:,} files catalogued"
            )
        else:
            self.scan_progress.setFormat(
                f"Scanning... {files_seen:,} files, {folders_seen:,} folders - {current_path}"
            )

    @Slot(dict)
    def on_scan_review_requested(self, changes: dict) -> None:
        worker = self.scan_worker
        if worker is None:
            return
        if self.scan_cancel_requested:
            worker.resolve_review(False)
            return

        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_progress.setFormat("Waiting for confirmation")
        self.statusBar().showMessage("Review the detected catalogue changes.")

        bytes_before = int(changes.get("bytes_before", 0))
        bytes_after = int(changes.get("bytes_after", 0))
        size_delta = bytes_after - bytes_before
        delta_text = format_size(abs(size_delta))
        if size_delta > 0:
            delta_text = f"+{delta_text}"
        elif size_delta < 0:
            delta_text = f"-{delta_text}"

        details = [
            f"Files added: {int(changes.get('files_added', 0)):,} "
            f"(+{format_size(int(changes.get('bytes_added', 0)))})",
            f"Files no longer present: {int(changes.get('files_removed', 0)):,} "
            f"(-{format_size(int(changes.get('bytes_removed', 0)))})",
            f"Files changed: {int(changes.get('files_changed', 0)):,}",
            f"Folders added: {int(changes.get('folders_added', 0)):,}",
            f"Folders no longer present: {int(changes.get('folders_removed', 0)):,}",
            "",
            f"Indexed size: {format_size(bytes_before)} → {format_size(bytes_after)} "
            f"({delta_text})",
        ]
        errors_count = int(changes.get("errors_count", 0))
        hash_errors = int(changes.get("hash_errors", 0))
        access_errors = max(0, errors_count - hash_errors)
        if access_errors:
            details.extend(
                [
                    "",
                    f"Warning: the scan reported {access_errors:,} access or file-stability "
                    "issues. Some items listed as no longer present may have been "
                    "inaccessible or changing during the scan.",
                ]
            )
        if hash_errors:
            details.extend(
                [
                    "",
                    f"SHA-256 unavailable for {hash_errors:,} files. Applying this update "
                    "will clear any older hash for those records; they will use clearly "
                    "labelled metadata-only evidence until a later successful rescan.",
                ]
            )

        box = QMessageBox(self)
        box.setWindowTitle("Review Scan Changes")
        box.setIcon(
            QMessageBox.Icon.Warning
            if access_errors or hash_errors
            else QMessageBox.Icon.Question
        )
        box.setText("Apply these changes to the catalogue?")
        box.setInformativeText("\n".join(details))
        apply_button = box.addButton("Apply Update", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(
            cancel_button if access_errors or hash_errors else apply_button
        )
        box.setEscapeButton(cancel_button)

        apply_changes = False
        try:
            box.exec()
            apply_changes = box.clickedButton() == apply_button
        finally:
            worker.resolve_review(apply_changes)

    @Slot(int, int, str, int, int)
    def on_scan_stats_progress(
        self,
        files_seen: int,
        folders_seen: int,
        message: str,
        done: int,
        total: int,
    ) -> None:
        if self.scan_cancel_requested:
            return
        label = self.scan_stats_progress_label(message)
        if total > 0:
            value = min(max(done, 0), total)
            if self.scan_progress.minimum() != 0 or self.scan_progress.maximum() != total:
                self.scan_progress.setRange(0, total)
            self.scan_progress.setValue(value)
            self.scan_progress.setFormat(f"{label}: %p% ({value:,}/{total:,} folders)")
        else:
            if self.scan_progress.maximum() != 0:
                self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(f"{label}...")
        self.statusBar().showMessage(
            f"Finalizing scan: {files_seen:,} files, {folders_seen:,} folders."
        )

    def scan_stats_progress_label(self, message: str) -> str:
        labels = {
            "Preparing folder statistics": "Preparing folder sizes",
            "Calculating folder statistics": "Calculating folder sizes",
            "Folder statistics updated": "Folder sizes calculated",
        }
        return labels.get(message, message)

    @Slot(dict)
    def on_scan_finished(self, result: dict) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        status = result.get("status", "completed")
        if status != "completed":
            self.post_scan_edit_volume_id = None
        self.scan_progress.setFormat(status.title())
        hash_errors = int(result.get("hash_errors", 0))
        access_errors = max(0, int(result.get("errors_count", 0)) - hash_errors)
        self.statusBar().showMessage(
            f"Scan {status}: {result.get('files_seen', 0)} files, "
            f"{result.get('files_hashed', 0)} SHA-256 hashes "
            f"({format_size(int(result.get('bytes_hashed', 0)))} read), "
            f"{result.get('media_metadata_collected', 0)}/"
            f"{result.get('media_files', 0)} media probes succeeded, "
            f"{access_errors} incomplete/access issues, {hash_errors} hash unavailable.",
            8000,
        )
        self.refresh_after_catalogue_write()

    @Slot(str)
    def on_scan_failed(self, details: str) -> None:
        self.post_scan_edit_volume_id = None
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Scan failed")
        QMessageBox.critical(self, "Scan Failed", details)
        self.refresh_after_catalogue_write()

    @Slot()
    def clear_scan_worker(self) -> None:
        self.scan_worker = None
        self.scan_thread = None
        self.scan_cancel_requested = False
        self._set_scan_running_ui(False)
        if self.backup_evidence_dialog is not None:
            self.refresh_backup_evidence_dialog()
        if self.post_scan_edit_volume_id is not None:
            volume_id = self.post_scan_edit_volume_id
            self.post_scan_edit_volume_id = None
            QTimer.singleShot(0, lambda volume_id=volume_id: self.edit_volume_by_id(volume_id))

    @Slot(str)
    def on_delete_progress(self, message: str) -> None:
        self.scan_progress.setFormat(message)

    @Slot(int)
    def on_delete_finished(self, volume_id: int) -> None:
        self.current_volume_id = None
        self.refresh_after_catalogue_write()
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        self.scan_progress.setFormat("Deleted")
        self.statusBar().showMessage("Volume deleted.", 4000)

    @Slot(str)
    def on_delete_failed(self, details: str) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Delete failed")
        QMessageBox.critical(self, "Delete Volume Failed", details)

    @Slot()
    def clear_delete_worker(self) -> None:
        self.delete_worker = None
        self.delete_thread = None
        self._set_catalogue_busy(False)
        if self.backup_evidence_dialog is not None:
            self.refresh_backup_evidence_dialog()

    def refresh_after_catalogue_write(self) -> None:
        if self.db is None:
            return
        # The scan/delete worker commits through a separate SQLite connection.
        # The next read on this connection sees that commit; reopening here is
        # unnecessary and runs catalogue validation (including quick_check) on
        # the GUI thread, which can make a large catalogue appear to hang just
        # as a scan finishes.
        self.refresh_volumes()
        self.perform_search()
        if getattr(self, "backup_evidence_dialog", None) is not None:
            self.refresh_backup_evidence_dialog()

    def select_volume(self, volume_id: int) -> bool:
        for row, item in enumerate(self.volume_model.items):
            if item.id == volume_id:
                index = self.volume_model.index(row, 0)
                self.volume_table.selectRow(row)
                self.volume_table.setCurrentIndex(index)
                self.volume_table.scrollTo(index)
                return True
        return False

    def load_volume_browser(self, volume_id: int | None) -> None:
        self.clear_browser()
        if self.db is None or volume_id is None:
            return
        volume = self.db.get_volume(volume_id)
        if volume is None:
            return
        connected = self.current_source_path_for_volume(volume) is not None
        self.set_browser_notice(
            "" if connected else "This volume is offline. Showing the saved catalogue."
        )
        root = self.db.get_root_folder(volume_id)
        if root is None:
            self.set_browser_notice("No scan data available for this volume.")
            return
        root_item = self._folder_tree_item(root)
        self.folder_tree.addTopLevelItem(root_item)
        self.add_placeholder_if_needed(root_item)
        self.folder_tree.setCurrentItem(root_item)
        root_item.setExpanded(True)

    def clear_browser(self) -> None:
        self.set_browser_notice("")
        self.folder_tree.clear()
        self.current_directory_items = []
        self.browser_model.set_items([])
        self.current_folder_id = None
        self.current_path_label.setText("/")
        self.up_button.setEnabled(False)

    def set_browser_notice(self, message: str) -> None:
        self.offline_label.setText(message)
        self.offline_label.setVisible(bool(message))

    def _folder_tree_item(self, folder) -> QTreeWidgetItem:
        name = folder["name"] or "/"
        if folder["missing"]:
            name = f"{name} (missing)"
        item = QTreeWidgetItem([name])
        item.setIcon(0, self.browser_icons.folder_icon)
        item.setData(0, ROLE_FOLDER_ID, folder["id"])
        item.setData(0, ROLE_RELATIVE_PATH, folder["relative_path"])
        return item

    def add_placeholder_if_needed(self, item: QTreeWidgetItem) -> None:
        folder_id = item.data(0, ROLE_FOLDER_ID)
        if self.db is None or self.current_volume_id is None or folder_id is None:
            return
        if self.db.list_child_folders(self.current_volume_id, int(folder_id)):
            placeholder = QTreeWidgetItem([""])
            placeholder.setData(0, ROLE_FOLDER_ID, -1)
            item.addChild(placeholder)

    def load_tree_children(self, item: QTreeWidgetItem) -> None:
        if self.db is None or self.current_volume_id is None:
            return
        if item.childCount() == 1 and item.child(0).data(0, ROLE_FOLDER_ID) == -1:
            item.takeChild(0)
        elif item.childCount() > 0:
            return
        folder_id = item.data(0, ROLE_FOLDER_ID)
        if folder_id is None or int(folder_id) < 0:
            return
        for folder in self.db.list_child_folders(self.current_volume_id, int(folder_id)):
            child = self._folder_tree_item(folder)
            item.addChild(child)
            self.add_placeholder_if_needed(child)

    def on_folder_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        if self.db is None or current is None or self.current_volume_id is None:
            return
        folder_id = current.data(0, ROLE_FOLDER_ID)
        if folder_id is None or int(folder_id) < 0:
            return
        self.current_folder_id = int(folder_id)
        self.load_directory_items(self.current_volume_id, self.current_folder_id)

    def load_directory_items(self, volume_id: int, folder_id: int) -> None:
        if self.db is None:
            return
        folder = self.db.get_folder(folder_id)
        if folder is None:
            self.current_directory_items = []
            self.browser_model.set_items([])
            self.current_path_label.setText("/")
            self.up_button.setEnabled(False)
            return

        items: list[BrowserItem] = []
        if folder["parent_id"] is not None:
            parent = self.db.get_folder(folder["parent_id"])
            if parent is not None:
                items.append(
                    BrowserItem(
                        item_type="folder",
                        item_id=parent["id"],
                        name="..",
                        relative_path=parent["relative_path"],
                        type_label="Folder",
                        size_bytes=parent["recursive_size_bytes"],
                        modified_at=parent["modified_at"],
                        missing=bool(parent["missing"]),
                        parent_id=parent["parent_id"],
                        is_parent_entry=True,
                    )
                )

        child_folders = self.db.list_child_folders(volume_id, folder_id)
        files = self.db.list_files(volume_id, folder_id)
        folder_statuses: dict[int, Any] = {}
        file_statuses: dict[int, Any] = {}
        engine = getattr(self, "backup_engine", None)
        if engine is not None:
            try:
                folder_statuses = engine.item_statuses(
                    "folder",
                    [int(child["id"]) for child in child_folders],
                )
                file_statuses = engine.item_statuses(
                    "file",
                    [int(file_row["id"]) for file_row in files],
                )
            except Exception:
                folder_statuses = {}
                file_statuses = {}

        volume_references = getattr(self, "backup_volume_references", {})
        for child in child_folders:
            items.append(
                BrowserItem(
                    item_type="folder",
                    item_id=child["id"],
                    name=child["name"],
                    relative_path=child["relative_path"],
                    type_label="Folder",
                    size_bytes=child["recursive_size_bytes"],
                    modified_at=child["modified_at"],
                    missing=bool(child["missing"]),
                    parent_id=child["parent_id"],
                    backup=item_backup_display(
                        folder_statuses.get(int(child["id"])),
                        volume_references,
                        item_type="folder",
                    ),
                )
            )

        for file_row in files:
            extension = file_row["extension"] or ""
            items.append(
                BrowserItem(
                    item_type="file",
                    item_id=file_row["id"],
                    name=file_row["name"],
                    relative_path=file_row["relative_path"],
                    type_label=file_type_label(extension),
                    extension=extension,
                    size_bytes=file_row["size_bytes"],
                    modified_at=file_row["modified_at"],
                    missing=bool(file_row["missing"]),
                    parent_id=file_row["folder_id"],
                    backup=item_backup_display(
                        file_statuses.get(int(file_row["id"])),
                        volume_references,
                        item_type="file",
                    ),
                )
            )

        self.current_directory_items = items
        MainWindow.apply_browser_backup_filter(self)
        self.current_path_label.setText(relative_path_for_display(folder["relative_path"]))
        self.up_button.setEnabled(folder["parent_id"] is not None)
        self.apply_table_default_columns(
            self.file_table,
            {1: 165, 2: 140, 3: 95, 4: 155, 5: 280, 6: 90},
            only_if_empty=True,
        )

    def browser_backup_filter_key(self) -> str:
        combo = getattr(self, "browser_backup_filter_combo", None)
        if combo is None:
            return "all"
        return str(combo.currentData() or "all")

    def search_backup_filter_key(self) -> str:
        combo = getattr(self, "search_backup_filter_combo", None)
        if combo is None:
            return "all"
        return str(combo.currentData() or "all")

    def apply_browser_backup_filter(self) -> None:
        items = getattr(self, "current_directory_items", [])
        filter_key = MainWindow.browser_backup_filter_key(self)
        visible = [
            item
            for item in items
            if item.is_parent_entry or backup_filter_matches(item.backup, filter_key)
        ]
        self.browser_model.set_items(visible)

    def selected_browser_item(self) -> BrowserItem | None:
        return self.browser_model.item_at(self.file_table.currentIndex())

    def open_selected_browser_item(self) -> None:
        item = self.selected_browser_item()
        if item is not None:
            self.open_browser_item(item)

    def open_browser_index(self, index: QModelIndex) -> None:
        item = self.browser_model.item_at(index)
        if item is None:
            return
        self.open_browser_item(item)

    def open_browser_item(self, item: BrowserItem) -> None:
        if item.is_folder:
            self.select_folder_path(item.relative_path)
            return
        self.open_real_browser_item(item, reveal=False)

    def navigate_parent_folder(self) -> None:
        if self.db is None or self.current_folder_id is None:
            return
        folder = self.db.get_folder(self.current_folder_id)
        if folder is None or folder["parent_id"] is None:
            return
        parent = self.db.get_folder(folder["parent_id"])
        if parent is None:
            return
        self.select_folder_path(parent["relative_path"])

    def show_browser_context_menu(self, point: QPoint) -> None:
        if self.db is None:
            return
        index = self.file_table.indexAt(point)
        if not index.isValid():
            return

        self.file_table.selectRow(index.row())
        self.file_table.setCurrentIndex(self.browser_model.index(index.row(), 0))
        item = self.browser_model.item_at(index)
        if item is None:
            return

        target = self.catalogue_ref_for_browser_item(item)
        if target is not None:
            self.show_catalogue_item_context_menu(target, self.file_table.viewport(), point)

    def show_folder_tree_context_menu(self, point: QPoint) -> None:
        if self.db is None:
            return
        tree_item = self.folder_tree.itemAt(point)
        if tree_item is None:
            return
        folder_id = tree_item.data(0, ROLE_FOLDER_ID)
        if folder_id is None or int(folder_id) < 0:
            return
        folder = self.db.get_folder(int(folder_id))
        if folder is None:
            return

        target = CatalogueItemRef(
            item_type="folder",
            item_id=int(folder["id"]),
            volume_id=int(folder["volume_id"]),
            relative_path=folder["relative_path"],
            missing=bool(folder["missing"]),
        )
        self.show_catalogue_item_context_menu(target, self.folder_tree.viewport(), point)

    def catalogue_ref_for_browser_item(self, item: BrowserItem) -> CatalogueItemRef | None:
        if self.current_volume_id is None:
            return None
        return CatalogueItemRef(
            item_type=item.item_type,
            item_id=item.item_id,
            volume_id=self.current_volume_id,
            relative_path=item.relative_path,
            missing=item.missing,
        )

    def show_catalogue_item_context_menu(
        self,
        target: CatalogueItemRef,
        viewport: QWidget,
        point: QPoint,
        *,
        include_catalogue_location: bool = False,
    ) -> None:
        menu = self.build_catalogue_item_context_menu(
            target,
            include_catalogue_location=include_catalogue_location,
        )
        menu.exec(viewport.mapToGlobal(point))

    def build_catalogue_item_context_menu(
        self,
        target: CatalogueItemRef,
        *,
        include_catalogue_location: bool = False,
    ) -> QMenu:
        real_path = self.catalogue_item_real_path(target)
        real_available = real_path is not None and real_path.exists() and not target.missing

        menu = QMenu(self)
        open_action = menu.addAction("Open")
        open_action.setEnabled(target.is_folder or real_available)
        open_action.triggered.connect(
            lambda checked=False, target=target: self.open_catalogue_item(target)
        )

        if include_catalogue_location:
            catalogue_action = menu.addAction("View in Catalogue")
            catalogue_action.triggered.connect(
                lambda checked=False, target=target: self.open_catalogue_location_for_item(target)
            )

        manager_action = menu.addAction("Open File Location")
        manager_action.setEnabled(real_available)
        manager_action.triggered.connect(
            lambda checked=False, target=target: self.open_catalogue_item_in_file_manager(target)
        )

        copy_action = menu.addAction("Copy Path")
        copy_action.setEnabled(real_path is not None)
        copy_action.triggered.connect(
            lambda checked=False, target=target: self.copy_catalogue_item_path(target)
        )

        menu.addSeparator()
        properties_action = menu.addAction("Properties")
        properties_action.triggered.connect(
            lambda checked=False, target=target: self.show_browser_item_properties(
                target.item_type,
                target.item_id,
            )
        )
        return menu

    def open_catalogue_item(self, target: CatalogueItemRef) -> None:
        if target.is_folder:
            self.open_catalogue_location_for_item(target)
            return
        self.open_real_catalogue_item(target, reveal=False)

    def open_catalogue_location_for_item(self, target: CatalogueItemRef) -> None:
        if self.current_volume_id != target.volume_id:
            if not self.select_volume(target.volume_id):
                self.volume_filter_edit.clear()
                if not self.select_volume(target.volume_id):
                    return
        self.tabs.setCurrentWidget(self.browser_tab)

        folder_path = (
            target.relative_path
            if target.is_folder
            else self.parent_catalogue_path(target.relative_path)
        )
        self.select_folder_path(folder_path)
        if not target.is_folder:
            QTimer.singleShot(
                0,
                lambda path=target.relative_path: self.select_browser_relative_path(path, focus=True),
            )

    def open_catalogue_item_in_file_manager(self, target: CatalogueItemRef) -> None:
        self.open_real_catalogue_item(target, reveal=not target.is_folder)

    def open_real_catalogue_item(self, target: CatalogueItemRef, reveal: bool) -> None:
        real_path = self.catalogue_item_real_path(target)
        if real_path is None or not real_path.exists() or target.missing:
            self.statusBar().showMessage(
                "The real item is not available because the volume is offline or changed.",
                5000,
            )
            return
        try:
            open_in_file_manager(real_path, reveal=reveal)
        except Exception as exc:
            QMessageBox.warning(self, "Open Failed", str(exc))

    def copy_catalogue_item_path(self, target: CatalogueItemRef) -> None:
        real_path = self.catalogue_item_real_path(target)
        if real_path is None:
            return
        QApplication.clipboard().setText(str(real_path))
        self.statusBar().showMessage("Path copied.", 3000)

    def catalogue_item_real_path(self, target: CatalogueItemRef) -> Path | None:
        if self.db is None:
            return None
        volume = self.db.get_volume(target.volume_id)
        if volume is None:
            return None
        return self.real_path_for(volume, target.relative_path)

    def show_browser_item_properties(self, item_type: str, item_id: int) -> None:
        if self.db is None:
            return
        record = self.db.get_item_properties(item_type, item_id)
        if record is None:
            QMessageBox.information(
                self,
                "Properties Unavailable",
                "The selected catalogue record is no longer available.",
            )
            return

        name = self.catalogue_item_display_name(record)
        type_label = self.catalogue_item_type_label(record)
        icon = self.browser_icons.icon_for(self.browser_item_from_record(record, type_label))
        dialog = ItemPropertiesDialog(
            self,
            icon,
            name,
            type_label,
            self.catalogue_item_property_rows(record),
        )
        dialog.exec()

    def browser_item_from_record(self, record, type_label: str) -> BrowserItem:
        return BrowserItem(
            item_type=record["item_type"],
            item_id=record["item_id"],
            name=self.catalogue_item_display_name(record),
            relative_path=record["relative_path"],
            type_label=type_label,
            extension=record["extension"] or "",
            size_bytes=record["size_bytes"],
            modified_at=record["modified_at"],
            missing=bool(record["missing"]),
            parent_id=record["parent_id"],
        )

    def catalogue_item_display_name(self, record) -> str:
        return record["name"] or "/"

    def catalogue_item_type_label(self, record) -> str:
        if record["item_type"] == "folder":
            return "Folder"
        extension = (record["extension"] or "").lstrip(".")
        category = file_type_label(extension)
        if not extension:
            return "File"
        if category == extension.upper():
            return f"{extension.upper()} file"
        return f"{extension.upper()} {category.lower()}"

    def catalogue_item_property_rows(self, record) -> list[tuple[str, str]]:
        item_type = record["item_type"]
        record_field = lambda name, default=None: object_value(record, name, default)
        relative_path = record["relative_path"] or ""
        volume = self.db.get_volume(record["volume_id"]) if self.db is not None else None
        source_path = self.current_source_path_for_volume(volume)
        physical_path = self.physical_path_for_source(source_path, relative_path) if source_path else None
        volume_connected = source_path is not None
        item_exists = self.current_item_exists_text(physical_path, volume_connected)

        properties = [
            ("Name", self.catalogue_item_display_name(record)),
            ("Kind", "Folder" if item_type == "folder" else "File"),
            ("Type", self.catalogue_item_type_label(record)),
            ("Volume", display_volume_name(record["volume_name"])),
            ("Relative path", relative_path_for_display(relative_path)),
            ("Full physical path", str(physical_path) if physical_path is not None else "Unavailable"),
            ("Parent folder", self.parent_folder_display(record)),
        ]

        if item_type == "file":
            extension = (record["extension"] or "").lstrip(".")
            raw_hash = record_field("content_hash")
            stored_hash_algorithm = str(
                record_field("content_hash_algorithm") or ""
            ).casefold()
            hash_algorithm = {
                "sha256": "SHA-256",
            }.get(stored_hash_algorithm, stored_hash_algorithm.upper())
            if raw_hash is not None and hash_algorithm:
                hash_text = f"{hash_algorithm} · {bytes(raw_hash).hex()}"
            else:
                hash_text = "Unavailable — rescan this volume to record current file content"
            properties.extend(
                [
                    ("Extension", f".{extension}" if extension else "Unavailable"),
                    ("Size", display_indexed_size(record["size_bytes"])),
                    ("Content hash", hash_text),
                ]
            )
            if extension.casefold() in (IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS):
                media_status = str(record_field("media_status") or "").casefold()
                media_source = str(record_field("media_source") or "")
                source_label = {
                    "ffprobe": "ffprobe",
                    "qt-image": "Qt image header reader",
                    "python-wave": "built-in WAV reader",
                    "ffprobe + python-wave": "ffprobe and built-in WAV reader",
                }.get(media_source, media_source or "no compatible reader")
                media_message = str(record_field("media_message") or "").strip()
                if media_status == "complete":
                    media_summary = f"Collected · {source_label}"
                elif media_status == "partial":
                    media_summary = f"Partially collected · {source_label}"
                    if media_message:
                        media_summary += f" · {media_message}"
                elif media_status in {"unavailable", "failed"}:
                    media_summary = "Not collected"
                    if media_message:
                        media_summary += f" — {media_message}"
                else:
                    media_summary = (
                        "Not collected — this catalogue record predates media inspection; "
                        "rescan the volume"
                    )
                properties.append(("Media details", media_summary))
                if record_field("media_duration_ms") is not None:
                    properties.append(
                        ("Duration", display_duration_ms(record_field("media_duration_ms")))
                    )
                if record_field("media_width") is not None and record_field("media_height") is not None:
                    properties.append(
                        (
                            "Dimensions",
                            f"{int(record_field('media_width')):,} × {int(record_field('media_height')):,}",
                        )
                    )
                if record_field("media_container"):
                    properties.append(("Media container", str(record_field("media_container"))))
                if record_field("video_codecs"):
                    properties.append(("Video codec", str(record_field("video_codecs"))))
                if record_field("audio_codecs"):
                    properties.append(("Audio codec", str(record_field("audio_codecs"))))
                if record_field("media_sample_rate_hz") is not None:
                    properties.append(
                        ("Sample rate", f"{int(record_field('media_sample_rate_hz')):,} Hz")
                    )
                if record_field("media_channels") is not None:
                    properties.append(("Audio channels", str(int(record_field("media_channels")))))
                if record_field("media_bit_rate") is not None:
                    properties.append(
                        ("Bit rate", f"{int(record_field('media_bit_rate')):,} bit/s")
                    )
                if record_field("media_probed_at"):
                    properties.append(
                        ("Media details recorded", self._display_time(record_field("media_probed_at")))
                    )
        else:
            properties.extend(
                [
                    ("Total indexed size", display_indexed_size(record["size_bytes"])),
                    ("Files", self._display_optional_count(record["recursive_file_count"])),
                    ("Subfolders", self._display_optional_count(record["recursive_subfolder_count"])),
                    ("Direct files", self._display_optional_count(record["direct_file_count"])),
                    ("Direct subfolders", self._display_optional_count(record["direct_subfolder_count"])),
                    ("Statistics updated", self._display_unknown_time(record["stats_updated_at"])),
                ]
            )

        properties.extend(
            [
                ("Modified", self._display_time(record["modified_at"])),
                ("Catalogue record ID", f"{item_type}:{record['item_id']}"),
                ("Catalogue status", "Missing" if record["missing"] else "Indexed"),
                ("Volume status", "Connected" if volume_connected else "Disconnected"),
                ("Exists on connected volume", item_exists),
                ("Last recorded by scan", self._display_time(record["scanned_at"])),
            ]
        )
        status = None
        engine = getattr(self, "backup_engine", None)
        if engine is not None:
            try:
                status = (
                    engine.folder_status(int(record["item_id"]))
                    if item_type == "folder"
                    else engine.file_status(int(record["item_id"]))
                )
            except Exception:
                status = None
        display = item_backup_display(
            status,
            getattr(self, "backup_volume_references", {}),
            item_type=item_type,
        )
        other_refs = backup_drive_references(
            object_value(status, "other_volume_ids", ()) if status is not None else (),
            getattr(self, "backup_volume_references", {}),
        )
        stale_status = bool(object_value(status, "is_stale", False))
        properties.extend(
            [
                ("Other-copy status", display.text),
                (
                    backup_evidence_label("Matching catalogue drives", stale_status),
                    ", ".join(other_refs) if other_refs else "None listed",
                ),
                (
                    backup_evidence_label("Match evidence", stale_status),
                    str(object_value(status, "evidence_text", "") or "Unavailable"),
                ),
            ]
        )
        if item_type == "file" and status is not None:
            verified_ids = tuple(object_value(status, "verified_volume_ids", ()) or ())
            verified_id_set = {int(value) for value in verified_ids}
            verified_refs = backup_drive_references(
                verified_ids,
                getattr(self, "backup_volume_references", {}),
            )
            metadata_strong_ids = [
                value
                for value in (object_value(status, "strong_volume_ids", ()) or ())
                if int(value) not in verified_id_set
            ]
            strong_refs = backup_drive_references(
                metadata_strong_ids,
                getattr(self, "backup_volume_references", {}),
            )
            possible_refs = backup_drive_references(
                object_value(status, "possible_volume_ids", ()) or (),
                getattr(self, "backup_volume_references", {}),
            )
            if verified_refs:
                properties.append(
                    (
                        backup_evidence_label("Hash-verified drives", stale_status),
                        ", ".join(verified_refs),
                    )
                )
            if strong_refs:
                properties.append(
                    (
                        backup_evidence_label("Strong metadata-only drives", stale_status),
                        ", ".join(strong_refs),
                    )
                )
            if possible_refs:
                properties.append(
                    (
                        backup_evidence_label("Possible-only drives", stale_status),
                        ", ".join(possible_refs),
                    )
                )
        if item_type == "folder" and status is not None:
            strong_refs = backup_drive_references(
                object_value(status, "strong_volume_ids", ()) or (),
                getattr(self, "backup_volume_references", {}),
            )
            possible_refs = backup_drive_references(
                object_value(status, "possible_volume_ids", ()) or (),
                getattr(self, "backup_volume_references", {}),
            )
            if strong_refs:
                properties.append(
                    (
                        backup_evidence_label("Complete structure drives", stale_status),
                        ", ".join(strong_refs),
                    )
                )
            if possible_refs:
                properties.append(
                    (
                        backup_evidence_label("Possible or partial drives", stale_status),
                        ", ".join(possible_refs),
                    )
                )
            files_percent = object_value(status, "best_coverage_files_percent")
            bytes_percent = object_value(status, "best_coverage_bytes_percent")
            if files_percent is not None or bytes_percent is not None:
                parts = []
                if files_percent is not None:
                    parts.append(f"{float(files_percent):.0f}% of files")
                if bytes_percent is not None:
                    parts.append(f"{float(bytes_percent):.0f}% of bytes")
                properties.append(
                    (
                        backup_evidence_label(
                            "Best single-drive folder coverage", stale_status
                        ),
                        ", ".join(parts),
                    )
                )
        properties.extend(
            [
                (
                    "Backup analysis",
                    self._display_time(object_value(status, "analysed_at"))
                    if status is not None
                    else "Not run",
                ),
                ("Verification", BACKUP_METADATA_DISCLAIMER),
            ]
        )
        return properties

    def parent_folder_display(self, record) -> str:
        if record["parent_id"] is None:
            return "None (volume root)"
        parent_path = record["parent_relative_path"]
        if parent_path is None:
            return "Unavailable"
        return relative_path_for_display(parent_path)

    def current_item_exists_text(self, physical_path: Path | None, volume_connected: bool) -> str:
        if physical_path is None:
            return "Unavailable"
        if not volume_connected:
            return "Unavailable (volume disconnected)"
        return "Yes" if physical_path.exists() else "No"

    def open_real_browser_item(self, item: BrowserItem, reveal: bool) -> None:
        target = self.catalogue_ref_for_browser_item(item)
        if target is not None:
            self.open_real_catalogue_item(target, reveal)

    def copy_selected_browser_path(self) -> None:
        item = self.selected_browser_item()
        if item is not None:
            self.copy_browser_path(item)

    def copy_browser_path(self, item: BrowserItem) -> None:
        target = self.catalogue_ref_for_browser_item(item)
        if target is not None:
            self.copy_catalogue_item_path(target)

    def browser_real_path(self, item: BrowserItem) -> Path | None:
        target = self.catalogue_ref_for_browser_item(item)
        return self.catalogue_item_real_path(target) if target is not None else None

    def real_path_for(self, volume, relative_path: str) -> Path | None:
        source_path = self.current_source_path_for_volume(volume)
        if source_path is None:
            return None
        return self.physical_path_for_source(source_path, relative_path)

    def physical_path_for_source(self, source_path: str, relative_path: str) -> Path:
        path = Path(source_path)
        for part in PurePosixPath(relative_path).parts:
            if part not in {"", "."}:
                path /= part
        return path

    def parent_catalogue_path(self, relative_path: str) -> str:
        parent = PurePosixPath(relative_path).parent
        return "" if str(parent) == "." else parent.as_posix()

    def select_browser_relative_path(self, relative_path: str, focus: bool = False) -> bool:
        for row, item in enumerate(self.browser_model.items):
            if item.relative_path == relative_path:
                index = self.browser_model.index(row, 0)
                self.file_table.selectRow(row)
                self.file_table.setCurrentIndex(index)
                self.file_table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                if focus:
                    self.file_table.setFocus(Qt.FocusReason.OtherFocusReason)
                return True
        return False

    def perform_search(self) -> None:
        self.search_request_id += 1
        request_id = self.search_request_id
        MainWindow.set_search_empty_state(self, False)
        if self.db is None:
            self.pending_search_request = None
            if (
                self.search_worker is not None
                and self.search_thread is not None
                and self.search_thread.isRunning()
            ):
                self.search_worker.cancel()
            self.search_model.set_items([])
            self.on_search_selection_changed()
            self.statusBar().clearMessage()
            return
        query = self.search_edit.text().strip()
        if not query:
            self.pending_search_request = None
            if (
                self.search_worker is not None
                and self.search_thread is not None
                and self.search_thread.isRunning()
            ):
                self.search_worker.cancel()
            self.search_model.set_items([])
            self.on_search_selection_changed()
            self.statusBar().clearMessage()
            return

        request = (
            request_id,
            self.db.path,
            query,
            self.search_include_paths,
        )
        self.search_model.set_items([])
        self.on_search_selection_changed()
        if self.search_thread is not None:
            self.pending_search_request = request
            if self.search_thread.isRunning() and self.search_worker is not None:
                self.search_worker.cancel()
            self.search_button.setText("Searching...")
            self.statusBar().showMessage(f'Searching for "{query}"...')
            return

        self._start_search(request)

    def _start_search(self, request: tuple[int, Path, str, bool]) -> None:
        request_id, db_path, query, include_paths = request
        self.set_search_empty_state(False)
        self.search_button.setText("Searching...")
        self.statusBar().showMessage(f'Searching for "{query}"...')

        self.search_thread = QThread(self)
        self.search_worker = SearchWorker(
            db_path,
            query,
            request_id,
            list(self._connected_volume_snapshots),
            include_paths=include_paths,
            backup_filter_key=MainWindow.search_backup_filter_key(self),
        )
        self.search_worker.moveToThread(self.search_thread)
        self.search_thread.started.connect(self.search_worker.run)
        self.search_worker.batch_ready.connect(self.on_search_batch_ready)
        self.search_worker.finished.connect(self.on_search_finished)
        self.search_worker.cancelled.connect(self.on_search_cancelled)
        self.search_worker.failed.connect(self.on_search_failed)
        self.search_worker.finished.connect(self.search_thread.quit)
        self.search_worker.cancelled.connect(self.search_thread.quit)
        self.search_worker.failed.connect(self.search_thread.quit)
        self.search_worker.finished.connect(self.search_worker.deleteLater)
        self.search_worker.cancelled.connect(self.search_worker.deleteLater)
        self.search_worker.failed.connect(self.search_worker.deleteLater)
        self.search_thread.finished.connect(self.search_thread.deleteLater)
        self.search_thread.finished.connect(self.clear_search_worker)
        self.search_thread.start()

    @Slot(int, list)
    def on_search_batch_ready(
        self,
        request_id: int,
        items: list[SearchResultItem],
    ) -> None:
        if request_id != self.search_request_id or self.db is None:
            return
        self.search_model.append_items(items)
        if items:
            self.set_search_empty_state(False)

    @Slot(int, int)
    def on_search_finished(self, request_id: int, result_count: int) -> None:
        if request_id != self.search_request_id or self.db is None:
            return
        self.search_model.sort(
            self.search_model.sort_column,
            self.search_model.sort_order,
        )
        self.set_search_empty_state(result_count == 0)
        self.on_search_selection_changed()
        self.statusBar().showMessage(f"{result_count} search results.", 4000)

    @Slot(int)
    def on_search_cancelled(self, request_id: int) -> None:
        if request_id == self.search_request_id and self.pending_search_request is None:
            self.statusBar().showMessage("Search cancelled.", 2000)

    @Slot(int, str)
    def on_search_failed(self, request_id: int, details: str) -> None:
        if request_id != self.search_request_id or self.db is None:
            return
        self.search_model.set_items([])
        self.set_search_empty_state(False)
        self.on_search_selection_changed()
        self.statusBar().showMessage("Search failed.", 4000)
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("Search Failed")
        dialog.setText("The catalogue could not be searched.")
        dialog.setDetailedText(details)
        dialog.exec()

    def set_search_empty_state(self, no_results: bool) -> None:
        stack = getattr(self, "search_results_stack", None)
        search_table = getattr(self, "search_table", None)
        empty_state = getattr(self, "search_empty_state", None)
        if stack is None or search_table is None or empty_state is None:
            return
        stack.setCurrentWidget(empty_state if no_results else search_table)

    @Slot()
    def clear_search_worker(self) -> None:
        self.search_worker = None
        self.search_thread = None
        pending = self.pending_search_request
        self.pending_search_request = None
        if pending is not None and pending[0] == self.search_request_id and self.db is not None:
            self._start_search(pending)
        else:
            self.search_button.setText("Search")

    def on_search_selection_changed(self, selected=None, deselected=None) -> None:
        item = self.selected_search_item()
        real_path = self.selected_search_real_path()
        real_available = item is not None and not item.missing and real_path is not None and real_path.exists()
        self.open_file_button.setEnabled(item is not None and (item.is_folder or real_available))
        self.reveal_file_button.setEnabled(real_available)

    def selected_search_item(self) -> SearchResultItem | None:
        return self.search_model.item_at(self.search_table.currentIndex())

    def show_search_context_menu(self, point: QPoint) -> None:
        if self.db is None:
            return
        index = self.search_table.indexAt(point)
        if not index.isValid():
            return

        self.search_table.selectRow(index.row())
        self.search_table.setCurrentIndex(self.search_model.index(index.row(), 0))
        item = self.search_model.item_at(index)
        if item is None:
            return

        target = self.catalogue_ref_for_search_item(item)
        self.show_catalogue_item_context_menu(
            target,
            self.search_table.viewport(),
            point,
            include_catalogue_location=True,
        )

    def catalogue_ref_for_search_item(self, item: SearchResultItem) -> CatalogueItemRef:
        return CatalogueItemRef(
            item_type=item.item_type,
            item_id=item.item_id,
            volume_id=item.volume_id,
            relative_path=item.relative_path,
            missing=item.missing,
        )

    def selected_search_real_path(self) -> Path | None:
        item = self.selected_search_item()
        if item is None:
            return None
        return self.catalogue_item_real_path(self.catalogue_ref_for_search_item(item))

    def open_selected_real_item(self, reveal: bool) -> None:
        item = self.selected_search_item()
        if item is not None:
            self.open_real_catalogue_item(self.catalogue_ref_for_search_item(item), reveal)

    def open_selected_search_item(self) -> None:
        item = self.selected_search_item()
        if item is not None:
            self.open_catalogue_item(self.catalogue_ref_for_search_item(item))

    def open_search_index(self, index: QModelIndex) -> None:
        item = self.search_model.item_at(index)
        if item is not None:
            self.open_catalogue_item(self.catalogue_ref_for_search_item(item))

    def select_folder_path(self, relative_path: str) -> None:
        if self.db is None or self.current_volume_id is None:
            return
        root = self.folder_tree.topLevelItem(0)
        if root is None:
            return
        if not relative_path:
            self.folder_tree.setCurrentItem(root)
            return

        item = root
        for part in PurePosixPath(relative_path).parts:
            self.load_tree_children(item)
            found = None
            for index in range(item.childCount()):
                child = item.child(index)
                child_path = child.data(0, ROLE_RELATIVE_PATH)
                if child_path and PurePosixPath(str(child_path)).name == part:
                    found = child
                    break
            if found is None:
                return
            item.setExpanded(True)
            item = found
        self.folder_tree.setCurrentItem(item)
        item.setExpanded(True)

    def load_scan_log(self, volume_id: int | None) -> None:
        self.scan_log.clear()
        if self.db is None or volume_id is None:
            return
        history = self.db.list_scan_history(volume_id)
        errors = self.db.list_scan_errors(volume_id)
        lines: list[str] = []
        for row in history:
            hash_errors = int(row["hash_errors"] or 0)
            access_errors = max(0, int(row["errors_count"] or 0) - hash_errors)
            lines.append(
                f"{self._display_time(row['started_at'])} - {row['status']} - "
                f"{row['files_seen']} files, {row['folders_seen']} folders, "
                f"{access_errors} incomplete/access issues"
            )
            lines.append(
                f"  SHA-256: {int(row['files_hashed'] or 0):,} files, "
                f"{format_size(int(row['bytes_hashed'] or 0))} read, "
                f"{hash_errors:,} unavailable"
            )
            if int(row["media_files"] or 0):
                media_files = int(row["media_files"] or 0)
                media_collected = int(row["media_metadata_collected"] or 0)
                lines.append(
                    f"  Media inspection: {media_collected:,} of {media_files:,} recognized "
                    f"media files returned complete or partial details this scan; "
                    f"{max(0, media_files - media_collected):,} unavailable"
                )
            if row["files_added"] is not None:
                lines.append(
                    f"  Changes: +{row['files_added']} files, "
                    f"-{row['files_removed']} files, {row['files_changed']} changed; "
                    f"+{row['folders_added']} folders, -{row['folders_removed']} folders"
                )
                size_delta = int(row["bytes_after"]) - int(row["bytes_before"])
                delta_prefix = "+" if size_delta > 0 else "-" if size_delta < 0 else ""
                lines.append(
                    f"  Indexed size: {format_size(row['bytes_before'])} -> "
                    f"{format_size(row['bytes_after'])} "
                    f"({delta_prefix}{format_size(abs(size_delta))})"
                )
            if row["message"]:
                lines.append(f"  {row['message']}")
        if errors:
            lines.append("")
            lines.append("Recent errors:")
            for row in errors:
                lines.append(f"{self._display_time(row['created_at'])} - {row['path']}: {row['message']}")
        self.scan_log.setPlainText("\n".join(lines))

    def _display_time(self, value: str | None) -> str:
        return display_db_time(value)

    def _display_unknown_time(self, value: str | None) -> str:
        return "Unknown" if not value else display_db_time(value)

    def _display_optional_count(self, value: int | None) -> str:
        return "Unknown" if value is None else f"{int(value):,}"


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == CATALOGUE_PROBE_ARGUMENT:
        return probe_catalogue_location(sys.argv[2])
    app = QApplication(sys.argv)
    app.setApplicationName("JVVV")
    app.setOrganizationName("JVVV")
    window = MainWindow()
    window.show()
    return app.exec()
