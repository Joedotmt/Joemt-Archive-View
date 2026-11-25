from __future__ import annotations

from concurrent.futures import Future
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
import os
import re
import sqlite3
import sys
import traceback
from threading import Lock, Thread
from time import monotonic
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QDate,
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
    QSignalBlocker,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QPolygon, QShortcut
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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import APP_NAME
from .list_ui import ListTableView, ListTableWidget, ListTreeWidget
from .archive_job import (
    VERIFY_PHASE,
    check_archive_write_locations, is_archive_volume_root, is_scanned_archive_volume,
    pending_archive, register_scanned_drive,
)
from .archive_ui import ArchiveDialog, ArchiveThread
from .archive_stream import ProjectSession, SharedArchiveStream
from .backup_analysis import BackupAnalysisEngine
from .backup_ui import (
    BACKUP_COLUMN_HEADER_TOOLTIP, BACKUP_FILTER_OPTIONS, NOT_ANALYSED_BACKUP_DISPLAY,
    BackupDisplay, BackupsPanel, BackupsSplitter, BackupSource, backup_filter_matches,
    display_db_time, item_backup_display, volume_backup_display,
)
from .catalogue_format import CATALOGUE_DB_NAME
from .content_hash import DEFAULT_HASH_ALGORITHM, HASH_LABELS
from .drive_marker import MARKER_IDENTITY
from .operations_queue import (
    ACTIONABLE, DONE, STATUSES, Operation, OperationDisks, OperationsQueue, operation_destination_problem,
    prepare_operation_previews,
)
from .scan_staging import StagedScan
from .operations_ui import OperationsDialog
from .database import (
    ARCHIVE_STATUSES,
    CATALOGUE_EXTENSION,
    COMPLETED_SCAN_STATUSES,
    CONNECTOR_OPTIONS,
    CatalogueInUseError,
    Database,
    VOLUME_CONDITIONS,
    catalogue_path_with_extension,
    create_catalogue,
    drive_id_sort_key,
    is_valid_drive_id,
    locate_catalogue,
    open_catalogue,
    sqlite_file_uri,
)
from .snapshot_repository import (
    LINK_LINKED,
    LINK_MISSING,
    LINK_UNLINKED,
    LINK_WRONG,
    REPOSITORY_EXTENSION,
    RelinkCandidate,
    RepositoryLink,
    PruneResult,
    RestoreResult,
    SnapshotCancelled,
    SnapshotManifest,
    SnapshotRepository,
    SnapshotResult,
    VerifyResult,
    inspect_relink_target,
    locate_repository_candidates,
    repository_link_settings,
    resolve_repository_link,
)
from .scanner import VolumeScanner
from .media_metadata import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    media_kind_for_extension,
)
from .preview_cache import PreviewEntry, PreviewFailure, PreviewStoreStatistics
from .preview_config import (
    BACKUP_POLICY_TEXT,
    CATALOGUE_PREVIEW_SETTING_KEYS,
    PREVIEWS_FFMPEG_PATH_SETTING,
    PreviewSettings,
)
from .preview_service import (
    DB_STATUS_AVAILABLE,
    DB_STATUS_FAILED,
    DB_STATUS_MISSING,
    DB_STATUS_UNSUPPORTED,
    MODE_ENABLED,
    MODE_SKIPPED_PREFLIGHT,
    PreviewService,
    PreviewStatistics,
    PreviewValidationReport,
    SCAN_OUTCOME_COMPLETED_WITH_WARNINGS,
    inspect_preview_file,
    preflight_preview_configuration,
    skipped_preflight_statistics,
    validate_preview_configuration,
)
from .preview_ui import (
    DeletePreviewsWorker,
    DeleteTemporariesWorker,
    PreviewCacheDialog,
    PreviewFailuresDialog,
    PreviewStoreStatisticsWorker,
    UnreferencedPreviewWorker,
)
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
    path_with_relative,
    percentage_full,
    rename_volume_label,
    relative_path_for_display,
    resolve_volume_source_path,
    snapshot_matches_volume,
    volume_identity_known,
)


ROLE_VOLUME_ID = Qt.ItemDataRole.UserRole
ROLE_FOLDER_ID = Qt.ItemDataRole.UserRole + 1
ROLE_RELATIVE_PATH = Qt.ItemDataRole.UserRole + 2
ROLE_ITEM_TYPE = Qt.ItemDataRole.UserRole + 3
ROLE_ITEM_ID = Qt.ItemDataRole.UserRole + 4
ROLE_PERCENT_FULL = Qt.ItemDataRole.UserRole + 5
VOLUME_CONNECTION_COLUMN = 7
VOLUME_FULL_COLUMN = 18
SEARCH_VOLUME_STATUS_COLUMN = 8
LAST_CATALOGUE_PATH_SETTING = "catalogues/lastPath"
RECENT_CATALOGUE_PATHS_SETTING = "catalogues/recentPaths"
MAX_RECENT_CATALOGUES = 8
SEARCH_INCLUDE_PATHS_SETTING = "search/includePaths"
THEME_STYLE_SETTING = "appearance/themeStyle"
COLOR_MODE_SETTING = "appearance/colorMode"
ACCENT_COLOR_SETTING = "appearance/accentColor"
CATALOGUE_FILE_FILTER = "Joemt Archive View Catalogues (*.jvvv)"
REPOSITORY_FILE_FILTER = "JVVV Snapshot Repositories (*.jvvvrepo)"
CATALOGUE_PROBE_ARGUMENT = "--catalogue-location-probe"
CATALOGUE_PROBE_TIMEOUT_MS = 8000
CATALOGUE_PROBE_OK = 0
CATALOGUE_PROBE_UNAVAILABLE = 2
CATALOGUE_PROBE_INVALID = 3
PREVIEW_PROGRESS_PREFIXES = ("Creating image preview", "Creating video preview")
SCAN_PROGRESS_DETAIL_PREFIXES = (
    "Hashing ",
    "Reading media details",
    "Catalogue ready",
    *PREVIEW_PROGRESS_PREFIXES,
)


def load_preview_settings(settings: QSettings) -> PreviewSettings:
    """Read computer-specific preview options; catalogue preferences load on open."""

    return PreviewSettings.from_mapping({
        PREVIEWS_FFMPEG_PATH_SETTING: settings.value(PREVIEWS_FFMPEG_PATH_SETTING),
    })


def save_preview_settings(settings: QSettings, preview_settings: PreviewSettings) -> None:
    """Save the local FFmpeg path; preview generation preferences belong to a catalogue."""
    settings.setValue(PREVIEWS_FFMPEG_PATH_SETTING, preview_settings.ffmpeg_path.strip())
    settings.sync()


def load_recent_catalogue_paths(settings: QSettings) -> list[str]:
    """Read recent locations without touching possibly offline drives."""
    values = settings.value(RECENT_CATALOGUE_PATHS_SETTING)
    if values is None:
        values = [settings.value(LAST_CATALOGUE_PATH_SETTING, "", type=str)]
    elif isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            continue
        path = os.path.abspath(os.path.expanduser(value.strip()))
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            paths.append(path)
        if len(paths) == MAX_RECENT_CATALOGUES:
            break
    return paths


def preview_open_label(media_kind: str) -> str:
    return "Play Preview" if media_kind == "video" else "Open Preview"


def preview_property_rows(
    record: Any,
    preview_settings: PreviewSettings | None,
    media_kind: str,
) -> list[tuple[str, str]]:
    """Properties rows describing the preview for an image or video."""

    if media_kind not in {"image", "video"}:
        return []
    record_field = lambda name, default=None: object_value(record, name, default)
    rows: list[tuple[str, str]] = []
    if preview_settings is None or preview_settings.root_path is None:
        rows.append(
            (
                "Offline preview",
                "Not configured — choose a preview storage directory in "
                "Window > Preview Manager to generate previews while scanning",
            )
        )
        return rows

    profile = preview_settings.profile_for(media_kind)
    try:
        current_profile_id = profile.profile_id
    except Exception:  # pragma: no cover - settings are validated on load
        current_profile_id = "invalid"
    content_hash = record_field("content_hash")
    info = inspect_preview_file(
        preview_settings, media_kind, content_hash,
        record_field("content_hash_algorithm") or DEFAULT_HASH_ALGORITHM,
    )
    stored_status = str(record_field("preview_status") or "").casefold()
    stored_profile = str(record_field("preview_profile_id") or "")
    stored_stage = str(record_field("preview_error_stage") or "")
    stored_message = str(record_field("preview_error_message") or "").strip()
    stored_hash = record_field("preview_source_hash")
    stale_status = (
        stored_status
        and stored_hash is not None
        and content_hash is not None
        and bytes(stored_hash) != bytes(content_hash)
    )

    if content_hash is None:
        summary = "Not available — no content hash is recorded for this file, so no preview path can be derived"
    elif info is not None and info.exists and info.valid:
        summary = "Available"
    elif info is not None and info.exists and not info.valid:
        summary = f"Present but invalid — {info.message}"
    elif stored_status == DB_STATUS_FAILED and not stale_status:
        summary = "Preview generation failed during the last scan"
        if stored_message:
            summary += f": {stored_message}"
        if stored_stage:
            summary += f" (stage: {stored_stage})"
    elif stored_status == DB_STATUS_AVAILABLE and not stale_status:
        summary = (
            "Missing — the last scan recorded a preview, but the file is no longer present "
            "at the expected location"
        )
    elif stored_status == DB_STATUS_MISSING and not stale_status:
        summary = "Not generated"
        if stored_message:
            summary += f" — {stored_message}"
    elif stored_status == DB_STATUS_UNSUPPORTED and not stale_status:
        summary = "Unsupported"
        if stored_message:
            summary += f" — {stored_message}"
    elif not preview_settings.enabled:
        summary = "Not generated — preview generation is disabled in Settings"
    else:
        summary = "Not generated — rescan this volume with previews enabled"
    rows.append(("Offline preview", summary))
    rows.append(("Preview profile", " · ".join(profile.describe())))
    if info is not None and info.exists and info.valid:
        if info.width and info.height:
            rows.append(("Preview dimensions", f"{info.width:,} × {info.height:,}"))
        if media_kind == "video" and info.duration_ms is not None:
            rows.append(("Preview duration", display_duration_ms(info.duration_ms)))
        rows.append(("Preview size", format_size(info.size_bytes)))
    if info is not None:
        rows.append(("Preview stored at", str(info.path)))
    if stored_status:
        recorded = str(record_field("preview_updated_at") or "")
        note = f"{stored_status}"
        if stored_profile and stored_profile != current_profile_id:
            note += f" · recorded for profile {stored_profile}, current profile is {current_profile_id}"
        if stale_status:
            note += " · recorded for previous file content"
        if recorded:
            note += f" · {display_db_time(recorded)}"
        rows.append(("Preview status recorded", note))
    return rows


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
    """Read a catalogue without modifying it to test whether its location responds.

    A catalogue is a folder; its registry (``catalogue.sqlite``) is probed.
    """
    catalogue_path = locate_catalogue(path)
    if catalogue_path.is_dir():
        registry = catalogue_path / CATALOGUE_DB_NAME
        if not registry.is_file():
            return CATALOGUE_PROBE_INVALID
        catalogue_path = registry
    elif catalogue_path.exists():
        # A bare file is never a catalogue.
        return CATALOGUE_PROBE_INVALID
    try:
        with catalogue_path.open("rb") as catalogue_file:
            header = catalogue_file.read(100)
        if len(header) < 16 or header[:16] != b"SQLite format 3\x00":
            return CATALOGUE_PROBE_INVALID

        connection = sqlite3.connect(
            sqlite_file_uri(catalogue_path, mode="ro"),
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
QUEUE_DRIVE_CHECK_MESSAGE = "Checking drive connection…"
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
        if not 0 <= column < len(self.columns):
            return
        self.sort_column = column
        self.sort_order = order
        self.layoutAboutToBeChanged.emit()
        self._sort_items(preserve_indexes=True)
        self.layoutChanged.emit()

    def role_value(self, item: Any, role: int) -> Any:
        return None

    def group_key(self, item: Any) -> Any:
        return 0

    def _sort_items(self, *, preserve_indexes: bool = False) -> None:
        indexes = self.persistentIndexList() if preserve_indexes else []
        reverse = self.sort_order == Qt.SortOrder.DescendingOrder
        column = self.columns[self.sort_column]
        sort_key = column.sort_key or column.display
        groups: dict[Any, list[int]] = {}
        for row, item in enumerate(self.items):
            groups.setdefault(self.group_key(item), []).append(row)
        row_order: list[int] = []
        for group in sorted(groups):
            rows = groups[group]
            rows.sort(
                key=lambda row: self._normalized_sort_value(sort_key(self.items[row])),
                reverse=reverse,
            )
            row_order.extend(rows)
        self.items = [self.items[row] for row in row_order]
        if indexes:
            old_rows = {index.row() for index in indexes}
            new_rows = {
                old_row: new_row for new_row, old_row in enumerate(row_order)
                if old_row in old_rows
            }
            self.changePersistentIndexList(
                indexes,
                [self.index(new_rows[index.row()], index.column()) for index in indexes],
            )

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


def enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold().replace("-", "_").replace(" ", "_")


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


class _FolderTypedItem:
    item_type: str

    @property
    def is_folder(self) -> bool:
        return self.item_type == "folder"


@dataclass(frozen=True)
class BrowserItem(_FolderTypedItem):
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

@dataclass(frozen=True)
class CatalogueItemRef(_FolderTypedItem):
    item_type: str
    item_id: int
    volume_id: int
    relative_path: str
    missing: bool = False

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


def home_icon(palette: QPalette) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(4, 4)
    pen = QPen(palette.color(QPalette.ColorRole.ButtonText))
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline(QPolygon([QPoint(1, 7), QPoint(8, 1), QPoint(15, 7)]))
    painter.drawPolyline(QPolygon([
        QPoint(3, 6), QPoint(3, 14), QPoint(6, 14), QPoint(6, 10),
        QPoint(10, 10), QPoint(10, 14), QPoint(13, 14), QPoint(13, 6),
    ]))
    painter.end()
    return QIcon(pixmap)


class BackupStatusIconProvider:
    COLORS = {
        "verified": "#1fa9b8",
        "strong": "#2e9b4b",
        "possible": "#d18a00",
        "none": "#cf3f3f",
        "unknown": "#7b8794",
    }

    def __init__(self) -> None:
        self.cache: dict[str, QIcon] = {}

    def icon_for(self, display: BackupDisplay) -> QIcon:
        return self.icon_for_state(display.state)

    def icon_for_state(self, state: str) -> QIcon:
        state = state if state in self.COLORS else "unknown"
        if state in self.cache:
            return self.cache[state]
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(self.COLORS[state])
        if state == "unknown":
            # A hollow ring: "can't tell" must not read as a verdict.
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(colour, 2))
            painter.drawEllipse(QRectF(3.5, 3.5, 9, 9))
        else:
            painter.setPen(QColor("#39424c"))
            painter.setBrush(colour)
            painter.drawEllipse(QRectF(3, 3, 10, 10))
            # One glyph per state so colour is never the only channel.
            glyph = QPen(QColor("white"), 1.6)
            glyph.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(glyph)
            if state == "verified":
                # Double tick: two ticks 3 px apart, so "proven identical" is
                # told from a plain tick without relying on aqua vs green.
                glyph.setWidthF(1.3)
                painter.setPen(glyph)
                painter.drawLine(QPoint(4, 8), QPoint(6, 10))
                painter.drawLine(QPoint(6, 10), QPoint(9, 6))
                painter.drawLine(QPoint(7, 8), QPoint(9, 10))
                painter.drawLine(QPoint(9, 10), QPoint(12, 6))
            elif state == "strong":
                painter.drawLine(QPoint(5, 8), QPoint(7, 10))
                painter.drawLine(QPoint(7, 10), QPoint(11, 6))
            elif state == "possible":
                painter.drawLine(QPoint(5, 8), QPoint(11, 8))
            else:
                painter.drawLine(QPoint(6, 6), QPoint(10, 10))
                painter.drawLine(QPoint(10, 6), QPoint(6, 10))
        painter.end()
        icon = QIcon(pixmap)
        self.cache[state] = icon
        return icon


def connection_status_column(
    title: str,
    icons: BackupStatusIconProvider,
) -> TableColumn:
    def status_text(item: Any) -> str:
        return "Connected" if item.connected else "Offline"

    return TableColumn(
        title,
        status_text,
        decoration=lambda item: icons.icon_for_state(
            "strong" if item.connected else "none"
        ),
        tooltip=status_text,
    )


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
    backup_references: dict[int, str] | None = None,
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
            backup_references,
            analysis_state,
        ),
    )


@dataclass(frozen=True)
class SearchResultItem(_FolderTypedItem):
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


INDEXED_SIZE_COLUMN = TableColumn(
    "Size",
    lambda item: display_indexed_size(item.size_bytes),
    sort_key=lambda item: size_sort_key(item.size_bytes),
    alignment=Qt.AlignmentFlag.AlignRight,
)
MODIFIED_COLUMN = TableColumn(
    "Modified",
    lambda item: display_db_time(item.modified_at),
    sort_key=lambda item: item.modified_at or "",
)
RELATIVE_PATH_COLUMN = TableColumn(
    "Relative Path", lambda item: relative_path_for_display(item.relative_path),
)


def other_copies_column(icons: BackupStatusIconProvider) -> TableColumn:
    def is_parent_entry(item: BrowserItem | SearchResultItem) -> bool:
        return getattr(item, "is_parent_entry", False)

    return TableColumn(
        "Backups",
        lambda item: "N/A" if is_parent_entry(item) else item.backup.text,
        sort_key=lambda item: (item.backup.sort_rank, item.backup.coverage_fraction),
        decoration=lambda item: None if is_parent_entry(item) else icons.icon_for(item.backup),
        tooltip=lambda item: None if is_parent_entry(item) else item.backup.tooltip,
        header_tooltip=BACKUP_COLUMN_HEADER_TOOLTIP,
    )


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
                other_copies_column(self.backup_icons),
                TableColumn("Type", lambda item: item.type_label),
                INDEXED_SIZE_COLUMN,
                MODIFIED_COLUMN,
                RELATIVE_PATH_COLUMN,
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
                connection_status_column("Connection", self.backup_icons),
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
                    "Backups",
                    lambda item: item.backup.text,
                    sort_key=lambda item: (
                        item.backup.sort_rank,
                        item.backup.coverage_fraction,
                    ),
                    decoration=lambda item: self.backup_icons.icon_for(item.backup),
                    tooltip=lambda item: item.backup.tooltip,
                    header_tooltip=BACKUP_COLUMN_HEADER_TOOLTIP,
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
                other_copies_column(self.backup_icons),
                TableColumn("Kind", lambda item: item.item_type.title()),
                TableColumn("Volume", lambda item: display_volume_name(item.volume_name)),
                TableColumn(
                    "Drive ID",
                    lambda item: item.drive_id or "-",
                    sort_key=lambda item: drive_id_sort_key(item.drive_id),
                ),
                RELATIVE_PATH_COLUMN,
                INDEXED_SIZE_COLUMN,
                MODIFIED_COLUMN,
                connection_status_column("Volume Status", self.backup_icons),
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


class ResponsiveStatusDelegate(QStyledItemDelegate):
    """Hide a status label when its column only has room for the indicator dot."""

    def initStyleOption(self, option, index: QModelIndex) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        width = option.rect.width()
        full_width = (
            option.decorationSize.width()
            + option.fontMetrics.horizontalAdvance("Connected")
            + 16
        )
        if 0 < width < full_width:
            option.text = ""
            option.displayAlignment = Qt.AlignmentFlag.AlignCenter
            option.decorationAlignment = Qt.AlignmentFlag.AlignCenter


class OtherCopiesDelegate(QStyledItemDelegate):
    """Shorten an Other-copies label instead of eliding its tail.

    Labels lead with the verdict word, so when the column is too narrow for the
    whole text we keep only the part before the first " · ", and when even that
    does not fit, the dot alone. Qt's default ellipsis would cut off the caution.
    """

    @staticmethod
    def short_text(text: str) -> str:
        return text.split(" · ", 1)[0]

    def initStyleOption(self, option, index: QModelIndex) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        text = option.text or ""
        width = option.rect.width()
        if not text or width <= 0:
            return
        metrics = option.fontMetrics
        padding = option.decorationSize.width() + 16
        if width >= padding + metrics.horizontalAdvance(text):
            return
        short = self.short_text(text)
        if width >= padding + metrics.horizontalAdvance(short):
            option.text = short
            return
        option.text = ""
        option.displayAlignment = Qt.AlignmentFlag.AlignCenter
        option.decorationAlignment = Qt.AlignmentFlag.AlignCenter


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


VolumeLocationKey = tuple[str, str, str, str, str]
FileOpenKey = tuple[str, str, bool]


def volume_location_key(volume) -> VolumeLocationKey:
    return (
        volume["uuid"], volume["source_path"], volume["identity_kind"],
        volume["identity_token"], volume["source_relative_path"],
    )


@dataclass(frozen=True)
class VolumeConnections:
    snapshots: list[VolumeSnapshot]
    paths: dict[VolumeLocationKey, str]


def _probe_connected_volumes(volumes, result: Future[VolumeConnections]) -> None:
    try:
        snapshots = list_connected_volume_snapshots()
        resolver = ConnectedVolumeResolver(snapshots)
        paths = {}
        for volume in volumes:
            try:
                path = resolver.resolve(volume)
            except OSError:
                continue
            if path is not None:
                paths[volume_location_key(volume)] = path
        result.set_result(VolumeConnections(snapshots, paths))
    except Exception as exc:
        result.set_exception(exc)


def _open_catalogue_file(
    volume: sqlite3.Row, connected_path: str | None, relative_path: str, reveal: bool, result: Future[bool],
) -> None:
    try:
        source = connected_path
        if source is None or not snapshot_matches_volume(capture_volume_snapshot(source), volume):
            source = resolve_volume_source_path(volume)
        path = path_with_relative(source, relative_path) if source is not None else None
        if path is not None and not path.exists():
            path = None
    except Exception as exc:
        if result.set_running_or_notify_cancel():
            result.set_exception(exc)
        return

    # Closing the catalogue can cancel the request while drive I/O is stalled.
    if not result.set_running_or_notify_cancel():
        return
    try:
        if path is not None:
            open_in_file_manager(path, reveal=reveal)
    except Exception as exc:
        result.set_exception(exc)
    else:
        result.set_result(path is not None)


@dataclass(frozen=True)
class QueueDriveCheck:
    location: VolumeLocationKey
    path: str | None
    problem: str
    disks: OperationDisks | None


def _verified_rescan_path(job: Operation, volume: sqlite3.Row, connected_path: str | None) -> str | None:
    if job.kind != "scan" or job.state == "relink" or not volume["last_scan_at"] or connected_path is None:
        return None
    try:
        snapshot = capture_volume_snapshot(connected_path)
        if snapshot is None or not snapshot_matches_volume(snapshot, volume):
            return None
    except OSError:
        return None
    if volume["identity_kind"] == MARKER_IDENTITY:
        # A marker can be copied to another disk. Also check the filesystem
        # identity that was saved when this volume was catalogued.
        serial = volume["identity_serial"]
        if (not serial or serial.casefold() != snapshot.identity_serial.casefold()
                or volume["identity_filesystem"].casefold() != snapshot.identity_filesystem.casefold()):
            return None
    return connected_path


def _probe_queue_drives(requests, result: Future[dict[str, QueueDriveCheck]]) -> None:
    try:
        resolver = None
        checks = {}
        for job, volume, connected_path in requests:
            path = _verified_rescan_path(job, volume, connected_path)
            problem = ""
            if path is None:
                if resolver is None:
                    resolver = ConnectedVolumeResolver()
                path, problem = resolver.resolve_details(volume)
            disks = OperationDisks.resolve(job, path) if path is not None else None
            checks[job.id] = QueueDriveCheck(volume_location_key(volume), path, problem, disks)
        result.set_result(checks)
    except Exception as exc:
        result.set_exception(exc)


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


class _ValidatedDialog(QDialog):
    validation_label: QLabel

    def validate_form(self) -> str | None:
        raise NotImplementedError

    def accept(self) -> None:
        message = self.validate_form()
        if message is not None:
            self.validation_label.setText(message)
            return
        super().accept()


class DriveIdDialog(_ValidatedDialog):
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

