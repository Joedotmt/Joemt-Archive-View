"""Non-modal view of the catalogue's operations queue."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QProgressBar, QPushButton,
    QTabWidget, QTableWidgetItem, QVBoxLayout,
)

from .list_ui import ListTableWidget
from .operations_queue import DONE, STATUS_TABS, STATUSES, Operation


@dataclass
class ProjectRow:
    id: str
    children: list[Operation]
    reorderable = False

    @property
    def detail(self):
        verified = sum(j.state in DONE for j in self.children)
        return f"{verified} of {len(self.children)} copies verified"

    @property
    def tab(self):
        if any(j.tab == "Pending" for j in self.children):
            return "Pending"
        return "Failed" if any(j.tab == "Failed" for j in self.children) else "Completed"


class OperationsDialog(QDialog):
    apply_requested = Signal()
    pause_requested = Signal()
    pause_job_requested = Signal(str)
    move_requested = Signal(str, int)
    remove_requested = Signal(str)
    remove_project_requested = Signal(str)
    retry_requested = Signal(str)
    add_copy_requested = Signal(str)
    preview_failures_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Queue Manager")
        self.resize(850, 380)
        self.tabs = QTabWidget()
        self.tables: dict[str, ListTableWidget] = {}
        self.jobs_by_tab: dict[str, list[Operation | ProjectRow]] = {}
        self.collapsed = set()
        self.preview_failure_job_id = None
        for tab in dict.fromkeys(STATUS_TABS.values()):
            table = self._operation_table()
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(lambda point, tab=tab: self._show_job_menu(tab, point))
            table.itemSelectionChanged.connect(self._show_selection)
            table.cellClicked.connect(lambda row, column, tab=tab: self._toggle(tab, row, column))
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
    def _operation_table() -> ListTableWidget:
        table = ListTableWidget(["Operation", "Volume", "Source project", "Status", "Progress"])
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
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
        if isinstance(job, ProjectRow):
            if job.tab == "Completed":
                menu.addAction("Add another copy…", lambda: self.add_copy_requested.emit(job.id))
            if all(child.state != "running" for child in job.children):
                menu.addAction("Remove from queue", lambda: self.remove_project_requested.emit(job.id))
            menu.addAction("Expand" if job.id in self.collapsed else "Collapse", lambda: self._toggle(tab, row, 0))
            menu.exec(table.viewport().mapToGlobal(point))
            menu.deleteLater()
            return
        if job.project_id and job.state in DONE:
            menu.addAction("Add another copy…", lambda: self.add_copy_requested.emit(job.project_id))
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
        jobs = self.jobs_by_tab[tab]
        job = self._selected_job(self.tables[tab], jobs)
        if job is not None:
            self.detail.setText(job.detail)
        else:
            self.detail.setText("No task selected." if jobs else f"No {tab.lower()} tasks.")

    def _toggle(self, tab, row, column):
        item = self.jobs_by_tab[tab][row]
        if column == 0 and isinstance(item, ProjectRow):
            self.collapsed.symmetric_difference_update({item.id})
            self.refresh(self._jobs, self.pause.isEnabled(), preview_failure_job_id=self.preview_failure_job_id)

    def refresh(self, jobs, active, *, preview_failure_job_id=None):
        self._jobs = jobs
        self.preview_failure_job_id = preview_failure_job_id
        projects = {}
        for job in jobs:
            if job.project_id:
                projects.setdefault(job.project_id, []).append(job)
        rows = {tab: [] for tab in self.tables}
        for job in jobs:
            children = projects.get(job.project_id, [])
            if len(children) < 2:
                rows[job.tab].append(job)
            elif job is children[0]:
                group = ProjectRow(job.project_id, children)
                rows[group.tab].append(group)
                if group.id not in self.collapsed:
                    rows[group.tab].extend(children)
        for tab, table in self.tables.items():
            selected = self._selected_job(table, self.jobs_by_tab[tab])
            tab_jobs = rows[tab]
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
            if job.project_id:
                table.item(row, 3).setText(message.split("…")[0])
            table.item(row, 3).setToolTip(message)
            if self.tabs.currentWidget() is table and table.currentRow() == row:
                self.detail.setText(message)
            break

    def _refresh_table(self, table, jobs, selected):
        with QSignalBlocker(table):
            select_row = table.rowCount() == 0 or selected is not None
            table.setRowCount(len(jobs))
            selected_row = 0
            groups = {row.id for row in jobs if isinstance(row, ProjectRow)}
            for row, job in enumerate(jobs):
                table.removeCellWidget(row, 4)
                if isinstance(job, ProjectRow):
                    symbol = "▸" if job.id in self.collapsed else "▾"
                    values = (f"{symbol} {Path(job.children[0].source).name}", "", job.children[0].source, job.detail)
                else:
                    status = STATUSES[job.state]
                    if job.project_id and job.state in DONE:
                        status = "Verified" if job.state == "completed" else "Verified with warnings"
                    if job.project_id in groups:
                        values = ("    Copy", job.volume_label, "", status)
                    else:
                        values = (job.kind_label, job.volume_label, job.source, status)
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(job.detail or value)
                    table.setItem(row, column, cell)
                if selected is not None and job.id == selected.id:
                    selected_row = row
            if jobs and select_row:
                table.selectRow(selected_row)
