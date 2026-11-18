"""Non-modal view of the catalogue's operations queue."""
from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QProgressBar, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .operations_queue import STATUS_TABS, STATUSES, Operation


class OperationsDialog(QDialog):
    apply_requested = Signal()
    pause_requested = Signal()
    pause_job_requested = Signal(str)
    move_requested = Signal(str, int)
    remove_requested = Signal(str)
    retry_requested = Signal(str)
    preview_failures_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Queue Manager")
        self.resize(850, 380)
        self.tabs = QTabWidget()
        self.tables: dict[str, QTableWidget] = {}
        self.jobs_by_tab: dict[str, list[Operation]] = {}
        self.preview_failure_job_id = None
        for tab in dict.fromkeys(STATUS_TABS.values()):
            table = self._operation_table()
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(lambda point, tab=tab: self._show_job_menu(tab, point))
            table.itemSelectionChanged.connect(self._show_selection)
            self.tables[tab] = table
            self.jobs_by_tab[tab] = []
            self.tabs.addTab(table, tab)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress = QLabel()
        self.progress.setWordWrap(True)
        self.apply_button = QPushButton("Apply")
        self.pause = QPushButton("Pause Queue")
        self.apply_button.clicked.connect(self.apply_requested.emit)
        self.pause.clicked.connect(self.pause_requested.emit)
        self.tabs.currentChanged.connect(self._show_selection)

        controls = QHBoxLayout()
        controls.addStretch()
        controls.addWidget(self.pause)
        controls.addWidget(self.apply_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.detail)
        layout.addWidget(self.progress)
        layout.addLayout(controls)

    @staticmethod
    def _operation_table() -> QTableWidget:
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["Operation", "Volume", "Source project", "Status", "Progress"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(4, 130)
        return table

    @staticmethod
    def _selected_job(table, jobs):
        row = table.currentRow()
        return jobs[row] if 0 <= row < len(jobs) else None

    def _show_job_menu(self, tab, point):
        table = self.tables[tab]
        row = table.rowAt(point.y())
        jobs = self.jobs_by_tab[tab]
        if not 0 <= row < len(jobs):
            return
        job = jobs[row]
        table.selectRow(row)
        menu = QMenu(self)
        if job.reorderable:
            if row > 0 and jobs[row - 1].reorderable:
                menu.addAction("Move Up", lambda: self.move_requested.emit(job.id, -1))
            if row + 1 < len(jobs) and jobs[row + 1].reorderable:
                menu.addAction("Move Down", lambda: self.move_requested.emit(job.id, 1))
        if job.state == "failed":
            menu.addAction("Retry", lambda: self.retry_requested.emit(job.id))
        if job.id == self.preview_failure_job_id:
            menu.addAction("Preview Failures", lambda: self.preview_failures_requested.emit())
        if job.state == "running":
            menu.addAction("Pause Job", lambda: self.pause_job_requested.emit(job.id))
        else:
            menu.addAction("Remove", lambda: self.remove_requested.emit(job.id))
        if not menu.isEmpty():
            menu.exec(table.viewport().mapToGlobal(point))
        menu.deleteLater()

    def _show_selection(self):
        tab = self.tabs.tabText(self.tabs.currentIndex())
        job = self._selected_job(self.tables[tab], self.jobs_by_tab[tab])
        self.detail.setText(job.detail if job is not None else f"No {tab.lower()} tasks.")

    def refresh(self, jobs, active, *, preview_failure_job_id=None):
        self.preview_failure_job_id = preview_failure_job_id
        for tab, table in self.tables.items():
            selected = self._selected_job(table, self.jobs_by_tab[tab])
            tab_jobs = [job for job in jobs if job.tab == tab]
            self.jobs_by_tab[tab] = tab_jobs
            self._refresh_table(table, tab_jobs, selected)
        self.pause.setEnabled(active)
        self._show_selection()

    def set_job_progress(self, job_id: str, message: str, done: int, total: int) -> None:
        table = self.tables["Pending"]
        for row, job in enumerate(self.jobs_by_tab["Pending"]):
            if job.id != job_id:
                continue
            bar = table.cellWidget(row, 4)
            if bar is None:
                bar = QProgressBar(table)
                table.setCellWidget(row, 4, bar)
            bar.setRange(0, 1000 if total else 0)
            if total:
                bar.setValue(min(1000, max(0, done * 1000 // total)))
            bar.setToolTip(message)
            table.item(row, 3).setToolTip(message)
            if self.tabs.currentWidget() is table and table.currentRow() == row:
                self.detail.setText(message)
            break

    @staticmethod
    def _refresh_table(table, jobs, selected):
        with QSignalBlocker(table):
            table.setRowCount(len(jobs))
            selected_row = 0
            for row, job in enumerate(jobs):
                table.removeCellWidget(row, 4)
                values = (job.kind_label, job.volume_label, job.source, STATUSES[job.state])
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(job.detail or value)
                    table.setItem(row, column, cell)
                if selected is not None and job.id == selected.id:
                    selected_row = row
            if jobs:
                table.selectRow(selected_row)
