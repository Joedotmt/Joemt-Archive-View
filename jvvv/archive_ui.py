"""Choose an archive operation and run approved copies off the UI thread."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .archive_job import ArchiveJob, ArchivePaused, check_archive_write_locations
from .database import Database
from .operations_queue import prepare_operation_previews
from .utils import ConnectedVolumeResolver


class ArchiveThread(QThread):
    progress = Signal(str, object, object)

    def __init__(self, catalogue, operation, volume_id, preview_settings, parent, *, writer_lock=None,
                 resolved_destination=None):
        super().__init__(parent)
        self.catalogue = catalogue
        self.operation = operation
        self.volume_id = volume_id
        self.preview_settings = preview_settings
        self.writer_lock = writer_lock
        self.resolved_destination = resolved_destination
        self.pause_requested = Event()
        self.result = None
        self.error = ""
        self.paused = False
        self.preview_problem = None

    def cancel(self):
        self.pause_requested.set()

    def run(self) -> None:
        try:
            source = Path(self.operation.source).resolve()
            check_archive_write_locations(source, self.catalogue)
            previews, self.preview_problem = prepare_operation_previews(self.preview_settings, source)
            db = Database(self.catalogue, initialize=False, create=False)
            try:
                job = ArchiveJob(db, preview_settings=previews,
                                 cancelled=self.pause_requested.is_set, progress=self.progress.emit,
                                 writer_lock=self.writer_lock)
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


class ArchiveDialog(QDialog):
    def __init__(self, db: Database, targets, parent=None, *, volume_id=None):
        super().__init__(parent)
        self.setWindowTitle("Archive Project")
        self.setMinimumWidth(570)
        self.source = QLineEdit()
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source, 1)
        source_layout.addWidget(browse)

        self.destination = QComboBox()
        self.destination.setPlaceholderText("Select a destination volume")
        resolver = ConnectedVolumeResolver()
        for volume, dependency in targets:
            if volume_id is not None and volume["id"] != volume_id:
                continue
            connected = resolver.resolve(volume)
            if Path(connected or volume["source_path"]).drive.casefold() == "q:":
                continue
            location = connected or "disconnected"
            note = ", after queued scan" if dependency else ""
            self.destination.addItem(
                f"{db.volume_reference(volume)} ({location}{note})",
                (volume["id"], volume["uuid"], dependency),
            )
        if volume_id is not None and self.destination.count():
            self.destination.setCurrentIndex(0)
        self.destination.setEnabled(volume_id is None and self.destination.count() > 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        if not self.destination.count():
            self.status.setText("Add a destination drive and queue its initial scan first.")

        form = QFormLayout()
        form.addRow("Source project", source_row)
        form.addRow("Destination volume", self.destination)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.start = buttons.addButton("Queue Archive", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self._accept_archive)
        buttons.rejected.connect(self.reject)
        self.destination.currentIndexChanged.connect(self._update_button)
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
        self.start.setEnabled(bool(self.source.text().strip() and self.destination.currentData() is not None))

    def _accept_archive(self):
        path = Path(self.source.text().strip())
        if not path.is_dir() or not path.resolve().name:
            self.status.setText("Choose an existing project folder inside the source drive.")
            return
        self.source.setText(str(path.resolve()))
        self.accept()
