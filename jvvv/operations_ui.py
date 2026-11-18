"""Non-modal view of the catalogue's operations queue."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .operations_queue import ACTIONABLE, DONE, STATUSES


class OperationsDialog(QDialog):
    apply_requested = Signal()
    pause_requested = Signal()
    move_requested = Signal(str, int)
    remove_requested = Signal(str)
    retry_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Operations Queue")
        self.resize(850, 380)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Operation", "Volume", "Source project", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress = QLabel()
        self.progress.setWordWrap(True)
        self.jobs = []
        self.up = QPushButton("Move Up")
        self.down = QPushButton("Move Down")
        self.remove = QPushButton("Remove")
        self.retry = QPushButton("Retry")
        self.preview_failures = QPushButton("Preview Failures")
        self.preview_failures.setToolTip("Show failures from the most recent scan.")
        self.apply = QPushButton("Apply")
        self.pause = QPushButton("Pause Queue")
        self.up.clicked.connect(lambda: self._with_selection(self.move_requested.emit, -1))
        self.down.clicked.connect(lambda: self._with_selection(self.move_requested.emit, 1))
        self.remove.clicked.connect(lambda: self._with_selection(self.remove_requested.emit))
        self.retry.clicked.connect(lambda: self._with_selection(self.retry_requested.emit))
        self.apply.clicked.connect(self.apply_requested.emit)
        self.pause.clicked.connect(self.pause_requested.emit)
        self.table.itemSelectionChanged.connect(self._show_selection)

        controls = QHBoxLayout()
        for button in (self.up, self.down, self.remove, self.retry):
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.preview_failures)
        controls.addWidget(self.pause)
        controls.addWidget(self.apply)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.detail)
        layout.addWidget(self.progress)
        layout.addLayout(controls)

    def _selected_job(self):
        row = self.table.currentRow()
        return self.jobs[row] if 0 <= row < len(self.jobs) else None

    def _with_selection(self, action, *args):
        job = self._selected_job()
        if job is not None:
            action(job.id, *args)

    def _show_selection(self):
        job = self._selected_job()
        row = self.table.currentRow()
        editable = job is not None and job.state not in (DONE | {"running"})
        self.up.setEnabled(editable and row > 0)
        self.down.setEnabled(editable and row + 1 < len(self.jobs))
        self.remove.setEnabled(job is not None and job.state != "running")
        self.retry.setEnabled(job is not None and job.state == "failed")
        self.detail.setText(job.detail if job is not None else "")

    def refresh(self, jobs, active):
        selected = self._selected_job()
        self.jobs = jobs
        self.table.blockSignals(True)
        self.table.setRowCount(len(jobs))
        previous = -1
        for row, job in enumerate(jobs):
            values = (
                "Scan Drive" if job.kind == "scan" else "Archive Project",
                job.volume_label, job.source, STATUSES[job.state],
            )
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(job.detail or value)
                self.table.setItem(row, col, cell)
            if selected is not None and job.id == selected.id:
                previous = row
        if jobs:
            self.table.selectRow(previous if previous >= 0 else 0)
        self.table.blockSignals(False)
        pending = {"queued", "paused"} | ACTIONABLE
        self.apply.setEnabled(any(job.state in pending for job in jobs))
        self.pause.setEnabled(active)
        self._show_selection()
