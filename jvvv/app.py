from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import sys
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QEventLoop,
    QFileInfo,
    QLockFile,
    QModelIndex,
    QObject,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QLocale,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap, QPolygon, QShortcut
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
    QSplitter,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import APP_NAME
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
from .scanner import VolumeScanner, get_storage_stats
from .utils import format_size, open_in_file_manager, percentage_full, relative_path_for_display


ROLE_VOLUME_ID = Qt.ItemDataRole.UserRole
ROLE_FOLDER_ID = Qt.ItemDataRole.UserRole + 1
ROLE_RELATIVE_PATH = Qt.ItemDataRole.UserRole + 2
ROLE_ITEM_TYPE = Qt.ItemDataRole.UserRole + 3
ROLE_ITEM_ID = Qt.ItemDataRole.UserRole + 4
ROLE_PERCENT_FULL = Qt.ItemDataRole.UserRole + 5
CATALOGUE_FILE_FILTER = "Joemt Archive View Files (*.jvvv)"


@dataclass(frozen=True)
class TableColumn:
    title: str
    display: Callable[[Any], Any]
    sort_key: Callable[[Any], Any] | None = None
    alignment: Qt.AlignmentFlag | None = None
    decoration: Callable[[Any], QIcon | None] | None = None


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


VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "avi", "wmv", "webm", "m4v"}
AUDIO_EXTENSIONS = {"mp3", "wav", "flac", "aac", "m4a", "ogg", "wma"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "tif", "tiff", "gif", "bmp", "webp", "heic"}
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


@dataclass(frozen=True)
class VolumeItem:
    id: int
    drive_id: str
    name: str
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


@dataclass(frozen=True)
class SearchResultItem:
    item_type: str
    item_id: int
    name: str
    volume_id: int
    volume_name: str
    relative_path: str
    size_bytes: int | None
    modified_at: str | None
    missing: bool
    source_path: str
    connected: bool

    @property
    def is_folder(self) -> bool:
        return self.item_type == "folder"