class VolumeDialog(_ValidatedDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "Add Volume",
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
        actions: list[tuple[str, Callable[[], None]]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Properties - {name}")
        self.resize(760, 520)
        self.setMinimumSize(560, 380)
        self.copy_text = "\n".join(f"{label}: {value}" for label, value in properties)
        self.action_buttons: list[QPushButton] = []

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
        for label, callback in actions or ():
            action_button = buttons.addButton(label, QDialogButtonBox.ButtonRole.ActionRole)
            action_button.clicked.connect(
                lambda checked=False, callback=callback: callback()
            )
            self.action_buttons.append(action_button)
        copy_button = buttons.addButton("Copy All", QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(self.copy_all)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top_layout)
        layout.addWidget(self.details_edit, 1)
        layout.addWidget(buttons)

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.copy_text)


class SnapshotPickerDialog(QDialog):
    """Choose the snapshot to use for a new catalogue."""

    def __init__(
        self,
        parent: QWidget | None,
        repository: SnapshotRepository,
        manifests: list[SnapshotManifest],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create New Catalogue from Repository — Choose Snapshot")
        self.resize(720, 380)
        self.manifests = manifests
        self.selected: SnapshotManifest | None = None

        location = QLabel(f"Repository: {repository.path}")
        location.setWordWrap(True)
        self.table = ListTableWidget(["Taken", "Label", "Volumes", "Files"])
        for manifest in reversed(manifests):
            self.table.append_text_row(
                (
                    display_db_time(manifest.created_at),
                    manifest.label,
                    str(manifest.volume_count),
                    f"{manifest.file_count:,}",
                ),
                user_data=manifest.snapshot_id,
            )
        self.table.resizeColumnsToContents()
        if self.table.rowCount():
            self.table.selectRow(0)
        self.table.doubleClicked.connect(lambda _index: self.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        choose_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        choose_button.setText("Choose New Catalogue Location…")
        choose_button.setEnabled(self.current_manifest() is not None)
        self.table.itemSelectionChanged.connect(
            lambda: choose_button.setEnabled(self.current_manifest() is not None)
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(location)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)

    def current_manifest(self) -> SnapshotManifest | None:
        items = self.table.selectedItems()
        if not items:
            return None
        snapshot_id = items[0].data(Qt.ItemDataRole.UserRole)
        return next((item for item in self.manifests if item.snapshot_id == snapshot_id), None)

    def accept(self) -> None:  # type: ignore[override]
        self.selected = self.current_manifest()
        if self.selected is None:
            return
        super().accept()

    @classmethod
    def choose(
        cls,
        parent: QWidget | None,
        repository: SnapshotRepository,
        manifests: list[SnapshotManifest],
    ) -> SnapshotManifest | None:
        dialog = cls(parent, repository, manifests)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected


class RelinkRepositoryDialog(QDialog):
    """Choose the moved snapshot repository: nearby matches first, or browse to it."""

    def __init__(
        self,
        parent: QWidget | None,
        link: RepositoryLink,
        candidates: list[RelinkCandidate],
        *,
        catalogue_uuid: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Locate Snapshot Repository")
        self.resize(760, 420)
        self.link = link
        self.catalogue_uuid = catalogue_uuid
        self.candidates: list[RelinkCandidate] = []
        self.selected: Path | None = None

        if link.status == LINK_MISSING:
            explanation = (
                f"Last known location:\n{link.recorded_path}\n\nThe folder is not there. "
                "Choose the repository at its new location. Only repositories that belong to "
                "this catalogue are accepted."
            )
        elif link.status == LINK_WRONG:
            explanation = (
                f"{link.recorded_path}\n\nThis folder cannot be used: {link.detail}. "
                "Choose this catalogue's repository."
            )
        else:
            explanation = (
                "This catalogue has no snapshot repository linked yet. Choose an existing "
                "repository of this catalogue, or cancel and take a snapshot to create one."
            )
        label = QLabel(explanation)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.table = ListTableWidget(["Location", "Match", "Snapshots", "Latest snapshot"])
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.doubleClicked.connect(lambda _index: self.accept())
        self.found_label = QLabel("")
        self.found_label.setObjectName("mutedLabel")

        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self.browse)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use This Repository")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(self.found_label, 1)
        row.addWidget(self.browse_button)
        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(self.buttons)

        for candidate in candidates:
            self.add_candidate(candidate, select=False)
        if self.table.rowCount():
            self.table.selectRow(0)
        self.found_label.setText(
            f"{len(candidates)} matching repositor{'y' if len(candidates) == 1 else 'ies'} found nearby."
            if candidates
            else "No matching repository was found nearby. Browse to it."
        )
        self.update_buttons()

    def add_candidate(self, candidate: RelinkCandidate, *, select: bool) -> None:
        for existing_row, existing in enumerate(self.candidates):
            if existing.path.resolve(strict=False) == candidate.path.resolve(strict=False):
                if select:
                    self.table.selectRow(existing_row)
                return
        self.candidates.append(candidate)
        row = len(self.candidates) - 1
        latest = candidate.latest_snapshot_id.split("-")[0] if candidate.latest_snapshot_id else "—"
        values = (str(candidate.path), candidate.match_label, str(candidate.snapshot_count), latest)
        self.table.append_text_row(values, user_data=row)
        self.table.resizeColumnsToContents()
        if select:
            self.table.selectRow(row)

    def browse(self) -> None:
        start = self.link.recorded_path.parent if self.link.recorded_path is not None else Path.home()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Locate Snapshot Repository",
            str(start),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not chosen:
            return
        try:
            candidate = inspect_relink_target(
                Path(chosen),
                repository_uuid=self.link.repository_uuid,
                catalogue_uuid=self.catalogue_uuid,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Not This Catalogue's Repository", str(exc))
            return
        self.add_candidate(candidate, select=True)

    def current_candidate(self) -> RelinkCandidate | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return self.candidates[int(items[0].data(Qt.ItemDataRole.UserRole))]

    def update_buttons(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.current_candidate() is not None
        )

    def accept(self) -> None:  # type: ignore[override]
        candidate = self.current_candidate()
        if candidate is None:
            return
        self.selected = candidate.path
        super().accept()

    @classmethod
    def choose(
        cls,
        parent: QWidget | None,
        link: RepositoryLink,
        candidates: list[RelinkCandidate],
        *,
        catalogue_uuid: str,
    ) -> Path | None:
        dialog = cls(parent, link, candidates, catalogue_uuid=catalogue_uuid)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected


class SnapshotHistoryDialog(QDialog):
    """Version history of the open catalogue: browse, create, delete, prune, verify."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window_ref = window
        self.setWindowTitle("Snapshot Manager")
        self.resize(1040, 760)
        self.setModal(False)
        self.manifests: list[SnapshotManifest] = []
        self.repository_path: Path | None = None
        self.link: RepositoryLink | None = None

        self.repository_group = QGroupBox("Repository")
        self.repository_status_label = QLabel()
        status_font = self.repository_status_label.font()
        status_font.setBold(True)
        self.repository_status_label.setFont(status_font)
        self.location_label = QLabel("")
        self.location_label.setWordWrap(True)
        self.location_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("mutedLabel")
        self.stats_label.setWordWrap(True)
        self.relink_button = QPushButton("Locate Repository…")
        self.relink_button.setToolTip(
            "Point the catalogue at its repository after the folder was moved or renamed"
        )
        self.relink_button.clicked.connect(self.relink)
        self.refresh_button = QPushButton("Check Again")
        self.refresh_button.setToolTip("Check the repository location again, for example after reconnecting its drive")
        self.refresh_button.clicked.connect(self.refresh)
        self.repository_tools_button = QPushButton("Repository Tools")
        tools_menu = QMenu(self.repository_tools_button)
        self.verify_action = tools_menu.addAction("Verify Repository…")
        self.verify_action.setToolTip("Check the integrity of all snapshots in this repository")
        self.verify_action.triggered.connect(self.verify)
        self.prune_action = tools_menu.addAction("Reclaim Unused Space…")
        self.prune_action.setToolTip("Remove stored data no longer used by any snapshot")
        self.prune_action.triggered.connect(self.prune)
        self.repository_tools_button.setMenu(tools_menu)
        header = QHBoxLayout()
        header.addWidget(self.repository_status_label, 1)
        header.addWidget(self.relink_button)
        header.addWidget(self.refresh_button)
        header.addWidget(self.repository_tools_button)
        repository_layout = QVBoxLayout(self.repository_group)
        repository_layout.addLayout(header)
        repository_layout.addWidget(self.location_label)
        repository_layout.addWidget(self.stats_label)

        self.snapshot_table = ListTableWidget(
            ["Taken", "Label", "Volumes", "Files", "Changed since previous"]
        )
        self.snapshot_table.itemSelectionChanged.connect(self.populate_selected)

        self.volume_table = ListTableWidget(
            ["Drive ID", "Name", "Files", "Folders", "Change in this snapshot"],
            selection_mode=QAbstractItemView.SelectionMode.NoSelection,
        )

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.snapshots_group = QGroupBox("Snapshots")
        snapshots_layout = QVBoxLayout(self.snapshots_group)
        self.create_button = QPushButton("Create Snapshot…")
        self.create_button.setObjectName("primaryButton")
        self.create_button.setToolTip("Save the current catalogue state in a new snapshot")
        self.create_button.clicked.connect(self.create_snapshot)
        snapshot_toolbar = QHBoxLayout()
        snapshot_hint = QLabel("Saved states of this catalogue, newest first.")
        snapshot_hint.setObjectName("mutedLabel")
        snapshot_toolbar.addWidget(snapshot_hint, 1)
        snapshot_toolbar.addWidget(self.create_button)
        snapshots_layout.addLayout(snapshot_toolbar)
        self.snapshot_empty_label = QLabel()
        self.snapshot_empty_label.setWordWrap(True)
        self.snapshot_empty_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        snapshots_layout.addWidget(self.snapshot_empty_label, 1)
        snapshots_layout.addWidget(self.snapshot_table)
        self.selected_snapshot_label = QLabel()
        self.selected_snapshot_label.setWordWrap(True)
        self.delete_button = QPushButton("Delete Selected Snapshot…")
        self.delete_button.setToolTip("Delete the selected snapshot from this repository")
        self.delete_button.clicked.connect(self.delete_snapshot)
        snapshot_actions = QHBoxLayout()
        snapshot_actions.addWidget(self.selected_snapshot_label, 1)
        snapshot_actions.addWidget(self.delete_button)
        snapshots_layout.addLayout(snapshot_actions)

        self.volumes_group = QGroupBox("Volumes in the selected snapshot")
        volumes_layout = QVBoxLayout(self.volumes_group)
        self.volume_context_label = QLabel("Select a snapshot to see its volumes.")
        self.volume_context_label.setObjectName("mutedLabel")
        self.volume_context_label.setWordWrap(True)
        volumes_layout.addWidget(self.volume_context_label)
        volumes_layout.addWidget(self.volume_table)
        splitter.addWidget(self.snapshots_group)
        splitter.addWidget(self.volumes_group)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        home_note = QLabel(
            "To create another catalogue from a snapshot, return Home and choose "
            "Create New Catalogue from Repository."
        )
        home_note.setObjectName("mutedLabel")
        home_note.setWordWrap(True)
        buttons.addWidget(home_note, 1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.repository_group)
        layout.addWidget(splitter, 1)
        layout.addLayout(buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    # -- data ----------------------------------------------------------

    def refresh(self) -> None:
        window = self.window_ref
        previous = self.selected_manifest()
        previous_id = previous.snapshot_id if previous is not None else None
        link = window.repository_link()
        self.link = link
        self.repository_path = link.path if link is not None and link.linked else None
        self.manifests = []
        self._repository_loaded = False
        self.snapshot_table.setRowCount(0)
        self.volume_table.setRowCount(0)
        if link is None:
            self.repository_status_label.setText("No catalogue open")
            self.location_label.setText("No catalogue is open.")
            self.stats_label.setText("")
            self.update_buttons()
            return
        if link.status == LINK_UNLINKED:
            self.repository_status_label.setText("No repository yet")
            self.location_label.setText("No snapshot repository yet.")
            self.stats_label.setText(f"The first snapshot creates {link.recorded_path}.")
            self.update_buttons()
            return
        if link.status == LINK_MISSING:
            self.repository_status_label.setText("Repository not found")
            self.location_label.setText(
                f"Repository offline — last known location: {link.recorded_path}"
            )
            self.stats_label.setText(
                "Reconnect the repository's drive and choose Check Again, or choose "
                "Locate Repository if the folder has moved."
            )
            self.update_buttons()
            return
        if link.status == LINK_WRONG:
            self.repository_status_label.setText("Repository unavailable")
            self.location_label.setText(f"Repository offline — {link.recorded_path}")
            self.stats_label.setText(
                f"The folder cannot be used: {link.detail}. Choose Locate Repository to find the correct folder."
            )
            self.update_buttons()
            return
        repository = SnapshotRepository(self.repository_path)
        self.repository_status_label.setText("Repository available")
        self.location_label.setText(f"Repository: {self.repository_path}")
        try:
            self.manifests = repository.list_snapshots()
            statistics = repository.statistics()
        except Exception as exc:
            self.repository_status_label.setText("Repository could not be read")
            self.stats_label.setText(f"The repository could not be read: {exc}")
            self.update_buttons()
            return
        self._repository_loaded = True
        self.stats_label.setText(
            f"{statistics.snapshot_count} snapshot(s) · {format_size(statistics.blob_bytes)} stored · "
            f"{format_size(statistics.unreferenced_bytes)} unused space can be reclaimed."
        )
        previous: SnapshotManifest | None = None
        deltas: dict[str, Any] = {}
        for manifest in self.manifests:
            deltas[manifest.snapshot_id] = SnapshotRepository.delta(manifest, previous)
            previous = manifest
        self._deltas = deltas
        for manifest in reversed(self.manifests):
            delta = deltas[manifest.snapshot_id]
            if manifest is self.manifests[0]:
                change_text = "first snapshot"
            elif delta.changed_count == 0:
                change_text = "nothing"
            else:
                parts = []
                if delta.added:
                    parts.append(f"{len(delta.added)} added")
                if delta.removed:
                    parts.append(f"{len(delta.removed)} removed")
                if delta.content_changed:
                    parts.append(f"{len(delta.content_changed)} rescanned")
                if delta.history_changed:
                    parts.append(f"{len(delta.history_changed)} scan history")
                if delta.metadata_changed:
                    parts.append(f"{len(delta.metadata_changed)} details edited")
                if delta.preview_settings_changed:
                    parts.append("preview settings")
                change_text = ", ".join(parts)
            values = (
                display_db_time(manifest.created_at),
                manifest.label,
                str(manifest.volume_count),
                f"{manifest.file_count:,}",
                change_text,
            )
            self.snapshot_table.append_text_row(values, user_data=manifest.snapshot_id)
        self.snapshot_table.resizeColumnsToContents()
        if self.snapshot_table.rowCount():
            selected_row = next((
                row for row in range(self.snapshot_table.rowCount())
                if self.snapshot_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == previous_id
            ), 0)
            self.snapshot_table.selectRow(selected_row)
        self.populate_selected()

    def selected_manifest(self) -> SnapshotManifest | None:
        items = self.snapshot_table.selectedItems()
        if not items:
            return None
        snapshot_id = items[0].data(Qt.ItemDataRole.UserRole)
        return next((item for item in self.manifests if item.snapshot_id == snapshot_id), None)

    def populate_selected(self) -> None:
        self.volume_table.setRowCount(0)
        manifest = self.selected_manifest()
        description = (
            f"{display_db_time(manifest.created_at)}"
            + (f" — {manifest.label}" if manifest.label else "")
        ) if manifest is not None else ""
        self.selected_snapshot_label.setText(
            f"Selected snapshot: {description}" if manifest is not None else "No snapshot selected."
        )
        self.volume_context_label.setText(
            f"From snapshot: {description}."
            if manifest is not None and manifest.volumes else
            "This snapshot contains no volumes." if manifest is not None else
            "Select a snapshot to see its volumes."
        )
        if manifest is not None:
            delta = getattr(self, "_deltas", {}).get(manifest.snapshot_id)
            changed: dict[str, str] = {}
            if delta is not None:
                for record in delta.added:
                    changed[record.uuid] = "added"
                for record in delta.content_changed:
                    changed[record.uuid] = "rescanned"
                for record in delta.history_changed:
                    changed[record.uuid] = "scan history"
                for record in delta.metadata_changed:
                    changed[record.uuid] = "details edited"
            records = sorted(
                manifest.volumes,
                key=lambda record: drive_id_sort_key(record.drive_id or None) + (record.name.casefold(),),
            )
            for record in records:
                values = (
                    record.drive_id,
                    record.name,
                    f"{record.counts.files:,}",
                    f"{record.counts.folders:,}",
                    changed.get(record.uuid, ""),
                )
                self.volume_table.append_text_row(values)
            self.volume_table.resizeColumnsToContents()
        self.update_buttons()

    def update_buttons(self) -> None:
        link = self.link
        has_repository = link is not None and link.linked
        manifest = self.selected_manifest()
        window = self.window_ref
        busy = window._catalogue_job_running() or window._catalogue_open_in_progress()
        writable = window.db is not None and not window.db.read_only
        first_snapshot = link is not None and (
            link.status == LINK_UNLINKED or (getattr(self, "_repository_loaded", False) and not self.manifests)
        )
        self.create_button.setText("Create First Snapshot…" if first_snapshot else "Create Snapshot…")
        self.create_button.setEnabled(
            link is not None and (link.linked or link.status == LINK_UNLINKED) and writable and not busy
        )
        self.delete_button.setEnabled(manifest is not None and not busy)
        self.delete_button.setVisible(manifest is not None)
        self.selected_snapshot_label.setVisible(manifest is not None)
        self.volumes_group.setVisible(manifest is not None)
        self.prune_action.setEnabled(has_repository and not busy)
        self.verify_action.setEnabled(has_repository and not busy)
        self.repository_tools_button.setVisible(has_repository)
        self.repository_tools_button.setEnabled(has_repository and not busy)
        needs_location = link is not None and link.offline
        self.relink_button.setVisible(needs_location)
        self.relink_button.setEnabled(needs_location and writable and not busy)
        self.refresh_button.setEnabled(not busy)
        self.snapshot_empty_label.setVisible(not self.manifests)
        self.snapshot_empty_label.setText(
            "Create your first snapshot to start this catalogue's saved history." if first_snapshot else
            "Locate the repository or reconnect its drive to view snapshots." if needs_location else
            "No snapshots are available."
        )
        self.snapshot_table.setVisible(bool(self.manifests))
        self.volume_table.setVisible(manifest is not None and bool(manifest.volumes))

    # -- actions --------------------------------------------------------

    def relink(self) -> None:
        self.window_ref.relink_repository()
        self.refresh()

    def create_snapshot(self) -> None:
        self.window_ref.create_snapshot_from_menu()

    def delete_snapshot(self) -> None:
        manifest = self.selected_manifest()
        if manifest is None or self.repository_path is None:
            return
        self.window_ref.delete_snapshot(self.repository_path, manifest)

    def prune(self) -> None:
        if self.repository_path is not None:
            self.window_ref.prune_repository(self.repository_path)

    def verify(self) -> None:
        if self.repository_path is not None:
            self.window_ref.verify_repository(self.repository_path)


class CatalogueSettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Catalogue Settings - {APP_NAME}")
        self.setMinimumWidth(520)

        hashing_group = QGroupBox("Catalogue hashing")
        self.hash_algorithm_combo = QComboBox()
        for algorithm, label in HASH_LABELS.items():
            self.hash_algorithm_combo.addItem(label, algorithm)
        self.hash_algorithm_explanation = QLabel()
        self.hash_algorithm_explanation.setWordWrap(True)
        hashing_form = QFormLayout(hashing_group)
        hashing_form.addRow("Hash algorithm:", self.hash_algorithm_combo)
        hashing_form.addRow(self.hash_algorithm_explanation)
        self.set_catalogue_hash_settings(DEFAULT_HASH_ALGORITHM, locked=False, catalogue_open=False)

        persistence_note = QLabel(
            "Hashing is saved in this catalogue. Configure previews in Window > Preview Manager."
        )
        persistence_note.setWordWrap(True)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(hashing_group)
        content_layout.addWidget(persistence_note)
        content_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
        self.resize(560, 300)

    def set_catalogue_hash_settings(
        self, algorithm: str, *, locked: bool, catalogue_open: bool = True, read_only: bool = False,
    ) -> None:
        self.hash_algorithm_combo.setCurrentIndex(self.hash_algorithm_combo.findData(algorithm))
        self.hash_algorithm_combo.setEnabled(catalogue_open and not locked and not read_only)
        if not catalogue_open:
            text = "Open or create a catalogue to choose its hash algorithm."
        elif read_only and not locked:
            text = "This catalogue is open read-only. Reopen it for editing to choose its hash algorithm."
        elif locked:
            text = (
                "Locked for this catalogue. The algorithm cannot change after the first volume "
                "is added, even if all volumes are later deleted."
            )
        else:
            text = (
                "Used by every drive in this catalogue. Choose before adding the first volume; "
                "the choice then locks permanently. All options read every byte. "
                "SHA-256 and BLAKE3 are cryptographic; XXH3-128 prioritises speed."
            )
        self.hash_algorithm_explanation.setText(text)

    def selected_hash_algorithm(self) -> str:
        return str(self.hash_algorithm_combo.currentData())

class PreferencesDialog(QDialog):
    """Application settings: appearance, search, and the local FFmpeg executable."""

    appearance_changed = Signal(str, str, str)

    def __init__(
        self,
        include_paths: bool,
        theme_style: str,
        color_mode: str,
        accent_color: str,
        parent: QWidget | None = None,
        preview_settings: PreviewSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"App Settings - {APP_NAME}")
        self.setMinimumWidth(520)

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

        ffmpeg_group = QGroupBox("FFmpeg")
        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_edit.setPlaceholderText("Leave empty to search PATH for ffmpeg")
        self.ffmpeg_browse_button = QPushButton("Browse...")
        self.ffmpeg_browse_button.clicked.connect(self.browse_ffmpeg)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(self.ffmpeg_browse_button)
        ffmpeg_form = QFormLayout(ffmpeg_group)
        ffmpeg_form.addRow("FFmpeg executable (optional):", ffmpeg_row)
        ffmpeg_note = QLabel(
            "Used to generate video previews for all catalogues on this computer. "
            "For preview quality and storage, open a catalogue and choose "
            "Window > Preview Manager."
        )
        ffmpeg_note.setWordWrap(True)
        ffmpeg_form.addRow(ffmpeg_note)
        self.set_preview_settings(preview_settings or PreviewSettings())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(appearance_group)
        content_layout.addWidget(search_group)
        content_layout.addWidget(ffmpeg_group)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
        self.resize(620, 540)

    def preview_settings(self) -> PreviewSettings:
        return replace(self._preview_settings, ffmpeg_path=self.ffmpeg_edit.text().strip())

    def set_preview_settings(self, preview_settings: PreviewSettings) -> None:
        self._preview_settings = preview_settings
        self.ffmpeg_edit.setText(preview_settings.ffmpeg_path)

    def accept(self) -> None:  # type: ignore[override]
        candidate = self.preview_settings()
        if candidate.enabled and candidate != self._preview_settings:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                report = validate_preview_configuration(candidate, include_encode_tests=True)
            finally:
                QApplication.restoreOverrideCursor()
            if not report.passed:
                self.ffmpeg_edit.setText(self._preview_settings.ffmpeg_path)
                QMessageBox.warning(
                    self,
                    "FFmpeg",
                    "The FFmpeg executable did not pass preview validation, so "
                    "the previous executable was restored.\n\n"
                    + report.failure_summary(heading="Validation failed."),
                )
                return
        super().accept()

    def browse_ffmpeg(self) -> None:
        current = self.ffmpeg_edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.home())
        filters = "Executables (*.exe);;All files (*)" if os.name == "nt" else "All files (*)"
        chosen, _filter = QFileDialog.getOpenFileName(self, "Select FFmpeg Executable", start, filters)
        if chosen:
            self.ffmpeg_edit.setText(chosen)

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
             "Start on <b>Home</b>: open a recent catalogue, choose <b>Open Catalogue</b>, "
             "or choose <b>Create Empty Catalogue</b> and pick a name. "
             "Use the Home button or <b>File → Close Catalogue</b> before opening another. "
             "The <b>Window</b> menu opens settings, managers, catalogue information, and Help. "
             "App Settings and Help are always available; catalogue windows appear while a catalogue is open. "
             "A catalogue is a "
             "<code>.jvvv</code> folder: <code>catalogue.sqlite</code> lists the drives, "
             "<code>volumes/</code> holds one self-contained file per drive, and "
             "<code>index.sqlite</code> is the search and evidence index, which JVVV "
             "rebuilds from the other two whenever it is missing."),
            ("2. Add a volume",
             "Choose <b>File &gt; Add Volume</b>, select a connected drive or folder, "
             "and it scans automatically. Scans read every file to record a full "
             "content hash, so large drives take longer. "
             "Scan it again later to review and apply catalogue changes."),
            ("3. Browse offline",
             "Select a saved volume to explore its folder tree, even when the "
             "original drive is disconnected."),
            ("4. Search",
             "Find files and folders by name or extension across the stored catalogue. "
             "Path matching can be enabled under <b>Window &gt; App Settings</b>."),
            ("5. Review copy evidence",
             "Open <b>Backups</b> in the Browser to compare saved hashes. "
             "Select a drive to see matching paths and file counts from its saved scan."),
            ("6. Snapshots: version history and backup",
             "Open <b>Window &gt; Snapshot Manager</b> and choose <b>Create Snapshot</b> to record the whole catalogue in "
             "its snapshot repository (a <code>.jvvvrepo</code> folder, by default next "
             "to the catalogue). Only drives whose contents changed since the last "
             "snapshot are written, so taking a snapshot after rescanning one drive takes "
             "seconds and a few megabytes. <b>Snapshot Manager</b> lists every "
             "snapshot, shows what changed and the volumes it contains, "
             "deletes old snapshots, and reclaims unused space or verifies the "
             "repository. On <b>Home</b>, <b>Create New Catalogue from Repository</b> creates a "
             "catalogue folder from any repository. Snapshots do not include offline "
             "preview files; to back up previews, copy the configured preview directory "
             "separately with your normal file-copy or backup software."),
            ("7. Previews",
             "Under <b>Window &gt; Preview Manager &gt; Previews</b> choose a "
             "preview storage directory (any local, removable, or network location), "
             "pick the image and video quality you want to spend storage on, and tick "
             "<b>Generate previews while scanning</b>. Set the FFmpeg executable "
             "under <b>Window &gt; App Settings</b> if it is not on PATH. "
             "Choose <b>Apply</b> to save preview preferences in this catalogue; "
             "snapshots preserve them too. FFmpeg's executable stays local to this computer. "
             "Enabling previews tests "
             "the directory, the image encoder, and FFmpeg's H.264 (libx264) encoder; "
             "if anything fails, the box turns itself back off and the exact problem is "
             "shown. Previews are named by each file's content hash and preview profile, so "
             "duplicates, renamed files, and several catalogues sharing one directory "
             "reuse the same preview. After a scan, JVVV reports every preview that "
             "could not be created. Use <b>Open Preview</b> / <b>Play Preview</b> in "
             "an item's context menu to open the preview in your operating system's "
             "default image or video application — JVVV never displays or plays media "
             "itself. <b>Window &gt; Preview Manager</b> shows what the preview "
             "directory holds and lists previews not referenced by the open catalogue."),
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
        <p><b>Home → Open or Create Catalogue → Browse or Search → Close Catalogue</b></p>
        {section_html}
        <hr>
        <p><b>About</b><br>{APP_NAME} is GPLv3 open-source software by Joemt.<br>
        <a href="https://github.com/joedotmt/Joemt-Archive-View">Source code</a>
        &nbsp;·&nbsp; <a href="https://joe.mt">joe.mt</a></p>
        """


class _SynchronousIoCanceller:
    """Cancel blocking file I/O on the current worker thread on Windows."""

    def __init__(self) -> None:
        self._thread_handle: Any | None = None

    def start(self) -> None:
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
            self._thread_handle = kernel32.OpenThread(
                0x0001,
                False,
                kernel32.GetCurrentThreadId(),
            )
        except Exception:
            self._thread_handle = None

    def stop(self) -> None:
        handle = self._thread_handle
        self._thread_handle = None
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

    def cancel(self) -> None:
        handle = self._thread_handle
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


def _interrupt_database(db: Database | None) -> None:
    if db is None:
        return
    try:
        db.connection.interrupt()
    except Exception:
        pass


def _create_worker_thread(
    parent: QObject,
    worker: QObject,
    terminal_signals: tuple[Any, ...],
    cleanup: Callable[[], None],
) -> QThread:
    """Create a worker thread with the shared Qt lifecycle connections."""
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)  # type: ignore[attr-defined]
    for signal in terminal_signals:
        signal.connect(thread.quit)
        signal.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(cleanup)
    return thread


def _wait_for_worker_thread(
    current_thread: Callable[[], QThread | None],
) -> bool:
    """Keep the UI responsive while waiting briefly for cancellation."""
    for _ in range(50):
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
        thread = current_thread()
        if thread is None or not thread.isRunning():
            return True
        thread.wait(100)
    return False


class ScanWorker(QObject):
    catalogue_ready = Signal()
    progress = Signal(int, int, str)
    stats_progress = Signal(int, int, str, int, int)
    log_message = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        db_path: Path,
        volume_id: int,
        *,
        preview_settings: PreviewSettings | None = None,
        preview_skip_reason: str | None = None,
        volume_uuid: str | None = None,
        writer_lock=None,
        resolved_source_path: str | None = None,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.volume_id = volume_id
        # ``preview_settings`` is only set when previews are enabled AND the
        # scan-start preflight passed; ``preview_skip_reason`` records the
        # user's choice to scan without previews after a failed preflight.
        self.preview_settings = preview_settings
        self.preview_skip_reason = preview_skip_reason
        self.volume_uuid = volume_uuid
        self.writer_lock = writer_lock
        self.resolved_source_path = resolved_source_path
        self.cancel_requested = False
        self.destination_problem = ""
        self._io_canceller = _SynchronousIoCanceller()

    def _build_preview_service(self) -> PreviewService | None:
        if self.preview_settings is None:
            return None
        return PreviewService(
            self.preview_settings,
            log_callback=self.log_message.emit,
        )

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        initial_scan = False
        volume = None
        try:
            self._io_canceller.start()
            if self.volume_uuid is not None:
                check_archive_write_locations(None, self.db_path)
                self.preview_settings, self.preview_skip_reason = prepare_operation_previews(self.preview_settings)
            db = Database(self.db_path) if self.writer_lock is None else Database(
                self.db_path, initialize=False, create=False,
            )
            if self.volume_uuid is not None:
                volume = db.get_volume(self.volume_id)
                if volume is None or volume["uuid"] != self.volume_uuid:
                    raise OSError("The queued volume was removed from the catalogue.")
                initial_scan = not volume["last_scan_at"]
            preview_statistics: PreviewStatistics | None = None
            if self.preview_skip_reason is not None:
                preview_statistics = skipped_preflight_statistics(self.preview_skip_reason)
                if self.volume_uuid is not None:
                    preview_statistics.message = f"Offline previews skipped. {self.preview_skip_reason}"
            with ExitStack() as resources:
                stage = resources.enter_context(StagedScan(db, volume, self.writer_lock)) if self.writer_lock else None

                def catalogue_ready():
                    if stage is not None:
                        stage.publish()
                    self.catalogue_ready.emit()

                scanner = VolumeScanner(
                    stage.index if stage is not None else db,
                    progress_callback=self.progress.emit,
                    stats_progress_callback=self.stats_progress.emit,
                    cancel_callback=lambda: self.cancel_requested,
                    require_complete=self.volume_uuid is not None,
                    preview_service=self._build_preview_service(),
                    preview_statistics=preview_statistics,
                    catalogue_ready_callback=catalogue_ready,
                    catalogue_path=self.db_path,
                    resolved_source_path=self.resolved_source_path,
                )
                try:
                    result = scanner.scan(self.volume_id)
                finally:
                    if stage is not None:
                        stage.publish()
            registration_warning = ""
            if initial_scan and result.status in COMPLETED_SCAN_STATUSES:
                try:
                    with self.writer_lock if self.writer_lock is not None else nullcontext():
                        register_scanned_drive(db, db.get_volume(self.volume_id))
                except (OSError, ValueError, sqlite3.Error) as exc:
                    registration_warning = f"Could not save the drive ID. {exc}"
                    self.log_message.emit(registration_warning)
            if result.status not in COMPLETED_SCAN_STATUSES and result.status != "cancelled":
                self.destination_problem = operation_destination_problem(volume)
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
                    "preview": result.preview or {},
                    "media_metadata_reused": result.media_metadata_reused,
                    "phase_seconds": result.phase_seconds,
                    "registration_warning": registration_warning,
                }
            )
        except Exception:
            details = traceback.format_exc()
            self.destination_problem = operation_destination_problem(volume)
            self.failed.emit(details)
        finally:
            if db is not None:
                db.close()
            self._io_canceller.stop()

    @Slot()
    def cancel(self) -> None:
        self.cancel_requested = True
        self._io_canceller.cancel()


@dataclass
class RunningOperation:
    job: Operation
    disks: OperationDisks
    worker: QObject
    thread: QThread
    events: QueueJobEvents
    message: str = "Preparing…"
    done: int = 0
    total: int = 0
    result: tuple[str, str] | None = None
    verifying: bool = False
    following_drive_checks: dict[str, QueueDriveCheck] | None = None


class QueueJobEvents(QObject):
    """Give each worker a GUI-thread receiver carrying its queue ID."""

    def __init__(self, window, job_id):
        super().__init__(window)
        self.window = window
        self.job_id = job_id

    @Slot(str, object, object)
    def archive_progress(self, label, done, total):
        self.window.on_archive_progress(label, done, total, job_id=self.job_id)

    @Slot()
    def archive_finished(self):
        self.window.on_archive_finished(self.job_id)

    @Slot()
    def catalogue_ready(self):
        self.window.on_scan_catalogue_ready(job_id=self.job_id)

    @Slot(int, int, str)
    def scan_progress(self, files, folders, path):
        self.window.on_scan_progress(files, folders, path, job_id=self.job_id)

    @Slot(int, int, str, int, int)
    def scan_stats_progress(self, files, folders, message, done, total):
        self.window.on_scan_stats_progress(files, folders, message, done, total, job_id=self.job_id)

    @Slot(dict)
    def scan_result(self, result):
        self.window.on_scan_finished(result, job_id=self.job_id)

    @Slot(str)
    def scan_failed(self, details):
        self.window.on_scan_failed(details, job_id=self.job_id)

    @Slot()
    def scan_thread_finished(self):
        self.window.clear_scan_worker(self.job_id)


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


class _CancellableWorker(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_requested = False

    def cancel(self) -> None:
        self.cancel_requested = True


class _RepositoryWorker(_CancellableWorker):
    """Run one long snapshot-repository (or conversion) operation off the GUI thread."""

    progress = Signal(object, object, str)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(object, str)

    def __init__(self, operation: Callable[..., Any], *, description: str) -> None:
        super().__init__()
        self.operation = operation
        self.description = description

    @Slot()
    def run(self) -> None:
        result: Any = None
        error: Exception | None = None
        details = ""
        try:
            result = self.operation(
                progress_callback=self._report_progress,
                cancel_callback=lambda: self.cancel_requested,
            )
        except SnapshotCancelled:
            self.cancel_requested = True
        except Exception as exc:
            error = exc
            details = traceback.format_exc()

        if self.cancel_requested:
            self.cancelled.emit()
        elif error is not None:
            self.failed.emit(error, details)
        else:
            self.finished.emit(result)

    def _report_progress(self, progress: Any) -> None:
        self.progress.emit(
            object_value(progress, "completed", 0),
            object_value(progress, "total", 0),
            str(object_value(progress, "message", "") or ""),
        )


class _SnapshotCloseProgressDialog(QProgressDialog):
    @Slot(object, object, str)
    def update_progress(self, completed: object, total: object, message: str) -> None:
        if self.wasCanceled():
            return
        self.setLabelText(message or "Creating snapshot…")
        maximum = max(0, int(total or 0))
        self.setRange(0, 1000 if maximum else 0)
        if maximum:
            self.setValue(min(1000, max(0, int(completed or 0)) * 1000 // maximum))


def snapshot_worker(repository_path: Path, catalogue_path: Path, label: str) -> _RepositoryWorker:
    def operation(*, progress_callback, cancel_callback) -> SnapshotResult:
        repository = SnapshotRepository.open_or_create(repository_path)
        return repository.create_snapshot(
            catalogue_path,
            label=label,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    return _RepositoryWorker(operation, description="snapshot")


def snapshot_restore_worker(
    repository_path: Path,
    snapshot_id: str,
    target_path: Path,
    *,
    overwrite: bool,
) -> _RepositoryWorker:
    def operation(*, progress_callback, cancel_callback) -> RestoreResult:
        repository = SnapshotRepository.open(repository_path)
        return repository.restore_snapshot(
            snapshot_id,
            target_path,
            overwrite=overwrite,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    return _RepositoryWorker(operation, description="restore")


def repository_prune_worker(repository_path: Path) -> _RepositoryWorker:
    def operation(*, progress_callback, cancel_callback) -> PruneResult:
        return SnapshotRepository.open(repository_path).prune(progress_callback=progress_callback)

    return _RepositoryWorker(operation, description="prune")


def repository_verify_worker(repository_path: Path) -> _RepositoryWorker:
    def operation(*, progress_callback, cancel_callback) -> VerifyResult:
        return SnapshotRepository.open(repository_path).verify(
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    return _RepositoryWorker(operation, description="verify")


@dataclass(frozen=True)
class SimpleProgress:
    completed: int
    total: int
    message: str


class _DatabaseWorker(_CancellableWorker):
    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = db_path
        self._active_db: Database | None = None

    def cancel(self) -> None:
        super().cancel()
        _interrupt_database(self._active_db)


class CatalogueInfoWorker(_DatabaseWorker):
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

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

class BackupAnalysisWorker(_DatabaseWorker):
    progress = Signal(int, int, str)
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        summary: Any = None
        error_details: str | None = None
        try:
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

class SearchWorker(_CancellableWorker):
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
                    # Name results remain usable if auxiliary backup evidence
                    # cannot be read.
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
            engine = BackupAnalysisEngine(db)
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
        self._io_canceller = _SynchronousIoCanceller()

    def _check_cancelled(self) -> None:
        if self.cancel_requested:
            raise _CatalogueOpenCancelled

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        lock: QLockFile | None = None
        result: tuple[Database, list[VolumeItem], VolumeConnections, QLockFile] | None = None
        error: Exception | None = None
        was_cancelled = False
        self._io_canceller.start()
        try:
            self._check_cancelled()
            self.progress.emit(0, 0, "Acquiring catalogue lock...")
            lock = acquire_catalogue_lock(self.path)
            self._check_cancelled()
            self.progress.emit(0, 0, "Opening and checking catalogue...")
            # This connection is created in the worker and, once this method is
            # finished with it, becomes the main-window connection.  Opening
            # also reconciles the index with the volume files, which can take a
            # while when the index had to be rebuilt.
            db = open_catalogue(
                self.path,
                check_same_thread=False,
                progress_callback=lambda done, total, message: self.progress.emit(
                    done, total, message
                ),
            )
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
            paths: dict[VolumeLocationKey, str] = {}
            for index, volume in enumerate(volumes, start=1):
                self._check_cancelled()
                path = resolver.resolve(volume)
                if path is not None:
                    paths[volume_location_key(volume)] = path
                items.append(volume_item_from_record(volume, path is not None))
                self.progress.emit(
                    index,
                    max(total, 1),
                    f"Loading volumes... {index}/{total}",
                )
            if not volumes:
                self.progress.emit(1, 1, "Catalogue ready")
            self._check_cancelled()
            result = (db, items, VolumeConnections(snapshots, paths), lock)
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
            self._io_canceller.stop()

        if result is not None:
            self.finished.emit(*result)
        elif was_cancelled:
            self.cancelled.emit()
        elif error is not None:
            self.failed.emit(error)

    def cancel(self) -> None:
        self.cancel_requested = True
        _interrupt_database(self._active_db)
        self._io_canceller.cancel()


class MainWindow(QMainWindow):
    queue_drive_probe_finished = Signal()
    catalogue_file_open_finished = Signal(object, object)

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
        self.preview_settings: PreviewSettings = load_preview_settings(self.settings)
        self.preview_cache_dialog: PreviewCacheDialog | None = None
        self.preview_store_thread: QThread | None = None
        self.preview_store_worker: QObject | None = None
        self.preview_store_operation = ""
        self.last_preview_statistics: PreviewStatistics | None = None
        self.last_preview_operation_id: str | None = None
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
        self.archive_dialog: ArchiveDialog | None = None
        self.operations_dialog: OperationsDialog | None = None
        self.operations_queue: OperationsQueue | None = None
        self.running_operations: dict[str, RunningOperation] = {}
        self._queue_drive_probe: tuple[Database, Future[dict[str, QueueDriveCheck]]] | None = None
        self._file_open_requests: dict[FileOpenKey, Future[bool]] = {}
        self.queue_writer_lock = Lock()
        self.project_sessions = {}
        self.scan_queue_result: tuple[str, str] | None = None
        self.scan_cancel_requested = False
        self.delete_thread: QThread | None = None
        self.delete_worker: DeleteVolumeWorker | None = None
        self.catalogue_info_thread: QThread | None = None
        self.catalogue_info_worker: CatalogueInfoWorker | None = None
        self.repository_thread: QThread | None = None
        self.repository_worker: _RepositoryWorker | None = None
        self.repository_operation = ""
        self._catalogue_close_in_progress = False
        self._snapshot_close_pending = False
        self._snapshot_close_succeeded = False
        self.pending_restored_catalogue_path: Path | None = None
        self.pending_restored_status_message = "New catalogue created from repository and opened."
        self.snapshot_history_dialog: SnapshotHistoryDialog | None = None
        self._repository_prompt_key: tuple[object, str, str] | None = None
        self.backup_analysis_thread: QThread | None = None
        self.backup_analysis_worker: BackupAnalysisWorker | None = None
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
        self.backup_analysis_state: Any = None
        self.volume_full_delegate = VolumeFullDelegate(self)
        self.volume_connection_delegate = ResponsiveStatusDelegate(self)
        self.search_connection_delegate = ResponsiveStatusDelegate(self)
        self.catalogue_actions: list[QAction] = []
        self._catalogue_ui_open = False
        self._catalogue_ui_busy = False
        self._catalogue_ui_loading = False
        self.catalogue_widgets: list[QWidget] = []
        self.scan_blocked_actions: list[QAction] = []
        self.scan_blocked_widgets: list[QWidget] = []
        self.base_ui_font = QFont(QApplication.font())
        self.ui_zoom = 1.0
        self._connected_volume_snapshots: list[VolumeSnapshot] = []
        self._connected_volume_paths: dict[VolumeLocationKey, str] = {}
        self._connected_volume_signature: tuple[tuple[str, str, str], ...] = ()
        self._volume_connection_probe: tuple[Database, Future[VolumeConnections]] | None = None
        self.volume_connection_timer = QTimer(self)
        self.volume_connection_timer.setInterval(VOLUME_CONNECTION_POLL_INTERVAL_MS)
        self.volume_connection_timer.timeout.connect(self.check_connected_volumes)
        self.volume_connection_refresh_timer = QTimer(self)
        self.volume_connection_refresh_timer.setSingleShot(True)
        self.volume_connection_refresh_timer.setInterval(VOLUME_CONNECTION_REFRESH_DELAY_MS)
        self.volume_connection_refresh_timer.timeout.connect(self.refresh_after_connected_volumes_changed)
        self.operations_timer = QTimer(self)
        self.operations_timer.setInterval(VOLUME_CONNECTION_POLL_INTERVAL_MS)
        self.operations_timer.timeout.connect(self._pump_operations)
        self.queue_drive_probe_finished.connect(self._pump_operations, Qt.ConnectionType.QueuedConnection)
        self.catalogue_file_open_finished.connect(
            self._finish_catalogue_file_open, Qt.ConnectionType.QueuedConnection,
        )
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
        self.app_menu_bar = self.menuBar()
        self.file_menu = self.app_menu_bar.addMenu("&File")

        self.open_catalogue_action = QAction("Open Catalogue\u2026", self)
        self.open_catalogue_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Open))
        self.open_catalogue_action.triggered.connect(self.open_catalogue_from_dialog)
        self.file_menu.addAction(self.open_catalogue_action)

        self.new_catalogue_action = QAction("Create Empty Catalogue\u2026", self)
        self.new_catalogue_action.setShortcut(QKeySequence(QKeySequence.StandardKey.New))
        self.new_catalogue_action.triggered.connect(self.new_catalogue)
        self.file_menu.addAction(self.new_catalogue_action)

        self.restore_snapshot_action = QAction("Create New Catalogue from Repository\u2026", self)
        self.restore_snapshot_action.setToolTip(
            "Choose a repository and snapshot, then create and open a new catalogue"
        )
        self.restore_snapshot_action.triggered.connect(self.restore_snapshot_from_dialog)
        self.file_menu.addAction(self.restore_snapshot_action)
        self.home_actions = [
            self.new_catalogue_action, self.open_catalogue_action, self.restore_snapshot_action,
        ]
        self.home_file_separator = self.file_menu.addSeparator()

        self.new_volume_action = QAction("Add Volume\u2026", self)
        self.new_volume_action.triggered.connect(self.add_volume)
        self.file_menu.addAction(self.new_volume_action)
        self.archive_project_action = QAction("Archive Project\u2026", self)
        self.archive_project_action.triggered.connect(lambda: self.archive_project())
        self.file_menu.addAction(self.archive_project_action)
        self.workspace_file_separator = self.file_menu.addSeparator()

        self.open_catalogue_location_action = QAction("Open Catalogue Folder", self)
        self.open_catalogue_location_action.triggered.connect(self.open_catalogue_location)
        self.file_menu.addAction(self.open_catalogue_location_action)

        self.close_catalogue_action = QAction("Close Catalogue", self)
        self.close_catalogue_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Close))
        self.close_catalogue_action.triggered.connect(lambda: self.close_catalogue())
        self.file_menu.addAction(self.close_catalogue_action)

        self.workspace_exit_separator = self.file_menu.addSeparator()

        self.exit_action = QAction("Exit", self)
        self.exit_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Quit))
        self.exit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.exit_action.triggered.connect(QApplication.instance().quit)
        self.file_menu.addAction(self.exit_action)

        self.view_menu = self.app_menu_bar.addMenu("&View")
        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_in_action.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        self.zoom_in_action.triggered.connect(self.zoom_in)
        self.view_menu.addAction(self.zoom_in_action)

        self.zoom_out_action = QAction("Zoom Out", self)
        self.zoom_out_action.setShortcut("Ctrl + -")
        self.zoom_out_action.triggered.connect(self.zoom_out)
        self.view_menu.addAction(self.zoom_out_action)

        self.window_menu = self.app_menu_bar.addMenu("&Window")
        self.preferences_action = QAction("App Settings", self)
        self.preferences_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.preferences_action.triggered.connect(self.show_preferences)
        self.window_menu.addAction(self.preferences_action)

        self.catalogue_settings_action = QAction("Catalogue Settings", self)
        self.catalogue_settings_action.triggered.connect(self.show_catalogue_settings)
        self.window_menu.addAction(self.catalogue_settings_action)
        self.window_settings_separator = self.window_menu.addSeparator()

        self.operations_queue_action = QAction("Queue Manager", self)
        self.operations_queue_action.triggered.connect(self.show_operations_queue)
        self.window_menu.addAction(self.operations_queue_action)

        self.snapshot_history_action = QAction("Snapshot Manager", self)
        self.snapshot_history_action.setToolTip(
            "Create and browse snapshots, delete old snapshots, and manage the repository"
        )
        self.snapshot_history_action.triggered.connect(self.show_snapshot_history)
        self.window_menu.addAction(self.snapshot_history_action)

        self.backup_evidence_action = QAction("Backups", self)
        self.backup_evidence_action.triggered.connect(lambda: self.show_backups())
        self.backup_evidence_action.setToolTip(BACKUP_COLUMN_HEADER_TOOLTIP)
        self.window_menu.addAction(self.backup_evidence_action)

        self.preview_cache_action = QAction("Preview Manager", self)
        self.preview_cache_action.setToolTip(
            "Inspect the preview directory and list previews not referenced by "
            "this catalogue"
        )
        self.preview_cache_action.triggered.connect(self.show_preview_cache)
        self.window_menu.addAction(self.preview_cache_action)
        self.window_information_separator = self.window_menu.addSeparator()

        self.catalogue_info_action = QAction("Catalogue Information", self)
        self.catalogue_info_action.triggered.connect(self.show_catalogue_info)
        self.window_menu.addAction(self.catalogue_info_action)

        self.help_action = QAction("Help", self)
        self.help_action.setShortcut(QKeySequence(QKeySequence.StandardKey.HelpContents))
        self.help_action.triggered.connect(self.show_help)
        self.window_menu.addAction(self.help_action)
        # Some Qt platform plugins require the PySide menu-action wrappers to
        # stay alive alongside their C++ owners.
        self.menu_actions = self.app_menu_bar.actions()

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
        if hasattr(self, "catalogue_name_label"):
            name_font = QFont(QApplication.font())
            name_font.setBold(True)
            self.catalogue_name_label.setFont(name_font)

        for table_name in ("volume_table", "file_table", "search_table"):
            table = getattr(self, table_name, None)
            if table is not None:
                table.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
                table.verticalHeader().setDefaultSectionSize(self.scaled_ui_value(24))

        if hasattr(self, "folder_tree"):
            self.folder_tree.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
            self.folder_tree.setIndentation(self.scaled_ui_value(16))
        if hasattr(self, "browser_backup_filter_combo"):
            self.browser_backup_filter_combo.setMaximumWidth(self.scaled_ui_value(280))
        for button in (getattr(self, "home_button", None), getattr(self, "up_button", None)):
            if button is None:
                continue
            navigation_size = self.scaled_ui_value(26)
            button.setFixedSize(navigation_size, navigation_size)
            button.setIconSize(QSize(self.scaled_ui_value(16), self.scaled_ui_value(16)))

        if hasattr(self, "scan_progress"):
            configure_progress_bar(self.scan_progress, self.ui_zoom)
        if hasattr(self, "catalogue_loading_progress"):
            configure_progress_bar(self.catalogue_loading_progress, self.ui_zoom)
        if hasattr(self, "detail_full"):
            configure_progress_bar(self.detail_full, self.ui_zoom)
        if hasattr(self, "workspace_splitter"):
            self.workspace_splitter.setHandleWidth(self.scaled_ui_value(3))
        if hasattr(self, "browser_splitter"):
            self.browser_splitter.setHandleWidth(self.scaled_ui_value(3))
        if hasattr(self, "welcome_new_button"):
            for button in self.home_buttons:
                button.setMinimumWidth(self.scaled_ui_value(320))
            self.refresh_recent_catalogues()
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
        self.catalogue_header = QWidget()
        header_layout = QHBoxLayout(self.catalogue_header)
        self.home_button = QToolButton()
        self.home_button.setObjectName("navigationButton")
        self.home_button.setIcon(home_icon(QApplication.palette()))
        self.home_button.setAccessibleName("Return to Home")
        self.home_button.setToolTip("Close this catalogue and return to Home")
        navigation_size = self.scaled_ui_value(26)
        self.home_button.setFixedSize(navigation_size, navigation_size)
        self.home_button.setIconSize(QSize(self.scaled_ui_value(16), self.scaled_ui_value(16)))
        self.home_button.clicked.connect(self.close_catalogue_action.trigger)
        self.catalogue_name_label = QLabel()
        name_font = self.catalogue_name_label.font()
        name_font.setBold(True)
        self.catalogue_name_label.setFont(name_font)
        self.catalogue_path_label = QLabel()
        self.catalogue_path_label.setObjectName("mutedLabel")
        self.catalogue_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.catalogue_path_label.setWordWrap(True)
        identity = QVBoxLayout()
        identity.addWidget(self.catalogue_name_label)
        identity.addWidget(self.catalogue_path_label)
        header_layout.addWidget(self.home_button)
        header_layout.addLayout(identity, 1)
        self.queue_summary_label = QLabel("No pending changes")
        self.queue_summary_label.setObjectName("mutedLabel")
        self.queue_summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.queue_summary_label.setWordWrap(True)
        self.queue_summary_label.setMaximumWidth(320)
        self.queue_manager_button = QPushButton("Queue Manager")
        self.queue_manager_button.clicked.connect(self.operations_queue_action.trigger)
        self.apply_queue_button = QPushButton("Apply")
        self.apply_queue_button.setToolTip("Run the pending changes in the queue")
        self.apply_queue_button.clicked.connect(self.apply_operations)
        header_layout.addWidget(self.queue_summary_label)
        header_layout.addWidget(self.queue_manager_button)
        header_layout.addWidget(self.apply_queue_button)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.catalogue_header)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

    def _build_welcome_page(self) -> QWidget:
        page = QScrollArea()
        page.setObjectName("welcomePage")
        page.setWidgetResizable(True)
        page.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.welcome_layout = layout
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(14)

        title = QLabel("Home")
        title.setObjectName("welcomeTitle")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 8)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome_title_label = title

        description = QLabel("Open a catalogue, start an empty one, or create one from a repository snapshot.")
        description.setObjectName("mutedLabel")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description.setWordWrap(True)

        self.welcome_new_button = QPushButton("Create Empty Catalogue…")
        self.welcome_open_button = QPushButton("Open Catalogue…")
        self.welcome_open_button.setObjectName("primaryButton")
        self.welcome_restore_button = QPushButton("Create New Catalogue from Repository…")
        self.welcome_settings_button = QPushButton("App Settings…")
        self.home_buttons = [
            self.welcome_open_button, self.welcome_new_button, self.welcome_restore_button,
        ]
        for button in self.home_buttons:
            button.setMinimumWidth(320)

        recent_group = QGroupBox("Recent catalogues")
        recent_group.setMaximumWidth(900)
        recent_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        recent_layout = QVBoxLayout(recent_group)
        recent_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.recent_catalogue_table = ListTableWidget(["Catalogue", "Location"])
        self.recent_catalogue_table.setColumnWidth(0, 220)
        self.recent_catalogue_table.doubleClicked.connect(lambda _index: self.open_recent_catalogue())
        self.recent_catalogue_table.itemSelectionChanged.connect(self.update_recent_catalogue_buttons)
        self.recent_empty_label = QLabel("Catalogues you open will appear here, most recent first.")
        self.recent_empty_label.setObjectName("mutedLabel")
        self.recent_open_button = QPushButton("Open Selected")
        self.recent_open_button.clicked.connect(self.open_recent_catalogue)
        self.recent_remove_button = QPushButton("Remove from List")
        self.recent_remove_button.setToolTip("Forget this recent entry; the catalogue stays on disk")
        self.recent_remove_button.clicked.connect(self.remove_recent_catalogue)
        recent_buttons = QHBoxLayout()
        recent_buttons.addWidget(self.recent_open_button)
        recent_buttons.addWidget(self.recent_remove_button)
        recent_buttons.addStretch(1)
        recent_layout.addWidget(self.recent_empty_label)
        recent_layout.addWidget(self.recent_catalogue_table)
        recent_layout.addLayout(recent_buttons)

        layout.addWidget(title)
        layout.addWidget(description)
        layout.addSpacing(8)
        layout.addWidget(self.welcome_open_button, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.welcome_new_button, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.welcome_restore_button, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(8)
        layout.addWidget(recent_group)
        layout.addWidget(self.welcome_settings_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.refresh_recent_catalogues()
        page.setWidget(content)
        return page

    def _build_loading_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("loadingPage")
        layout = QVBoxLayout(page)
        self.loading_layout = layout
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(14)

        title = QLabel("Opening Catalogue")
        self.catalogue_loading_title_label = title
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

    def refresh_recent_catalogues(self) -> None:
        paths = load_recent_catalogue_paths(self.settings)
        self.recent_catalogue_table.setRowCount(0)
        for path in paths:
            self.recent_catalogue_table.append_text_row((Path(path).name, path), user_data=path, tooltip=path)
        self.recent_empty_label.setVisible(not paths)
        self.recent_catalogue_table.setVisible(bool(paths))
        self.recent_open_button.setVisible(bool(paths))
        self.recent_remove_button.setVisible(bool(paths))
        if paths:
            table = self.recent_catalogue_table
            row_height = max(table.fontMetrics().height() + 12, self.scaled_ui_value(26))
            table.verticalHeader().setDefaultSectionSize(row_height)
            table.setFixedHeight(min(
                self.scaled_ui_value(250),
                table.horizontalHeader().sizeHint().height() + len(paths) * row_height
                + table.frameWidth() * 2 + 4,
            ))
            self.recent_catalogue_table.selectRow(0)
        self.update_recent_catalogue_buttons()

    def selected_recent_catalogue(self) -> str | None:
        items = self.recent_catalogue_table.selectedItems()
        return str(items[0].data(Qt.ItemDataRole.UserRole)) if items else None

    def _home_commands_available(self) -> bool:
        return (
            self.db is None
            and not self._catalogue_ui_open
            and not self._catalogue_ui_busy
            and not self._catalogue_ui_loading
            and not self._catalogue_open_in_progress()
            and self.repository_worker is None
        )

    def update_recent_catalogue_buttons(self) -> None:
        enabled = self._home_commands_available() and self.selected_recent_catalogue() is not None
        self.recent_open_button.setEnabled(enabled)
        self.recent_remove_button.setEnabled(enabled)

    def remember_recent_catalogue(self, path: Path) -> None:
        location = os.path.abspath(os.path.expanduser(str(path)))
        key = os.path.normcase(location)
        paths = [location] + [
            existing for existing in load_recent_catalogue_paths(self.settings)
            if os.path.normcase(existing) != key
        ]
        self.settings.setValue(RECENT_CATALOGUE_PATHS_SETTING, paths[:MAX_RECENT_CATALOGUES])
        self.settings.setValue(LAST_CATALOGUE_PATH_SETTING, location)
        self.settings.sync()
        self.refresh_recent_catalogues()

    def open_recent_catalogue(self) -> None:
        if not self._home_commands_available():
            return
        path = self.selected_recent_catalogue()
        if path is not None:
            self.open_catalogue_path(Path(path))

    def remove_recent_catalogue(self) -> None:
        if not self._home_commands_available():
            return
        path = self.selected_recent_catalogue()
        if path is None:
            return
        paths = [existing for existing in load_recent_catalogue_paths(self.settings) if existing != path]
        self.settings.setValue(RECENT_CATALOGUE_PATHS_SETTING, paths)
        self.settings.sync()
        self.refresh_recent_catalogues()

    def _build_catalogue_workspace(self) -> QWidget:
        self.volume_table = ListTableView()
        self.volume_table.setObjectName("volumeTable")
        self.volume_table.setModel(self.volume_model)
        self.configure_table_view(self.volume_table)
        self.volume_table.setItemDelegateForColumn(
            VOLUME_CONNECTION_COLUMN,
            self.volume_connection_delegate,
        )
        self.volume_table.setItemDelegateForColumn(VOLUME_FULL_COLUMN, self.volume_full_delegate)
        self.volume_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.volume_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
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
        self.operation_progress_label = QLabel()
        self.operation_progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.operation_progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.operation_progress_label.hide()

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
        right_layout.addWidget(self.operation_progress_label)
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
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
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
            (1, 0, "Connector", "connector"),
            (1, 2, "Status", "register_status"),
            (1, 4, "Condition", "condition"),
            (2, 0, "Capacity", "capacity"),
            (2, 2, "Used", "used"),
            (2, 4, "Free", "free"),
            (3, 0, "Files", "files"),
            (3, 2, "Folders", "folders"),
            (3, 4, "Last Scan", "last_scan"),
        ]
        for row, column, label, key in summary_rows:
            key_label = QLabel(label)
            key_label.setObjectName("detailKey")
            grid.addWidget(key_label, row, column)
            grid.addWidget(self._detail_value_label(key, word_wrap=key == "name"), row, column + 1)

        path_row = 4
        scan_path_label = QLabel("Scan Path")
        scan_path_label.setObjectName("detailKey")
        grid.addWidget(scan_path_label, path_row, 0)
        grid.addWidget(self._detail_value_label("path", word_wrap=True), path_row, 1, 1, 5)

        coverage_row = 5
        coverage_label = QLabel("Backups")
        coverage_label.setObjectName("detailKey")
        coverage_label.setToolTip(BACKUP_COLUMN_HEADER_TOOLTIP)
        grid.addWidget(coverage_label, coverage_row, 0)
        coverage_value = self._detail_value_label("backup_coverage")
        coverage_value.setToolTip(BACKUP_COLUMN_HEADER_TOOLTIP)
        grid.addWidget(coverage_value, coverage_row, 1, 1, 5)

        full_row = 6
        full_label = QLabel("Space used")
        full_label.setObjectName("detailKey")
        grid.addWidget(full_label, full_row, 0)
        grid.addWidget(self.detail_full, full_row, 1, 1, 5)
        return box

    def _build_browser_tab(self) -> QWidget:
        self.offline_label = QLabel("")
        self.offline_label.setObjectName("offlineNotice")
        self.offline_label.setVisible(False)
        self.folder_tree = ListTreeWidget()
        self.folder_tree.setObjectName("folderTree")
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

        backup_filter_label = QLabel("Filter by backup result")
        backup_filter_label.setObjectName("detailKey")
        self.browser_backup_filter_combo = QComboBox()
        self.browser_backup_filter_combo.setObjectName("browserBackupFilter")
        self.browser_backup_filter_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.browser_backup_filter_combo.setMinimumContentsLength(16)
        self.browser_backup_filter_combo.setMaximumWidth(self.scaled_ui_value(280))
        backup_filter_label.setBuddy(self.browser_backup_filter_combo)
        self.browser_backup_filter_combo.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        for label, key in BACKUP_FILTER_OPTIONS:
            self.browser_backup_filter_combo.addItem(label, key)
        self.browser_backup_filter_combo.setToolTip(BACKUP_COLUMN_HEADER_TOOLTIP)
        self.browser_status_strip = QLabel("")
        self.browser_status_strip.setObjectName("browserStatusStrip")
        self.browser_status_strip.setWordWrap(True)
        self.browser_status_strip.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.browser_status_strip.setVisible(False)

        self.browser_file_toolbar = QWidget()
        self.browser_file_toolbar.setObjectName("browserFileToolbar")
        backup_filter_row = QHBoxLayout(self.browser_file_toolbar)
        backup_filter_row.setContentsMargins(0, 0, 0, 0)
        backup_filter_row.setSpacing(4)
        backup_filter_row.addWidget(backup_filter_label)
        backup_filter_row.addWidget(self.browser_backup_filter_combo)
        backup_filter_row.addStretch(1)
        self.backups_button = QPushButton("Backups")
        self.backups_button.setCheckable(True)
        self.backups_button.toggled.connect(self.toggle_backups_panel)
        backup_filter_row.addWidget(self.backups_button)

        self.file_table = ListTableView()
        self.file_table.setObjectName("fileTable")
        self.file_table.setModel(self.browser_model)
        self.configure_table_view(self.file_table)
        self.file_table.setItemDelegateForColumn(1, OtherCopiesDelegate(self.file_table))
        self.file_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.file_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.file_table,
                {1: 230, 2: 140, 3: 95, 4: 155, 5: 280, 6: 90},
            ),
        )

        self.browser_file_pane = QWidget()
        self.browser_file_pane.setObjectName("browserFilePane")
        file_layout = QVBoxLayout(self.browser_file_pane)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(self.scaled_ui_value(5))
        file_layout.addWidget(self.browser_file_toolbar)
        file_layout.addWidget(self.browser_status_strip)
        file_layout.addWidget(self.file_table, 1)

        browser_splitter = QSplitter()
        self.browser_splitter = browser_splitter
        browser_splitter.setObjectName("browserSplitter")
        browser_splitter.setHandleWidth(3)
        browser_splitter.addWidget(self.folder_tree)
        self.backups_panel = BackupsPanel()
        self.backups_panel.hide()
        self.backups_panel.analyse_requested.connect(self.start_backup_analysis)
        self.backups_panel.cancel_requested.connect(self.cancel_backup_analysis)
        self.backups_panel.location_requested.connect(self.open_backup_location)
        self.backups_splitter = BackupsSplitter(Qt.Orientation.Horizontal)
        self.backups_splitter.addWidget(self.browser_file_pane)
        self.backups_splitter.addWidget(self.backups_panel)
        self.backups_splitter.setChildrenCollapsible(False)
        self.backups_splitter.setStretchFactor(0, 3)
        self.backups_splitter.setStretchFactor(1, 2)
        browser_splitter.addWidget(self.backups_splitter)
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
        layout.addWidget(browser_splitter, 1)
        return widget

    def configure_table_view(self, table: QTableView) -> None:
        table.setSortingEnabled(True)
        table.setIconSize(QSize(self.scaled_ui_value(18), self.scaled_ui_value(18)))
        table.verticalHeader().setDefaultSectionSize(self.scaled_ui_value(24))

        header = table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(True)

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
        search_row.addWidget(QLabel("Backup result"))
        search_row.addWidget(self.search_backup_filter_combo)
        search_row.addWidget(self.search_button)

        self.search_table = ListTableView()
        self.search_table.setObjectName("searchTable")
        self.search_table.setModel(self.search_model)
        self.configure_table_view(self.search_table)
        self.search_table.setItemDelegateForColumn(
            SEARCH_VOLUME_STATUS_COLUMN,
            self.search_connection_delegate,
        )
        self.search_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.search_table.setItemDelegateForColumn(1, OtherCopiesDelegate(self.search_table))
        self.search_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.search_table,
                {1: 230, 2: 80, 3: 150, 4: 85, 5: 280, 6: 95, 7: 155, 8: 120},
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
        self.welcome_restore_button.clicked.connect(self.restore_snapshot_from_dialog)
        self.welcome_settings_button.clicked.connect(self.show_preferences)
        self.catalogue_loading_cancel_button.clicked.connect(self.cancel_home_operation)
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
        self.file_table.doubleClicked.connect(self.open_browser_index)
        self.file_table.selectionModel().selectionChanged.connect(self.refresh_backups_panel)
        self.file_table.selectionModel().currentChanged.connect(self.refresh_backups_panel)
        self.file_table.customContextMenuRequested.connect(self.show_browser_context_menu)
        self.search_button.clicked.connect(self.perform_search)
        self.stop_scan_button.clicked.connect(self.cancel_current_operation)
        self.search_edit.returnPressed.connect(self.perform_search)
        self.search_backup_filter_combo.currentIndexChanged.connect(
            lambda _index: self.perform_search()
        )
        self.search_table.doubleClicked.connect(self.open_search_index)
        self.search_table.customContextMenuRequested.connect(self.show_search_context_menu)

        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.setShortcut("F5")
        self.refresh_action.triggered.connect(self.refresh_volumes)
        self.addAction(self.refresh_action)
        self.catalogue_actions = [
            self.close_catalogue_action,
            self.open_catalogue_location_action,
            self.snapshot_history_action,
            self.new_volume_action,
            self.archive_project_action,
            self.operations_queue_action,
            self.backup_evidence_action,
            self.catalogue_info_action,
            self.preview_cache_action,
            self.catalogue_settings_action,
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
            self.archive_project_action,
            self.snapshot_history_action,
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
            if hasattr(self, "home_button"):
                self.home_button.setIcon(home_icon(application.palette()))

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
        current_preview_settings = getattr(self, "preview_settings", None)
        preview_setter = getattr(dialog, "set_preview_settings", None)
        if callable(preview_setter) and current_preview_settings is not None:
            preview_setter(current_preview_settings)

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
        preview_getter = getattr(dialog, "preview_settings", None)
        new_preview_settings = preview_getter() if callable(preview_getter) else None
        preview_changed = (
            new_preview_settings is not None
            and new_preview_settings != current_preview_settings
        )
        if preview_changed:
            MainWindow.persist_ffmpeg_path(self, new_preview_settings.ffmpeg_path)
        if not search_changed and not appearance_changed:
            if preview_changed:
                self.statusBar().showMessage("App settings saved.", 3000)
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
        self.statusBar().showMessage("App settings saved.", 3000)

    def show_catalogue_settings(self) -> None:
        if self.db is None:
            return
        dialog = CatalogueSettingsDialog(self)
        catalogue = self.db
        dialog.set_catalogue_hash_settings(
            catalogue.hash_algorithm if catalogue is not None else DEFAULT_HASH_ALGORITHM,
            locked=catalogue.hash_algorithm_locked if catalogue is not None else False,
            read_only=catalogue.read_only if catalogue is not None else False,
            catalogue_open=catalogue is not None,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        hash_changed = False
        if catalogue is not None and dialog.selected_hash_algorithm() != catalogue.hash_algorithm:
            try:
                catalogue.set_hash_algorithm(dialog.selected_hash_algorithm())
                hash_changed = True
            except (CatalogueError, ValueError) as exc:
                QMessageBox.warning(self, "Catalogue Hashing", str(exc))
                return
        if hash_changed:
            self.statusBar().showMessage("Catalogue settings saved.", 3000)

    def search_placeholder_text(self) -> str:
        if self.search_include_paths:
            return "Search filename, extension, folder, or relative path"
        return "Search filename, extension, or folder"

    def persist_preview_settings(self, preview_settings: PreviewSettings) -> bool:
        """Apply catalogue preferences only after their registry write succeeds."""
        if self.db is None or self.db.read_only:
            QMessageBox.warning(self, "Previews", "Open this catalogue for editing to change its previews.")
            return False
        if self._catalogue_job_running() or self._catalogue_open_in_progress():
            self._show_catalogue_job_running_message()
            return False
        try:
            self.db.set_preview_settings(preview_settings)
            applied = self.db.preview_settings(ffmpeg_path=self.preview_settings.ffmpeg_path)
        except Exception as exc:
            QMessageBox.warning(self, "Preview Settings Not Saved", str(exc))
            return False
        self.preview_settings = applied
        dialog = getattr(self, "preview_cache_dialog", None)
        if dialog is not None:
            dialog.set_settings(applied)
        self.statusBar().showMessage("Preview settings saved in this catalogue.", 4000)
        return True

    def persist_ffmpeg_path(self, path: str) -> bool:
        """Keep the executable on this computer without changing catalogue preferences."""
        current = getattr(self, "preview_settings", PreviewSettings())
        updated = replace(current, ffmpeg_path=path.strip())
        save_preview_settings(self.settings, updated)
        self.preview_settings = updated
        dialog = getattr(self, "preview_cache_dialog", None)
        if dialog is not None:
            dialog.set_ffmpeg_path(updated.ffmpeg_path)
        return True

    def load_catalogue_preview_settings(self, db: Database) -> PreviewSettings:
        """Import legacy app preferences once, then use the catalogue's own configuration."""
        ffmpeg_path = load_preview_settings(self.settings).ffmpeg_path
        if not db.has_preview_settings():
            legacy = PreviewSettings.from_mapping({
                key: self.settings.value(key) for key in CATALOGUE_PREVIEW_SETTING_KEYS
            })
            if db.read_only:
                return replace(legacy, ffmpeg_path=ffmpeg_path)
            try:
                db.set_preview_settings(legacy)
            except Exception as exc:
                self.statusBar().showMessage(f"Preview preferences could not be migrated: {exc}", 10000)
                return replace(legacy, ffmpeg_path=ffmpeg_path)
        return db.preview_settings(ffmpeg_path=ffmpeg_path)

    # -- previews: opening -------------------------------------------
    def catalogue_item_media_kind(self, target: CatalogueItemRef) -> str | None:
        if target.is_folder:
            return None
        return media_kind_for_extension(PurePosixPath(target.relative_path).suffix)

    def preview_info_for_target(self, target: CatalogueItemRef):
        """Return (media_kind, PreviewFileInfo | None, file_row | None)."""

        media_kind = self.catalogue_item_media_kind(target)
        if media_kind not in {"image", "video"} or self.db is None:
            return media_kind, None, None
        file_row = self.db.get_file(target.item_id)
        if file_row is None:
            return media_kind, None, None
        info = inspect_preview_file(
            self.preview_settings, media_kind, file_row["content_hash"],
            file_row["content_hash_algorithm"] or DEFAULT_HASH_ALGORITHM,
        )
        return media_kind, info, file_row

    def preview_unavailable_reason(self, media_kind: str, info, file_row) -> str:
        if self.preview_settings.root_path is None:
            return "No preview storage directory is configured in Window > Preview Manager."
        if file_row is not None and file_row["content_hash"] is None:
            return "No content hash is recorded for this file, so no preview path can be derived."
        if info is None:
            return "The preview location could not be determined."
        if not info.exists:
            status = self.db.get_file_preview_status(int(file_row["id"])) if self.db is not None and file_row is not None else None
            if status is not None and str(status["status"]) == DB_STATUS_FAILED:
                detail = str(status["error_message"] or "").strip()
                return "Preview generation failed during the last scan" + (f": {detail}" if detail else ".")
            if not self.preview_settings.enabled:
                return (
                    "No preview exists at the expected location and preview "
                    "generation is disabled in Settings."
                )
            return "No preview exists at the expected location. Rescan the volume to generate it."
        if not info.valid:
            return f"The preview file exists but is not valid: {info.message}"
        return ""

    def open_preview_for_target(self, target: CatalogueItemRef, *, reveal: bool = False) -> None:
        media_kind, info, file_row = self.preview_info_for_target(target)
        if media_kind not in {"image", "video"}:
            return
        if info is None or not info.exists or not info.valid:
            # Spec §11/§46: a file at the preview path that fails validation
            # is corrupt and must never be launched as if it were valid.  Only
            # a genuinely absent file is recorded as missing; a corrupt one is
            # left in place for the next scan to detect and regenerate.
            if info is not None and not info.exists:
                # ``info is None`` means no preview root is configured, so
                # nothing is known about the file; leave the status untouched.
                self._mark_preview_missing(file_row)
            reason = self.preview_unavailable_reason(media_kind, info, file_row)
            QMessageBox.information(
                self,
                "Preview Not Available",
                f"The preview is not available.\n\n{reason}",
            )
            return
        try:
            # External applications only (spec §19, §44, §45): the operating
            # system's default image/video handler, or the file manager.
            open_in_file_manager(info.path, reveal=reveal)
        except Exception as exc:
            QMessageBox.warning(self, "Open Preview Failed", str(exc))

    def _mark_preview_missing(self, file_row) -> None:
        """Spec §46: a stored 'available' status must not survive a vanished file."""

        if self.db is None or file_row is None:
            return
        try:
            status = self.db.get_file_preview_status(int(file_row["id"]))
            if status is None or str(status["status"]) != DB_STATUS_AVAILABLE:
                return
            with self.db.transaction():
                self.db.replace_file_preview_status(
                    int(file_row["id"]),
                    {
                        "media_kind": status["media_kind"],
                        "profile_id": status["profile_id"],
                        "status": DB_STATUS_MISSING,
                        "source_hash": status["source_hash"],
                        "error_stage": "preview-missing",
                        "error_message": (
                            "The preview file recorded by the last scan is no longer present "
                            "at the expected location."
                        ),
                    },
                )
        except Exception as exc:
            # Never let bookkeeping stop the user from seeing the message,
            # but never hide that the catalogue could not be updated either.
            self.statusBar().showMessage(
                f"The preview status could not be updated in the catalogue: {exc}", 10000
            )

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

    def toggle_backups_panel(self, show: bool) -> None:
        self.backups_panel.setVisible(show)
        if show:
            self.backups_splitter.reset_split()
            self.refresh_backups_panel(refresh=True)
        else:
            self.backups_panel.set_source(None, None, can_compare=False)

    def show_backups(self, target: CatalogueItemRef | None = None, *, navigate: bool = False) -> None:
        if self.db is None:
            return
        if target is not None and navigate:
            self.open_catalogue_location_for_item(target)
        self.tabs.setCurrentWidget(self.browser_tab)
        self.backups_button.setChecked(True)
        self.refresh_backups_panel(target=target, refresh=True)
        self.backups_panel.compare_button.setFocus()

    def show_drive_backups(self, volume_id: int) -> None:
        if self.db is None:
            return
        root = self.db.get_root_folder(volume_id)
        if root is not None:
            target = CatalogueItemRef("folder", int(root["id"]), volume_id, root["relative_path"])
            self.show_backups(target, navigate=True)
        else:
            self.show_backups()

    def open_backup_location(self, location) -> None:
        if self.db is None or self.backup_engine is None:
            return
        state = self.backup_engine.state()
        if state.status != "completed" or state.active_run_id != self.backups_panel.run_id:
            self.refresh_backups_panel(refresh=True)
            return
        if location.item_type == "file":
            self.browser_backup_filter_combo.setCurrentIndex(0)
        target = CatalogueItemRef(
            location.item_type, location.item_id, location.volume_id, location.relative_path,
        )
        self.open_catalogue_location_for_item(target)

    def refresh_backups_panel(
        self, *_signals, target: CatalogueItemRef | None = None, refresh: bool = False,
    ) -> None:
        panel = getattr(self, "backups_panel", None)
        if panel is None:
            return
        if self.db is None:
            panel.set_source(None, None, can_compare=False)
            return
        if not self.backups_button.isChecked():
            return
        volume_id = target.volume_id if target is not None else self.current_volume_id
        backup_source = None
        if volume_id is not None:
            volume = self.db.get_volume(volume_id)
            if volume is not None:
                drive = volume_reference(volume["drive_id"], volume["name"])
                selected = self.selected_browser_item() if target is None and self.file_table.selectionModel().hasSelection() else None
                if selected is not None and not selected.is_parent_entry:
                    backup_source = BackupSource(
                        selected.item_type, selected.item_id, volume_id, selected.name, selected.relative_path,
                        drive, selected.missing,
                    )
                elif target is not None:
                    record = self.db.get_item_properties(target.item_type, target.item_id)
                    if record is not None:
                        backup_source = BackupSource(
                            target.item_type, target.item_id, volume_id, record["name"] or drive,
                            target.relative_path, drive, target.missing or bool(record["missing"]),
                        )
                else:
                    folder = (
                        self.db.get_folder(self.current_folder_id) if self.current_folder_id is not None
                        else self.db.get_root_folder(volume_id)
                    )
                    backup_source = BackupSource(
                        "folder", int(folder["id"]) if folder is not None else None, volume_id,
                        folder["name"] if folder is not None and folder["relative_path"] else drive,
                        folder["relative_path"] if folder is not None else "", drive,
                        bool(folder["missing"]) if folder is not None else False,
                    )
        panel.set_source(
            self.db.path, backup_source, refresh=refresh,
            can_compare=not self.db.read_only and not self.running_operations and self.delete_worker is None,
        )

    def start_backup_analysis(self) -> None:
        if self.db is None or self.db.read_only or self.backup_analysis_worker is not None:
            return
        if self.running_operations or self.delete_worker is not None:
            self._show_catalogue_job_running_message()
            return
        if self.backups_panel is not None:
            self.backups_panel.set_analysis_running(True)
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Comparing saved hashes…")
        self.statusBar().showMessage("Comparing saved hashes…")

        self.backup_analysis_worker = BackupAnalysisWorker(self.db.path)
        self.backup_analysis_worker.progress.connect(self.on_backup_analysis_progress)
        self.backup_analysis_worker.finished.connect(self.on_backup_analysis_finished)
        self.backup_analysis_worker.cancelled.connect(self.on_backup_analysis_cancelled)
        self.backup_analysis_worker.failed.connect(self.on_backup_analysis_failed)
        self.backup_analysis_thread = _create_worker_thread(
            self,
            self.backup_analysis_worker,
            (
                self.backup_analysis_worker.finished,
                self.backup_analysis_worker.cancelled,
                self.backup_analysis_worker.failed,
            ),
            self.clear_backup_analysis_worker,
        )
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
        if self.backups_panel is not None:
            self.backups_panel.set_analysis_progress(completed, total, label)
        self.statusBar().showMessage(f"{label}; no drives are being read.")

    def cancel_backup_analysis(self) -> None:
        worker = self.backup_analysis_worker
        if worker is None:
            return
        worker.cancel()
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Cancelling saved-evidence analysis…")
        if self.backups_panel is not None:
            self.backups_panel.cancel_button.setEnabled(False)
            self.backups_panel.progress.setRange(0, 0)
            self.backups_panel.progress.setFormat("Cancelling…")
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
            "Backups updated from saved file hashes.",
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
        self.refresh_backups_panel(refresh=True)

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
        self.refresh_backups_panel(refresh=True)

    def refresh_backup_evidence_views(self) -> None:
        if self.db is None:
            return
        self.refresh_volumes()
        if self.current_volume_id is not None and self.current_folder_id is not None:
            self.load_directory_items(self.current_volume_id, self.current_folder_id)
        if self.search_edit.text().strip():
            self.perform_search()
        self.refresh_backups_panel(refresh=True)

    @Slot()
    def clear_backup_analysis_worker(self) -> None:
        self.backup_analysis_worker = None
        self.backup_analysis_thread = None
        if getattr(self, "backups_panel", None) is not None:
            self.backups_panel.set_analysis_running(False)
            self.refresh_backups_panel(refresh=True)

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

        self.catalogue_info_worker = CatalogueInfoWorker(db_path)
        self.catalogue_info_worker.finished.connect(self.on_catalogue_info_finished)
        self.catalogue_info_worker.cancelled.connect(self.on_catalogue_info_cancelled)
        self.catalogue_info_worker.failed.connect(self.on_catalogue_info_failed)
        self.catalogue_info_thread = _create_worker_thread(
            self,
            self.catalogue_info_worker,
            (
                self.catalogue_info_worker.finished,
                self.catalogue_info_worker.cancelled,
                self.catalogue_info_worker.failed,
            ),
            self.clear_catalogue_info_worker,
        )
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
        dialog.setWindowTitle("Catalogue Information")
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
        add_row("Format", "Catalogue folder: catalogue.sqlite + volumes/*.jvvvol + index.sqlite")
        for label, value in self._catalogue_layout_rows():
            add_row(label, value, wrap=True)
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

    def _catalogue_layout_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        db = self.db
        if db is None or self.catalogue_path is None:
            return rows
        try:
            meta = db.catalogue_meta()
            rows.append(("Catalogue UUID", str(meta.get("catalogue_uuid", "-"))))
            volume_files = list(db.layout.iter_volume_files())
            rows.append(("Volume files", f"{len(volume_files)} in {db.layout.volumes_dir}"))
            try:
                index_size = db.index_path.stat().st_size
            except OSError:
                index_size = 0
            volume_bytes = 0
            for path in volume_files:
                try:
                    volume_bytes += path.stat().st_size
                except OSError:
                    pass
            rows.append(("Volume files size", format_size(volume_bytes)))
            rows.append(("Index size (rebuildable)", format_size(index_size)))
            link = resolve_repository_link(self.catalogue_path, meta)
            if link.status == LINK_LINKED:
                text = f"{link.path} (linked)"
            elif link.status == LINK_UNLINKED:
                text = f"none yet — the first snapshot creates {link.recorded_path}"
            elif link.status == LINK_MISSING:
                text = f"{link.recorded_path} (offline — choose Locate Repository in Snapshot Manager)"
            else:
                text = f"{link.recorded_path} ({link.detail or 'wrong repository'} — choose Locate Repository in Snapshot Manager)"
            rows.append(("Snapshot repository", text))
            report = db.last_reconcile
            if report is not None and report.orphan_files:
                rows.append(
                    (
                        "Unregistered volume files",
                        "\n".join(path.name for path in report.orphan_files),
                    )
                )
        except Exception as exc:  # pragma: no cover - informational only
            rows.append(("Layout details", f"Unavailable: {exc}"))
        return rows

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
        dialog.setWindowTitle("Catalogue Information Failed")
        dialog.setText("The catalogue information could not be loaded.")
        dialog.setDetailedText(details)
        dialog.exec()

    @Slot()
    def clear_catalogue_info_worker(self) -> None:
        self.catalogue_info_worker = None
        self.catalogue_info_thread = None
        self._set_catalogue_busy(False)

    # ------------------------------------------------------------------
    # Snapshots (version history)
    # ------------------------------------------------------------------

    def repository_link(self) -> RepositoryLink | None:
        """Where the open catalogue's snapshot repository is, or why it is offline."""

        if self.db is None or self.catalogue_path is None:
            return None
        try:
            meta = self.db.catalogue_meta()
        except Exception:
            meta = {}
        return resolve_repository_link(self.catalogue_path, meta)

    def catalogue_repository_path(self) -> Path | None:
        """The linked repository, or None while it is offline or does not exist yet."""

        link = self.repository_link()
        return link.path if link is not None else None

    def record_repository_link(self, repository_path: Path) -> bool:
        """Remember *repository_path* (and its id) as this catalogue's repository."""

        if self.db is None or self.catalogue_path is None:
            return False
        try:
            settings = repository_link_settings(self.catalogue_path, repository_path)
            current = self.db.catalogue_meta()
            for key, value in settings.items():
                if current.get(key) != value:
                    self.db.set_catalogue_meta(key, value)
        except Exception as exc:
            QMessageBox.critical(self, "Repository Link Failed", str(exc))
            return False
        return True

    def prompt_missing_repository(self, expected_catalogue: Database | None = None) -> None:
        """Offer to locate an unavailable repository once per location per open catalogue."""
        if expected_catalogue is not None and self.db is not expected_catalogue:
            return
        if (
            self.db is None or self.db.read_only or self._catalogue_close_in_progress
            or self._catalogue_open_in_progress() or self._catalogue_job_running()
        ):
            return
        try:
            link = self.repository_link()
        except Exception as exc:
            self.statusBar().showMessage(f"Could not check the snapshot repository: {exc}", 8000)
            return
        if link is None or not link.offline:
            return
        key = (self.db, str(link.recorded_path), link.status)
        if self._repository_prompt_key == key:
            return
        self._repository_prompt_key = key
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Snapshot Repository Unavailable")
        box.setText("The catalogue opened, but its snapshot repository is unavailable.")
        reason = f"\n\n{link.detail}" if link.detail else ""
        box.setInformativeText(
            f"Last known location:\n{link.recorded_path}{reason}\n\n"
            "If the folder moved, choose Locate Repository. If its drive is disconnected, "
            "you can reconnect it later. You can continue browsing and editing the local catalogue."
        )
        locate = box.addButton("Locate Repository…", QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton("Continue Without Repository", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(later)
        box.setEscapeButton(later)
        box.exec()
        if box.clickedButton() is locate:
            self.relink_repository()

    def relink_repository(self) -> Path | None:
        """Point the catalogue at its repository after it moved (like relinking media)."""

        if self.db is None or self.catalogue_path is None:
            return None
        link = self.repository_link()
        if link is None:
            return None
        try:
            catalogue_uuid = self.db.catalogue_uuid()
        except Exception:
            catalogue_uuid = ""
        candidates = locate_repository_candidates(
            self.catalogue_path,
            repository_uuid=link.repository_uuid,
            catalogue_uuid=catalogue_uuid,
        )
        chosen = RelinkRepositoryDialog.choose(self, link, candidates, catalogue_uuid=catalogue_uuid)
        if chosen is None:
            return None
        if not self.record_repository_link(chosen):
            return None
        self._repository_prompt_key = None
        self.statusBar().showMessage("Snapshot repository relinked.", 4000)
        self.refresh_snapshot_history_dialog()
        return chosen

    def ensure_repository_for_snapshot(self) -> Path | None:
        """Resolve where a new snapshot goes; offer Relink when the repository is offline."""

        link = self.repository_link()
        if link is None:
            return None
        if link.linked:
            return link.path
        if link.status == LINK_UNLINKED:
            return link.recorded_path
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Snapshot Repository Offline")
        reason = (
            f"Nothing is at its last known location:\n{link.recorded_path}"
            if link.status == LINK_MISSING
            else f"The folder at {link.recorded_path} cannot be used: {link.detail}."
        )
        box.setText(
            f"The catalogue's snapshot repository is offline.\n\n{reason}\n\n"
            "If you moved or renamed it, choose Locate Repository. Creating a new repository starts the "
            "version history from scratch; the old one is left where it is."
        )
        relink = box.addButton("Locate Repository…", QMessageBox.ButtonRole.AcceptRole)
        create = box.addButton("Create New Repository", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is relink:
            return self.relink_repository()
        if clicked is create:
            if link.status == LINK_WRONG:
                QMessageBox.warning(
                    self,
                    "Location In Use",
                    f"{link.recorded_path} is occupied by another repository. Move it away "
                    "first, or choose Locate Repository to find the correct one.",
                )
                return None
            return link.recorded_path
        return None

    def create_snapshot_from_menu(self) -> None:
        if self.db is None or self.catalogue_path is None:
            return
        if self._catalogue_job_running() or self._catalogue_open_in_progress():
            self._show_catalogue_job_running_message()
            return
        repository = self.ensure_repository_for_snapshot()
        if repository is None:
            return
        label, accepted = QInputDialog.getText(
            self,
            "Create Snapshot",
            f"Snapshot label (optional)\n\nRepository: {repository}",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not accepted:
            return
        self.start_snapshot(repository, label)

    def start_snapshot(self, repository: Path, label: str) -> None:
        if self.catalogue_path is None:
            return
        worker = snapshot_worker(repository, self.catalogue_path, label)
        self._start_repository_worker(worker, "snapshot")

    def show_snapshot_history(self) -> None:
        if self.db is None or self.catalogue_path is None:
            return
        if self.snapshot_history_dialog is None:
            self.snapshot_history_dialog = SnapshotHistoryDialog(self)
        self.snapshot_history_dialog.refresh()
        self.snapshot_history_dialog.show()
        self.snapshot_history_dialog.raise_()
        self.snapshot_history_dialog.activateWindow()
        self.prompt_missing_repository()

    def refresh_snapshot_history_dialog(self) -> None:
        dialog = self.snapshot_history_dialog
        if dialog is not None and dialog.isVisible():
            dialog.refresh()

    def restore_snapshot_from_dialog(self) -> None:
        """Home: choose a repository snapshot for a new catalogue."""

        if not self._home_commands_available():
            return
        start_dir = Path.home()
        repository_text = QFileDialog.getExistingDirectory(
            self,
            "Create New Catalogue — Choose Catalogue Repository",
            str(start_dir),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not repository_text:
            return
        repository_path = Path(repository_text).expanduser()
        try:
            repository = SnapshotRepository.open(repository_path)
            manifests = repository.list_snapshots()
        except Exception as exc:
            QMessageBox.critical(self, "Snapshot Repository", str(exc))
            return
        if not manifests:
            QMessageBox.information(self, "Snapshot Repository", "This repository has no snapshots.")
            return
        manifest = SnapshotPickerDialog.choose(self, repository, manifests)
        if manifest is None:
            return
        self.restore_snapshot_to_new_location(repository_path, manifest)

    def restore_snapshot_to_new_location(self, repository_path: Path, manifest: SnapshotManifest) -> None:
        if not self._home_commands_available():
            return
        target = self._choose_restored_catalogue_path(repository_path, manifest)
        if target is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Create New Catalogue from Repository")
        box.setText("Create and open this catalogue?")
        box.setInformativeText(
            f"Repository: {repository_path}\n"
            f"Snapshot: {display_db_time(manifest.created_at)}"
            + (f" ({manifest.label})" if manifest.label else "")
            + f"\nNew catalogue: {target}"
        )
        create = box.addButton("Create and Open Catalogue", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(create)
        box.exec()
        if box.clickedButton() is not create:
            return
        worker = snapshot_restore_worker(repository_path, manifest.snapshot_id, target, overwrite=False)
        self.pending_restored_status_message = "New catalogue created from repository and opened."
        self._start_repository_worker(worker, "restore")

    def delete_snapshot(self, repository_path: Path, manifest: SnapshotManifest) -> None:
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        answer = QMessageBox.question(
            self,
            "Delete Snapshot",
            f"Delete the snapshot taken {display_db_time(manifest.created_at)}"
            + (f" ({manifest.label})" if manifest.label else "")
            + "?\n\nBlobs only this snapshot references are removed by Prune.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            SnapshotRepository.open(repository_path).delete_snapshot(manifest.snapshot_id)
        except Exception as exc:
            QMessageBox.critical(self, "Delete Snapshot Failed", str(exc))
            return
        self.statusBar().showMessage("Snapshot deleted.", 4000)
        self.refresh_snapshot_history_dialog()

    def prune_repository(self, repository_path: Path) -> None:
        if self._catalogue_job_running() or self._catalogue_open_in_progress():
            self._show_catalogue_job_running_message()
            return
        self._start_repository_worker(repository_prune_worker(repository_path), "prune")

    def verify_repository(self, repository_path: Path) -> None:
        if self._catalogue_job_running() or self._catalogue_open_in_progress():
            self._show_catalogue_job_running_message()
            return
        self._start_repository_worker(repository_verify_worker(repository_path), "verify")

    def _choose_restored_catalogue_path(
        self,
        repository_path: Path,
        manifest: SnapshotManifest,
    ) -> Path | None:
        suggested_stem = manifest.catalogue_name or repository_path.stem
        return self._choose_new_catalogue_destination(
            "Create New Catalogue from Repository — Choose Location",
            repository_path.parent,
            f"{suggested_stem} (from snapshot){CATALOGUE_EXTENSION}",
        )

    def _start_repository_worker(self, worker: _RepositoryWorker, operation: str) -> None:
        self.repository_operation = operation
        if operation != "restore":
            self.pending_restored_catalogue_path = None
        self.repository_worker = worker
        worker.progress.connect(self.on_repository_progress)
        worker.finished.connect(self.on_repository_finished)
        worker.cancelled.connect(self.on_repository_cancelled)
        worker.failed.connect(self.on_repository_failed)
        self.repository_thread = _create_worker_thread(
            self,
            worker,
            (worker.finished, worker.cancelled, worker.failed),
            self.clear_repository_worker,
        )
        self._set_repository_running(True)
        self.repository_thread.start()

    @Slot(object, object, str)
    def on_repository_progress(
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
        label = message or f"Running {self.repository_operation}…"
        self.scan_progress.setFormat(label)
        self.statusBar().showMessage(label)
        if self._catalogue_open_in_progress() or self.db is None:
            self.catalogue_loading_progress.setFormat(label)

    @Slot(object)
    def on_repository_finished(self, result: object) -> None:
        operation = self.repository_operation
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        if operation == "snapshot":
            self.on_snapshot_finished(result)
        elif operation == "restore":
            self.on_snapshot_restore_finished(result)
        elif operation == "prune":
            self.on_prune_finished(result)
        elif operation == "verify":
            self.on_verify_finished(result)

    def on_snapshot_finished(self, result: SnapshotResult) -> None:
        self.scan_progress.setFormat("Snapshot created")
        # The first snapshot (or one into a freshly created repository) links it.
        linked = self.record_repository_link(result.repository_path)
        if getattr(self, "_snapshot_close_pending", False):
            self._snapshot_close_succeeded = linked
            self.statusBar().showMessage("Snapshot created.", 5000)
            self.refresh_snapshot_history_dialog()
            return
        manifest = result.manifest
        changed = result.volumes_written
        corrected = ""
        if result.digests_corrected:
            corrected = (
                "\n\nStored digests were recomputed for: "
                + ", ".join(result.digests_corrected)
            )
        QMessageBox.information(
            self,
            "Snapshot Created",
            f"Snapshot {manifest.snapshot_id}"
            + (f" ({manifest.label})" if manifest.label else "")
            + " recorded.\n\n"
            f"Volumes: {manifest.volume_count} ({changed} written, {result.volumes_reused} unchanged and reused)\n"
            f"Files: {manifest.file_count:,}\n"
            f"New data written: {format_size(result.bytes_written)} in {result.blobs_added} blob(s)\n"
            f"Time: {result.seconds:.1f} s\n\n{result.repository_path}"
            f"{corrected}\n\n{BACKUP_POLICY_TEXT}",
        )
        self.statusBar().showMessage("Snapshot created.", 5000)
        self.refresh_snapshot_history_dialog()

    def on_snapshot_restore_finished(self, result: RestoreResult) -> None:
        self.scan_progress.setFormat("Catalogue created from repository")
        QMessageBox.information(
            self,
            "Catalogue Created from Repository",
            "A new catalogue was created from the selected snapshot.\n\n"
            f"Catalogue: {result.catalogue_path}\n"
            f"Snapshot: {result.snapshot_id}\n"
            f"Volumes: {result.volumes_restored}, files: {result.files_restored:,}, "
            f"folders: {result.folders_restored:,}\n"
            f"Time: {result.seconds:.1f} s\n\n"
            "The search index was rebuilt. Compare saved hashes again.",
        )
        self.pending_restored_catalogue_path = result.catalogue_path
        self.statusBar().showMessage("New catalogue created from repository.", 5000)

    def on_prune_finished(self, result: PruneResult) -> None:
        self.scan_progress.setFormat("Repository pruned")
        QMessageBox.information(
            self,
            "Repository Pruned",
            f"Removed {result.removed_blobs} unreferenced blob(s) ({format_size(result.bytes_freed)}) "
            f"and {result.removed_temp_files} temporary file(s). {result.kept_blobs} blob(s) kept.",
        )
        self.statusBar().showMessage("Snapshot repository pruned.", 5000)
        self.refresh_snapshot_history_dialog()

    def on_verify_finished(self, result: VerifyResult) -> None:
        self.scan_progress.setFormat("Repository verified")
        if result.ok:
            QMessageBox.information(
                self,
                "Repository Verified",
                f"All {result.blobs_checked} blob(s) match their digests and every one of "
                f"{result.snapshots_checked} snapshot(s) is complete.",
            )
        else:
            details = []
            if result.corrupt_blobs:
                details.append("Corrupt blobs:\n" + "\n".join(result.corrupt_blobs))
            if result.missing_blobs:
                details.append(
                    "Missing blobs:\n"
                    + "\n".join(f"{snapshot}: {digest}" for snapshot, digest in result.missing_blobs)
                )
            if result.unreadable_snapshots:
                details.append(
                    "Unreadable snapshots:\n"
                    + "\n".join(f"{name}: {error}" for name, error in result.unreadable_snapshots)
                )
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Repository Problems Found")
            dialog.setText(
                f"{len(result.corrupt_blobs)} corrupt blob(s), {len(result.missing_blobs)} missing "
                f"blob reference(s), {len(result.unreadable_snapshots)} unreadable snapshot(s)."
            )
            dialog.setDetailedText("\n\n".join(details))
            dialog.exec()
        self.statusBar().showMessage("Snapshot repository verified.", 5000)
        self.refresh_snapshot_history_dialog()

    @Slot()
    def on_repository_cancelled(self) -> None:
        operation = (
            "Catalogue creation from repository"
            if self.repository_operation == "restore" else self.repository_operation.title()
        )
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat(f"{operation} cancelled")
        self.statusBar().showMessage(f"{operation} cancelled.", 4000)

    @Slot(object, str)
    def on_repository_failed(self, exc: Exception, details: str) -> None:
        operation = (
            "catalogue creation from repository"
            if self.repository_operation == "restore" else self.repository_operation or "operation"
        )
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat(f"{operation.title()} failed")
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle(f"{operation.title()} Failed")
        dialog.setText(str(exc) or f"The {operation} failed.")
        diagnostic = format_exception_diagnostics(exc)
        dialog.setDetailedText(diagnostic or details)
        dialog.exec()
        self.statusBar().showMessage(f"{operation.title()} failed.", 5000)

    @Slot()
    def clear_repository_worker(self) -> None:
        restored_path = self.pending_restored_catalogue_path
        self.pending_restored_catalogue_path = None
        self.repository_worker = None
        self.repository_thread = None
        self.repository_operation = ""
        self._set_repository_running(False)
        if restored_path is not None:
            self.open_catalogue_path(restored_path, status_message=self.pending_restored_status_message)

    def _set_repository_running(self, running: bool) -> None:
        self._set_catalogue_busy(running)
        self._update_context_actions()
        if running:
            labels = {
                "snapshot": "Creating snapshot…",
                "restore": "Creating new catalogue from repository…",
                "prune": "Pruning snapshot repository…",
                "verify": "Verifying snapshot repository…",
            }
            label = labels.get(self.repository_operation, "Working…")
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(label)
            self.statusBar().showMessage(label)
            if self.db is None:
                self.catalogue_loading_title_label.setText("Creating Catalogue from Repository")
                self.catalogue_loading_path_label.setText(label)
                self.catalogue_loading_progress.setRange(0, 0)
                self.catalogue_loading_progress.setFormat(label)
                self.catalogue_loading_cancel_button.setEnabled(True)
                self.stack.setCurrentWidget(self.loading_page)
        elif self.db is None and not self._catalogue_open_in_progress():
            self._set_catalogue_open(False)

    def new_catalogue(self) -> None:
        if not self._home_commands_available():
            return
        path = self._choose_new_catalogue_path()
        if path is None:
            return
        lock: QLockFile | None = None
        db: Database | None = None
        try:
            lock = self._acquire_catalogue_lock(path)
            db = create_catalogue(path)
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
        if not self._home_commands_available():
            return
        path_text = QFileDialog.getExistingDirectory(
            self,
            "Open Catalogue Folder",
            str(self.catalogue_path.parent if self.catalogue_path else Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not path_text:
            return
        self.open_catalogue_path(Path(path_text))

    def open_last_catalogue(self) -> None:
        if (
            self.db is not None
            or self._catalogue_open_in_progress()
            or self.repository_worker is not None
        ):
            return
        path_text = self.settings.value(LAST_CATALOGUE_PATH_SETTING, "", type=str)
        if not path_text:
            return

        self.open_catalogue_path(Path(path_text), status_message="Last catalogue opened.")

    def open_catalogue_path(self, path: str | Path, status_message: str = "Catalogue opened.") -> None:
        if not self._home_commands_available():
            return
        path = locate_catalogue(path)

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
                "The selected folder is not a JVVV catalogue (it has no valid "
                f"{CATALOGUE_DB_NAME})."
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

        self.catalogue_open_worker = CatalogueOpenWorker(path)
        self.catalogue_open_worker.progress.connect(self.on_catalogue_open_progress)
        self.catalogue_open_worker.finished.connect(self.on_catalogue_open_finished)
        self.catalogue_open_worker.failed.connect(self.on_catalogue_open_failed)
        self.catalogue_open_worker.cancelled.connect(self.on_catalogue_open_cancelled)
        self.catalogue_open_thread = _create_worker_thread(
            self,
            self.catalogue_open_worker,
            (
                self.catalogue_open_worker.finished,
                self.catalogue_open_worker.failed,
                self.catalogue_open_worker.cancelled,
            ),
            self.clear_catalogue_open_worker,
        )
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
        connections: VolumeConnections,
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
            volume_connections=connections,
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
        catalogue = self.db
        if catalogue is not None:
            QTimer.singleShot(0, lambda: self.prompt_missing_repository(catalogue))

    def _catalogue_open_in_progress(self) -> bool:
        return (
            getattr(self, "catalogue_probe_process", None) is not None
            or getattr(self, "catalogue_open_worker", None) is not None
        )

    def _set_catalogue_loading(self, loading: bool, path: Path | None = None) -> None:
        self._catalogue_ui_loading = loading
        self._update_context_actions()
        self.catalogue_loading_cancel_button.setEnabled(
            loading and not self.catalogue_open_cancel_requested
        )
        if loading:
            self.catalogue_loading_title_label.setText("Opening Catalogue")
            self.catalogue_loading_path_label.setText(str(path) if path is not None else "")
            self.catalogue_loading_progress.setRange(0, 0)
            self.catalogue_loading_progress.setFormat("Opening and checking catalogue...")
            self.stack.setCurrentWidget(self.loading_page)
            self.statusBar().showMessage("Opening catalogue...")

    def cancel_home_operation(self) -> None:
        if self.db is None and self.repository_worker is not None:
            self.repository_worker.cancel()
            self.catalogue_loading_cancel_button.setEnabled(False)
            self.catalogue_loading_progress.setFormat("Cancelling catalogue creation…")
            return
        self.cancel_catalogue_open()

    def close_catalogue(self, show_status: bool = True) -> bool:
        # Closing can enter a modal event loop while the snapshot worker runs.
        # Ignore a second close request until that first decision has completed.
        if self._catalogue_close_in_progress:
            return False
        self._catalogue_close_in_progress = True
        try:
            return self._close_catalogue(show_status)
        finally:
            self._catalogue_close_in_progress = False

    def _confirm_snapshot_before_close(self) -> bool:
        if self.db is None or self.catalogue_path is None or self.db.read_only:
            return True
        text = (
            "Your changes are saved in the local catalogue, but haven't been "
            "recorded in the snapshot repository."
        )
        try:
            link = self.repository_link()
            if link is not None and link.linked:
                if not SnapshotRepository(link.path).has_changes(self.catalogue_path):
                    return True
            elif link is not None and link.offline:
                text = (
                    "Your changes are saved in the local catalogue. The snapshot "
                    "repository is offline, so their snapshot status could not be checked."
                )
        except Exception as exc:
            text = (
                "Your changes are saved in the local catalogue, but their snapshot "
                f"status could not be checked.\n\n{exc}"
            )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Create Snapshot Before Closing?")
        box.setText("Create a snapshot before closing?")
        box.setInformativeText(text)
        create = box.addButton("Create Snapshot && Close", QMessageBox.ButtonRole.AcceptRole)
        skip = box.addButton("Close Without Snapshot", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(create)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is skip:
            return True
        if box.clickedButton() is not create:
            return False
        repository = self.ensure_repository_for_snapshot()
        if repository is None:
            return False
        return self._create_snapshot_before_close(repository)

    def _create_snapshot_before_close(self, repository: Path) -> bool:
        """Wait responsively for a snapshot, then let the original close continue."""
        worker = snapshot_worker(repository, self.catalogue_path, "")
        loop = QEventLoop(self)
        dialog = _SnapshotCloseProgressDialog("Creating snapshot…", "Cancel", 0, 0, self)
        dialog.setWindowTitle("Create Snapshot Before Closing")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)

        # Cancellation only sets a flag; it must not wait for the worker's busy
        # event loop. Progress updates belong to the dialog's GUI thread.
        dialog.canceled.connect(worker.cancel, Qt.ConnectionType.DirectConnection)
        worker.progress.connect(dialog.update_progress)
        worker.finished.connect(dialog.hide)
        worker.cancelled.connect(dialog.hide)
        worker.failed.connect(dialog.hide)
        self._snapshot_close_pending = True
        self._snapshot_close_succeeded = False
        try:
            self._start_repository_worker(worker, "snapshot")
            self.repository_thread.finished.connect(loop.quit)
            dialog.show()
            loop.exec()
            return self._snapshot_close_succeeded and not dialog.wasCanceled()
        finally:
            self._snapshot_close_pending = False
            dialog.hide()
            dialog.deleteLater()
            loop.deleteLater()

    def _close_catalogue(self, show_status: bool) -> bool:
        if self.archive_dialog is not None:
            self.archive_dialog.reject()
            return False
        if self.running_operations:
            self.pause_operations()
            return False
        if not self._stop_repository_for_close():
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
        if not self._stop_search_for_catalogue_close():
            return False
        if not self._stop_preview_store_for_close():
            return False

        if not self._confirm_snapshot_before_close():
            return False

        db = self.db
        lock = self.catalogue_lock
        self.operations_timer.stop()
        self._queue_drive_probe = None
        self._release_idle_projects()
        for request in self._file_open_requests.values():
            request.cancel()
        self._file_open_requests.clear()
        if self.operations_dialog is not None:
            self.operations_dialog.close()
            self.operations_dialog.deleteLater()
            self.operations_dialog = None
        self.operations_queue = None
        self.db = None
        self.catalogue_path = None
        self.catalogue_lock = None
        self.backup_engine = None
        self.preview_settings = load_preview_settings(self.settings)
        self.last_preview_statistics = None
        self.last_preview_operation_id = None
        self.backup_volume_references = {}
        self.backups_button.setChecked(False)
        self.backups_panel.set_source(None, None, can_compare=False)
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.close()
            self.preview_cache_dialog = None
        if self.snapshot_history_dialog is not None:
            self.snapshot_history_dialog.close()
            self.snapshot_history_dialog = None
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
        return self._choose_new_catalogue_destination("Create Empty Catalogue", Path.home())

    def _choose_new_catalogue_destination(
        self, title: str, directory: Path, suggested_name: str = "",
    ) -> Path | None:
        dialog = QFileDialog(self, title)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(CATALOGUE_FILE_FILTER)
        dialog.setDefaultSuffix(CATALOGUE_EXTENSION.lstrip("."))
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.setDirectory(str(directory))
        if suggested_name:
            dialog.selectFile(suggested_name)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.selectedFiles()
            if not selected:
                return None
            path = catalogue_path_with_extension(selected[0])
            if not path.exists():
                return path
            QMessageBox.warning(
                self,
                "Choose a New Location",
                f"A folder or file already exists here. Choose a new name or location.\n\n{path}",
            )
        return None

    def _acquire_catalogue_lock(self, path: Path) -> QLockFile:
        return acquire_catalogue_lock(path)

    def _open_catalogue_in_window(
        self,
        db: Database,
        path: Path,
        lock: QLockFile,
        *,
        initial_volume_items: list[VolumeItem] | None = None,
        volume_connections: VolumeConnections | None = None,
    ) -> None:
        self.preview_settings = self.load_catalogue_preview_settings(db)
        self.last_preview_statistics = None
        self.last_preview_operation_id = None
        self.db = db
        self._repository_prompt_key = None
        try:
            self.backup_engine = BackupAnalysisEngine(db)
            if not db.read_only:
                self.backup_engine.ensure_schema()
        except Exception:
            self.backup_engine = None
        self.catalogue_path = path
        self.catalogue_lock = lock
        if not db.read_only:
            try:
                self._get_operations_queue()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.statusBar().showMessage(f"Could not open the saved queue. {exc}", 10000)
        self.remember_recent_catalogue(path)
        self._set_catalogue_open(True)
        report = getattr(db, "last_reconcile", None)
        if report is not None and report.rebuilt:
            self.statusBar().showMessage(
                "The catalogue index was rebuilt from the volume files. Compare saved hashes again.",
                10000,
            )
        self.start_connected_volume_monitor(volume_connections)
        if initial_volume_items is None or self.backup_engine is not None:
            self.refresh_volumes()
        else:
            self._apply_volume_items(initial_volume_items)
        QTimer.singleShot(0, lambda: self.prompt_missing_repository(db))

    def start_connected_volume_monitor(
        self,
        connections: VolumeConnections | None = None,
    ) -> None:
        self._connected_volume_snapshots = connections.snapshots if connections is not None else []
        self._connected_volume_paths = connections.paths if connections is not None else {}
        self._connected_volume_signature = connected_volume_signature(
            self._connected_volume_snapshots
        )
        self.volume_connection_refresh_timer.stop()
        self.volume_connection_timer.start()
        if connections is None:
            self.check_connected_volumes()

    def stop_connected_volume_monitor(self) -> None:
        self.volume_connection_timer.stop()
        self.volume_connection_refresh_timer.stop()
        self._connected_volume_snapshots = []
        self._connected_volume_paths = {}
        self._connected_volume_signature = ()

    @Slot()
    def check_connected_volumes(self) -> None:
        if self.db is None:
            return
        if self._volume_connection_probe is not None:
            catalogue, result = self._volume_connection_probe
            if not result.done():
                return
            self._volume_connection_probe = None
            if catalogue is self.db and result.exception() is None:
                self._apply_volume_connections(result.result())

        volumes = self.db.list_volumes()
        result = Future()
        self._volume_connection_probe = (self.db, result)
        # A sleeping or unplugged drive can stall an OS call. A daemon probe
        # keeps it off the UI thread and requires no wait when closing the app.
        Thread(
            target=_probe_connected_volumes, args=(volumes, result),
            name="jvvv-volume-connections", daemon=True,
        ).start()

    def _apply_volume_connections(self, connections: VolumeConnections) -> None:
        signature = connected_volume_signature(connections.snapshots)
        changed = signature != self._connected_volume_signature or connections.paths != self._connected_volume_paths
        self._connected_volume_snapshots = connections.snapshots
        self._connected_volume_paths = connections.paths
        self._connected_volume_signature = signature
        if changed:
            self.volume_connection_refresh_timer.start()

    @Slot()
    def refresh_after_connected_volumes_changed(self) -> None:
        if self.db is None:
            return
        self.refresh_volumes(reload_browser=False)
        if self.search_edit.text().strip():
            self.perform_search()
        self.statusBar().showMessage("Connected volumes updated.", 3000)

    def _stop_repository_for_close(self) -> bool:
        worker = getattr(self, "repository_worker", None)
        thread = getattr(self, "repository_thread", None)
        if worker is None or thread is None or not thread.isRunning():
            return True
        operation = getattr(self, "repository_operation", "") or "operation"
        answer = QMessageBox.question(
            self,
            f"{operation.title()} Running",
            f"A {operation} is still running. Cancel it before closing?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        worker.cancel()
        self.scan_progress.setFormat("Cancelling…")
        self.statusBar().showMessage(f"Cancelling {operation}…")
        if _wait_for_worker_thread(lambda: self.repository_thread):
            return True
        QMessageBox.information(
            self,
            f"{operation.title()} Cancelling",
            "Cancellation has been requested. Close the application after the operation stops.",
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

        if _wait_for_worker_thread(lambda: self.search_thread):
            return True

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

        if _wait_for_worker_thread(lambda: self.catalogue_info_thread):
            return True

        QMessageBox.information(
            self,
            "Catalogue Information Cancelling",
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
        if _wait_for_worker_thread(lambda: self.backup_analysis_thread):
            return True
        QMessageBox.information(
            self,
            "Backup Analysis Cancelling",
            "Cancellation has been requested. Close the catalogue after analysis stops.",
        )
        return False

    @property
    def operation_id(self) -> str | None:
        return next(iter(self.running_operations), None)

    @property
    def scan_worker(self) -> ScanWorker | None:
        return next((entry.worker for entry in self.running_operations.values() if entry.job.kind == "scan"), None)

    def _catalogue_job_running(self, *, allow_queue=False) -> bool:
        return (
            (not allow_queue and bool(self.running_operations))
            or self.archive_dialog is not None
            or self.delete_worker is not None
            or self.catalogue_info_worker is not None
            or getattr(self, "repository_worker", None) is not None
            or getattr(self, "backup_analysis_worker", None) is not None
            or getattr(self, "preview_store_worker", None) is not None
        )

    def _show_catalogue_job_running_message(self) -> None:
        if getattr(self, "preview_store_worker", None) is not None:
            QMessageBox.information(
                self,
                "Preview Manager Busy",
                "Wait for the preview cache operation to finish or cancel it in "
                "Window > Preview Manager.",
            )
            return
        if self.delete_worker is not None:
            QMessageBox.information(
                self,
                "Volume Deleting",
                "Wait for the current volume delete to finish.",
            )
        elif self.catalogue_info_worker is not None:
            QMessageBox.information(
                self,
                "Catalogue Information Loading",
                "Wait for catalogue information to finish loading.",
            )
        elif getattr(self, "repository_worker", None) is not None:
            operation = getattr(self, "repository_operation", "") or "snapshot operation"
            QMessageBox.information(
                self,
                f"{operation.title()} Running",
                f"Wait for the {operation} to finish before starting another job.",
            )
        elif getattr(self, "backup_analysis_worker", None) is not None:
            QMessageBox.information(
                self,
                "Backup Analysis Running",
                "Wait for the comparison to finish or cancel it in Backups.",
            )
        else:
            QMessageBox.information(
                self,
                "Catalogue Job Running",
                "Wait for the current jobs to finish or pause them in Queue Manager.",
            )

    def _set_catalogue_busy(self, busy: bool) -> None:
        self._catalogue_ui_busy = busy
        enabled = self.db is not None and not busy
        for action in self.catalogue_actions:
            action.setEnabled(enabled)
        for widget in self.catalogue_widgets:
            widget.setEnabled(enabled)
        for shortcut in self.browser_shortcuts:
            shortcut.setEnabled(enabled)
        self._update_context_actions()
        if self.snapshot_history_dialog is not None:
            self.snapshot_history_dialog.update_buttons()
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_writable(
                self.db is not None and not self.db.read_only and not busy
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
        if getattr(self, "preview_cache_dialog", None) is not None:
            self.preview_cache_dialog.set_writable(
                self.db is not None and not self.db.read_only and not running
            )

    def _set_catalogue_open(self, is_open: bool) -> None:
        self._catalogue_ui_open = is_open
        if hasattr(self, "stack"):
            self.stack.setCurrentWidget(self.catalogue_page if is_open else self.welcome_page)
        self.close_catalogue_action.setEnabled(is_open)
        for action in self.catalogue_actions:
            action.setEnabled(is_open)
        for widget in self.catalogue_widgets:
            widget.setEnabled(is_open)
        for shortcut in self.browser_shortcuts:
            shortcut.setEnabled(is_open)
        if hasattr(self, "volume_table"):
            self.volume_table.setEnabled(is_open)
        if hasattr(self, "tabs"):
            self.tabs.setEnabled(is_open)

        if not is_open:
            self.stop_connected_volume_monitor()
            self._clear_catalogue_views()
            self.refresh_recent_catalogues()
        self._update_context_actions()
        self._update_window_title()

    def _update_context_actions(self) -> None:
        is_open = self._catalogue_ui_open
        home_enabled = self._home_commands_available()
        for action in self.home_actions:
            action.setVisible(not is_open)
            action.setEnabled(home_enabled)
        for button in self.home_buttons:
            button.setEnabled(home_enabled)
        self.recent_catalogue_table.setEnabled(home_enabled)
        self.update_recent_catalogue_buttons()
        for action in self.catalogue_actions:
            action.setVisible(is_open)
        self._update_archive_action()
        self.home_file_separator.setVisible(not is_open)
        self.workspace_file_separator.setVisible(is_open)
        self.workspace_exit_separator.setVisible(is_open)
        self.window_settings_separator.setVisible(is_open)
        self.window_information_separator.setVisible(is_open)
        self.catalogue_header.setVisible(is_open)
        self.home_button.setEnabled(self.close_catalogue_action.isEnabled())

    def _update_archive_action(self, volumes=None) -> None:
        self.archive_project_action.setEnabled(
            self._catalogue_ui_open and self.db is not None and not self.db.read_only
            and not self._catalogue_ui_busy and not self._catalogue_job_running()
            and bool(self.archive_targets(volumes))
        )
        self.operations_queue_action.setEnabled(
            self._catalogue_ui_open and self.db is not None and not self.db.read_only
        )
        self._refresh_queue_controls()

    def _clear_catalogue_views(self) -> None:
        self.current_volume_id = None
        self.current_folder_id = None
        self.current_directory_items = []
        self.backup_volume_summaries = {}
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
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Idle")
        self.operation_progress_label.clear()
        self.operation_progress_label.hide()
        self.statusBar().clearMessage()

    def _update_window_title(self) -> None:
        if self.catalogue_path is None:
            self.setWindowTitle(f"{APP_NAME} - Home")
            self.catalogue_name_label.clear()
            self.catalogue_path_label.clear()
        else:
            self.setWindowTitle(f"{APP_NAME} - {self.catalogue_path.name}")
            self.catalogue_name_label.setText(self.catalogue_path.name)
            self.catalogue_path_label.setText(str(self.catalogue_path))

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

    def refresh_volumes(self, *, reload_browser: bool = True) -> None:
        if self.db is None:
            self._clear_catalogue_views()
            return
        volumes = self.db.list_volumes()
        self._update_archive_action(volumes)
        self.backup_volume_references = {
            int(volume["id"]): volume_reference(volume["drive_id"], volume["name"])
            for volume in volumes
        }
        summaries: dict[int, Any] = {}
        analysis_state: Any = None
        engine = getattr(self, "backup_engine", None)
        if engine is not None:
            try:
                analysis_state = engine.state()
                summaries = {
                    int(object_value(summary, "volume_id")): summary
                    for summary in engine.volume_summaries()
                }
            except Exception:
                summaries = {}
                analysis_state = None
        self.backup_volume_summaries = summaries
        self.backup_analysis_state = analysis_state
        all_items = [
            volume_item_from_record(
                volume,
                self.current_source_path_for_volume(volume) is not None,
                summaries.get(int(volume["id"])),
                self.backup_volume_references,
                analysis_state,
            )
            for volume in volumes
        ]
        self._apply_volume_items(all_items, reload_browser=reload_browser)

    def _apply_volume_items(self, all_items: list[VolumeItem], *, reload_browser: bool = True) -> None:
        selected_id = self.selected_volume_id() or self.current_volume_id
        filter_text = self.volume_filter_edit.text() if hasattr(self, "volume_filter_edit") else ""
        items = [item for item in all_items if volume_matches_filter(item, filter_text)]
        with QSignalBlocker(self.volume_table.selectionModel()):
            self.volume_model.set_items(items)
            selected = selected_id is not None and self.select_volume(selected_id)
            if not selected:
                self.clear_volume_selection()
        if not selected:
            return
        if reload_browser or selected_id != self.current_volume_id:
            self.show_selected_volume(selected_id)
        else:
            volume = self.db.get_volume(selected_id)
            self.show_volume_details(volume)
            if self.folder_tree.topLevelItemCount():
                self.update_browser_connection_notice(volume)

    def selected_volume_id(self) -> int | None:
        item = self.volume_model.item_at(self.volume_table.currentIndex())
        return item.id if item is not None else None

    def selected_volume(self):
        if self.db is None:
            return None
        volume_id = self.selected_volume_id()
        return self.db.get_volume(volume_id) if volume_id is not None else None

    def current_source_path_for_volume(self, volume) -> str | None:
        if volume is None:
            return None
        return self._connected_volume_paths.get(volume_location_key(volume))

    def show_volume_context_menu(self, point: QPoint) -> None:
        if self.db is None:
            return
        index = self.volume_table.indexAt(point)
        menu = QMenu(self)
        busy = self._catalogue_job_running()

        if not index.isValid():
            new_action = menu.addAction("Add Volume…")
            new_action.triggered.connect(self.add_volume)
            new_action.setEnabled(not busy)
            menu.exec(self.volume_table.viewport().mapToGlobal(point))
            return

        self.volume_table.selectRow(index.row())
        self.volume_table.setCurrentIndex(self.volume_model.index(index.row(), 0))

        new_action = menu.addAction("Add Volume…")
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
        volume_id = self.selected_volume_id()
        volume = self.db.get_volume(volume_id) if volume_id is not None else None
        backups_action = menu.addAction("View backups")
        backups_action.setEnabled(volume is not None)
        backups_action.triggered.connect(lambda: self.show_drive_backups(volume_id))
        scan_action = menu.addAction("Queue Scan")
        scan_action.triggered.connect(lambda: self.start_scan(volume_id=volume_id))
        scan_action.setEnabled(not busy and not self.db.read_only)
        link_action = menu.addAction("Relink Drive…")
        link_action.triggered.connect(lambda: self.relink_drive(volume_id))
        link_action.setEnabled(not busy and not self.db.read_only and volume is not None)

        cancel_action = menu.addAction("Cancel Scan")
        scan_job_id = next((
            entry.job.id for entry in self.running_operations.values()
            if volume is not None and entry.job.kind == "scan" and entry.job.volume_uuid == volume["uuid"]
        ), None)
        cancel_action.triggered.connect(lambda: self.pause_queue_job(scan_job_id))
        cancel_action.setEnabled(scan_job_id is not None)

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
        # Clear the previous volume's rows before loading the selected catalogue
        # records.
        self.folder_tree.viewport().repaint()
        self.file_table.viewport().repaint()
        volume = self.db.get_volume(volume_id) if self.db is not None and volume_id is not None else None
        self.show_volume_details(volume)
        self.load_volume_browser(volume)
        self.load_scan_log(volume_id)
        MainWindow.refresh_backups_panel(self)

    def show_volume_details(self, volume) -> None:
        if volume is None:
            for widget in self.detail_labels.values():
                widget.setText("-")
                widget.setToolTip("")
            self.detail_full.setValue(0)
            return

        current_source_path = self.current_source_path_for_volume(volume)
        connected = current_source_path is not None
        full = percentage_full(volume["used_bytes"], volume["capacity_bytes"])
        volume_id = int(volume["id"])
        backup = volume_backup_display(
            getattr(self, "backup_volume_summaries", {}).get(volume_id),
            getattr(self, "backup_volume_references", {}),
            getattr(self, "backup_analysis_state", None),
        )
        values = {
            "drive_id": volume["drive_id"] or "-",
            "name": display_volume_name(volume["name"]),
            "path": current_source_path or volume["source_path"] or "-",
            "connection": "Connected" if connected else "Offline",
            "connector": volume["connector"] or "Unknown",
            "register_status": volume["register_status"],
            "condition": volume["condition"],
            "capacity": format_size(volume["capacity_bytes"]) if volume["capacity_bytes"] else "-",
            "used": format_size(volume["used_bytes"]) if volume["capacity_bytes"] else "-",
            "free": format_size(volume["free_bytes"]) if volume["capacity_bytes"] else "-",
            "files": f"{int(volume['indexed_file_count'] or 0):,}",
            "folders": f"{int(volume['indexed_folder_count'] or 0):,}",
            "last_scan": self._display_time(volume["last_scan_at"]),
            "backup_coverage": backup.text,
        }
        for key, value in values.items():
            self.detail_labels[key].setText(value)
        self.detail_labels["backup_coverage"].setToolTip(backup.tooltip)
        self.detail_labels["path"].setToolTip(values["path"])
        self.detail_labels["name"].setToolTip(values["name"])
        self.detail_full.setValue(full)

    def add_volume(self) -> None:
        if self.db is None or self.db.read_only:
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
            self.start_scan(volume_id=volume_id)
        except Exception as exc:
            QMessageBox.critical(self, "Add Volume Failed", str(exc))

    def choose_new_volume_location(self) -> str | None:
        directory = QFileDialog.getExistingDirectory(self, "Choose Volume to Scan", str(Path.home()))
        return directory or None

    def relink_drive(self, volume_id: int) -> None:
        if self.db is None or self.db.read_only or self._catalogue_job_running():
            return
        volume = self.db.get_volume(volume_id)
        if volume is None:
            return
        label = self.db.volume_reference(volume)
        selected = QFileDialog.getExistingDirectory(self, f"Choose the Correct Drive or Folder for {label}")
        if not selected:
            return
        try:
            snapshot = capture_volume_snapshot(Path(selected).resolve(strict=True))
            if snapshot is None or not snapshot.identity_token:
                raise OSError("JVVV could not identify the selected drive.")
            if volume_identity_known(volume) and (
                snapshot.source_relative_path.casefold() != volume["source_relative_path"].casefold()
            ):
                raise OSError("Choose the same location on the drive that this volume was indexed from.")
            if volume["identity_kind"] == MARKER_IDENTITY and not snapshot_matches_volume(snapshot, volume):
                raise OSError("Choose the drive with the matching JVVV drive ID file.")
            location = snapshot.location_for(volume)
            resolver = ConnectedVolumeResolver()
            candidate = {**dict(volume), **location, "source_path": snapshot.source_path}
            path, problem = resolver.resolve_details(candidate)
            if problem == "Relink Required":
                raise OSError("More than one connected drive has this identity. Disconnect the duplicate and try again.")
            if path is None or Path(path) != Path(snapshot.source_path):
                raise OSError("JVVV could not confirm the selected drive.")
            answer = QMessageBox.question(
                self, "Relink Drive", f"Use {snapshot.source_path} as the location of {label}?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.db.update_volume_location(volume_id, snapshot.source_path, location)
                self.refresh_volumes()
                self.statusBar().showMessage("Volume location updated.", 5000)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could Not Relink Drive", str(exc))

    def choose_new_volume_drive_id(self, source_path: str, snapshot: VolumeSnapshot | None) -> str | None:
        if self.db is None:
            return None
        volume_label = snapshot.identity_label if snapshot is not None else ""
        suggested_drive_id = suggested_new_volume_drive_id(volume_label, self.db.next_drive_id())
        can_rename = snapshot is not None and sys.platform == "win32"
        if can_rename:
            try:
                check_archive_write_locations(None, Path(source_path))
            except OSError:
                can_rename = False
        dialog = DriveIdDialog(
            self,
            suggested_drive_id=suggested_drive_id,
            source_path=source_path,
            volume_label=volume_label,
            existing_volumes=self.db.list_volumes(),
            allow_volume_label_rename=can_rename,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        drive_id = dialog.value()
        target_volume_label = (
            drive_id
            if dialog.should_rename_volume_label()
            else dialog.volume_label_value()
        )
        if can_rename and target_volume_label != volume_label:
            check_archive_write_locations(None, Path(source_path))
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

        self.delete_worker = DeleteVolumeWorker(self.db.path, volume_id)
        self.delete_worker.progress.connect(self.on_delete_progress)
        self.delete_worker.finished.connect(self.on_delete_finished)
        self.delete_worker.failed.connect(self.on_delete_failed)
        self.delete_thread = _create_worker_thread(
            self,
            self.delete_worker,
            (self.delete_worker.finished, self.delete_worker.failed),
            self.clear_delete_worker,
        )
        self.delete_thread.start()

    def archive_project(self, volume_id: int | None = None, *, project_id=None) -> None:
        if self.db is None or self.db.read_only:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        try:
            queue = self._get_operations_queue()
            targets = self.archive_targets()
            source, verified = "", {}
            if project_id:
                record = self.db.archive_records.project(project_id)
                if record is None:
                    raise OSError("The original project record could not be found.")
                source = record["source"]
                copied = self.db.archive_records.copies(project_id)
                occupied = copied | {job.volume_uuid for job in queue.jobs if job.project_id == project_id}
                verified = {v["uuid"]: self.db.volume_reference(v) for v in self.db.list_volumes() if v["uuid"] in copied}
                targets = [(v, dependency) for v, dependency in targets if v["uuid"] not in occupied]
            dialog = ArchiveDialog(
                self.db, targets, self, volume_id=volume_id, source=source, verified=verified.values(),
                connected_path=self.current_source_path_for_volume,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Cannot Open Archive Project", str(exc))
            return
        self.archive_dialog = dialog
        self._update_context_actions()
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                targets = []
                for target_id, target_uuid, dependency in dialog.selected_targets():
                    volume = self.db.get_volume(target_id)
                    if volume is None or volume["uuid"] != target_uuid:
                        raise OSError("The destination volume changed. Choose the drive again.")
                    targets.append((volume, self.db.volume_reference(volume), dependency))
                queue.add_archive(targets, dialog.source.text(), project_id, verified=verified.items())
                self._refresh_operations()
                self.statusBar().showMessage(f"Queued {Path(dialog.source.text()).name} for {len(targets)} drive(s).", 4000)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(str(exc), 10000)
        finally:
            self.archive_dialog = None
            dialog.deleteLater()
            self._update_context_actions()

    def start_scan(self, volume_id: int | None = None) -> None:
        if self.db is None or self.db.read_only:
            return
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return
        volume = self.db.get_volume(volume_id) if volume_id is not None else self.selected_volume()
        if volume is None:
            return

        try:
            queue = self._get_operations_queue()
            existing = queue.pending_scan(volume["uuid"])
            if existing is None:
                existing = queue.add("scan", volume, self.db.volume_reference(volume))
            elif existing.state == "failed":
                queue.set_state(existing.id, "queued")
            self._update_archive_action()
            self._refresh_operations()
            self.statusBar().showMessage(f"{existing.title} is in the queue.", 4000)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(str(exc), 10000)

    def archive_targets(self, volumes=None):
        if self.db is None:
            return []
        targets = []
        for volume in self.db.list_volumes() if volumes is None else volumes:
            if not is_archive_volume_root(volume):
                continue
            scan = self.operations_queue.pending_scan(volume["uuid"]) if self.operations_queue is not None else None
            if scan is not None or is_scanned_archive_volume(self.db, volume):
                targets.append((volume, scan.id if scan is not None else ""))
        return targets

    def _get_operations_queue(self) -> OperationsQueue:
        if self.operations_queue is None:
            queue = OperationsQueue(self.db.path, self.db.catalogue_uuid())
            if not queue.path.exists():
                try:
                    saved = pending_archive(self.db.path)
                    if saved and saved.get("catalogue") == self.db.catalogue_uuid():
                        volume = next((row for row in self.db.list_volumes()
                                       if row["uuid"] == saved.get("volume_uuid")), None)
                        if volume is not None:
                            queue.add("archive", volume, self.db.volume_reference(volume),
                                      source=saved["source"], legacy=True)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    self.statusBar().showMessage(f"Could not import the old paused archive. {exc}", 10000)
            self.operations_queue = queue
        return self.operations_queue

    def show_operations_queue(self) -> None:
        if self.db is None or self.db.read_only:
            return
        try:
            self._get_operations_queue()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.statusBar().showMessage(f"Could not open the saved queue. {exc}", 10000)
            return
        if self.operations_dialog is None:
            self.operations_dialog = OperationsDialog(self)
            self.operations_dialog.apply_requested.connect(self.apply_operations)
            self.operations_dialog.pause_requested.connect(self.pause_operations)
            self.operations_dialog.pause_job_requested.connect(self.pause_queue_job)
            self.operations_dialog.move_requested.connect(self.move_operation)
            self.operations_dialog.remove_requested.connect(self.remove_operation)
            self.operations_dialog.remove_project_requested.connect(
                lambda pid: self._change_operations(self.operations_queue.remove_project, pid)
            )
            self.operations_dialog.retry_requested.connect(self.retry_operation)
            self.operations_dialog.add_copy_requested.connect(lambda pid: self.archive_project(project_id=pid))
            self.operations_dialog.preview_failures_requested.connect(lambda: self.show_preview_failures())
        self._refresh_operations()
        if self.operation_id is not None:
            self.operations_dialog.progress.setText(self.operation_progress_label.text())
        self.operations_dialog.show()
        self.operations_dialog.raise_()

    def _refresh_queue_controls(self) -> None:
        queue = self.operations_queue
        pending = [job for job in queue.jobs if job.state not in DONE] if queue is not None else []
        groups = {job.project_id or job.id for job in pending}
        if len(groups) == 1 and pending[0].project_id and sum(
                job.project_id == pending[0].project_id for job in queue.jobs) > 1:
            children = [job for job in queue.jobs if job.project_id == pending[0].project_id]
            summary = f"{Path(children[0].source).name} · {sum(j.state in DONE for j in children)} of {len(children)} copies verified"
        elif len(pending) == 1:
            summary = pending[0].title
        else:
            summary = f"{len(groups)} pending changes" if pending else "No pending changes"
        self.queue_summary_label.setText(summary)
        self.queue_summary_label.setToolTip(summary)
        enabled = self.operations_queue_action.isEnabled()
        self.queue_manager_button.setEnabled(enabled)
        apply_text = "Apply All" if queue is not None and queue.apply_count(active=False) > 1 else "Apply"
        can_apply = bool(
            enabled and self.operation_id is None and queue is not None
            and queue.apply_count(active=self.operations_timer.isActive())
        )
        self.apply_queue_button.setText(apply_text)
        self.apply_queue_button.setEnabled(can_apply)
        if self.operations_dialog is not None:
            self.operations_dialog.apply_button.setText(apply_text)
            self.operations_dialog.apply_button.setEnabled(can_apply)

    def _refresh_operations(self) -> None:
        self._release_idle_projects()
        self._refresh_queue_controls()
        if self.operations_dialog is not None:
            previews = self.last_preview_statistics
            failure_job_id = (
                self.last_preview_operation_id
                if previews is not None and (previews.failures or previews.storage_skipped)
                else None
            )
            self.operations_dialog.refresh(
                self.operations_queue.jobs, self.operations_timer.isActive(), preview_failure_job_id=failure_job_id,
            )
            for running in self.running_operations.values():
                self.operations_dialog.set_job_progress(running.job.id, running.message, running.done, running.total)

    def _change_operations(self, change, *args) -> bool:
        try:
            change(*args)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(str(exc), 10000)
            if self.operations_dialog is not None:
                self.operations_dialog.detail.setText(str(exc))
            return False
        self._refresh_operations()
        self._update_archive_action()
        return True

    def move_operation(self, job_id: str, offset: int) -> None:
        self._change_operations(self.operations_queue.move, job_id, offset)

    def remove_operation(self, job_id: str) -> None:
        self._change_operations(self.operations_queue.remove, job_id)

    def retry_operation(self, job_id: str) -> None:
        self._change_operations(self.operations_queue.set_state, job_id, "queued")

    def apply_operations(self) -> None:
        queue = self.operations_queue
        if (self.db is None or self.db.read_only or queue is None or self.operation_id is not None
                or not queue.apply_count(active=self.operations_timer.isActive())):
            return
        if self._change_operations(queue.apply):
            self.operations_timer.start()
            self._refresh_operations()
            QTimer.singleShot(0, self._pump_operations)

    def pause_operations(self) -> None:
        self.operations_timer.stop()
        self._clear_queue_check_progress()
        for job_id in list(self.running_operations):
            self.pause_queue_job(job_id)
        self._refresh_operations()

    def _release_idle_projects(self):
        for identifier in list(self.project_sessions):
            if any(active.job.project_id == identifier for active in self.running_operations.values()):
                continue
            if self.operations_timer.isActive() and self.operations_queue and any(
                job.project_id == identifier and job.state in ACTIONABLE for job in self.operations_queue.jobs
            ):
                continue
            self.project_sessions.pop(identifier).close(wait=False)

    def pause_queue_job(self, job_id: str) -> None:
        active = self.running_operations.get(job_id)
        if active is not None:
            active.worker.cancel()
            self._queue_progress(job_id, "Pausing?")

    def _pump_operations(self) -> None:
        if (self.db is None or not self.operations_timer.isActive() or len(self.running_operations) >= 2
                or self._catalogue_close_in_progress or self._catalogue_job_running(allow_queue=True)
                or QApplication.activeModalWidget() is not None):
            return
        queue = self.operations_queue
        for identifier, session in list(self.project_sessions.items()):
            if session.guard is None or any(op.job.project_id == identifier for op in self.running_operations.values()):
                continue
            try:
                session.guard.check()
            except OSError as exc:
                for waiting in queue.jobs:
                    if waiting.project_id == identifier and waiting.state in ACTIONABLE:
                        if not self._change_operations(queue.set_state, waiting.id, "failed", str(exc)):
                            self.pause_operations()
                            return
        candidates = [job for job in queue.jobs if job.state in ACTIONABLE]
        if not candidates:
            if not self.running_operations:
                self.operations_timer.stop()
                self._clear_queue_check_progress()
            self._refresh_operations()
            return
        ready = next((job for job in candidates if job.state == "ready"), None)
        if ready is not None and not self.running_operations:
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(QUEUE_DRIVE_CHECK_MESSAGE)
            self._show_queued_progress(QUEUE_DRIVE_CHECK_MESSAGE, operation=ready)
        volumes = {volume["uuid"]: volume for volume in self.db.list_volumes()}
        checks = None
        if len(self.running_operations) == 1:
            active = next(iter(self.running_operations.values()))
            if all(job.volume_uuid == active.job.volume_uuid for job in candidates):
                if not active.verifying:
                    return
                volume = volumes.get(active.job.volume_uuid)
                if volume is not None and active.following_drive_checks and all(
                    job.id in active.following_drive_checks
                    and active.following_drive_checks[job.id].location == volume_location_key(volume)
                    for job in candidates
                ):
                    checks = active.following_drive_checks
        if checks is None:
            checks = self._queue_drive_checks(candidates, volumes)
            if checks is None:
                return

        def current_check(job, volume):
            check = checks.get(job.id)
            return check if check is not None and check.location == volume_location_key(volume) else None

        def availability(job):
            volume = volumes.get(job.volume_uuid)
            if volume is None:
                return "failed", "This volume was removed from the catalogue."
            check = current_check(job, volume)
            if check is None:
                return "ready", ""
            if check.problem == "Relink Required":
                return "relink", "Resolve the drive identity from the volume's Relink Drive action."
            if check.path is None:
                return "waiting", f"Connect {job.volume_label} to continue."
            if job.kind == "archive" and not is_scanned_archive_volume(self.db, volume):
                return "failed", "The destination needs a completed initial drive scan."
            return "ready", ""

        def can_start(job):
            check = current_check(job, volumes[job.volume_uuid])
            if job.project_id and any(a.job.project_id == job.project_id for a in self.running_operations.values()):
                return False
            return check is not None and check.disks is not None and all(
                not check.disks.conflicts_with(active.disks) for active in self.running_operations.values()
            )

        try:
            while len(self.running_operations) < 2:
                job = queue.next_ready(availability, can_start)
                destination_ready = None
                if job is None and len(self.running_operations) == 1:
                    active = next(iter(self.running_operations.values()))
                    if active.verifying:
                        def can_prepare(candidate):
                            check = current_check(candidate, volumes[candidate.volume_uuid])
                            disks = check.disks if check else None
                            return (candidate.kind == "archive" and disks is not None
                                    and disks.destination is not None and disks.destination == active.disks.destination
                                    and (disks.shared_qnap or disks.source is not None)
                                    and disks.source != disks.destination)

                        job = queue.next_ready(availability, can_prepare, preparing_after=active.job)
                        destination_ready = active.worker.finished_writing
                if job is None:
                    break
                if job.project_id and destination_ready is None:
                    peers = self._archive_stream_group(job, queue.ready_jobs(availability, can_start), checks)
                    if len(peers) > 1:
                        if self.running_operations:
                            break
                        self._start_archive_group(peers, checks, volumes)
                        continue
                queue.set_state(job.id, "running")
                check = checks[job.id]
                self._start_queue_job(job, check.disks, volumes[job.volume_uuid]["id"], check.path, destination_ready)
                if job.kind == "archive":
                    self.running_operations[job.id].following_drive_checks = {
                        other.id: checks[other.id] for other in candidates
                        if other.id != job.id and other.volume_uuid == job.volume_uuid and other.id in checks
                        and checks[other.id].path == check.path
                    }
        except (OSError, ValueError) as exc:
            self.pause_operations()
            self.statusBar().showMessage(f"The queue could not be saved. {exc}", 10000)
            return
        if not self.running_operations:
            if not any(item.state in ACTIONABLE for item in queue.jobs):
                self.operations_timer.stop()
                self._clear_queue_check_progress()
            else:
                waiting = next((item for item in queue.jobs if item.state in {"waiting", "relink"}), None)
                if waiting is not None:
                    self.scan_progress.setRange(0, 1)
                    self.scan_progress.setValue(0)
                    self.scan_progress.setFormat(STATUSES[waiting.state])
                    session = self.project_sessions.get(waiting.project_id)
                    protection = "Source protected · " if session and session.guard is not None else ""
                    self._show_queued_progress(protection + (waiting.detail or STATUSES[waiting.state]), operation=waiting)
        self._refresh_operations()

    def _archive_stream_group(self, first, ready_jobs, checks):
        """Fresh writers to distinct disks can consume one ordered stream."""
        peers = []
        disks = set()
        for job in ready_jobs:
            if job.project_id != first.project_id:
                continue
            disk = checks[job.id].disks
            # Partial archives may need to resume at different source files.
            if (disk.destination is None or disk.destination == disk.source or disk.destination in disks
                    or (self.db.path / f"archive-job-{job.id}.sqlite").exists()):
                if job.id == first.id:
                    return [first]
                continue
            peers.append(job)
            disks.add(disk.destination)
        return peers

    def _start_archive_group(self, peers, checks, volumes):
        stream = SharedArchiveStream([job.id for job in peers])
        try:
            for job in peers:
                target = checks[job.id]
                self.operations_queue.set_state(job.id, "running")
                self._start_queue_job(job, target.disks, volumes[job.volume_uuid]["id"],
                                      target.path, shared_stream=stream)
        finally:
            for job in peers:
                if job.id not in self.running_operations:
                    stream.detach(job.id)
                    self.operations_queue.set_state(job.id, "paused", "Could not start the archive writer.")

    def _queue_drive_checks(self, candidates, volumes):
        probe = self._queue_drive_probe
        if probe is None or probe[0] is not self.db:
            requests = [
                (job, volume, self.current_source_path_for_volume(volume))
                for job in candidates
                if (volume := volumes.get(job.volume_uuid)) is not None
            ]
            result = Future()
            self._queue_drive_probe = (self.db, result)
            result.add_done_callback(lambda _: self.queue_drive_probe_finished.emit())
            Thread(
                target=_probe_queue_drives, args=(requests, result), name="jvvv-queue-drive-probe", daemon=True,
            ).start()
            return
        result = probe[1]
        if not result.done():
            return
        self._queue_drive_probe = None
        try:
            return result.result()
        except Exception as exc:
            self.statusBar().showMessage(f"Could not check the drives for queued jobs. {exc}", 10000)
            return

    def _clear_queue_check_progress(self) -> None:
        if self.scan_progress.format() != QUEUE_DRIVE_CHECK_MESSAGE:
            return
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Idle")
        self.operation_progress_label.clear()
        self.operation_progress_label.hide()
        if self.operations_dialog is not None:
            self.operations_dialog.progress.clear()

    def _start_queue_job(self, job: Operation, disks: OperationDisks, volume_id: int, path: str,
                         destination_ready=None, *, shared_stream=None) -> None:
        events = QueueJobEvents(self, job.id)
        if job.kind == "archive":
            session = self.project_sessions.get(job.project_id)
            if job.project_id and session is None and any(
                other.project_id == job.project_id and other.id != job.id and other.tab == "Pending"
                for other in self.operations_queue.jobs
            ):
                session = self.project_sessions[job.project_id] = ProjectSession()
            worker = thread = ArchiveThread(
                self.db.path, job, volume_id, self.preview_settings, self, writer_lock=self.queue_writer_lock,
                resolved_destination=path, destination_ready=destination_ready,
                session=session,
                shared_stream=shared_stream,
            )
            worker.progress.connect(events.archive_progress)
            thread.finished.connect(events.archive_finished)
            message = "Preparing archive…"
        else:
            worker = ScanWorker(
                self.db.path, volume_id, preview_settings=self.preview_settings,
                volume_uuid=job.volume_uuid, writer_lock=self.queue_writer_lock, resolved_source_path=path,
            )
            worker.catalogue_ready.connect(events.catalogue_ready)
            worker.progress.connect(events.scan_progress)
            worker.stats_progress.connect(events.scan_stats_progress)
            worker.log_message.connect(self.on_scan_log_message)
            worker.finished.connect(events.scan_result)
            worker.failed.connect(events.scan_failed)
            thread = _create_worker_thread(
                self, worker, (worker.finished, worker.failed), events.scan_thread_finished,
            )
            message = "Preparing scan…"
        self.running_operations[job.id] = RunningOperation(job, disks, worker, thread, events)
        self.scan_cancel_requested = False
        self._set_scan_running_ui(True)
        self._update_context_actions()
        self._refresh_operations()
        self._queue_progress(job.id, message)
        thread.start()

    def _finish_operation(self, job_id: str, state: str, detail: str) -> None:
        active = self.running_operations.pop(job_id)
        active.events.deleteLater()
        if state == "failed":
            problem = active.worker.destination_problem
            if problem == "Waiting for Drive":
                state = "waiting"
            elif problem == "Relink Required":
                state = "relink"
        self._set_scan_running_ui(bool(self.running_operations))
        saved = self._change_operations(self.operations_queue.set_state, job_id, state, detail)
        if active.job.kind == "archive":
            active.worker.finished_writing.set_result(saved and state in DONE)
        if not saved:
            self.operations_timer.stop()
            self._refresh_operations()
        if self.running_operations:
            self._render_running_progress()
        else:
            self.stop_scan_button.setText("Cancel Scan")
            self.scan_progress.setRange(0, 1)
            self.scan_progress.setValue(1 if state in DONE else 0)
            self.scan_progress.setFormat(STATUSES[state])
            self.operation_progress_label.clear()
            self.operation_progress_label.hide()
            if self.operations_dialog is not None:
                self.operations_dialog.progress.clear()
        QTimer.singleShot(0, self._pump_operations)

    def on_archive_progress(self, label, done, total, *, job_id) -> None:
        active = self.running_operations.get(job_id)
        if active is None or active.worker.pause_requested.is_set():
            return
        if label == VERIFY_PHASE and not active.verifying:
            active.verifying = True
            QTimer.singleShot(0, self._pump_operations)
        message = f"{label} {format_size(done)} / {format_size(total)}" if total else label
        self._queue_progress(job_id, message, done, total)
        self.statusBar().showMessage(message)

    def _queue_progress(self, job_id: str, message: str, done: int = 0, total: int = 0) -> None:
        active = self.running_operations.get(job_id)
        if active is None:
            return
        active.message, active.done, active.total = message, done, total
        if self.operations_dialog is not None:
            self.operations_dialog.set_job_progress(job_id, message, done, total)
        self._render_running_progress()

    def _render_running_progress(self) -> None:
        active = list(self.running_operations.values())
        project_id = active[0].job.project_id
        if project_id and all(a.job.project_id == project_id for a in active):
            members = [j for j in self.operations_queue.jobs if j.project_id == project_id]
            if len(members) > 1:
                completed = sum(j.state in DONE for j in members)
                # Each disk has two byte passes: writing and reading back.
                passes = 2.0 * completed
                for entry in active:
                    passes += int(entry.verifying)
                    if entry.total:
                        passes += min(1, entry.done / entry.total)
                    elif entry.verifying:
                        passes += 1  # verification finished; catalogue or previews are being finalized
                message = f"{Path(active[0].job.source).name} · {completed} of {len(members)} copies verified"
                detail = " · ".join(f"{a.job.volume_label}: {a.message}" for a in active)
                self.scan_progress.setRange(0, 1000)
                self.scan_progress.setValue(round(1000 * passes / (2 * len(members))))
                self.scan_progress.setFormat(f"{message} (%p%)")
                self.operation_progress_label.setText(detail)
                self.operation_progress_label.setToolTip(detail)
                self.operation_progress_label.show()
                self.stop_scan_button.setText("Pause Queue")
                if self.operations_dialog is not None:
                    self.operations_dialog.progress.setText(message)
                return
        if len(active) > 1:
            message = f"{len(active)} jobs running · See Queue Manager for progress"
            preparing = next((entry for entry in active if entry.job.kind == "archive"
                              and entry.worker.destination_ready is not None
                              and not entry.worker.destination_ready.done()), None)
            if preparing is not None:
                message = f"Preparing {Path(preparing.job.source).name} · {preparing.message}"
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(message)
            self.operation_progress_label.setText(message)
            self.operation_progress_label.setToolTip("\n".join(
                f"{entry.job.title} ? {entry.message}" for entry in active
            ))
            self.operation_progress_label.show()
            self.stop_scan_button.setText("Pause Queue")
            return
        entry = active[0]
        self.stop_scan_button.setText("Pause Archive" if entry.job.kind == "archive" else "Pause Scan")
        self._show_queued_progress(entry.message, operation=entry.job)
        if entry.total > 0:
            # Qt progress values are bounded integers, but file byte counts are not.
            self.scan_progress.setRange(0, 1000)
            self.scan_progress.setValue(min(1000, max(0, entry.done * 1000 // entry.total)))
            self.scan_progress.setFormat(f"{entry.message} (%p%)")
        else:
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat(entry.message)

    def _show_queued_progress(self, message: str, *, operation=None) -> None:
        label = getattr(self, "operation_progress_label", None)
        if label is None:
            return
        if operation is None and self.operation_id is not None:
            operation = self.operations_queue.get(self.operation_id)
        if operation is None:
            return
        text = f"{operation.title} · {message}"
        label.setText(text)
        label.setToolTip(text)
        label.show()
        if self.operations_dialog is not None:
            self.operations_dialog.progress.setText(text)

    def on_archive_finished(self, job_id: str) -> None:
        thread = self.running_operations[job_id].thread
        result = thread.result
        if result is not None:
            message = f"Verified {result.files:,} files ({format_size(result.bytes_copied)}). {result.path}"
            if thread.preview_problem:
                message += f" Offline previews skipped. {thread.preview_problem}"
            elif result.preview_failures:
                message += f" {result.preview_failures:,} previews failed."
            self.statusBar().showMessage(message, 8000)
            self.refresh_after_catalogue_write()
            self._finish_operation(job_id, "warning" if result.preview_failures or thread.preview_problem else "completed", message)
        else:
            self._finish_operation(job_id, "paused" if thread.paused else "failed", thread.error)
        thread.deleteLater()

    def plan_scan_previews(self) -> tuple[PreviewSettings | None, str | None] | None:
        """Scan-start preview preflight (spec §26, §27).

        Returns ``(settings, None)`` to scan with previews, ``(None, None)`` when
        previews are disabled, ``(None, reason)`` when the user chose to scan
        without previews after a failed preflight, or ``None`` to cancel the scan.
        """

        while True:
            settings = self.preview_settings
            if not settings.enabled:
                return None, None
            report = self.run_preview_preflight(settings)
            if report.passed:
                return settings, None
            choice = self.ask_preview_preflight_decision(report)
            if choice == "settings":
                self.edit_preview_settings()
                continue
            if choice == "app_settings":
                self.show_preferences()
                continue
            if choice == "without":
                failure = report.first_failure
                reason = (
                    f"{failure.label}: {failure.detail}"
                    if failure is not None
                    else "the preview configuration failed validation"
                )
                return None, reason
            return None

    def run_preview_preflight(self, settings: PreviewSettings) -> PreviewValidationReport:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return preflight_preview_configuration(settings)
        finally:
            QApplication.restoreOverrideCursor()

    def ask_preview_preflight_decision(self, report: PreviewValidationReport) -> str:
        """Return 'settings', 'app_settings', 'without', or 'cancel'."""

        failure = report.first_failure
        problem = "The preview configuration failed validation."
        if failure is not None:
            problem = f"{failure.label} failed."
            if failure.detail:
                problem += f"\n{failure.detail}"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Offline Preview Preflight Failed")
        box.setText(
            "Offline preview generation is enabled, but the configuration is not working."
        )
        box.setInformativeText(f"Problem:\n{problem}")
        box.setDetailedText(report.report_text())
        settings_button = box.addButton("Preview Manager", QMessageBox.ButtonRole.ActionRole)
        app_settings_button = box.addButton("App Settings", QMessageBox.ButtonRole.ActionRole)
        without_button = box.addButton("Scan Without Previews", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton("Cancel Scan", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked == settings_button:
            return "settings"
        if clicked == app_settings_button:
            return "app_settings"
        if clicked == without_button:
            return "without"
        return "cancel"

    @Slot(str)
    def on_scan_log_message(self, message: str) -> None:
        if hasattr(self, "scan_log"):
            self.scan_log.appendPlainText(message)

    @Slot()
    def on_scan_catalogue_ready(self, *, job_id=None) -> None:
        if job_id is not None:
            self._queue_progress(job_id, "Generating previews…")
            self.refresh_after_catalogue_write()
            return
        self.scan_preview_phase = True
        self.refresh_after_catalogue_write()
        self.stop_scan_button.setText("Cancel Previews")
        self.stop_scan_button.setToolTip("Stop previews; the updated catalogue will be kept.")
        self._show_queued_progress("Generating previews…")
        self.statusBar().showMessage("Catalogue updated and ready to browse. Generating previews…")

    def cancel_current_operation(self) -> None:
        if self.running_operations:
            if len(self.running_operations) > 1:
                self.pause_operations()
            else:
                self.pause_queue_job(self.operation_id)
            self.stop_scan_button.setEnabled(False)
        else:
            self.cancel_scan()

    def cancel_scan(self) -> None:
        if getattr(self, "running_operations", None):
            job = next((entry.job for entry in self.running_operations.values() if entry.job.kind == "scan"), None)
            if job is not None:
                self.pause_queue_job(job.id)
            return
        if self.scan_worker is None or self.scan_cancel_requested:
            return
        self.scan_cancel_requested = True
        self.scan_worker.cancel()
        self.stop_scan_button.setEnabled(False)
        message = (
            "Cancelling previews; catalogue update kept..."
            if getattr(self, "scan_preview_phase", False) else "Cancelling scan..."
        )
        self.scan_progress.setFormat(message)
        MainWindow._show_queued_progress(self, message)
        self.statusBar().showMessage(message)

    @Slot(int, int, str)
    def on_scan_progress(self, files_seen: int, folders_seen: int, current_path: str, *, job_id=None) -> None:
        if job_id is None and self.scan_cancel_requested:
            return
        if current_path.startswith(SCAN_PROGRESS_DETAIL_PREFIXES):
            text = f"{current_path} · {files_seen:,} files catalogued"
        else:
            text = f"Scanning... {files_seen:,} files, {folders_seen:,} folders - {current_path}"
        if job_id is not None:
            active = self.running_operations.get(job_id)
            if active is not None and not active.worker.cancel_requested:
                self._queue_progress(job_id, text)
            return
        if self.scan_progress.maximum() != 0:
            self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat(text)
        MainWindow._show_queued_progress(self, text)
        # A busy (indeterminate) QProgressBar never paints its format text, so
        # the phase ("Creating video preview · file · 42% of preview encode")
        # must also be shown somewhere visible (spec §30, §31).
        self.statusBar().showMessage(text)

    @Slot(int, int, str, int, int)
    def on_scan_stats_progress(
        self,
        files_seen: int,
        folders_seen: int,
        message: str,
        done: int,
        total: int,
        *,
        job_id=None,
    ) -> None:
        if job_id is not None:
            active = self.running_operations.get(job_id)
            if active is not None and not active.worker.cancel_requested:
                label = self.scan_stats_progress_label(message)
                self._queue_progress(job_id, f"{label} ({done:,}/{total:,})" if total else label, done, total)
            return
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
        MainWindow._show_queued_progress(self, f"{label} ({done:,}/{total:,})" if total > 0 else label)
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
    def on_scan_finished(self, result: dict, *, job_id=None) -> None:
        if job_id is None:
            self.scan_progress.setRange(0, 1)
            self.scan_progress.setValue(1)
        status = result.get("status", "completed")
        indexed = status in COMPLETED_SCAN_STATUSES
        registration_warning = str(result.get("registration_warning") or "")
        statistics = PreviewStatistics.from_dict(result.get("preview") or {})
        self.last_preview_statistics = statistics
        self.last_preview_operation_id = job_id
        preview_warning = status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
        # Spec §15: never a generic "completed" when previews failed.
        if job_id is None:
            self.scan_progress.setFormat(
                "Completed; previews interrupted" if statistics.interrupted
                else "Completed with preview errors" if preview_warning
                else "Completed with warnings" if registration_warning else status.title()
            )
        hash_errors = int(result.get("hash_errors", 0))
        access_errors = max(0, int(result.get("errors_count", 0)) - hash_errors)
        changes = result.get("changes") or {}
        files_removed = changes.get("files_removed", 0)
        folders_removed = changes.get("folders_removed", 0)
        removed_text = ""
        if indexed and (files_removed or folders_removed):
            removed_text = f" Removed from catalogue: {files_removed:,} files, {folders_removed:,} folders."
        preview_text = ""
        if statistics.mode == MODE_ENABLED:
            preview_text = (
                f" Offline previews: {statistics.total_generated} generated, "
                f"{statistics.total_reused} reused, {statistics.total_failed} failed"
                + (
                    f", {statistics.storage_skipped} skipped (storage unavailable)"
                    if statistics.storage_skipped
                    else ""
                )
                + "."
            )
        elif statistics.mode == MODE_SKIPPED_PREFLIGHT:
            preview_text = " Offline previews skipped (preflight failed)."
        self.statusBar().showMessage(
            f"Scan {status.replace('_', ' ')}: {result.get('files_seen', 0)} files, "
            f"{result.get('files_hashed', 0)} content hashes "
            f"({format_size(int(result.get('bytes_hashed', 0)))} read), "
            f"{result.get('media_metadata_collected', 0)}/"
            f"{result.get('media_files', 0)} media details available "
            f"({result.get('media_metadata_reused', 0)} reused), "
            f"{access_errors} incomplete/access issues, {hash_errors} hash unavailable."
            f"{removed_text}{preview_text}",
            8000,
        )
        self.refresh_after_catalogue_write()
        timings = result.get("phase_seconds") or {}
        if timings:
            self.on_scan_log_message(
                "Scan timing: indexing {:.2f}s (hashing {:.2f}s, media {:.2f}s), "
                "previews {:.2f}s, total {:.2f}s. Media details reused: {}.".format(
                    timings.get("indexing", 0), timings.get("hashing", 0),
                    timings.get("media", 0), timings.get("previews", 0),
                    timings.get("total", 0), result.get("media_metadata_reused", 0),
                )
            )
        detail = f"{result.get('files_seen', 0):,} files. {result.get('message') or ''}".strip()
        if registration_warning:
            detail += f" {registration_warning}"
        if indexed and changes:
            detail += (f" Added {changes.get('files_added', 0):,}, changed {changes.get('files_changed', 0):,},"
                       f" removed {files_removed:,} files and {folders_removed:,} folders from the catalogue.")
        if indexed and statistics.mode == MODE_ENABLED:
            detail += (f" Previews generated {statistics.total_generated:,}, reused {statistics.total_reused:,},"
                       f" failed {statistics.total_failed:,}, skipped {statistics.storage_skipped:,}.")
            if statistics.root:
                detail += f" Preview storage {statistics.root} ({format_size(statistics.bytes_written)} written)."
            if statistics.storage_unavailable_reason:
                detail += f" {statistics.storage_unavailable_reason}"
            if statistics.interrupted:
                detail += " Previews were interrupted. The updated catalogue and completed previews were kept."
        state = (
            "warning" if indexed and (preview_warning or statistics.interrupted
                                      or statistics.mode == MODE_SKIPPED_PREFLIGHT or registration_warning)
            else "completed" if indexed
            else "paused" if status == "cancelled"
            else "failed"
        )
        if job_id is None:
            self.scan_queue_result = (state, detail)
        else:
            active = self.running_operations[job_id]
            if statistics.interrupted and active.worker.cancel_requested:
                state = "paused"
            active.result = (state, detail)

    def show_preview_failures(self, statistics: PreviewStatistics | None = None) -> None:
        statistics = statistics or self.last_preview_statistics
        if statistics is None:
            return
        dialog = PreviewFailuresDialog(
            list(statistics.failures),
            self,
            storage_unavailable_reason=statistics.storage_unavailable_reason,
        )
        dialog.exec()

    @Slot(str)
    def on_scan_failed(self, details: str, *, job_id=None) -> None:
        if job_id is None:
            self.scan_progress.setRange(0, 1)
            self.scan_progress.setValue(0)
            self.scan_progress.setFormat("Scan failed")
        self.on_scan_log_message(details)
        message = details.strip().splitlines()[-1] if details.strip() else "Scan failed."
        if job_id is None:
            self.scan_queue_result = ("failed", message)
        else:
            self.running_operations[job_id].result = ("failed", message)
        self.statusBar().showMessage(message, 10000)
        self.refresh_after_catalogue_write()

    @Slot()
    def clear_scan_worker(self, job_id=None) -> None:
        if job_id is not None:
            result = self.running_operations[job_id].result or ("failed", "Scan finished without a result.")
            self._finish_operation(job_id, *result)
            if self.backups_panel is not None:
                self.refresh_backups_panel(refresh=True)
            return
        self.scan_preview_phase = False
        self.stop_scan_button.setText("Cancel Scan")
        self.stop_scan_button.setToolTip("")
        self.scan_cancel_requested = False
        self._set_scan_running_ui(False)
        self._update_archive_action()
        if self.backups_panel is not None:
            self.refresh_backups_panel(refresh=True)
        self.scan_queue_result = None

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
        if self.backups_panel is not None:
            self.refresh_backups_panel(refresh=True)

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
        if getattr(self, "backups_panel", None) is not None:
            self.refresh_backups_panel(refresh=True)
        cache_dialog = getattr(self, "preview_cache_dialog", None)
        if cache_dialog is not None:
            # The catalogue's hashes changed, so a displayed "not referenced by
            # this catalogue" list may now be wrong (spec §22).
            cache_dialog.invalidate_unreferenced()

    def select_volume(self, volume_id: int) -> bool:
        for row, item in enumerate(self.volume_model.items):
            if item.id == volume_id:
                index = self.volume_model.index(row, 0)
                self.volume_table.selectRow(row)
                self.volume_table.setCurrentIndex(index)
                self.volume_table.scrollTo(index)
                return True
        return False

    def load_volume_browser(self, volume) -> None:
        if volume is None:
            return
        root = self.db.get_root_folder(volume["id"])
        if root is None:
            self.set_browser_notice("No scan data available for this volume.")
            return
        self.update_browser_connection_notice(volume)
        root_item = self._folder_tree_item(root)
        self.folder_tree.addTopLevelItem(root_item)
        self.add_placeholder_if_needed(root_item)
        self.folder_tree.setCurrentItem(root_item)
        root_item.setExpanded(True)

    def update_browser_connection_notice(self, volume) -> None:
        connected = self.current_source_path_for_volume(volume) is not None
        self.set_browser_notice(
            "" if connected else "This volume is offline. Showing the saved catalogue."
        )

    def clear_browser(self) -> None:
        self.set_browser_notice("")
        self.folder_tree.clear()
        self.current_directory_items = []
        self.browser_model.set_items([])
        self.current_folder_id = None
        self.current_path_label.setText("/")
        self.up_button.setEnabled(False)
        MainWindow.update_browser_status_strip(self, None, None, {})

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
        self.refresh_backups_panel()

    def load_directory_items(self, volume_id: int, folder_id: int) -> None:
        if self.db is None:
            return
        folder = self.db.get_folder(folder_id)
        if folder is None:
            self.current_directory_items = []
            self.browser_model.set_items([])
            self.current_path_label.setText("/")
            self.up_button.setEnabled(False)
            MainWindow.update_browser_status_strip(self, None, None, {})
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
                    ),
                )
            )

        self.current_directory_items = items
        MainWindow.apply_browser_backup_filter(self)
        MainWindow.update_browser_status_strip(self, folder, engine, volume_references)
        self.current_path_label.setText(relative_path_for_display(folder["relative_path"]))
        self.up_button.setEnabled(folder["parent_id"] is not None)
        self.apply_table_default_columns(
            self.file_table,
            {1: 230, 2: 140, 3: 95, 4: 155, 5: 280, 6: 90},
            only_if_empty=True,
        )

    def update_browser_status_strip(
        self,
        folder: Any,
        engine: Any,
        volume_references: dict[int, str],
    ) -> None:
        strip = getattr(self, "browser_status_strip", None)
        if strip is None:
            return
        folder_display: BackupDisplay | None = None
        if engine is not None and folder is not None:
            try:
                folder_display = item_backup_display(
                    engine.folder_status(int(folder["id"])),
                    volume_references,
                )
            except Exception:
                folder_display = None
        text = ""
        if folder_display is not None and folder_display.reason != "not_analyzed":
            text = f"This folder: {folder_display.text}"
        strip.setText(text)
        strip.setVisible(bool(text))

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
        if hasattr(self, 'backups_panel'):
            self.refresh_backups_panel()

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
        real_path = self.cached_catalogue_item_path(target)
        real_available = real_path is not None and not target.missing

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

        if target.is_folder and self.db and not self.db.read_only and len(PurePosixPath(target.relative_path).parts) == 1:
            volume = self.db.get_volume(target.volume_id)
            project_id = self.db.archive_records.folder_project(volume["uuid"], target.relative_path) if volume else None
            if project_id:
                menu.addAction("Add another copy…", lambda: self.archive_project(project_id=project_id))

        media_kind = (
            None
            if target.is_folder
            else media_kind_for_extension(PurePosixPath(target.relative_path).suffix)
        )
        if media_kind in {"image", "video"}:
            # Spec §19/§45/§46: external-only opening; enabled iff the preview
            # file exists AND passes validation (a corrupt file is never
            # treated as valid), with the reason shown as a tooltip otherwise.
            _kind, info, file_row = self.preview_info_for_target(target)
            available = info is not None and info.exists and info.valid
            reason = "" if available else self.preview_unavailable_reason(media_kind, info, file_row)
            preview_action = menu.addAction(preview_open_label(media_kind))
            preview_action.setEnabled(available)
            if reason:
                preview_action.setToolTip(reason)
                preview_action.setStatusTip(reason)
            preview_action.triggered.connect(
                lambda checked=False, target=target: self.open_preview_for_target(target)
            )
            reveal_preview_action = menu.addAction("Open Preview Location")
            reveal_preview_action.setEnabled(available)
            if reason:
                reveal_preview_action.setToolTip(reason)
            reveal_preview_action.triggered.connect(
                lambda checked=False, target=target: self.open_preview_for_target(
                    target,
                    reveal=True,
                )
            )
            if reason:
                # Disabled menu items never show tooltips under the Fusion
                # style, so the reason is stated as its own menu line (§19).
                note_action = menu.addAction(f"Preview not available: {reason}")
                note_action.setEnabled(False)

        menu.addSeparator()
        backups_action = menu.addAction("View backups")
        backups_action.triggered.connect(
            lambda: self.show_backups(target, navigate=include_catalogue_location)
        )
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
        volume = self.db.get_volume(target.volume_id) if self.db is not None else None
        if volume is None or target.missing:
            self.statusBar().showMessage(
                "The real item is not available because the volume is offline or changed.",
                5000,
            )
            return
        key = (volume["uuid"], target.relative_path, reveal)
        if key in self._file_open_requests:
            return
        result = Future()
        self._file_open_requests[key] = result
        result.add_done_callback(lambda done: self.catalogue_file_open_finished.emit(key, done))
        self.statusBar().showMessage("Opening file location…" if reveal else "Opening item…", 5000)
        Thread(
            target=_open_catalogue_file,
            args=(volume, self.current_source_path_for_volume(volume), target.relative_path, reveal, result),
            name="jvvv-open-catalogue-file", daemon=True,
        ).start()

    @Slot(object, object)
    def _finish_catalogue_file_open(self, key: FileOpenKey, result: Future[bool]) -> None:
        if self._file_open_requests.get(key) is not result:
            return
        del self._file_open_requests[key]
        try:
            opened = result.result()
        except Exception as exc:
            QMessageBox.warning(self, "Open Failed", str(exc))
        else:
            if not opened:
                self.statusBar().showMessage(
                    "The real item is not available because the volume is offline or changed.",
                    5000,
                )

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

    def cached_catalogue_item_path(self, target: CatalogueItemRef) -> Path | None:
        volume = self.db.get_volume(target.volume_id) if self.db is not None else None
        source = self.current_source_path_for_volume(volume)
        return self.physical_path_for_source(source, target.relative_path) if source is not None else None

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
        target = CatalogueItemRef(
            item_type, item_id, int(record["volume_id"]), record["relative_path"], bool(record["missing"]),
        )

        def view_backups() -> None:
            dialog.accept()
            self.show_backups(target, navigate=self.tabs.currentWidget() is self.search_tab)

        dialog = ItemPropertiesDialog(
            self,
            icon,
            name,
            type_label,
            self.catalogue_item_property_rows(record),
            actions=[("View backups", view_backups), *self.preview_property_actions(record)],
        )
        dialog.exec()

    def preview_property_actions(self, record) -> list[tuple[str, Callable[[], None]]]:
        """Preview actions for a file whose preview is usable."""

        if record["item_type"] != "file":
            return []
        target = CatalogueItemRef(
            item_type="file",
            item_id=int(record["item_id"]),
            volume_id=int(record["volume_id"]),
            relative_path=record["relative_path"] or "",
            missing=bool(record["missing"]),
        )
        media_kind, info, _file_row = self.preview_info_for_target(target)
        if media_kind not in {"image", "video"} or info is None:
            return []
        if not (info.exists and info.valid):
            return []
        return [
            (
                preview_open_label(media_kind),
                lambda target=target: self.open_preview_for_target(target),
            ),
            (
                "Open Preview Location",
                lambda target=target: self.open_preview_for_target(target, reveal=True),
            ),
        ]

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
            hash_algorithm = HASH_LABELS.get(stored_hash_algorithm, stored_hash_algorithm.upper())
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
                    "pillow": "image reader (Pillow, pillow-heif, rawpy)",
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
                preview_kind = media_kind_for_extension(extension)
                if preview_kind in {"image", "video"}:
                    properties.extend(
                        preview_property_rows(
                            record,
                            getattr(self, "preview_settings", None),
                            preview_kind,
                        )
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
        engine = getattr(self, "backup_engine", None)
        status = None
        if engine is not None:
            try:
                item_id = int(record["item_id"])
                status = engine.item_statuses(item_type, (item_id,)).get(item_id)
            except Exception:
                pass
        display = item_backup_display(status, getattr(self, "backup_volume_references", {}))
        properties.append(("Backups", display.text))
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

    def real_path_for(self, volume, relative_path: str) -> Path | None:
        source_path = resolve_volume_source_path(volume)
        if source_path is None:
            return None
        return self.physical_path_for_source(source_path, relative_path)

    def physical_path_for_source(self, source_path: str, relative_path: str) -> Path:
        return path_with_relative(source_path, relative_path)

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
            self.statusBar().clearMessage()
            return

        request = (
            request_id,
            self.db.path,
            query,
            self.search_include_paths,
        )
        self.search_model.set_items([])
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

        self.search_worker = SearchWorker(
            db_path,
            query,
            request_id,
            list(self._connected_volume_snapshots),
            include_paths=include_paths,
            backup_filter_key=MainWindow.search_backup_filter_key(self),
        )
        self.search_worker.batch_ready.connect(self.on_search_batch_ready)
        self.search_worker.finished.connect(self.on_search_finished)
        self.search_worker.cancelled.connect(self.on_search_cancelled)
        self.search_worker.failed.connect(self.on_search_failed)
        self.search_thread = _create_worker_thread(
            self,
            self.search_worker,
            (
                self.search_worker.finished,
                self.search_worker.cancelled,
                self.search_worker.failed,
            ),
            self.clear_search_worker,
        )
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
                f"  Content hashes: {int(row['files_hashed'] or 0):,} files, "
                f"{format_size(int(row['bytes_hashed'] or 0))} read, "
                f"{hash_errors:,} unavailable"
            )
            if int(row["media_files"] or 0):
                media_files = int(row["media_files"] or 0)
                media_collected = int(row["media_metadata_collected"] or 0)
                lines.append(
                    f"  Media inspection: {media_collected:,} of {media_files:,} recognized "
                    f"media files have complete or partial details (new or reused); "
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
            preview_mode = str(row["preview_mode"] or "disabled")
            if preview_mode == MODE_ENABLED:
                storage_skipped = int(row["previews_storage_skipped"] or 0)
                lines.append(
                    "  Offline previews: images "
                    f"{int(row['image_previews_generated'] or 0):,} generated, "
                    f"{int(row['image_previews_reused'] or 0):,} reused, "
                    f"{int(row['image_previews_failed'] or 0):,} failed; videos "
                    f"{int(row['video_previews_generated'] or 0):,} generated, "
                    f"{int(row['video_previews_reused'] or 0):,} reused, "
                    f"{int(row['video_previews_failed'] or 0):,} failed; "
                    f"{format_size(int(row['preview_bytes_written'] or 0))} written"
                    + (
                        f"; {storage_skipped:,} not attempted (preview storage unavailable)"
                        if storage_skipped
                        else ""
                    )
                )
            elif preview_mode == MODE_SKIPPED_PREFLIGHT:
                lines.append("  Offline previews: skipped because the preflight check failed")
            if row["preview_message"]:
                lines.append(f"  {row['preview_message']}")
            if row["message"]:
                lines.append(f"  {row['message']}")
        if errors:
            lines.append("")
            lines.append("Recent errors:")
            for row in errors:
                lines.append(f"{self._display_time(row['created_at'])} - {row['path']}: {row['message']}")
        preview_failures = self.db.list_preview_failures(volume_id, limit=500)
        if preview_failures:
            lines.append("")
            lines.append(
                f"Offline preview failures recorded for indexed files ({len(preview_failures):,}):"
            )
            for row in preview_failures:
                stage = f" [{row['error_stage']}]" if row["error_stage"] else ""
                lines.append(
                    f"{self._display_time(row['updated_at'])} - {row['relative_path']}"
                    f"{stage}: {row['error_message']}"
                )
        self.scan_log.setPlainText("\n".join(lines))

    # -- previews: cache manager (spec §22) --------------------------
    def show_preview_cache(self) -> None:
        if self.db is None:
            return
        if self.preview_cache_dialog is None:
            dialog = PreviewCacheDialog(self.preview_settings, self)
            dialog.scan_requested.connect(self.start_preview_store_scan)
            dialog.unreferenced_requested.connect(self.start_unreferenced_preview_scan)
            dialog.delete_requested.connect(self.start_delete_previews)
            dialog.delete_temporaries_requested.connect(self.start_delete_temporaries)
            dialog.open_folder_requested.connect(self.open_preview_folder)
            dialog.cancel_requested.connect(self.cancel_preview_store_operation)
            dialog.settings_requested.connect(self.persist_preview_settings)
            self.preview_cache_dialog = dialog
        else:
            self.preview_cache_dialog.set_settings(self.preview_settings)
        self.preview_cache_dialog.set_writable(
            not self.db.read_only and not self._catalogue_job_running()
        )
        self.preview_cache_dialog.show()
        self.preview_cache_dialog.raise_()
        self.preview_cache_dialog.activateWindow()

    def edit_preview_settings(self) -> None:
        """Allow scan preflight to wait for configuration before retrying the scan."""
        self.show_preview_cache()
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.exec()

    def open_preview_folder(self) -> None:
        root = self.preview_settings.root_path
        if root is None:
            QMessageBox.information(
                self,
                "Preview Manager",
                "Choose a preview storage directory in Window > Preview Manager first.",
            )
            return
        try:
            root.mkdir(parents=True, exist_ok=True)
            open_in_file_manager(root, reveal=False)
        except Exception as exc:
            QMessageBox.warning(self, "Open Preview Folder Failed", str(exc))

    def _preview_store_ready(self) -> bool:
        if self.preview_settings.root_path is None:
            QMessageBox.information(
                self,
                "Preview Manager",
                "Choose a preview storage directory in Window > Preview Manager first.",
            )
            return False
        if self._catalogue_job_running():
            self._show_catalogue_job_running_message()
            return False
        return True

    def _start_preview_store_worker(self, worker: QObject, operation: str, message: str) -> None:
        self.preview_store_worker = worker
        self.preview_store_operation = operation
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_busy(True, message)
        worker.progress.connect(self.on_preview_store_progress)
        worker.failed.connect(self.on_preview_store_failed)
        self.preview_store_thread = _create_worker_thread(
            self,
            worker,
            (worker.finished, worker.failed),
            self.clear_preview_store_worker,
        )
        self.statusBar().showMessage(message)
        self.preview_store_thread.start()

    def start_preview_store_scan(self) -> None:
        if not self._preview_store_ready():
            return
        worker = PreviewStoreStatisticsWorker(self.preview_settings)
        worker.finished.connect(self.on_preview_store_statistics)
        self._start_preview_store_worker(worker, "scan", "Scanning the preview store…")

    def start_unreferenced_preview_scan(self) -> None:
        if not self._preview_store_ready() or self.db is None:
            return
        worker = UnreferencedPreviewWorker(self.preview_settings, self.db.path)
        worker.finished.connect(self.on_unreferenced_previews)
        self._start_preview_store_worker(
            worker,
            "unreferenced",
            "Comparing the preview store with this catalogue's content hashes…",
        )

    def start_delete_previews(self, paths: list) -> None:
        if not paths or not self._preview_store_ready():
            return
        worker = DeletePreviewsWorker(self.preview_settings, [str(path) for path in paths])
        worker.finished.connect(self.on_previews_deleted)
        self._start_preview_store_worker(worker, "delete", "Deleting selected previews…")

    def start_delete_temporaries(self) -> None:
        if not self._preview_store_ready():
            return
        worker = DeleteTemporariesWorker(self.preview_settings)
        worker.finished.connect(self.on_temporaries_deleted)
        self._start_preview_store_worker(
            worker, "temporaries", "Deleting leftover temporary preview files…"
        )

    @Slot(int, int, list)
    def on_temporaries_deleted(self, deleted: int, kept: int, errors: list) -> None:
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_temporaries_deleted(int(deleted), int(kept), list(errors))
        if errors:
            QMessageBox.warning(
                self,
                "Some Temporary Files Were Not Deleted",
                f"{int(deleted):,} temporary files were deleted. "
                f"{len(errors):,} could not be deleted:\n\n" + "\n".join(str(e) for e in errors[:20])
                + ("\n…" if len(errors) > 20 else ""),
            )
        self.statusBar().showMessage(f"{int(deleted):,} temporary preview files deleted.", 5000)

    def cancel_preview_store_operation(self) -> None:
        worker = self.preview_store_worker
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
            if self.preview_cache_dialog is not None:
                self.preview_cache_dialog.set_busy(True, "Cancelling…")

    @Slot(int, str)
    def on_preview_store_progress(self, count: int, message: str) -> None:
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_progress(count, message)

    @Slot(object, object)
    def on_preview_store_statistics(self, statistics: object, free_bytes: object) -> None:
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_store_statistics(
                statistics if isinstance(statistics, PreviewStoreStatistics) else None,
                int(free_bytes) if free_bytes is not None else None,
            )
        self.statusBar().showMessage("Preview store scanned.", 4000)

    @Slot(list, int)
    def on_unreferenced_previews(self, entries: list, total_found: int) -> None:
        worker = getattr(self, "preview_store_worker", None)
        cancelled = bool(
            worker is not None
            and hasattr(worker, "is_cancelled")
            and worker.is_cancelled()
        )
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_unreferenced(
                [entry for entry in entries if isinstance(entry, PreviewEntry)],
                int(total_found),
                partial=cancelled,
            )
        if cancelled:
            self.statusBar().showMessage(
                "Unreferenced-preview comparison cancelled; the list is incomplete.",
                6000,
            )
        else:
            self.statusBar().showMessage(
                f"{int(total_found):,} previews are not referenced by this catalogue.",
                6000,
            )

    @Slot(int, list, list)
    def on_previews_deleted(self, deleted_count: int, deleted_paths: list, errors: list) -> None:
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.remove_entries([str(path) for path in deleted_paths])
            self.preview_cache_dialog.set_busy(
                False,
                f"{int(deleted_count):,} previews deleted"
                + (f"; {len(errors):,} could not be deleted." if errors else "."),
            )
        if errors:
            QMessageBox.warning(
                self,
                "Some Previews Were Not Deleted",
                f"{int(deleted_count):,} previews were deleted. "
                f"{len(errors):,} could not be deleted:\n\n" + "\n".join(str(e) for e in errors[:20])
                + ("\n…" if len(errors) > 20 else ""),
            )
        self.statusBar().showMessage(f"{int(deleted_count):,} previews deleted.", 5000)

    @Slot(str)
    def on_preview_store_failed(self, details: str) -> None:
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_busy(False, "The preview cache operation failed.")
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("Preview Manager Operation Failed")
        dialog.setText("The preview cache operation could not be completed.")
        dialog.setDetailedText(details)
        dialog.exec()

    @Slot()
    def clear_preview_store_worker(self) -> None:
        self.preview_store_worker = None
        self.preview_store_thread = None
        self.preview_store_operation = ""
        if self.preview_cache_dialog is not None:
            self.preview_cache_dialog.set_busy(False)

    def _stop_preview_store_for_close(self) -> bool:
        worker = getattr(self, "preview_store_worker", None)
        thread = getattr(self, "preview_store_thread", None)
        if worker is None or thread is None or not thread.isRunning():
            return True
        answer = QMessageBox.question(
            self,
            "Preview Manager Busy",
            "A preview cache operation is still running. Cancel it before closing?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if hasattr(worker, "cancel"):
            worker.cancel()
        if _wait_for_worker_thread(lambda: self.preview_store_thread):
            return True
        QMessageBox.information(
            self,
            "Preview Manager Cancelling",
            "Cancellation has been requested. Close the catalogue after the operation stops.",
        )
        return False

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
