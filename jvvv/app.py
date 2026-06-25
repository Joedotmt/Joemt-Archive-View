from __future__ import annotations

from dataclasses import dataclass
import sys
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from PySide6.QtCore import (
    QAbstractTableModel,
    QFileInfo,
    QModelIndex,
    QObject,
    QPoint,
    QRectF,
    QSize,
    Qt,
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
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .database import Database, parse_db_time
from .scanner import VolumeScanner, get_storage_stats
from .utils import format_size, open_in_file_manager, percentage_full, relative_path_for_display


ROLE_VOLUME_ID = Qt.ItemDataRole.UserRole
ROLE_FOLDER_ID = Qt.ItemDataRole.UserRole + 1
ROLE_RELATIVE_PATH = Qt.ItemDataRole.UserRole + 2
ROLE_ITEM_TYPE = Qt.ItemDataRole.UserRole + 3
ROLE_ITEM_ID = Qt.ItemDataRole.UserRole + 4
ROLE_PERCENT_FULL = Qt.ItemDataRole.UserRole + 5


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
    size_bytes: int = 0
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
    name: str
    source_path: str
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
    size_bytes: int
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
                    lambda item: "" if item.is_folder else format_size(item.size_bytes),
                    sort_key=lambda item: item.size_bytes,
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
                TableColumn("Name", lambda item: item.name),
                TableColumn("Status", lambda item: "Connected" if item.connected else "Offline"),
                TableColumn("Full", lambda item: f"{item.percent_full}%", sort_key=lambda item: item.percent_full),
                TableColumn(
                    "Files",
                    lambda item: str(item.indexed_file_count),
                    sort_key=lambda item: item.indexed_file_count,
                    alignment=Qt.AlignmentFlag.AlignRight,
                ),
                TableColumn(
                    "Folders",
                    lambda item: str(item.indexed_folder_count),
                    sort_key=lambda item: item.indexed_folder_count,
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
                    lambda item: "" if item.is_folder else format_size(item.size_bytes),
                    sort_key=lambda item: item.size_bytes,
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


class VolumeDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "New Volume",
        name: str = "",
        source_path: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)

        self.name_edit = QLineEdit(name)
        self.path_edit = QLineEdit(source_path)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse)

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Drive or folder", path_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose Drive or Folder", self.path_edit.text())
        if directory:
            self.path_edit.setText(directory)
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(directory).name or directory)

    def values(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.path_edit.text().strip()

    def accept(self) -> None:
        name, source_path = self.values()
        if not name or not source_path:
            QMessageBox.warning(self, "Missing Details", "Enter a volume name and source path.")
            return
        super().accept()


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
        self.db = Database()
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
        
        self.setWindowTitle("Joemt Archive View")
        self.resize(1180, 760)
        self.setStatusBar(QStatusBar())

        self._build_ui()
        self._connect_signals()
        self.refresh_volumes()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.scan_worker is not None:
            answer = QMessageBox.question(
                self,
                "Scan Running",
                "A scan is still running. Cancel it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.scan_worker.cancel()
            self.statusBar().showMessage("Cancellation requested. Close the app after the scan stops.")
            event.ignore()
            return
        self.db.close()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self.volume_table = QTableView()
        self.volume_table.setModel(self.volume_model)
        self.configure_table_view(self.volume_table)
        self.volume_table.setItemDelegateForColumn(2, self.volume_full_delegate)
        self.volume_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.volume_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        QTimer.singleShot(
            0,
            lambda: self.apply_table_default_columns(
                self.volume_table,
                {1: 95, 2: 95, 3: 80, 4: 85, 5: 145},
            ),
        )

        self.add_button = QPushButton("New Volume")

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Volumes"))
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
        self.setCentralWidget(splitter)

    def _build_details_box(self) -> QGroupBox:
        box = QGroupBox("Volume Details")
        self.detail_name = QLabel("-")
        self.detail_path = QLabel("-")
        self.detail_status = QLabel("-")
        self.detail_capacity = QLabel("-")
        self.detail_used = QLabel("-")
        self.detail_free = QLabel("-")
        self.detail_files = QLabel("-")
        self.detail_folders = QLabel("-")
        self.detail_last_scan = QLabel("-")
        self.detail_full = QProgressBar()
        self.detail_full.setRange(0, 100)
        self.detail_full.setFormat("%p% full")

        grid = QGridLayout(box)
        labels = [
            ("Name", self.detail_name),
            ("Path", self.detail_path),
            ("Status", self.detail_status),
            ("Capacity", self.detail_capacity),
            ("Used", self.detail_used),
            ("Free", self.detail_free),
            ("Files", self.detail_files),
            ("Folders", self.detail_folders),
            ("Last scan", self.detail_last_scan),
        ]
        for index, (label, widget) in enumerate(labels):
            row = index // 3
            col = (index % 3) * 2
            grid.addWidget(QLabel(label), row, col)
            grid.addWidget(widget, row, col + 1)
        grid.addWidget(QLabel("Full"), 3, 0)
        grid.addWidget(self.detail_full, 3, 1, 1, 5)
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
        self.add_button.clicked.connect(self.add_volume)
        self.volume_table.selectionModel().selectionChanged.connect(self.on_volume_selection_changed)
        self.volume_table.customContextMenuRequested.connect(self.show_volume_context_menu)
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

        refresh_action = QAction("Refresh", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.refresh_volumes)
        self.addAction(refresh_action)

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

    def refresh_volumes(self) -> None:
        selected_id = self.selected_volume_id() or self.current_volume_id
        volumes = self.db.list_volumes()
        items = [
            VolumeItem(
                id=volume["id"],
                name=volume["name"],
                source_path=volume["source_path"],
                capacity_bytes=volume["capacity_bytes"],
                used_bytes=volume["used_bytes"],
                free_bytes=volume["free_bytes"],
                indexed_file_count=volume["indexed_file_count"],
                indexed_folder_count=volume["indexed_folder_count"],
                last_scan_at=volume["last_scan_at"],
                connected=Path(volume["source_path"]).exists(),
                percent_full=percentage_full(volume["used_bytes"], volume["capacity_bytes"]),
            )
            for volume in volumes
        ]
        self.volume_model.set_items(items)

        if items:
            visible_ids = {item.id for item in self.volume_model.items}
            target_id = selected_id if selected_id in visible_ids else self.volume_model.items[0].id
            if self.select_volume(target_id):
                self.show_selected_volume(target_id)
        elif not volumes:
            self.show_selected_volume(None)

    def selected_volume_id(self) -> int | None:
        item = self.volume_model.item_at(self.volume_table.currentIndex())
        return item.id if item is not None else None

    def selected_volume(self):
        volume_id = self.selected_volume_id()
        return self.db.get_volume(volume_id) if volume_id is not None else None

    def show_volume_context_menu(self, point: QPoint) -> None:
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
        connected = bool(volume and Path(volume["source_path"]).exists())
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
        volume_id = self.selected_volume_id()
        self.show_selected_volume(volume_id)

    def show_selected_volume(self, volume_id: int | None) -> None:
        self.current_volume_id = volume_id
        volume = self.db.get_volume(volume_id) if volume_id is not None else None
        self.show_volume_details(volume)
        self.load_volume_browser(volume_id)
        self.load_scan_log(volume_id)

    def show_volume_details(self, volume) -> None:
        if volume is None:
            values = ["-"] * 9
            widgets = [
                self.detail_name,
                self.detail_path,
                self.detail_status,
                self.detail_capacity,
                self.detail_used,
                self.detail_free,
                self.detail_files,
                self.detail_folders,
                self.detail_last_scan,
            ]
            for widget, value in zip(widgets, values):
                widget.setText(value)
            self.detail_full.setValue(0)
            return

        connected = Path(volume["source_path"]).exists()
        full = percentage_full(volume["used_bytes"], volume["capacity_bytes"])
        self.detail_name.setText(volume["name"])
        self.detail_path.setText(volume["source_path"])
        self.detail_status.setText("Connected" if connected else "Offline")
        self.detail_capacity.setText(format_size(volume["capacity_bytes"]))
        self.detail_used.setText(format_size(volume["used_bytes"]))
        self.detail_free.setText(format_size(volume["free_bytes"]))
        self.detail_files.setText(str(volume["indexed_file_count"]))
        self.detail_folders.setText(str(volume["indexed_folder_count"]))
        self.detail_last_scan.setText(self._display_time(volume["last_scan_at"]))
        self.detail_full.setValue(full)

    def add_volume(self) -> None:
        dialog = VolumeDialog(self, "New Volume")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source_path = dialog.values()
        try:
            volume_id = self.db.create_volume(name, source_path)
            path = Path(source_path)
            if path.exists():
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
        volume = self.selected_volume()
        if volume is None:
            return
        dialog = VolumeDialog(self, "Edit Volume", volume["name"], volume["source_path"])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source_path = dialog.values()
        try:
            self.db.update_volume(volume["id"], name, source_path)
            self.refresh_volumes()
            self.statusBar().showMessage("Volume updated.", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Edit Volume Failed", str(exc))

    def delete_volume(self) -> None:
        volume = self.selected_volume()
        if volume is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Volume",
            f"Delete catalogue volume '{volume['name']}' and all indexed records?",
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
        if self.scan_worker is not None:
            QMessageBox.information(self, "Scan Running", "Wait for the current scan to finish or cancel it.")
            return
        volume = self.selected_volume()
        if volume is None:
            return
        if not Path(volume["source_path"]).exists():
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
        self.db.close()
        self.db = Database(self.db.path)
        self.refresh_volumes()
        self.perform_search()

    def select_volume(self, volume_id: int) -> bool:
        for row, item in enumerate(self.volume_model.items):
            if item.id == volume_id:
                self.volume_table.selectRow(row)
                self.volume_table.scrollTo(self.volume_model.index(row, 0))
                return True
        return False

    def load_volume_browser(self, volume_id: int | None) -> None:
        self.clear_browser()
        if volume_id is None:
            return
        volume = self.db.get_volume(volume_id)
        if volume is None:
            return
        connected = Path(volume["source_path"]).exists()
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
        if self.current_volume_id is None or folder_id is None:
            return
        if self.db.list_child_folders(self.current_volume_id, int(folder_id)):
            placeholder = QTreeWidgetItem([""])
            placeholder.setData(0, ROLE_FOLDER_ID, -1)
            item.addChild(placeholder)

    def load_tree_children(self, item: QTreeWidgetItem) -> None:
        if self.current_volume_id is None:
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
        if current is None or self.current_volume_id is None:
            return
        folder_id = current.data(0, ROLE_FOLDER_ID)
        if folder_id is None or int(folder_id) < 0:
            return
        self.current_folder_id = int(folder_id)
        self.load_directory_items(self.current_volume_id, self.current_folder_id)

    def load_directory_items(self, volume_id: int, folder_id: int) -> None:
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
        if self.current_folder_id is None:
            return
        folder = self.db.get_folder(self.current_folder_id)
        if folder is None or folder["parent_id"] is None:
            return
        parent = self.db.get_folder(folder["parent_id"])
        if parent is None:
            return
        self.select_folder_path(parent["relative_path"])

    def show_browser_context_menu(self, point: QPoint) -> None:
        index = self.file_table.indexAt(point)
        if not index.isValid():
            return

        self.file_table.selectRow(index.row())
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

        menu.exec(self.file_table.viewport().mapToGlobal(point))

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
        if self.current_volume_id is None:
            return None
        volume = self.db.get_volume(self.current_volume_id)
        if volume is None:
            return None
        return self.real_path_for(volume, item.relative_path)

    def real_path_for(self, volume, relative_path: str) -> Path:
        path = Path(volume["source_path"])
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
                connected=Path(result["source_path"]).exists(),
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
        item = self.selected_search_item()
        if item is None:
            return None
        volume = self.db.get_volume(item.volume_id)
        if volume is None:
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
        if self.current_volume_id is None:
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
        if volume_id is None:
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


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("JVVV")
    app.setOrganizationName("JVVV")
    window = MainWindow()
    window.show()
    return app.exec()