class BrowserTableModel(StandardTableModel):
    def __init__(self, icons: CatalogueIconProvider, parent: QObject | None = None) -> None:
        self.icons = icons
        super().__init__(
            [
                TableColumn("Name", lambda item: item.name, decoration=self.icons.icon_for),
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
        return 0 if item.is_folder else 1


class VolumeTableModel(StandardTableModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            [
                TableColumn(
                    "Drive ID",
                    lambda item: item.drive_id or "-",
                    sort_key=lambda item: drive_id_sort_key(item.drive_id),
                ),
                TableColumn("Name", lambda item: item.name),
                TableColumn("Status", lambda item: item.register_status),
                TableColumn("Condition", lambda item: item.condition),
                TableColumn("Connector", lambda item: item.connector),
                TableColumn("Connection", lambda item: "Connected" if item.connected else "Offline"),
                TableColumn("Full", lambda item: f"{item.percent_full}%", sort_key=lambda item: item.percent_full),
                TableColumn(
                    "Files",
                    lambda item: str(item.indexed_file_count),
                    sort_key=lambda item: item.indexed_file_count,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn("Last Scan", lambda item: display_db_time(item.last_scan_at), sort_key=lambda item: item.last_scan_at or ""),
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
    def __init__(self, icons: CatalogueIconProvider, parent: QObject | None = None) -> None:
        self.icons = icons
        super().__init__(
            [
                TableColumn("Name", lambda item: item.name, decoration=self.icon_for),
                TableColumn("Kind", lambda item: item.item_type.title()),
                TableColumn("Volume", lambda item: item.volume_name),
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
        progress = QStyleOptionProgressBar()
        progress.rect = option.rect.adjusted(6, 4, -6, -4)
        progress.minimum = 0
        progress.maximum = 100
        progress.progress = percent
        progress.text = f"{percent}%"
        progress.textVisible = True
        QApplication.style().drawControl(QStyle.ControlElement.CE_ProgressBar, progress, painter)


def display_db_time(value: str | None) -> str:
    parsed = parse_db_time(value)
    if parsed is None:
        return "-"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def display_indexed_size(value: int | None) -> str:
    if value is None:
        return "Unknown"
    return format_size(value)


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


def volume_matches_filter(item: VolumeItem, query: str) -> bool:
    text = query.strip().casefold()
    if not text:
        return True
    haystack = " ".join(
        [
            item.drive_id or "",
            item.name,
            item.register_status,
            item.condition,
            item.description,
            item.connector,
        ]
    ).casefold()
    return all(term in haystack for term in text.split())


def guess_content_dates_from_path(source_path: str) -> tuple[str | None, str | None]:
    if not source_path:
        return None, None
    root = Path(source_path)
    if not root.exists():
        return None, None

    earliest: str | None = None
    latest: str | None = None

    def include_timestamp(timestamp: float) -> None:
        nonlocal earliest, latest
        value = datetime.fromtimestamp(timestamp).date().isoformat()
        earliest = value if earliest is None or value < earliest else earliest
        latest = value if latest is None or value > latest else latest

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            include_timestamp(current.stat().st_mtime)
        except OSError:
            pass
        if not current.is_dir():
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                        include_timestamp(stat_result.st_mtime)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue

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
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(680)
        self.current_volume_id = int(volume["id"]) if volume is not None else None
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
        self.name_edit = QLineEdit(volume["name"] if volume is not None else "")
        self.path_edit = QLineEdit(volume["source_path"] if volume is not None else "")
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse)

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
        for row in sorted(self.master_options, key=lambda item: (drive_id_sort_key(item["drive_id"]), item["name"].casefold())):
            self.master_combo.addItem(volume_reference(row["drive_id"], row["name"]), int(row["id"]))
        if volume is not None and volume["master_volume_id"] is not None:
            self.set_master_volume_id(int(volume["master_volume_id"]))

        self.description_edit = QPlainTextEdit(volume["description"] if volume is not None else "")
        self.description_edit.setMinimumHeight(90)

        self.validation_label = QLabel("")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b91c1c;")

        form = QFormLayout()
        form.addRow("Drive ID", self.drive_id_edit)
        form.addRow("Name", self.name_edit)
        form.addRow("Drive or folder", path_row)
        form.addRow("Status", self.status_combo)
        form.addRow("Condition", self.condition_combo)
        form.addRow("Connector", self.connector_combo)
        form.addRow("Date Added", self.date_added_edit)
        form.addRow("Earliest Content Date", self.earliest_date_edit)
        form.addRow("Latest Content Date", self.latest_date_edit)
        form.addRow("Retired Date", self.retired_date_edit)
        form.addRow("", self.mirror_check)
        form.addRow("Master Drive", self.master_combo)
        form.addRow("Mirror Date", self.mirror_date_edit)
        form.addRow("Description", self.description_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.validation_label)
        layout.addWidget(buttons)

        self.status_combo.currentTextChanged.connect(self.on_status_changed)
        self.mirror_check.toggled.connect(self.on_mirror_toggled)
        self.on_status_changed(self.status_combo.currentText())
        self.on_mirror_toggled(self.mirror_check.isChecked())

    def browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose Drive or Folder", self.path_edit.text())
        if directory:
            self.path_edit.setText(directory)
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(directory).name or directory)
            self.apply_content_date_guess(directory)

    def apply_content_date_guess(self, source_path: str | None = None) -> None:
        earliest, latest = guess_content_dates_from_path(source_path or self.path_edit.text().strip())
        self.earliest_date_edit.set_value_if_empty(earliest)
        self.latest_date_edit.set_value_if_empty(latest)

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
        if not name:
            return "Enter a volume name."

        existing_name_id = self.existing_names.get(name.casefold())
        if existing_name_id is not None and existing_name_id != self.current_volume_id:
            return "Volume names must be unique within the catalogue."

        drive_id = register["drive_id"]
        if not is_valid_drive_id(drive_id):
            return "Drive ID must use the format AID- followed by at least three digits."

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
            ("2. Add and scan a volume",
             "Choose <b>New Volume</b>, select a drive or folder, then scan it. "
             "Rescan later to record changes and mark missing items."),
            ("3. Browse offline",
             "Select a saved volume to explore its folder tree, even when the "
             "original drive is disconnected."),
            ("4. Search",
             "Find files and folders by name, extension, or relative path across "
             "the stored catalogue."),
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
        <p><b>New Catalogue → New Volume → Scan → Browse or Search</b></p>
        {section_html}
        <hr>
        <p><b>About</b><br>{APP_NAME} is GPLv3 open-source software by Joemt.<br>
        <a href="https://github.com/joedotmt/Joemt-Archive-View">Source code</a>
        &nbsp;·&nbsp; <a href="https://joe.mt">joe.mt</a></p>
        """


class ScanWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: Path, volume_id: int, remove_deleted: bool) -> None:
        super().__init__()
        self.db_path = db_path
        self.volume_id = volume_id
        self.remove_deleted = remove_deleted
        self.cancel_requested = False

    @Slot()
    def run(self) -> None:
        db: Database | None = None
        try:
            db = Database(self.db_path)
            scanner = VolumeScanner(
                db,
                progress_callback=lambda files, folders, path: self.progress.emit(files, folders, path),
                cancel_callback=lambda: self.cancel_requested,
            )
            result = scanner.scan(self.volume_id, remove_deleted=self.remove_deleted)
            self.finished.emit(
                {
                    "status": result.status,
                    "files_seen": result.files_seen,
                    "folders_seen": result.folders_seen,
                    "errors_count": result.errors_count,
                    "message": result.message or "",
                }
            )
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            if db is not None:
                db.close()

    @Slot()
    def cancel(self) -> None:
        self.cancel_requested = True


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.db: Database | None = None
        self.catalogue_path: Path | None = None
        self.catalogue_lock: QLockFile | None = None
        self.current_volume_id: int | None = None
        self.current_folder_id: int | None = None
        self.scan_thread: QThread | None = None
        self.scan_worker: ScanWorker | None = None
        self.browser_shortcuts: list[QShortcut] = []
        self.browser_icons = CatalogueIconProvider()
        self.volume_model = VolumeTableModel(self)
        self.browser_model = BrowserTableModel(self.browser_icons, self)
        self.search_model = SearchResultsTableModel(self.browser_icons, self)
        self.volume_full_delegate = VolumeFullDelegate(self)
        self.catalogue_actions: list[QAction] = []
        self.catalogue_widgets: list[QWidget] = []

        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)
        self.setStatusBar(QStatusBar())

        self._build_menu_bar()
        self._build_ui()
        self._connect_signals()
        self._set_catalogue_open(False)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if not self.close_catalogue(show_status=False):
            event.ignore()
            return
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

        help_menu = self.menuBar().addMenu("&Help")
        self.help_action = QAction("Help", self)
        self.help_action.setShortcut(QKeySequence(QKeySequence.StandardKey.HelpContents))
        self.help_action.triggered.connect(self.show_help)
        help_menu.addAction(self.help_action)

    def _build_ui(self) -> None:
        self.stack = QStackedWidget()
        self.welcome_page = self._build_welcome_page()
        self.catalogue_page = self._build_catalogue_workspace()
        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.catalogue_page)
        self.setCentralWidget(self.stack)

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(14)

        title = QLabel("No Catalogue Open")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 8)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        description = QLabel("Create a new catalogue file or open an existing .jvvv catalogue.")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description.setWordWrap(True)

        self.welcome_new_button = QPushButton("Create New Catalogue")
        self.welcome_open_button = QPushButton("Open Existing Catalogue")
        self.welcome_new_button.setMinimumWidth(240)
        self.welcome_open_button.setMinimumWidth(240)

        layout.addWidget(title)
        layout.addWidget(description)
        layout.addSpacing(8)
        layout.addWidget(self.welcome_new_button, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.welcome_open_button, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def _build_catalogue_workspace(self) -> QWidget:
        self.volume_table = QTableView()
        self.volume_table.setModel(self.volume_model)
        self.configure_table_view(self.volume_table)
        self.volume_table.setItemDelegateForColumn(6, self.volume_full_delegate)
        self.volume_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.volume_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.volume_table,
                {1: 150, 2: 105, 3: 95, 4: 110, 5: 95, 6: 80, 7: 80, 8: 145},
            ),
        )

        self.volume_filter_edit = QLineEdit()
        self.volume_filter_edit.setPlaceholderText("Filter volumes by ID, name, status, condition, description, or connector")
        self.add_button = QPushButton("New Volume")

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Volumes"))
        left_layout.addWidget(self.volume_filter_edit)
        left_layout.addWidget(self.volume_table, 1)
        left_layout.addWidget(self.add_button)

        self.details_box = self._build_details_box()
        self.tabs = QTabWidget()
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

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.details_box)
        right_layout.addWidget(self.tabs, 1)
        right_layout.addWidget(self.scan_progress)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_details_box(self) -> QGroupBox:
        box = QGroupBox("Volume Details")
        self.detail_labels: dict[str, QLabel] = {}
        self.detail_full = QProgressBar()
        self.detail_full.setRange(0, 100)
        self.detail_full.setFormat("%p% full")
        self.detail_description = QPlainTextEdit()
        self.detail_description.setReadOnly(True)
        self.detail_description.setMaximumHeight(76)

        grid = QGridLayout(box)
        labels = [
            ("drive_id", "Drive ID"),
            ("name", "Name"),
            ("path", "Scan Path"),
            ("connection", "Connection"),
            ("register_status", "Status"),
            ("condition", "Condition"),
            ("connector", "Connector"),
            ("mirror", "Mirror Drive"),
            ("master", "Master Drive"),
            ("date_added", "Date Added"),
            ("earliest_content_date", "Earliest Content"),
            ("latest_content_date", "Latest Content"),
            ("retired_date", "Retired Date"),
            ("mirror_date", "Mirror Date"),
            ("capacity", "Capacity"),
            ("used", "Used"),
            ("free", "Free"),
            ("files", "Files"),
            ("folders", "Folders"),
            ("last_scan", "Last Scan"),
        ]
        for index, (key, label) in enumerate(labels):
            widget = QLabel("-")
            widget.setWordWrap(key in {"path", "master"})
            widget.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            self.detail_labels[key] = widget
            row = index // 2
            col = (index % 2) * 2
            grid.addWidget(QLabel(label), row, col)
            grid.addWidget(widget, row, col + 1)
        full_row = (len(labels) + 1) // 2
        grid.addWidget(QLabel("Full"), full_row, 0)
        grid.addWidget(self.detail_full, full_row, 1, 1, 3)
        description_row = full_row + 1
        grid.addWidget(QLabel("Description"), description_row, 0)
        grid.addWidget(self.detail_description, description_row, 1, 1, 3)
        return box

    def _build_browser_tab(self) -> QWidget:
        self.offline_label = QLabel("")
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabel("Folders")
        self.folder_tree.setIconSize(QSize(18, 18))

        self.up_button = QPushButton("UP")
        self.up_button.setEnabled(False)
        self.current_path_label = QLabel("/")
        self.current_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        path_row = QHBoxLayout()
        path_row.addWidget(self.up_button)
        path_row.addWidget(self.current_path_label, 1)

        self.file_table = QTableView()
        self.file_table.setModel(self.browser_model)
        self.configure_table_view(self.file_table)
        self.file_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.file_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.file_table,
                {1: 140, 2: 95, 3: 155, 4: 280, 5: 90},
            ),
        )

        browser_splitter = QSplitter()
        browser_splitter.addWidget(self.folder_tree)
        browser_splitter.addWidget(self.file_table)
        browser_splitter.setStretchFactor(0, 1)
        browser_splitter.setStretchFactor(1, 2)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self.offline_label)
        layout.addLayout(path_row)
        layout.addWidget(browser_splitter, 1)
        return widget

    def configure_table_view(self, table: QTableView) -> None:
        table.setSortingEnabled(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setIconSize(QSize(18, 18))
        table.setWordWrap(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(24)

        header = table.horizontalHeader()
        header.setSectionsMovable(False)
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
        if only_if_empty and table.columnWidth(stretch_column) > 0:
            return

        remaining = table.viewport().width() - sum(default_widths.values()) - 24
        table.setColumnWidth(stretch_column, max(220, remaining))
        for column, width in default_widths.items():
            table.setColumnWidth(column, width)

    def _build_search_tab(self) -> QWidget:
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search filename, extension, folder, or relative path")
        self.search_button = QPushButton("Search")
        self.open_file_button = QPushButton("Open File")
        self.reveal_file_button = QPushButton("Reveal")
        self.open_file_button.setEnabled(False)
        self.reveal_file_button.setEnabled(False)

        search_row = QHBoxLayout()
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.open_file_button)
        search_row.addWidget(self.reveal_file_button)

        self.search_table = QTableView()
        self.search_table.setModel(self.search_model)
        self.configure_table_view(self.search_table)
        self.search_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.search_table,
                {1: 80, 2: 150, 3: 280, 4: 95, 5: 155, 6: 120},
            ),
        )

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addLayout(search_row)
        layout.addWidget(self.search_table, 1)
        return widget

    def _build_log_tab(self) -> QWidget:
        self.scan_log = QPlainTextEdit()
        self.scan_log.setReadOnly(True)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self.scan_log)
        return widget

    def _connect_signals(self) -> None:
        self.welcome_new_button.clicked.connect(self.new_catalogue)
        self.welcome_open_button.clicked.connect(self.open_catalogue_from_dialog)
        self.add_button.clicked.connect(self.add_volume)
        self.volume_filter_edit.textChanged.connect(lambda _text: self.refresh_volumes())
        self.volume_table.selectionModel().selectionChanged.connect(self.on_volume_selection_changed)
        self.volume_table.customContextMenuRequested.connect(self.show_volume_context_menu)
        self.volume_table.doubleClicked.connect(self.edit_volume_index)
        self.folder_tree.itemExpanded.connect(self.load_tree_children)
        self.folder_tree.currentItemChanged.connect(self.on_folder_changed)
        self.up_button.clicked.connect(self.navigate_parent_folder)
        self.file_table.doubleClicked.connect(self.open_browser_index)
        self.file_table.customContextMenuRequested.connect(self.show_browser_context_menu)
        self.search_button.clicked.connect(self.perform_search)
        self.search_edit.returnPressed.connect(self.perform_search)
        self.search_table.selectionModel().selectionChanged.connect(self.on_search_selection_changed)
        self.search_table.doubleClicked.connect(self.open_search_location)
        self.open_file_button.clicked.connect(lambda: self.open_selected_real_item(reveal=False))
        self.reveal_file_button.clicked.connect(lambda: self.open_selected_real_item(reveal=True))

        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.setShortcut("F5")
        self.refresh_action.triggered.connect(self.refresh_volumes)
        self.addAction(self.refresh_action)
        self.catalogue_actions = [self.close_catalogue_action, self.refresh_action]
        self.catalogue_widgets = [self.add_button, self.volume_filter_edit, self.search_edit, self.search_button]

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

    def new_catalogue(self) -> None:
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
        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "Open Catalogue",
            str(self.catalogue_path.parent if self.catalogue_path else Path.home()),
            CATALOGUE_FILE_FILTER,
        )
        if not path_text:
            return
        self.open_catalogue_path(catalogue_path_with_extension(path_text))

    def open_catalogue_path(self, path: str | Path) -> None:
        path = catalogue_path_with_extension(path)
        if self.db is not None and not self.close_catalogue(show_status=False):
            return

        lock: QLockFile | None = None
        db: Database | None = None
        try:
            lock = self._acquire_catalogue_lock(path)
            db = open_catalogue(path)
        except Exception as exc:
            if db is not None:
                db.close()
            if lock is not None:
                lock.unlock()
            self._show_catalogue_error("Open Catalogue Failed", exc)
            return

        self._open_catalogue_in_window(db, path, lock)
        self.statusBar().showMessage("Catalogue opened.", 4000)

    def close_catalogue(self, show_status: bool = True) -> bool:
        if self.db is None:
            self._set_catalogue_open(False)
            return True

        if not self._stop_scan_for_catalogue_close():
            return False

        db = self.db
        lock = self.catalogue_lock
        self.db = None
        self.catalogue_path = None
        self.catalogue_lock = None
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
        lock = QLockFile(f"{path}.lock")
        if not lock.tryLock(100):
            raise CatalogueInUseError(
                "This catalogue appears to be open in another JVVV window or process."
            )
        return lock

    def _open_catalogue_in_window(self, db: Database, path: Path, lock: QLockFile) -> None:
        self.db = db
        self.catalogue_path = path
        self.catalogue_lock = lock
        self._set_catalogue_open(True)
        self.refresh_volumes()

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

        self.scan_worker.cancel()
        self.scan_progress.setFormat("Cancelling...")
        self.statusBar().showMessage("Cancelling scan...")

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
            self._clear_catalogue_views()
        self._update_window_title()

    def _clear_catalogue_views(self) -> None:
        self.current_volume_id = None
        self.current_folder_id = None
        self.volume_model.set_items([])
        self.browser_model.set_items([])
        self.search_model.set_items([])
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
        if isinstance(exc, CatalogueInUseError):
            QMessageBox.warning(self, "Catalogue In Use", message)
        elif isinstance(exc, (CatalogueError, OSError, FileNotFoundError, PermissionError)):
            QMessageBox.critical(self, title, message)
        else:
            QMessageBox.critical(self, title, message)

    def refresh_volumes(self) -> None:
        if self.db is None:
            self._clear_catalogue_views()
            return
        selected_id = self.selected_volume_id() or self.current_volume_id
        volumes = self.db.list_volumes()
        all_items = [
            VolumeItem(
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
                connected=source_path_exists(volume["source_path"]),
                percent_full=percentage_full(volume["used_bytes"], volume["capacity_bytes"]),
            )
            for volume in volumes
        ]
        filter_text = self.volume_filter_edit.text() if hasattr(self, "volume_filter_edit") else ""
        items = [item for item in all_items if volume_matches_filter(item, filter_text)]
        self.volume_model.set_items(items)

        if items:
            visible_ids = {item.id for item in self.volume_model.items}
            target_id = selected_id if selected_id in visible_ids else self.volume_model.items[0].id
            if self.select_volume(target_id):
                self.show_selected_volume(target_id)
        else:
            self.show_selected_volume(None)

    def selected_volume_id(self) -> int | None:
        item = self.volume_model.item_at(self.volume_table.currentIndex())
        return item.id if item is not None else None

    def selected_volume(self):
        if self.db is None:
            return None
        volume_id = self.selected_volume_id()
        return self.db.get_volume(volume_id) if volume_id is not None else None

    def show_volume_context_menu(self, point: QPoint) -> None:
        if self.db is None:
            return
        index = self.volume_table.indexAt(point)
        menu = QMenu(self)

        if not index.isValid():
            new_action = menu.addAction("New Volume")
            new_action.triggered.connect(self.add_volume)
            menu.exec(self.volume_table.viewport().mapToGlobal(point))
            return

        self.volume_table.selectRow(index.row())
        self.volume_table.setCurrentIndex(self.volume_model.index(index.row(), 0))
        volume = self.selected_volume()
        connected = bool(volume and source_path_exists(volume["source_path"]))
        scan_running = self.scan_worker is not None

        new_action = menu.addAction("New Volume")
        new_action.triggered.connect(self.add_volume)
        menu.addSeparator()

        edit_action = menu.addAction("Edit Volume")
        edit_action.triggered.connect(self.edit_volume)
        edit_action.setEnabled(not scan_running)

        delete_action = menu.addAction("Delete Volume")
        delete_action.triggered.connect(self.delete_volume)
        delete_action.setEnabled(not scan_running)

        menu.addSeparator()
        scan_action = menu.addAction("Scan")
        scan_action.triggered.connect(lambda: self.start_scan(remove_deleted=True, is_rescan=False))
        scan_action.setEnabled(connected and not scan_running)

        rescan_action = menu.addAction("Rescan")
        rescan_action.triggered.connect(self.start_rescan)
        rescan_action.setEnabled(connected and not scan_running)

        cancel_action = menu.addAction("Cancel Scan")
        cancel_action.triggered.connect(self.cancel_scan)
        cancel_action.setEnabled(scan_running)

        menu.exec(self.volume_table.viewport().mapToGlobal(point))

    def on_volume_selection_changed(self, selected=None, deselected=None) -> None:
        if self.db is None:
            return
        volume_id = self.selected_volume_id()
        self.show_selected_volume(volume_id)

    def show_selected_volume(self, volume_id: int | None) -> None:
        self.current_volume_id = volume_id
        volume = self.db.get_volume(volume_id) if self.db is not None and volume_id is not None else None
        self.show_volume_details(volume)
        self.load_volume_browser(volume_id)
        self.load_scan_log(volume_id)

    def show_volume_details(self, volume) -> None:
        if volume is None:
            for widget in self.detail_labels.values():
                widget.setText("-")
            self.detail_description.clear()
            self.detail_full.setValue(0)
            return

        connected = source_path_exists(volume["source_path"])
        full = percentage_full(volume["used_bytes"], volume["capacity_bytes"])
        values = {
            "drive_id": volume["drive_id"] or "-",
            "name": volume["name"],
            "path": volume["source_path"] or "-",
            "connection": "Connected" if connected else "Offline",
            "register_status": volume["register_status"],
            "condition": volume["condition"],
            "connector": volume["connector"],
            "mirror": "Yes" if volume["is_mirror"] else "No",
            "master": volume_reference(volume["master_drive_id"], volume["master_name"])
            if volume["master_volume_id"] is not None
            else "-",
            "date_added": display_db_date(volume["date_added"]),
            "earliest_content_date": display_db_date(volume["earliest_content_date"]),
            "latest_content_date": display_db_date(volume["latest_content_date"]),
            "retired_date": display_db_date(volume["retired_date"]),
            "mirror_date": display_db_date(volume["mirror_date"]),
            "capacity": format_size(volume["capacity_bytes"]),
            "used": format_size(volume["used_bytes"]),
            "free": format_size(volume["free_bytes"]),
            "files": str(volume["indexed_file_count"]),
            "folders": str(volume["indexed_folder_count"]),
            "last_scan": self._display_time(volume["last_scan_at"]),
        }
        for key, value in values.items():
            self.detail_labels[key].setText(value)
        self.detail_description.setPlainText(volume["description"] or "")
        self.detail_full.setValue(full)

    def add_volume(self) -> None:
        if self.db is None:
            return
        dialog = VolumeDialog(
            self,
            "New Volume",
            suggested_drive_id=self.db.next_drive_id(),
            master_options=self.db.list_master_volume_options(),
            existing_volumes=self.db.list_volumes(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source_path, register = dialog.values()
        if source_path and (not register["earliest_content_date"] or not register["latest_content_date"]):
            dialog.apply_content_date_guess(source_path)
            name, source_path, register = dialog.values()
        try:
            volume_id = self.db.create_volume(name, source_path, register)
            path = Path(source_path) if source_path else None
            if path is not None and path.exists():
                try:
                    capacity, used, free = get_storage_stats(path)
                    self.db.update_volume_storage(volume_id, capacity, used, free)
                except OSError:
                    pass
            self.current_volume_id = volume_id
            self.refresh_volumes()
            self.statusBar().showMessage("Volume added.", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "New Volume Failed", str(exc))

    def edit_volume(self) -> None:
        if self.db is None:
            return
        if self.scan_worker is not None:
            QMessageBox.information(self, "Scan Running", "Wait for the current scan to finish or cancel it.")
            return
        volume = self.selected_volume()
        if volume is None:
            return
        dialog = VolumeDialog(
            self,
            "Edit Volume",
            volume=volume,
            master_options=self.db.list_master_volume_options(volume["id"]),
            mirror_dependents=self.db.list_mirror_dependents(volume["id"]),
            existing_volumes=self.db.list_volumes(),
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
        self.db.delete_volume(volume["id"])
        self.current_volume_id = None
        self.refresh_volumes()
        self.statusBar().showMessage("Volume deleted.", 4000)

    def start_rescan(self) -> None:
        volume = self.selected_volume()
        if volume is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Rescan Volume")
        box.setText("How should catalogue records for deleted files be handled?")
        remove_button = box.addButton("Remove Deleted", QMessageBox.ButtonRole.AcceptRole)
        mark_button = box.addButton("Mark Missing", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == remove_button:
            self.start_scan(remove_deleted=True, is_rescan=True)
        elif clicked == mark_button:
            self.start_scan(remove_deleted=False, is_rescan=True)

    def start_scan(self, remove_deleted: bool, is_rescan: bool) -> None:
        if self.db is None:
            return
        if self.scan_worker is not None:
            QMessageBox.information(self, "Scan Running", "Wait for the current scan to finish or cancel it.")
            return
        volume = self.selected_volume()
        if volume is None:
            return
        if not source_path_exists(volume["source_path"]):
            QMessageBox.warning(self, "Volume Offline", "The source path is not currently connected.")
            return

        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFormat("Starting scan...")
        self.statusBar().showMessage("Rescanning..." if is_rescan else "Scanning...")

        self.scan_thread = QThread(self)
        self.scan_worker = ScanWorker(self.db.path, volume["id"], remove_deleted)
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.failed.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_worker.failed.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.finished.connect(self.clear_scan_worker)
        self.scan_thread.start()

    def cancel_scan(self) -> None:
        if self.scan_worker is not None:
            self.scan_worker.cancel()
            self.scan_progress.setFormat("Cancelling...")
            self.statusBar().showMessage("Cancelling scan...")

    @Slot(int, int, str)
    def on_scan_progress(self, files_seen: int, folders_seen: int, current_path: str) -> None:
        self.scan_progress.setFormat(f"{files_seen} files, {folders_seen} folders - {current_path}")

    @Slot(dict)
    def on_scan_finished(self, result: dict) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(1)
        status = result.get("status", "completed")
        self.scan_progress.setFormat(status.title())
        self.statusBar().showMessage(
            f"Scan {status}: {result.get('files_seen', 0)} files, "
            f"{result.get('folders_seen', 0)} folders, {result.get('errors_count', 0)} errors.",
            8000,
        )
        self.refresh_after_scan()

    @Slot(str)
    def on_scan_failed(self, details: str) -> None:
        self.scan_progress.setRange(0, 1)
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat("Scan failed")
        QMessageBox.critical(self, "Scan Failed", details)
        self.refresh_after_scan()

    @Slot()
    def clear_scan_worker(self) -> None:
        self.scan_worker = None
        self.scan_thread = None

    def refresh_after_scan(self) -> None:
        if self.db is None:
            return
        path = self.db.path
        self.db.close()
        try:
            self.db = open_catalogue(path)
        except Exception as exc:
            self.db = None
            if self.catalogue_lock is not None:
                self.catalogue_lock.unlock()
                self.catalogue_lock = None
            self.catalogue_path = None
            self._set_catalogue_open(False)
            self._show_catalogue_error("Catalogue Refresh Failed", exc)
            return
        self.refresh_volumes()
        self.perform_search()

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
        connected = source_path_exists(volume["source_path"])
        self.offline_label.setText(
            "" if connected else "This volume is offline. Showing the saved catalogue."
        )
        root = self.db.get_root_folder(volume_id)
        if root is None:
            self.offline_label.setText("No scan data available for this volume.")
            return
        root_item = self._folder_tree_item(root)
        self.folder_tree.addTopLevelItem(root_item)
        self.add_placeholder_if_needed(root_item)
        self.folder_tree.setCurrentItem(root_item)
        root_item.setExpanded(True)

    def clear_browser(self) -> None:
        self.offline_label.setText("")
        self.folder_tree.clear()
        self.browser_model.set_items([])
        self.current_folder_id = None
        self.current_path_label.setText("/")
        self.up_button.setEnabled(False)

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
            self.browser_model.set_items([])
            self.current_path_label.setText("/")
            self.up_button.setEnabled(False)
            return

        items: list[BrowserItem] = []
        for child in self.db.list_child_folders(volume_id, folder_id):
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
                )
            )

        for file_row in self.db.list_files(volume_id, folder_id):
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
                )
            )

        self.browser_model.set_items(items)
        self.current_path_label.setText(relative_path_for_display(folder["relative_path"]))
        self.up_button.setEnabled(folder["parent_id"] is not None)
        self.apply_table_default_columns(
            self.file_table,
            {1: 140, 2: 95, 3: 155, 4: 280, 5: 90},
            only_if_empty=True,
        )

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

    def open_catalogue_location_for_browser_item(self, item: BrowserItem) -> None:
        if item.is_folder:
            self.select_folder_path(item.relative_path)
            return

        folder_path = self.parent_catalogue_path(item.relative_path)
        self.select_folder_path(folder_path)
        self.select_browser_relative_path(item.relative_path)

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

        real_path = self.browser_real_path(item)
        real_available = real_path is not None and real_path.exists() and not item.missing

        menu = QMenu(self)
        open_action = menu.addAction("Open")
        open_action.setEnabled(item.is_folder or real_available)
        open_action.triggered.connect(lambda checked=False, item=item: self.open_browser_item(item))

        catalogue_action = menu.addAction("Open Catalogue Location")
        catalogue_action.triggered.connect(
            lambda checked=False, item=item: self.open_catalogue_location_for_browser_item(item)
        )

        reveal_action = menu.addAction("Reveal in File Manager")
        reveal_action.setEnabled(real_available)
        reveal_action.triggered.connect(
            lambda checked=False, item=item: self.open_real_browser_item(item, reveal=True)
        )

        copy_action = menu.addAction("Copy Path")
        copy_action.setEnabled(real_path is not None)
        copy_action.triggered.connect(lambda checked=False, item=item: self.copy_browser_path(item))

        menu.addSeparator()
        properties_action = menu.addAction("Properties")
        properties_action.triggered.connect(
            lambda checked=False, item_type=item.item_type, item_id=item.item_id: self.show_browser_item_properties(
                item_type,
                item_id,
            )
        )

        menu.exec(self.file_table.viewport().mapToGlobal(point))

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
        relative_path = record["relative_path"] or ""
        source_path = record["source_path"] or ""
        physical_path = self.physical_path_for_source(source_path, relative_path) if source_path else None
        volume_connected = bool(source_path and Path(source_path).exists())
        item_exists = self.current_item_exists_text(physical_path, volume_connected)

        properties = [
            ("Name", self.catalogue_item_display_name(record)),
            ("Kind", "Folder" if item_type == "folder" else "File"),
            ("Type", self.catalogue_item_type_label(record)),
            ("Volume", record["volume_name"]),
            ("Relative path", relative_path_for_display(relative_path)),
            ("Full physical path", str(physical_path) if physical_path is not None else "Unavailable"),
            ("Parent folder", self.parent_folder_display(record)),
        ]

        if item_type == "file":
            extension = (record["extension"] or "").lstrip(".")
            properties.extend(
                [
                    ("Extension", f".{extension}" if extension else "Unavailable"),
                    ("Size", display_indexed_size(record["size_bytes"])),
                ]
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
        real_path = self.browser_real_path(item)
        if real_path is None or not real_path.exists() or item.missing:
            self.statusBar().showMessage("The real item is not available because the volume is offline or changed.", 5000)
            return
        try:
            open_in_file_manager(real_path, reveal=reveal)
        except Exception as exc:
            QMessageBox.warning(self, "Open Failed", str(exc))

    def copy_selected_browser_path(self) -> None:
        item = self.selected_browser_item()
        if item is not None:
            self.copy_browser_path(item)

    def copy_browser_path(self, item: BrowserItem) -> None:
        real_path = self.browser_real_path(item)
        if real_path is None:
            return
        QApplication.clipboard().setText(str(real_path))
        self.statusBar().showMessage("Path copied.", 3000)

    def browser_real_path(self, item: BrowserItem) -> Path | None:
        if self.db is None or self.current_volume_id is None:
            return None
        volume = self.db.get_volume(self.current_volume_id)
        if volume is None:
            return None
        if not volume["source_path"]:
            return None
        return self.real_path_for(volume, item.relative_path)

    def real_path_for(self, volume, relative_path: str) -> Path:
        return self.physical_path_for_source(volume["source_path"], relative_path)

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
        if self.db is None:
            self.search_model.set_items([])
            self.on_search_selection_changed()
            return
        query = self.search_edit.text().strip()
        if not query:
            self.search_model.set_items([])
            self.on_search_selection_changed()
            return

        items = [
            SearchResultItem(
                item_type=result["item_type"],
                item_id=result["item_id"],
                name=result["name"],
                volume_id=result["volume_id"],
                volume_name=result["volume_name"],
                relative_path=result["relative_path"],
                size_bytes=result["size_bytes"],
                modified_at=result["modified_at"],
                missing=bool(result["missing"]),
                source_path=result["source_path"],
                connected=source_path_exists(result["source_path"]),
            )
            for result in self.db.search(query)
        ]
        self.search_model.set_items(items)
        self.on_search_selection_changed()
        self.statusBar().showMessage(f"{len(items)} search results.", 4000)

    def on_search_selection_changed(self, selected=None, deselected=None) -> None:
        item = self.selected_search_item()
        real_path = self.selected_search_real_path()
        enabled = item is not None and not item.missing and real_path is not None and real_path.exists()
        self.open_file_button.setEnabled(enabled)
        self.reveal_file_button.setEnabled(enabled)

    def selected_search_item(self) -> SearchResultItem | None:
        return self.search_model.item_at(self.search_table.currentIndex())

    def selected_search_real_path(self) -> Path | None:
        if self.db is None:
            return None
        item = self.selected_search_item()
        if item is None:
            return None
        volume = self.db.get_volume(item.volume_id)
        if volume is None:
            return None
        if not volume["source_path"]:
            return None
        return self.real_path_for(volume, item.relative_path)

    def open_selected_real_item(self, reveal: bool) -> None:
        item = self.selected_search_item()
        path = self.selected_search_real_path()
        if item is None or item.missing or path is None or not path.exists():
            QMessageBox.information(self, "Unavailable", "The real item is not currently available.")
            return
        try:
            open_in_file_manager(path, reveal=reveal)
        except Exception as exc:
            QMessageBox.warning(self, "Open Failed", str(exc))

    def open_search_location(self, clicked_index: QModelIndex | None = None) -> None:
        if self.db is None:
            return
        item = self.search_model.item_at(clicked_index) if clicked_index is not None else self.selected_search_item()
        if item is None:
            return
        folder_path = self.parent_catalogue_path(item.relative_path) if item.item_type == "file" else item.relative_path
        self.tabs.setCurrentWidget(self.browser_tab)
        self.select_volume(item.volume_id)
        self.select_folder_path(folder_path)
        if item.item_type == "file":
            QTimer.singleShot(
                0,
                lambda path=item.relative_path: self.select_browser_relative_path(path, focus=True),
            )

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
            lines.append(
                f"{self._display_time(row['started_at'])} - {row['status']} - "
                f"{row['files_seen']} files, {row['folders_seen']} folders, "
                f"{row['errors_count']} errors"
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
    app = QApplication(sys.argv)
    app.setApplicationName("JVVV")
    app.setOrganizationName("JVVV")
    window = MainWindow()
    window.show()
    return app.exec()
