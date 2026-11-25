"""The catalogue's in-browser backup results and drill-down."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QSplitter,
    QVBoxLayout, QWidget,
)

from .backup_analysis import (
    DETAIL_PAGE_SIZE, AnalysisState, BackupAnalysisEngine, BackupDrive, BackupLocation,
    ItemBackupStatus, VolumeBackupSummary,
)
from .database import Database, parse_db_time
from .list_ui import ListTableWidget

BACKUP_COLUMN_HEADER_TOOLTIP = "Comparison of saved file hashes. Open Backups to see matching drives and paths."
BACKUP_FILTER_OPTIONS = (
    ("All items", "all"),
    ("Full matches", "full_match"),
    ("Has matches", "matches"),
    ("Needs attention", "attention"),
)


def display_db_time(value: str | None) -> str:
    parsed = parse_db_time(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M") if parsed is not None else "-"


@dataclass(frozen=True)
class BackupDisplay:
    state: str
    text: str
    tooltip: str
    sort_rank: int
    coverage_fraction: float = 0.0
    reason: str = ""


NOT_ANALYSED_BACKUP_DISPLAY = BackupDisplay(
    "unknown", "Not compared", BACKUP_COLUMN_HEADER_TOOLTIP, 2, reason="not_analyzed",
)


def item_backup_display(
    item: ItemBackupStatus | None, references: dict[int, str] | None = None,
) -> BackupDisplay:
    if item is None:
        return NOT_ANALYSED_BACKUP_DISPLAY
    kind = item.status
    state, text, rank = {
        "not_analyzed": ("unknown", "Not compared", 2),
        "outdated": ("unknown", "Outdated", 2),
        "unavailable": ("unknown", "Unavailable", 2),
        "no_files": ("unknown", "No files to compare", 2),
        "needs_rescan": ("unknown", "Needs rescan", 2),
        "scan_incomplete": ("unknown", "Scan incomplete", 2),
        "no_match": ("none", "No matching hashes", 0),
        "full_match": ("verified", "Full match", 4),
        "hash_match": (
            "verified", f"Hash match on {item.other_drive_count:,} "
                        f"{'drive' if item.other_drive_count == 1 else 'drives'}", 4,
        ),
        "all_contents": ("strong", f"{item.matched_files:,}/{item.total_files:,} file contents found", 3),
        "partial": ("possible", f"{item.matched_files:,}/{item.total_files:,} file contents found", 1),
    }[kind]
    drive = (references or {}).get(item.best_target_volume_id)
    if drive:
        text += f" · {drive}"
    if item.scan_incomplete and kind in {"partial", "no_match"}:
        text += " · scan incomplete"
    fraction = item.matched_files / item.total_files if item.total_files else 0
    return BackupDisplay(state, text, BACKUP_COLUMN_HEADER_TOOLTIP, rank, fraction, kind)


def volume_backup_display(
    result: VolumeBackupSummary | None, references: dict[int, str] | None = None,
    state: AnalysisState | None = None,
) -> BackupDisplay:
    if result is not None:
        return item_backup_display(result.item, references)
    if state is None or state.status == "not_analyzed":
        return NOT_ANALYSED_BACKUP_DISPLAY
    status = state.status if state.is_stale else "needs_rescan"
    return item_backup_display(ItemBackupStatus("folder", 0, status))


def backup_filter_matches(display: BackupDisplay, filter_key: str) -> bool:
    if filter_key == "full_match":
        return display.reason == "full_match"
    if filter_key == "matches":
        return display.coverage_fraction > 0
    if filter_key == "attention":
        return display.reason in {"partial", "no_match", "needs_rescan", "scan_incomplete", "outdated"}
    return True


@dataclass(frozen=True)
class BackupSource:
    item_type: str
    item_id: int | None
    volume_id: int
    name: str
    path: str
    drive: str
    missing: bool = False


@dataclass(frozen=True)
class _Overview:
    state: AnalysisState
    item: ItemBackupStatus | None
    drives: list[BackupDrive]
    scanned_at: str | None


class _QuerySignals(QObject):
    done = Signal(int, str, object, str)


class _Query(QRunnable):
    def __init__(self, sequence: int, kind: str, path: Path, read: Callable) -> None:
        super().__init__()
        self.sequence = sequence
        self.kind = kind
        self.path = path
        self.read = read
        self.signals = _QuerySignals()

    def run(self) -> None:
        db = None
        data = None
        error = ""
        try:
            db = Database(self.path, initialize=False, create=False, read_only=True)
            db.connection.execute("BEGIN")
            data = self.read(BackupAnalysisEngine(db))
        except Exception as exc:
            error = str(exc)
        finally:
            if db is not None:
                db.close()
        self.signals.done.emit(self.sequence, self.kind, data, error)


def _read_overview(engine: BackupAnalysisEngine, source: BackupSource | None) -> _Overview:
    state = engine.state()
    if source is None:
        return _Overview(state, None, [], None)
    scanned_at = engine.scan_date(source.volume_id)
    if source.missing:
        item = ItemBackupStatus(source.item_type, source.item_id or 0, "unavailable")
    elif source.item_id is None:
        item = ItemBackupStatus(source.item_type, 0, "needs_rescan")
    else:
        item = engine.item_statuses(source.item_type, (source.item_id,))[source.item_id]
    drives = (
        engine.drives_for(item, source.volume_id, state.active_run_id)
        if state.status == "completed" and state.active_run_id is not None else []
    )
    return _Overview(state, item, drives, scanned_at)


def _label(text: str = "", *, heading: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    if heading:
        font = label.font()
        font.setBold(True)
        label.setFont(font)
    return label


def _table(headers: list[str]) -> ListTableWidget:
    table = ListTableWidget(headers)
    table.setMinimumWidth(0)
    table.setAccessibleName(" / ".join(headers))
    return table


def _size_rows(table: ListTableWidget, maximum_rows: int) -> None:
    rows = max(1, min(table.rowCount(), maximum_rows))
    content = table.horizontalHeader().sizeHint().height() + rows * table.verticalHeader().defaultSectionSize()
    table.setFixedHeight(content + table.horizontalScrollBar().sizeHint().height() + 2 * table.frameWidth())


def _row_reference(table: ListTableWidget):
    item = table.item(table.currentRow(), 0)
    return item.data(Qt.ItemDataRole.UserRole) if item is not None and item.isSelected() else None


class _DetailList(QWidget):
    open_requested = Signal(object)
    page_requested = Signal(int)
    location_changed = Signal(object)

    def __init__(self, headers: list[str]) -> None:
        super().__init__()
        self.table = _table(headers)
        self.path_label = _label()
        self.previous = QPushButton("Previous")
        self.next = QPushButton("Next")
        self.open_button = QPushButton("View in catalogue")
        self.page = _label()
        self.offset = 0
        self.open_button.clicked.connect(self._open)
        self.table.cellDoubleClicked.connect(self._open)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.previous.clicked.connect(lambda: self.page_requested.emit(max(0, self.offset - DETAIL_PAGE_SIZE)))
        self.next.clicked.connect(lambda: self.page_requested.emit(self.offset + DETAIL_PAGE_SIZE))

        actions = QHBoxLayout()
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        actions.addWidget(self.previous)
        actions.addWidget(self.page)
        actions.addWidget(self.next)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addWidget(self.path_label)
        layout.addLayout(actions)
        self.clear()

    def clear(self) -> None:
        self.set_rows([], 0)

    def set_rows(self, rows: list[tuple[list[str], BackupLocation | None]], offset: int) -> None:
        self.offset = offset
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
            for values, location in rows[:DETAIL_PAGE_SIZE]:
                self.table.append_text_row(values, user_data=location)
            if self.table.rowCount():
                self.table.selectRow(0)
        _size_rows(self.table, 5)
        self.previous.setEnabled(offset > 0)
        self.next.setEnabled(len(rows) > DETAIL_PAGE_SIZE)
        self.page.setText(f"Page {offset // DETAIL_PAGE_SIZE + 1}" if rows or offset else "")
        self._selection_changed()

    def _selection_changed(self) -> None:
        location = _row_reference(self.table)
        self.open_button.setEnabled(location is not None)
        source_cell = self.table.item(self.table.currentRow(), 0)
        text = source_cell.text() if source_cell and source_cell.isSelected() else ""
        if self.table.columnCount() > 1 and location:
            text = f"{text}\n{location.relative_path or '/'}"
        self.path_label.setText(text)
        self.location_changed.emit(location)

    def _open(self, *_args) -> None:
        location = _row_reference(self.table)
        if location is not None:
            self.open_requested.emit(location)


class BackupsPanel(QWidget):
    analyse_requested = Signal()
    cancel_requested = Signal()
    location_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("backupsPanel")
        self.setMinimumWidth(0)
        self.source: BackupSource | None = None
        self.path: Path | None = None
        self.run_id: int | None = None
        self._request = 0
        self._can_compare = False
        self._running = False
        self._loading_locations = False
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.setInterval(60)
        self._summary_timer.timeout.connect(self._load_overview)

        self.title = _label("Backups", heading=True)
        self.name = _label(heading=True)
        self.source_path = _label()
        self.status = _label("Select a file, folder, or drive.", heading=True)
        self.note = _label()
        self.compare_button = QPushButton("Compare saved hashes")
        self.cancel_button = QPushButton("Cancel")
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Backup comparison progress")
        self.compare_button.clicked.connect(self.analyse_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)

        self.drives = _table(["Drive", "Result", "Coverage", "Saved scan"])
        for column, width in enumerate((120, 118, 78)):
            self.drives.setColumnWidth(column, width)
        self.drives.itemSelectionChanged.connect(self._drive_changed)

        self.location_heading = _label(heading=True)
        self.locations = _DetailList(["Catalogue path"])
        self.locations.page_requested.connect(self._load_locations)
        self.locations.location_changed.connect(self._exact_selected)
        self.locations.open_requested.connect(self.location_requested)

        self.files_heading = _label("Files on selected drive", heading=True)
        self.file_filter = QComboBox()
        self.file_filter.setAccessibleName("Filter files by backup result")
        for label, key in (("All files", "all"), ("No match", "no_match"), ("Needs rescan", "needs_rescan")):
            self.file_filter.addItem(label, key)
        self.file_filter.currentIndexChanged.connect(lambda: self._load_files(0))
        self.files = _DetailList(["Source file", "Result", "Matching path"])
        self.files.table.setColumnWidth(0, 130)
        self.files.table.setColumnWidth(1, 100)
        self.files.page_requested.connect(self._load_files)
        self.files.open_requested.connect(self.location_requested)
        self.detail_status = _label()

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 6, 0)
        for widget in (self.name, self.source_path, self.status, self.note, self.drives,
                       self.detail_status, self.location_heading, self.locations,
                       self.files_heading, self.file_filter, self.files):
            body_layout.addWidget(widget)
        body_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(body)
        buttons = QHBoxLayout()
        buttons.addWidget(self.compare_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 2, 0)
        layout.addWidget(self.title)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        layout.addWidget(self.scroll, 1)
        self._clear_results()
        self.set_analysis_running(False)

    def set_source(
        self, path: Path | None, source: BackupSource | None, *, can_compare: bool,
        refresh: bool = False,
    ) -> None:
        self._can_compare = can_compare
        self.compare_button.setEnabled(can_compare and not self._running)
        if not refresh and (path, source) == (self.path, self.source):
            return
        self.path, self.source = path, source
        self._request += 1
        self.run_id = None
        self._summary_timer.stop()
        self._clear_results()
        self.scroll.verticalScrollBar().setValue(0)
        self.name.setText(source.name if source else "")
        description = ""
        if source is not None:
            description = source.path or "/"
            if source.name != source.drive:
                description = f"{source.drive} · {description}"
        self.source_path.setText(description)
        self.status.setText("Loading saved comparison…" if path else "Open a catalogue to see backups.")
        self.note.clear()
        if path is not None:
            self._summary_timer.start()

    def set_analysis_running(self, running: bool) -> None:
        self._running = running
        self.progress.setVisible(running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)
        self.compare_button.setEnabled(self._can_compare and not running)
        if running:
            self.set_analysis_progress(0, 0, "Comparing saved hashes")

    def set_analysis_progress(self, completed: int, total: int, message: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(completed)
        self.progress.setFormat(f"{message} · %p%" if total else message)

    def _clear_results(self) -> None:
        with QSignalBlocker(self.drives):
            self.drives.setRowCount(0)
        self.drives.hide()
        self._clear_details()

    def _clear_details(self) -> None:
        self._loading_locations = True
        self.locations.clear()
        self.files.clear()
        self.locations.setEnabled(True)
        self.files.setEnabled(True)
        self._loading_locations = False
        for widget in (self.location_heading, self.locations, self.files_heading,
                       self.file_filter, self.files, self.detail_status):
            widget.hide()

    def _submit(self, kind: str, read: Callable) -> None:
        if self.path is None:
            return
        self._request += 1
        job = _Query(self._request, kind, self.path, read)
        job.signals.done.connect(self._show_result, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(job)

    def _load_overview(self) -> None:
        source = self.source
        self._submit("overview", lambda engine: _read_overview(engine, source))

    @Slot(int, str, object, str)
    def _show_result(self, sequence: int, kind: str, data, error: str) -> None:
        if sequence != self._request:
            return
        if data is None and not error:
            self.set_source(self.path, self.source, can_compare=self._can_compare, refresh=True)
            return
        if error:
            self.detail_status.setText(error)
            self.detail_status.show()
            if kind == "overview":
                self.status.setText("Comparison unavailable")
            return
        if kind == "overview":
            self._show_overview(data)
        elif kind == "locations":
            locations, offset = data
            self.locations.setEnabled(True)
            self._loading_locations = True
            self.locations.set_rows([([location.relative_path or "/"], location) for location in locations], offset)
            self._loading_locations = False
            self.detail_status.setText("Select a path to view it in the catalogue." if locations else "No matching paths on this drive.")
            if self.source is not None and self.source.item_type == "folder":
                self._load_files(0)
        else:
            files, offset = data
            self.files.setEnabled(True)
            labels = {"found": "Hash match", "needs_rescan": "Needs rescan", "no_match": "No match"}
            self.files.set_rows(
                [([row.source_path, labels[row.status], row.target.relative_path if row.target else ""], row.target)
                 for row in files],
                offset,
            )
            self.detail_status.setText("" if files else "No files in this view.")

    def _show_overview(self, overview: _Overview) -> None:
        state, item, drives = overview.state, overview.item, overview.drives
        self.run_id = state.active_run_id if state.status == "completed" else None
        notes = []
        dates = []
        if self.source is not None and overview.scanned_at:
            dates.append(f"Scan {display_db_time(overview.scanned_at)}")
        if state.status == "outdated":
            self.status.setText("Outdated")
            notes.append(f"{state.stale_reason} Choose Compare saved hashes.")
        elif state.status == "not_analyzed":
            self.status.setText("Not compared")
            notes.append("Choose Compare saved hashes to find copies in the catalogue.")
        elif item is None:
            self.status.setText("Select a file, folder, or drive.")
        else:
            references = {drive.volume_id: drive.name for drive in drives}
            self.status.setText(item_backup_display(item, references).text)
            dates.append(f"Compared {display_db_time(state.analysed_at)}")
            if item.status == "full_match":
                notes.append("File hashes, paths, and subfolders match.")
            elif item.status == "all_contents":
                notes.append("All file contents were found individually on this drive.")
            if item.missing_hashes:
                if item.missing_hashes == 1:
                    notes.append("1 source file needs a saved hash.")
                else:
                    notes.append(f"{item.missing_hashes:,} source files need a saved hash.")
            if item.scan_incomplete:
                notes.append("The source listing is incomplete. Counts cover its recorded files.")
            if not drives and item.total_files:
                notes.append("Add and scan another drive to look for copies.")
        if dates:
            notes.insert(0, " · ".join(dates))
        self.note.setText("\n".join(notes))
        self.drives.setVisible(bool(drives))
        with QSignalBlocker(self.drives):
            self.drives.setRowCount(0)
            for drive in drives:
                self._append_drive(drive)
            if drives:
                self.drives.selectRow(0)
        _size_rows(self.drives, 5)
        self._drive_changed()

    def _append_drive(self, drive: BackupDrive) -> None:
        if drive.full_match:
            result = "Full match"
        elif drive.scanned_at is None:
            result = "Not scanned"
        elif drive.scan_incomplete:
            result = "Scan incomplete"
        elif drive.matched_files:
            result = "Hash matches"
        else:
            result = "No matching hashes"
        if drive.missing_hashes and not drive.full_match:
            result += " · hash gaps"
        details = [drive.name, result, "", display_db_time(drive.scanned_at) if drive.scanned_at else "—"]
        self.drives.append_text_row(details, user_data=drive)
        if drive.total_files and drive.scanned_at:
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(drive.matched_files * 1000 // drive.total_files)
            bar.setFormat(f"{drive.matched_files:,}/{drive.total_files:,}")
            bar.setAccessibleName(f"{drive.name} file contents found")
            self.drives.setCellWidget(self.drives.rowCount() - 1, 2, bar)

    def _drive_changed(self) -> None:
        self._request += 1
        self._clear_details()
        drive = _row_reference(self.drives)
        if self.source is None or drive is None or self.source.item_id is None:
            return
        if drive.full_match or self.source.item_type == "file":
            self.location_heading.setText("Exact folders" if drive.full_match else "Matching file locations")
            self.location_heading.show()
            self.locations.show()
            self._load_locations(0)
        else:
            self._load_files(0)

    def _load_locations(self, offset: int) -> None:
        source, drive, run_id = self.source, _row_reference(self.drives), self.run_id
        if source is None or source.item_id is None or drive is None or run_id is None:
            return
        self.detail_status.setText("Loading matching paths…")
        self.detail_status.show()
        self.locations.setEnabled(False)
        self.files.setEnabled(False)

        def read(engine: BackupAnalysisEngine):
            rows = engine.locations(source.item_type, source.item_id, drive.volume_id, run_id, offset)
            state = engine.state()
            return (rows, offset) if state.status == "completed" and state.active_run_id == run_id else None

        self._submit("locations", read)

    def _load_files(self, offset: int) -> None:
        source, drive, run_id = self.source, _row_reference(self.drives), self.run_id
        if (source is None or source.item_type != "folder" or source.item_id is None
                or drive is None or run_id is None):
            return
        exact = _row_reference(self.locations.table) if drive.full_match else None
        file_filter = self.file_filter.currentData()
        self.files_heading.show()
        self.file_filter.show()
        self.files.show()
        self.files.setEnabled(False)
        self.detail_status.setText("Loading file matches…")
        self.detail_status.show()

        def read(engine: BackupAnalysisEngine):
            files = engine.folder_files(
                source.item_id, drive.volume_id, run_id, file_filter=file_filter, offset=offset,
                exact_folder_id=exact.item_id if exact is not None else None,
            )
            state = engine.state()
            return (files, offset) if state.status == "completed" and state.active_run_id == run_id else None

        self._submit("files", read)

    def _exact_selected(self, _location) -> None:
        if not self._loading_locations:
            self._load_files(0)


class BackupsSplitter(QSplitter):
    """Keep the Browser and the backup panel reachable on narrow windows."""

    def reset_split(self) -> None:
        if self.orientation() == Qt.Orientation.Vertical:
            span, panel_share = self.height(), 0.60
        else:
            span, panel_share = self.width(), 0.50
        panel_size = int(span * panel_share)
        self.setSizes([max(1, span - panel_size), max(1, panel_size)])

    def resizeEvent(self, event) -> None:
        orientation = Qt.Orientation.Vertical if event.size().width() < 835 else Qt.Orientation.Horizontal
        if self.orientation() != orientation:
            self.setOrientation(orientation)
            self.reset_split()
        super().resizeEvent(event)
