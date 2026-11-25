"""Choose an archive operation and run approved copies off the UI thread."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .archive_job import ArchiveJob, ArchivePaused, check_archive_write_locations
from .database import Database
from .operations_queue import operation_destination_problem, prepare_operation_previews


class ArchiveThread(QThread):
    progress = Signal(str, object, object)

    def __init__(self, catalogue, operation, volume_id, preview_settings, parent, *, writer_lock=None,
                 resolved_destination=None, destination_ready=None, session=None, shared_stream=None):
        super().__init__(parent)
        self.catalogue = catalogue
        self.operation = operation
        self.volume_id = volume_id
        self.preview_settings = preview_settings
        self.writer_lock = writer_lock
        self.resolved_destination = resolved_destination
        self.destination_ready = destination_ready
        self.session = session
        self.shared_stream = shared_stream
        self.finished_writing = Future()
        self.pause_requested = Event()
        self.result = None
        self.error = ""
        self.paused = False
        self.preview_problem = None
        self.destination_problem = ""

    def cancel(self):
        self.pause_requested.set()

    def run(self) -> None:
        volume = None
        try:
            source = Path(self.operation.source).resolve()
            check_archive_write_locations(source, self.catalogue)
            previews, self.preview_problem = prepare_operation_previews(self.preview_settings, source)
            db = Database(self.catalogue, initialize=False, create=False)
            try:
                volume = db.get_volume(self.volume_id)
                job = ArchiveJob(db, preview_settings=previews,
                                 cancelled=self.pause_requested.is_set, progress=self.progress.emit,
                                 writer_lock=self.writer_lock, destination_ready=self.destination_ready,
                                 session=self.session, shared_stream=self.shared_stream,
                                 project_id=self.operation.project_id, require_record=self.operation.require_record)
                self.result = job.run(
                    source, self.volume_id, volume_uuid=self.operation.volume_uuid,
                    job_id=None if self.operation.legacy_journal else self.operation.id,
                    resolved_destination=self.resolved_destination,
                )
            finally:
                db.close()
        except ArchivePaused as exc:
            self.error = str(exc)
            self.paused = True
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            if self.shared_stream:
                self.shared_stream.detach(self.operation.id)
        if self.error and not self.paused:
            self.destination_problem = operation_destination_problem(volume)


class ArchiveDialog(QDialog):
    def __init__(self, db: Database, targets, parent=None, *, volume_id=None, connected_path,
                 source="", verified=()):
        super().__init__(parent)
        self.setWindowTitle("Add Another Copy" if source else "Archive Project")
        self.setMinimumWidth(570)
        self.source = QLineEdit()
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source, 1)
        source_layout.addWidget(browse)
        if source:
            self.source.setText(source)
            self.source.setReadOnly(True)
            browse.hide()

        self.destination = QListWidget()
        for volume, dependency in targets:
            connected = connected_path(volume)
            if Path(connected or volume["source_path"]).drive.casefold() == "q:":
                continue
            location = connected or "disconnected"
            note = ", after queued scan" if dependency else ""
            item = QListWidgetItem(f"{db.volume_reference(volume)} ({location}{note})", self.destination)
            item.setData(Qt.ItemDataRole.UserRole, (volume["id"], volume["uuid"], dependency))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if volume_id == volume["id"] else Qt.CheckState.Unchecked)
        self.status = QLabel()
        self.status.setWordWrap(True)
        if not self.destination.count():
            self.status.setText("Choose another catalogue volume after its initial scan." if source else
                                "Add a destination drive and queue its initial scan first.")

        form = QFormLayout()
        form.addRow("Source project", source_row)
        if verified:
            form.addRow("Verified copies", QLabel(", ".join(verified)))
        form.addRow("Destination drives", self.destination)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.start = buttons.addButton("Queue Copy" if source else "Queue Archive", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self._accept_archive)
        buttons.rejected.connect(self.reject)
        self.destination.itemChanged.connect(self._update_button)
        self.source.textChanged.connect(self._update_button)
        self._update_button()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("JVVV copies and verifies the project when you apply the queue."))
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

    def _browse(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose Source Project", self.source.text())
        if selected:
            self.source.setText(selected)

    def _update_button(self):
        self.start.setEnabled(bool(self.source.text().strip() and self.selected_targets()))

    def selected_targets(self):
        return [item.data(Qt.ItemDataRole.UserRole) for index in range(self.destination.count())
                if (item := self.destination.item(index)).checkState() == Qt.CheckState.Checked]

    def _accept_archive(self):
        path = Path(self.source.text().strip())
        if not path.is_dir() or not path.resolve().name:
            self.status.setText("Choose an existing project folder inside the source drive.")
            return
        self.source.setText(str(path.resolve()))
        self.accept()
