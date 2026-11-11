"""Qt widgets and background workers for the offline-preview feature.

Covers the Settings section (spec §1, §2, §3, §34, §35, §37, §42), the
end-of-scan failure list (spec §15), and the Preview Manager (spec §22)
together with the workers the main window runs on ``QThread``s.

Nothing here displays or plays media: previews are always opened externally,
so this module never imports ``PySide6.QtMultimedia``.  Workers never touch
widgets; they only emit signals.
"""

from __future__ import annotations

from .content_hash import HASH_LABELS

import os
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .database import Database
from .media_metadata import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .preview_cache import (
    STAGE_CONFIGURATION,
    STAGE_PREVIEW_ROOT,
    PreviewCache,
    PreviewCancelled,
    PreviewEntry,
    PreviewError,
    PreviewFailure,
    PreviewStoreStatistics,
)
from .preview_config import (
    BACKUP_POLICY_TEXT,
    DEFAULT_VIDEO_PRESET,
    IMAGE_JPEG_QUALITY_RANGE,
    IMAGE_MAX_DIMENSION_RANGE,
    ROOT_CHANGE_WARNING_TEXT,
    STORAGE_TRADEOFF_TEXT,
    VIDEO_CRF_RANGE,
    VIDEO_FPS_RANGE,
    VIDEO_MAX_HEIGHT_RANGE,
    VIDEO_PRESET_DESCRIPTIONS,
    VIDEO_PRESETS,
    ImagePreviewProfile,
    PreviewConfigError,
    PreviewSettings,
    VideoPreviewProfile,
)
from .preview_service import (
    PreviewStatistics,
    PreviewValidationReport,
    ValidationStep,
    validate_preview_configuration,
)
from .utils import format_size


Validator = Callable[..., PreviewValidationReport]
ShowError = Callable[[QWidget, str, str], None]
Confirm = Callable[[QWidget, str, str], bool]
DirectoryChooser = Callable[[QWidget, str, str], str]
FileChooser = Callable[[QWidget, str, str], str]

# The unreferenced list is capped so a multi-terabyte store can never fill
# the UI process with table rows; the total is still reported (spec §18).
MAX_UNREFERENCED_ROWS = 20000
WORKER_PROGRESS_INTERVAL = 1000
DELETE_PROGRESS_INTERVAL = 100

FAILURE_COLUMNS: tuple[str, ...] = ("#", "Path", "Type", "Profile", "Stage", "Error", "Detail")
UNREFERENCED_COLUMNS: tuple[str, ...] = ("", "Type", "Profile", "Content hash", "Size", "Path")

UNREFERENCED_HEADING_TEXT = "Not referenced by this catalogue"
STATUS_ENABLED_TEXT = "Previews enabled — configuration verified."
STATUS_DISABLED_TEXT = "Previews disabled."
ENABLE_ERROR_TITLE = "Previews"


def _media_kind_label(media_kind: str) -> str:
    text = (media_kind or "").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def _describe_exception(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _default_show_error(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def _default_confirm(parent: QWidget, title: str, text: str) -> bool:
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def _default_directory_chooser(parent: QWidget, title: str, start: str) -> str:
    return QFileDialog.getExistingDirectory(parent, title, start)


def _default_file_chooser(parent: QWidget, title: str, start: str) -> str:
    filters = "Executables (*.exe);;All files (*)" if os.name == "nt" else "All files (*)"
    path, _selected_filter = QFileDialog.getOpenFileName(parent, title, start, filters)
    return path


def _copy_to_clipboard(text: str) -> None:
    application = QGuiApplication.instance()
    if application is None:
        return
    try:
        QGuiApplication.clipboard().setText(text)
    except Exception:  # pragma: no cover - clipboard is a convenience only
        pass


def _readonly_item(text: str, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def _muted_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("mutedLabel")
    label.setWordWrap(True)
    return label


def _normalized_path_key(path: str | os.PathLike[str]) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError, OSError):
        return str(path)


def preview_summary_text(statistics: PreviewStatistics, root: str | None) -> str:
    """The end-of-scan summary block (spec §15)."""

    return statistics.summary_text(root)


# ---------------------------------------------------------------------------
# Settings section
# ---------------------------------------------------------------------------


class OfflinePreviewSettingsWidget(QGroupBox):
    """Preview configuration controls shared by the preview manager and tests.

    Ticking the checkbox runs the full validation immediately; on failure the
    box unticks itself and the exact failure is shown.  ``validated`` fires
    only after a successful enable-validation so the caller can persist the
    configuration straight away (spec §2).
    """

    validated = Signal(object)
    settings_changed = Signal()

    def __init__(
        self,
        settings: PreviewSettings,
        *,
        validator: Validator = validate_preview_configuration,
        show_error: ShowError | None = None,
        parent: QWidget | None = None,
        show_ffmpeg: bool = True,
    ) -> None:
        super().__init__("Previews", parent)
        self._validator: Validator = validator
        self._show_error: ShowError = show_error or _default_show_error
        self._last_known_good: PreviewSettings | None = settings if settings.enabled else None
        self.directory_chooser: DirectoryChooser = _default_directory_chooser
        self.file_chooser: FileChooser = _default_file_chooser
        self._populating = False
        self._build_ui()
        self.storage_form.setRowVisible(1, show_ffmpeg)
        self._connect_signals()
        self.set_settings(settings)

    # -- construction ---------------------------------------------------------
    def _build_ui(self) -> None:
        self.enable_check = QCheckBox("Generate previews while scanning")
        self.enable_check.setToolTip(
            "Ticking this tests the preview directory and encoders. "
            "Previews stay disabled if any test fails."
        )
        self.status_label = _muted_label(STATUS_DISABLED_TEXT)

        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("Choose a directory for generated previews")
        self.root_edit.setToolTip(
            "Any local, removable, or network location. Previews are stored under "
            "images/<profile>/ and videos/<profile>/ inside this directory."
        )
        self.browse_button = QPushButton("Browse...")
        root_row = QHBoxLayout()
        self.root_row = root_row
        root_row.setContentsMargins(0, 0, 0, 0)
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(self.browse_button)

        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_edit.setPlaceholderText("Leave empty to search PATH for ffmpeg")
        self.ffmpeg_edit.setToolTip(
            "Optional explicit ffmpeg executable. When empty, JVVV searches PATH."
        )
        self.ffmpeg_browse_button = QPushButton("Browse...")
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.setContentsMargins(0, 0, 0, 0)
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(self.ffmpeg_browse_button)

        storage_form = QFormLayout()
        self.storage_form = storage_form
        storage_form.addRow("Preview storage directory:", root_row)
        storage_form.addRow("FFmpeg executable (optional):", ffmpeg_row)

        image_group = QGroupBox("Images")
        self.image_max_dimension_spin = QSpinBox()
        self.image_max_dimension_spin.setRange(*IMAGE_MAX_DIMENSION_RANGE)
        self.image_max_dimension_spin.setSuffix(" px")
        self.image_max_dimension_spin.setToolTip(
            "The larger side of an image preview never exceeds this value; smaller "
            "images are not enlarged."
        )
        self.image_quality_spin = QSpinBox()
        self.image_quality_spin.setRange(*IMAGE_JPEG_QUALITY_RANGE)
        self.image_quality_spin.setToolTip("JPEG quality of image previews.")
        image_form = QFormLayout(image_group)
        image_form.addRow("Maximum resolution:", self.image_max_dimension_spin)

        video_group = QGroupBox("Videos")
        self.video_fps_spin = QDoubleSpinBox()
        self.video_fps_spin.setRange(*VIDEO_FPS_RANGE)
        # Three decimals match preview_config.format_fps, so a persisted value
        # such as 0.333 survives a round trip through the dialog unchanged.
        self.video_fps_spin.setDecimals(3)
        self.video_fps_spin.setSingleStep(0.5)
        self.video_fps_spin.setToolTip(
            "Frames per second of the video preview. The original duration is kept."
        )
        self.video_max_height_spin = QSpinBox()
        self.video_max_height_spin.setRange(*VIDEO_MAX_HEIGHT_RANGE)
        self.video_max_height_spin.setSuffix(" px")
        self.video_max_height_spin.setToolTip(
            "Video previews are scaled down to this height at most; they are never upscaled."
        )
        self.video_crf_spin = QSpinBox()
        self.video_crf_spin.setRange(*VIDEO_CRF_RANGE)
        self.video_crf_spin.setToolTip("H.264 constant rate factor: lower values use more storage.")
        self.video_preset_combo = QComboBox()
        for preset in VIDEO_PRESETS:
            self.video_preset_combo.addItem(
                f"{preset} — {VIDEO_PRESET_DESCRIPTIONS[preset]}", preset
            )
        self.video_preset_combo.setToolTip("libx264 encoder preset: speed versus file size.")
        video_form = QFormLayout(video_group)
        video_form.addRow("Frames per second:", self.video_fps_spin)
        video_form.addRow("Maximum resolution (height):", self.video_max_height_spin)

        self.image_profile_label = QLabel()
        self.image_profile_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.video_profile_label = QLabel()
        self.video_profile_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        profile_note = _muted_label(
            "Previews are stored per profile. Changing any image or video setting "
            "creates a new profile; previews of other profiles are never overwritten."
        )

        self.test_button = QPushButton("Test Preview Configuration")
        self.test_button.setToolTip(
            "Run the same checks as enabling previews, without changing the setting."
        )
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self.test_button)
        test_row.addStretch(1)

        self.storage_note = _muted_label(
            f"{STORAGE_TRADEOFF_TEXT}\n\n{ROOT_CHANGE_WARNING_TEXT}\n\n{BACKUP_POLICY_TEXT}"
        )

        self.advanced_toggle = QPushButton("Show Advanced")
        self.advanced_toggle.setCheckable(True)
        self.advanced_widget = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_widget)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_form = QFormLayout()
        advanced_form.addRow("Image JPEG quality:", self.image_quality_spin)
        advanced_form.addRow("Video quality (CRF):", self.video_crf_spin)
        advanced_form.addRow("Video encoder preset:", self.video_preset_combo)
        advanced_layout.addLayout(advanced_form)
        advanced_layout.addWidget(self.image_profile_label)
        advanced_layout.addWidget(self.video_profile_label)
        advanced_layout.addWidget(profile_note)
        advanced_layout.addWidget(self.storage_note)
        self.advanced_widget.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced_widget.setVisible)
        self.advanced_toggle.toggled.connect(
            lambda checked: self.advanced_toggle.setText("Hide Advanced" if checked else "Show Advanced")
        )

        layout = QVBoxLayout(self)
        layout.addWidget(self.enable_check)
        layout.addWidget(self.status_label)
        layout.addLayout(storage_form)
        layout.addWidget(image_group)
        layout.addWidget(video_group)
        layout.addLayout(test_row)
        layout.addWidget(self.advanced_toggle)
        layout.addWidget(self.advanced_widget)

    def _connect_signals(self) -> None:
        self.enable_check.toggled.connect(self.on_enable_toggled)
        self.browse_button.clicked.connect(self.browse_root)
        self.ffmpeg_browse_button.clicked.connect(self.browse_ffmpeg)
        self.test_button.clicked.connect(self.on_test_clicked)
        self.root_edit.textChanged.connect(self._on_control_changed)
        self.ffmpeg_edit.textChanged.connect(self._on_control_changed)
        self.image_max_dimension_spin.valueChanged.connect(self._on_control_changed)
        self.image_quality_spin.valueChanged.connect(self._on_control_changed)
        self.video_fps_spin.valueChanged.connect(self._on_control_changed)
        self.video_max_height_spin.valueChanged.connect(self._on_control_changed)
        self.video_crf_spin.valueChanged.connect(self._on_control_changed)
        self.video_preset_combo.currentIndexChanged.connect(self._on_control_changed)

    def _value_controls(self) -> tuple[QWidget, ...]:
        return (
            self.enable_check,
            self.root_edit,
            self.ffmpeg_edit,
            self.image_max_dimension_spin,
            self.image_quality_spin,
            self.video_fps_spin,
            self.video_max_height_spin,
            self.video_crf_spin,
            self.video_preset_combo,
        )

    # -- state ---------------------------------------------------------------
    @property
    def last_known_good(self) -> PreviewSettings | None:
        """The last configuration proven to work, or ``None`` when never enabled."""

        return self._last_known_good

    @last_known_good.setter
    def last_known_good(self, value: PreviewSettings | None) -> None:
        self._last_known_good = value

    def settings(self) -> PreviewSettings:
        """The configuration currently shown by the controls."""

        preset = self.video_preset_combo.currentData()
        return PreviewSettings(
            enabled=self.enable_check.isChecked(),
            root_directory=self.root_edit.text().strip(),
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            image=ImagePreviewProfile(
                max_dimension=int(self.image_max_dimension_spin.value()),
                jpeg_quality=int(self.image_quality_spin.value()),
            ),
            video=VideoPreviewProfile(
                fps=float(self.video_fps_spin.value()),
                max_height=int(self.video_max_height_spin.value()),
                crf=int(self.video_crf_spin.value()),
                preset=str(preset) if preset else DEFAULT_VIDEO_PRESET,
            ),
        )

    def set_settings(self, settings: PreviewSettings) -> None:
        """Populate the controls without triggering validation or change signals."""

        controls = self._value_controls()
        self._populating = True
        for control in controls:
            control.blockSignals(True)
        try:
            self.enable_check.setChecked(bool(settings.enabled))
            self.root_edit.setText(settings.root_directory)
            self.ffmpeg_edit.setText(settings.ffmpeg_path)
            self.image_max_dimension_spin.setValue(int(settings.image.max_dimension))
            self.image_quality_spin.setValue(int(settings.image.jpeg_quality))
            self.video_fps_spin.setValue(float(settings.video.fps))
            self.video_max_height_spin.setValue(int(settings.video.max_height))
            self.video_crf_spin.setValue(int(settings.video.crf))
            index = self.video_preset_combo.findData(settings.video.preset)
            if index < 0:
                index = max(0, self.video_preset_combo.findData(DEFAULT_VIDEO_PRESET))
            self.video_preset_combo.setCurrentIndex(index)
        finally:
            for control in controls:
                control.blockSignals(False)
            self._populating = False
        if settings.enabled:
            # A persisted enabled configuration was proven to work when it was
            # enabled; it is the baseline that spec §35 reverts to.
            self._last_known_good = settings
        self._set_status(STATUS_ENABLED_TEXT if settings.enabled else STATUS_DISABLED_TEXT)
        self._refresh_profile_labels()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _refresh_profile_labels(self) -> None:
        current = self.settings()
        try:
            image_id = current.image.profile_id
        except PreviewConfigError as exc:
            image_id = f"invalid ({exc})"
        try:
            video_id = current.video.profile_id
        except PreviewConfigError as exc:
            video_id = f"invalid ({exc})"
        self.image_profile_label.setText(f"Current image profile: {image_id}")
        self.video_profile_label.setText(f"Current video profile: {video_id}")

    def _on_control_changed(self, *_args: object) -> None:
        if self._populating:
            return
        self._refresh_profile_labels()
        self.settings_changed.emit()

    # -- validation ----------------------------------------------------------
    def run_validation(self, *, include_encode_tests: bool = True) -> PreviewValidationReport:
        """Validate the current controls as if enabled; never changes the checkbox."""

        candidate = self.settings().with_enabled(True)
        application = QApplication.instance()
        if application is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            try:
                return self._validator(candidate, include_encode_tests=include_encode_tests)
            except Exception as exc:  # the validator should never raise; stay visible if it does
                return _exception_report(candidate, exc, include_encode_tests)
        finally:
            if application is not None:
                QApplication.restoreOverrideCursor()

    def on_test_clicked(self) -> None:
        """Spec §3: run the full validation and show the report; enabled state is untouched."""

        report = self.run_validation(include_encode_tests=True)
        if report.passed:
            self._set_status("Configuration test: PASS.")
        else:
            self._set_status(f"Configuration test: FAIL — {_failed_step_label(report)} failed.")
        PreviewValidationReportDialog(report, self).exec()

    def on_enable_toggled(self, checked: bool) -> None:
        if self._populating:
            return
        if not checked:
            self._set_status(STATUS_DISABLED_TEXT)
            self.settings_changed.emit()
            return
        report = self.run_validation(include_encode_tests=True)
        if report.passed:
            current = self.settings()
            self._last_known_good = current
            self._set_status(STATUS_ENABLED_TEXT)
            self.settings_changed.emit()
            self.validated.emit(current)
            return
        # Spec §2: leave previews disabled and return the checkbox to unchecked.
        self.enable_check.blockSignals(True)
        try:
            self.enable_check.setChecked(False)
        finally:
            self.enable_check.blockSignals(False)
        self._set_status(
            f"Previews could not be enabled — {_failed_step_label(report)} failed."
        )
        self.settings_changed.emit()
        self._show_error(self, ENABLE_ERROR_TITLE, _failure_text(report))

    def validate_for_save(self) -> tuple[PreviewSettings, PreviewValidationReport | None]:
        """Spec §35: re-validate an enabled configuration that changed since it last worked.

        Returns the settings the caller should persist plus the report when a
        validation ran.  On failure the controls revert to the last known-good
        configuration and that configuration is returned.
        """

        current = self.settings()
        if not current.enabled:
            return current, None
        known = self._last_known_good
        if known is not None and current.output_signature() == known.output_signature():
            return current, None
        report = self.run_validation(include_encode_tests=True)
        if report.passed:
            self._last_known_good = current
            self._set_status(STATUS_ENABLED_TEXT)
            return current, report
        failed_label = _failed_step_label(report)
        if known is not None:
            self.set_settings(known)
            self._set_status(
                f"{failed_label} failed — the last known-good configuration was restored."
            )
            return known, report
        fallback = current.with_enabled(False)
        self.set_settings(fallback)
        self._set_status(f"Previews disabled — {failed_label} failed.")
        return fallback, report

    # -- browsing ------------------------------------------------------------
    def browse_root(self) -> None:
        start = self.root_edit.text().strip() or str(Path.home())
        chosen = self.directory_chooser(self, "Select Preview Storage Directory", start)
        if chosen:
            self.root_edit.setText(str(chosen))

    def browse_ffmpeg(self) -> None:
        current = self.ffmpeg_edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.home())
        chosen = self.file_chooser(self, "Select FFmpeg Executable", start)
        if chosen:
            self.ffmpeg_edit.setText(str(chosen))


def _failed_step_label(report: PreviewValidationReport) -> str:
    failure = report.first_failure
    if failure is not None:
        return failure.label
    for step in report.steps:
        if not step.passed:
            return step.label
    return "Validation"


def _failure_text(report: PreviewValidationReport) -> str:
    summary = report.failure_summary(heading="Previews could not be enabled.")
    if summary:
        return summary
    return "Previews could not be enabled.\n\n" + report.report_text()


def _exception_report(
    settings: PreviewSettings,
    exc: BaseException,
    include_encode_tests: bool,
) -> PreviewValidationReport:
    step = ValidationStep(
        "validation",
        "Preview configuration validation",
        False,
        f"The validation itself failed unexpectedly: {_describe_exception(exc)}",
        STAGE_CONFIGURATION,
    )
    return PreviewValidationReport(
        passed=False,
        steps=(step,),
        settings=settings,
        root=settings.root_directory.strip(),
        free_bytes=None,
        total_bytes=None,
        ffmpeg_path=None,
        ffmpeg_version=None,
        encoder_available=None,
        image_backend="Unknown",
        image_profile_id=None,
        video_profile_id=None,
        include_encode_tests=include_encode_tests,
    )


# ---------------------------------------------------------------------------
# Validation report dialog (spec §3)
# ---------------------------------------------------------------------------


class PreviewValidationReportDialog(QDialog):
    def __init__(self, report: PreviewValidationReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.report = report
        verdict = "PASS" if report.passed else "FAIL"
        self.setWindowTitle(f"Preview Configuration Test - {verdict}")
        self.setMinimumSize(640, 460)

        self.verdict_label = QLabel(f"Overall result: {verdict}")
        self.verdict_label.setObjectName("mutedLabel" if report.passed else "offlineNotice")
        self.verdict_label.setWordWrap(True)

        self.report_edit = QPlainTextEdit()
        self.report_edit.setReadOnly(True)
        self.report_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.report_edit.setPlainText(report.report_text())

        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy the full report to the clipboard")
        self.copy_button.clicked.connect(self.copy_report)
        self.close_button = QPushButton("Close")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.verdict_label)
        layout.addWidget(self.report_edit, 1)
        layout.addLayout(buttons)

    def copy_report(self) -> None:
        _copy_to_clipboard(self.report_edit.toPlainText())


# ---------------------------------------------------------------------------
# Failure list (spec §15)
# ---------------------------------------------------------------------------


class PreviewFailuresDialog(QDialog):
    """Every failed preview with its reason; nothing is truncated (spec §15)."""

    def __init__(
        self,
        failures: list[PreviewFailure],
        parent: QWidget | None = None,
        *,
        storage_unavailable_reason: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.failures = list(failures)
        self.storage_unavailable_reason = storage_unavailable_reason
        self.setWindowTitle("Preview Failures")
        self.setMinimumSize(760, 480)
        self.resize(1000, 620)

        count = len(self.failures)
        self.heading_label = QLabel(f"{count:,} preview failure(s)")
        self.heading_label.setObjectName("emptyStateTitle")
        self.heading_label.setWordWrap(True)
        explanation = _muted_label(
            "Catalogue indexing is unaffected by these failures. Each row names the "
            "source file, the preview profile, the stage that failed, and the error."
        )

        self.storage_label = QLabel()
        self.storage_label.setObjectName("offlineNotice")
        self.storage_label.setWordWrap(True)
        if storage_unavailable_reason:
            self.storage_label.setText(
                "Preview generation stopped because preview storage became unavailable: "
                f"{storage_unavailable_reason}"
            )
        else:
            self.storage_label.setVisible(False)

        self.table = QTableWidget(0, len(FAILURE_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(FAILURE_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._populate_table()

        self.copy_text = self._build_copy_text()
        self.copy_button = QPushButton("Copy All")
        self.copy_button.setToolTip("Copy the numbered failure list as plain text")
        self.copy_button.clicked.connect(self.copy_all)
        self.close_button = QPushButton("Close")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading_label)
        layout.addWidget(explanation)
        layout.addWidget(self.storage_label)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)

    def _populate_table(self) -> None:
        table = self.table
        table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.setRowCount(len(self.failures))
            for row, failure in enumerate(self.failures):
                path_text = failure.relative_path or failure.source_name
                tooltip_lines = [path_text]
                if failure.volume_label:
                    tooltip_lines.append(f"Volume: {failure.volume_label}")
                if failure.preview_path:
                    tooltip_lines.append(f"Expected preview: {failure.preview_path}")
                if failure.sha256:
                    tooltip_lines.append(f"{HASH_LABELS[failure.hash_algorithm]}: {failure.sha256}")
                tooltip = "\n".join(tooltip_lines)
                number_item = _readonly_item(str(row + 1))
                number_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                table.setItem(row, 0, number_item)
                table.setItem(row, 1, _readonly_item(path_text, tooltip))
                table.setItem(row, 2, _readonly_item(_media_kind_label(failure.media_kind)))
                table.setItem(row, 3, _readonly_item(failure.profile_id))
                table.setItem(row, 4, _readonly_item(failure.stage))
                table.setItem(row, 5, _readonly_item(failure.message, failure.message))
                table.setItem(row, 6, _readonly_item(failure.detail, failure.detail))
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        if self.failures:
            table.resizeColumnToContents(0)
            table.setColumnWidth(1, 260)
            table.setColumnWidth(2, 70)
            table.setColumnWidth(3, 200)
            table.setColumnWidth(4, 120)
            table.setColumnWidth(5, 260)

    def _build_copy_text(self) -> str:
        lines = ["Preview Failures", ""]
        if self.storage_unavailable_reason:
            lines.extend(
                [
                    "Preview generation stopped because preview storage became unavailable: "
                    f"{self.storage_unavailable_reason}",
                    "",
                ]
            )
        for index, failure in enumerate(self.failures, 1):
            lines.append(f"{index}. {failure.relative_path or failure.source_name}")
            if failure.volume_label:
                lines.append(f"   Volume: {failure.volume_label}")
            for line in failure.display_lines():
                lines.append(f"   {line}")
            lines.append("")
        if not self.failures:
            lines.append("No preview failures were recorded.")
        return "\n".join(lines).rstrip() + "\n"

    def copy_all(self) -> None:
        _copy_to_clipboard(self.copy_text)


# ---------------------------------------------------------------------------
# Preview Manager (spec §22)
# ---------------------------------------------------------------------------


class PreviewCacheDialog(QDialog):
    """Catalogue preview configuration and separate storage maintenance tools.

    The dialog only requests work; the main window runs the workers and feeds
    results back through ``set_store_statistics`` / ``set_unreferenced``.
    """

    scan_requested = Signal()
    unreferenced_requested = Signal()
    delete_requested = Signal(list)
    delete_temporaries_requested = Signal()
    open_folder_requested = Signal()
    cancel_requested = Signal()
    settings_requested = Signal(object)

    def __init__(self, settings: PreviewSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview Manager")
        self.setMinimumSize(760, 560)
        self.resize(1040, 720)
        self.confirm: Confirm = _default_confirm
        self._settings = settings
        self._entries: list[PreviewEntry] = []
        self._total_found = 0
        self._busy = False
        self._writable = True
        self._build_ui()
        self.set_settings(settings)
        self.set_store_statistics(None, None)

    # -- construction ---------------------------------------------------------
    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.previews_page = QWidget()
        previews_layout = QVBoxLayout(self.previews_page)
        self.scope_label = _muted_label("Configure previews for this catalogue. Changes are saved when you choose Apply.")
        self.preview_widget = OfflinePreviewSettingsWidget(self._settings, show_ffmpeg=False)
        self.preview_widget.setTitle("Generation")
        self.open_folder_button = QPushButton("Open Folder")
        self.open_folder_button.setToolTip("Open the saved preview directory; apply a changed location first")
        self.preview_widget.root_row.addWidget(self.open_folder_button)
        configuration = QWidget()
        configuration_layout = QVBoxLayout(configuration)
        configuration_layout.setContentsMargins(0, 0, 0, 0)
        configuration_layout.addWidget(self.scope_label)
        configuration_layout.addWidget(self.preview_widget)
        configuration_layout.addStretch(1)
        self.configuration_scroll = QScrollArea()
        self.configuration_scroll.setWidgetResizable(True)
        self.configuration_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.configuration_scroll.setWidget(configuration)
        previews_layout.addWidget(self.configuration_scroll, 1)
        apply_row = QHBoxLayout()
        self.settings_state_label = _muted_label()
        self.apply_button = QPushButton("Apply")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply_settings)
        apply_row.addWidget(self.settings_state_label, 1)
        apply_row.addWidget(self.apply_button)
        previews_layout.addLayout(apply_row)
        self.preview_widget.settings_changed.connect(self._update_configuration_state)
        self.tabs.addTab(self.previews_page, "Previews")

        self.usage_group = QGroupBox("Storage usage")
        usage_layout = QVBoxLayout(self.usage_group)
        self.scan_button = QPushButton("Refresh Usage")
        self.scan_button.setToolTip("Count preview files and the space they use")
        usage_toolbar = QHBoxLayout()
        usage_toolbar.addWidget(_muted_label("Usage of the saved preview directory."), 1)
        usage_toolbar.addWidget(self.scan_button)
        usage_layout.addLayout(usage_toolbar)
        self.free_space_label = self._value_label()
        self.image_count_label = self._value_label()
        self.video_count_label = self._value_label()
        self.total_storage_label = self._value_label()
        usage_cards = QHBoxLayout()
        for title, value in (
            ("Space used", self.total_storage_label),
            ("Free space", self.free_space_label),
            ("Images", self.image_count_label),
            ("Videos", self.video_count_label),
        ):
            card = QWidget()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            heading = _muted_label(title)
            heading.setWordWrap(False)
            card_layout.addWidget(heading)
            font = value.font()
            font.setBold(True)
            value.setFont(font)
            card_layout.addWidget(value)
            usage_cards.addWidget(card, 1)
        usage_layout.addLayout(usage_cards)
        self.profiles_toggle = QPushButton("Show Profile Breakdown")
        self.profiles_toggle.setCheckable(True)
        self.profiles_toggle.setFlat(True)
        self.profiles_label = _muted_label("Not checked yet.")
        self.profiles_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.profiles_label.hide()
        self.profiles_toggle.toggled.connect(self.profiles_label.setVisible)
        self.profiles_toggle.toggled.connect(
            lambda checked: self.profiles_toggle.setText("Hide Profile Breakdown" if checked else "Show Profile Breakdown")
        )
        breakdown_row = QHBoxLayout()
        breakdown_row.addWidget(self.profiles_toggle)
        breakdown_row.addStretch(1)
        usage_layout.addLayout(breakdown_row)
        usage_layout.addWidget(self.profiles_label)

        self.unreferenced_group = QGroupBox("Unused previews")
        unreferenced_layout = QVBoxLayout(self.unreferenced_group)
        self.unreferenced_heading = QLabel(UNREFERENCED_HEADING_TEXT)
        self.unreferenced_heading.setObjectName("mutedLabel")
        self.unreferenced_button = QPushButton("Find Unused Previews")
        self.unreferenced_button.setToolTip("Compare previews with the saved catalogue and its current preview profiles")
        unused_toolbar = QHBoxLayout()
        unused_toolbar.addWidget(self.unreferenced_heading, 1)
        unused_toolbar.addWidget(self.unreferenced_button)
        unreferenced_layout.addLayout(unused_toolbar)
        self.unreferenced_count_label = _muted_label("Not checked yet.")
        unreferenced_layout.addWidget(self.unreferenced_count_label)
        self.unreferenced_table = QTableWidget(0, len(UNREFERENCED_COLUMNS))
        self.unreferenced_table.setHorizontalHeaderLabels(list(UNREFERENCED_COLUMNS))
        self.unreferenced_table.verticalHeader().setVisible(False)
        self.unreferenced_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.unreferenced_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.unreferenced_table.setAlternatingRowColors(True)
        self.unreferenced_table.setWordWrap(False)
        header = self.unreferenced_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((32, 70, 180, 180, 90)):
            self.unreferenced_table.setColumnWidth(column, width)
        self.unreferenced_table.itemChanged.connect(self._on_table_item_changed)
        self.unreferenced_table.setMinimumHeight(150)
        unreferenced_layout.addWidget(self.unreferenced_table, 1)
        self.select_all_button = QPushButton("Select All")
        self.select_none_button = QPushButton("Clear Selection")
        self.delete_button = QPushButton("Delete Selected")
        self.delete_button.setEnabled(False)
        self.delete_button.setToolTip("Delete only the ticked preview files after confirmation")
        selection_row = QHBoxLayout()
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.select_none_button)
        selection_row.addStretch(1)
        selection_row.addWidget(self.delete_button)
        unreferenced_layout.addLayout(selection_row)
        self.unreferenced_note = _muted_label(
            "Another catalogue sharing the same preview directory may still need these files. Nothing is deleted automatically."
        )
        unreferenced_layout.addWidget(self.unreferenced_note)

        self.temporary_group = QGroupBox("Temporary files")
        temporary_layout = QHBoxLayout(self.temporary_group)
        self.temporary_label = self._value_label()
        temporary_layout.addWidget(QLabel("Files:"))
        temporary_layout.addWidget(self.temporary_label, 1)
        self.temporaries_button = QPushButton("Clean Up Temporary Files")
        self.temporaries_button.setToolTip("Remove leftover temporary files; files newer than 24 hours are kept")
        temporary_layout.addWidget(self.temporaries_button)
        self.cleanup_page = QWidget()
        cleanup_layout = QVBoxLayout(self.cleanup_page)
        cleanup_layout.setContentsMargins(0, 0, 0, 0)
        cleanup_layout.addWidget(self.usage_group)
        cleanup_layout.addWidget(self.unreferenced_group, 1)
        cleanup_layout.addWidget(self.temporary_group)
        self.cleanup_scroll = QScrollArea()
        self.cleanup_scroll.setWidgetResizable(True)
        self.cleanup_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.cleanup_scroll.setWidget(self.cleanup_page)
        cleanup_tab = QWidget()
        cleanup_tab_layout = QVBoxLayout(cleanup_tab)
        cleanup_tab_layout.addWidget(self.cleanup_scroll, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()
        self.status_label = _muted_label("Choose Refresh Usage to check storage, or Find Unused Previews to review cleanup candidates.")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        operation_row = QHBoxLayout()
        operation_row.addWidget(self.status_label, 1)
        operation_row.addWidget(self.cancel_button)
        cleanup_tab_layout.addWidget(self.progress)
        cleanup_tab_layout.addLayout(operation_row)
        self.tabs.addTab(cleanup_tab, "Storage && Cleanup")
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(bottom)
        self.open_folder_button.clicked.connect(self.open_folder_requested)
        self.scan_button.clicked.connect(self.scan_requested)
        self.unreferenced_button.clicked.connect(self.unreferenced_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.delete_button.clicked.connect(self.on_delete_clicked)
        self.temporaries_button.clicked.connect(self.on_delete_temporaries_clicked)
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    @staticmethod
    def _value_label() -> QLabel:
        label = QLabel("—")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    # -- data ----------------------------------------------------------------
    def set_settings(self, settings: PreviewSettings) -> None:
        previous = self._settings
        draft = self.preview_widget.settings()
        dirty = draft != previous
        changed_output = (previous.root_directory, previous.image, previous.video) != (
            settings.root_directory, settings.image, settings.video,
        )
        self._settings = settings
        if not dirty or draft == settings or getattr(self, "_applying", False):
            self.preview_widget.set_settings(settings)
        else:
            # App Settings may change FFmpeg while the user has unapplied edits
            # here. Preserve those edits and overlay the computer's executable.
            known = self.preview_widget.last_known_good
            if known is not None and known.ffmpeg_path != settings.ffmpeg_path:
                known = None
            self.preview_widget.set_settings(replace(draft, ffmpeg_path=settings.ffmpeg_path))
            self.preview_widget.last_known_good = known
        root_text = settings.root_directory.strip()
        has_root = bool(root_text)
        if not self._busy:
            self.scan_button.setEnabled(has_root)
            self.unreferenced_button.setEnabled(has_root)
            self.open_folder_button.setEnabled(has_root)
            self.temporaries_button.setEnabled(has_root)
        if changed_output and self.image_count_label.text() != "—":
            # Root/profile changes make the old comparison unsafe for deletion.
            self.set_store_statistics(None, None)
            self.set_unreferenced([], 0)
            self.unreferenced_count_label.setText("Not checked for these preview settings yet.")
            self.status_label.setText(
                "Preview settings changed. Refresh Usage to update the figures."
            )
        self._refresh_free_space()
        self._update_configuration_state()

    def set_ffmpeg_path(self, path: str) -> None:
        """Update computer-specific FFmpeg without discarding catalogue drafts."""

        known = self.preview_widget.last_known_good
        if known is not None and known.ffmpeg_path != path:
            known = None
        self._settings = replace(self._settings, ffmpeg_path=path)
        self.preview_widget.set_settings(replace(self.preview_widget.settings(), ffmpeg_path=path))
        # A new executable has not been tested with the pending configuration.
        self.preview_widget.last_known_good = known
        self._update_configuration_state()

    def set_writable(self, writable: bool) -> None:
        """Allow editing only when the current catalogue can accept changes."""

        self._writable = bool(writable)
        self._update_configuration_state()

    def _update_configuration_state(self) -> None:
        editable = self._writable and not self._busy
        dirty = self.preview_widget.settings() != self._settings
        self.preview_widget.setEnabled(editable)
        self.apply_button.setEnabled(editable and dirty)
        self.open_folder_button.setEnabled(
            not self._busy and bool(self._settings.root_directory.strip())
            and self.preview_widget.root_edit.text().strip() == self._settings.root_directory.strip()
        )
        if not self._writable:
            text = "Preview settings cannot be changed right now."
        elif self._busy:
            text = "Wait for the storage operation to finish before changing preview settings."
        elif dirty:
            text = "Unsaved changes. Choose Apply to save them in this catalogue."
        else:
            text = "Settings are saved in this catalogue."
        self.settings_state_label.setText(text)

    def apply_settings(self) -> None:
        if not self._writable or self._busy:
            return
        candidate, report = self.preview_widget.validate_for_save()
        if report is not None and not report.passed:
            self.preview_widget._show_error(self, "Previews", _failure_text(report))
            self._update_configuration_state()
            return
        try:
            candidate.validate(require_root=candidate.enabled)
        except PreviewConfigError as exc:
            self.preview_widget._show_error(self, "Previews", str(exc))
            return
        if candidate != self._settings:
            self._applying = True
            try:
                self.settings_requested.emit(candidate)
            finally:
                self._applying = False
        self._update_configuration_state()

    def _refresh_free_space(self) -> None:
        """Free space is one cheap ``disk_usage`` call; show it without a full store walk."""

        free_bytes: int | None = None
        root = self._settings.root_path
        if root is not None:
            try:
                usage = _cache_for(self._settings).free_space()
            except PreviewError:
                usage = None
            if usage is not None:
                free_bytes = usage[1]
        self.free_space_label.setText(
            format_size(int(free_bytes)) if free_bytes is not None else "Unknown"
        )

    def set_store_statistics(
        self,
        statistics: PreviewStoreStatistics | None,
        free_bytes: int | None,
    ) -> None:
        if free_bytes is not None:
            self.free_space_label.setText(format_size(int(free_bytes)))
        elif self.free_space_label.text() in {"—", ""}:
            self.free_space_label.setText("Unknown")
        if statistics is None:
            for label in (
                self.image_count_label,
                self.video_count_label,
                self.total_storage_label,
                self.temporary_label,
            ):
                label.setText("Not scanned")
            self.profiles_label.setText("Not scanned")
            return
        self.image_count_label.setText(f"{int(statistics.image_count):,}")
        self.video_count_label.setText(f"{int(statistics.video_count):,}")
        self.total_storage_label.setText(format_size(int(statistics.total_bytes)))
        self.total_storage_label.setToolTip(f"{int(statistics.total_bytes):,} bytes")
        self.temporary_label.setText(f"{int(statistics.temporary_files):,}")
        profile_lines = []
        for (media_kind, profile_id), profile_stats in sorted(statistics.profiles.items()):
            marker = ""
            try:
                if profile_id == self._settings.profile_id(media_kind):
                    marker = " (current)"
            except PreviewConfigError:
                marker = ""
            profile_lines.append(
                f"{_media_kind_label(media_kind)} {profile_id}{marker}: "
                f"{int(profile_stats.count):,} previews, {format_size(int(profile_stats.bytes))}"
            )
        self.profiles_label.setText("\n".join(profile_lines) if profile_lines else "No previews found")
        if statistics.cancelled:
            self.status_label.setText("Preview store scan cancelled — the figures are partial.")
        else:
            self.status_label.setText("Usage updated.")

    def set_unreferenced(
        self,
        entries: list[PreviewEntry],
        total_found: int,
        *,
        partial: bool = False,
    ) -> None:
        """Show the unreferenced list; ``partial`` marks a cancelled, incomplete comparison."""

        self._entries = list(entries)
        self._total_found = max(int(total_found), len(self._entries))
        self._partial = bool(partial)
        table = self.unreferenced_table
        table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.setRowCount(0)
            table.setRowCount(len(self._entries))
            for row, entry in enumerate(self._entries):
                path_text = os.fspath(entry.path)
                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                check.setCheckState(Qt.CheckState.Unchecked)
                check.setData(Qt.ItemDataRole.UserRole, path_text)
                table.setItem(row, 0, check)
                table.setItem(row, 1, _readonly_item(_media_kind_label(entry.media_kind)))
                table.setItem(row, 2, _readonly_item(entry.profile_id))
                hash_text = f"{HASH_LABELS[entry.hash_algorithm]}: {entry.sha256}"
                table.setItem(row, 3, _readonly_item(hash_text, hash_text))
                size_item = _readonly_item(format_size(int(entry.size_bytes)), f"{int(entry.size_bytes):,} bytes")
                size_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                table.setItem(row, 4, size_item)
                table.setItem(row, 5, _readonly_item(path_text, path_text))
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        self._refresh_unreferenced_count()
        self._update_delete_button()

    def invalidate_unreferenced(self) -> None:
        """Drop a displayed unreferenced list once the catalogue has changed (spec §22).

        The list means "not referenced by this catalogue *as it was when the
        list was made*"; after a scan or delete the user must compare again
        before deleting anything.
        """

        if not self._entries and not self._total_found:
            return
        self.set_unreferenced([], 0)
        self.status_label.setText(
            "The catalogue changed — run Find Unused Previews again before deleting anything."
        )

    def _refresh_unreferenced_count(self) -> None:
        shown = len(self._entries)
        total = self._total_found
        if getattr(self, "_partial", False):
            # A cancelled comparison must never read as "nothing is unreferenced".
            self.unreferenced_count_label.setText(
                f"The comparison was cancelled before it finished: {shown:,} preview(s) not "
                "referenced by this catalogue were found so far. Run Find Unused "
                "Previews again for a complete list."
            )
            return
        if shown == 0 and total == 0:
            self.unreferenced_count_label.setText(
                "No previews under the current profiles are unreferenced by this catalogue."
            )
        elif total > shown:
            self.unreferenced_count_label.setText(
                f"Showing {shown:,} of {total:,} previews not referenced by this catalogue "
                f"(the list is capped at {MAX_UNREFERENCED_ROWS:,} rows; delete and re-run to see more)."
            )
        else:
            self.unreferenced_count_label.setText(
                f"{shown:,} preview(s) not referenced by this catalogue."
            )

    def selected_unreferenced_paths(self) -> list[str]:
        paths: list[str] = []
        table = self.unreferenced_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                paths.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return paths

    def remove_entries(self, paths: list[str]) -> None:
        removed = {_normalized_path_key(path) for path in paths}
        if not removed:
            return
        table = self.unreferenced_table
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        try:
            for row in range(table.rowCount() - 1, -1, -1):
                item = table.item(row, 0)
                if item is None:
                    continue
                if _normalized_path_key(str(item.data(Qt.ItemDataRole.UserRole))) in removed:
                    table.removeRow(row)
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(False)
        before = len(self._entries)
        self._entries = [
            entry for entry in self._entries if _normalized_path_key(entry.path) not in removed
        ]
        self._total_found = max(0, self._total_found - (before - len(self._entries)))
        self._refresh_unreferenced_count()
        self._update_delete_button()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        has_root = bool(self._settings.root_directory.strip())
        self.scan_button.setEnabled(not busy and has_root)
        self.unreferenced_button.setEnabled(not busy and has_root)
        self.temporaries_button.setEnabled(not busy and has_root)
        self.open_folder_button.setEnabled(not busy and has_root)
        self.select_all_button.setEnabled(not busy)
        self.select_none_button.setEnabled(not busy)
        self.cancel_button.setEnabled(bool(busy))
        self.cancel_button.setVisible(bool(busy))
        if busy:
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setVisible(False)
        if message:
            self.status_label.setText(message)
        self._update_configuration_state()
        self._update_delete_button()

    def set_progress(self, count: int, message: str) -> None:
        text = f"{int(count):,} previews examined"
        if message:
            text += f" — {message}"
        self.status_label.setText(text)

    # -- selection and deletion ----------------------------------------------
    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_delete_button()

    def _set_all_checked(self, checked: bool) -> None:
        table = self.unreferenced_table
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        table.blockSignals(True)
        try:
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item is not None:
                    item.setCheckState(state)
        finally:
            table.blockSignals(False)
        self._update_delete_button()

    def _update_delete_button(self) -> None:
        self.delete_button.setEnabled(
            not self._busy and bool(self.selected_unreferenced_paths())
        )

    def on_delete_temporaries_clicked(self) -> None:
        text = (
            "Delete leftover temporary preview files?\n\n"
            "A temporary file (.<name>.tmp-…) is a preview that was still being written "
            "when JVVV or the computer stopped. Files newer than 24 hours are kept, because "
            "a scan running in another JVVV window may still be writing them.\n\n"
            "Finished previews are never touched by this action."
        )
        if not self.confirm(self, "Delete Temporary Files", text):
            return
        self.delete_temporaries_requested.emit()

    def set_temporaries_deleted(self, deleted: int, kept: int, errors: list[str]) -> None:
        """Report the outcome of Delete Temporary Files and refresh the count."""

        remaining = int(kept) + len(errors)
        self.temporary_label.setText(f"{remaining:,}")
        message = f"{int(deleted):,} temporary file(s) deleted"
        if kept:
            message += f"; {int(kept):,} newer than 24 hours kept"
        if errors:
            message += f"; {len(errors):,} could not be deleted"
        self.status_label.setText(message + ".")

    def on_delete_clicked(self) -> None:
        paths = self.selected_unreferenced_paths()
        if not paths:
            self._update_delete_button()
            return
        count = len(paths)
        text = (
            f"Delete {count:,} selected preview file(s)?\n\n"
            "These previews are not referenced by this catalogue. Another catalogue "
            "sharing the same preview directory may still use them, so make sure no "
            "other catalogue needs these files.\n\n"
            "Deleted previews are regenerated the next time a scan with offline previews "
            "enabled encounters the same source content."
        )
        if not self.confirm(self, "Delete Unreferenced Previews", text):
            return
        self.delete_requested.emit(list(paths))


def _profile_id_text(settings: PreviewSettings, media_kind: str) -> str:
    try:
        return settings.profile_id(media_kind)
    except PreviewConfigError as exc:
        return f"Invalid ({exc})"


# ---------------------------------------------------------------------------
# Workers (run on QThreads by the main window; never touch widgets)
# ---------------------------------------------------------------------------


def _cache_for(settings: PreviewSettings) -> PreviewCache:
    root = settings.root_path
    if root is None:
        raise PreviewError(
            STAGE_CONFIGURATION,
            "No preview storage directory is configured in Window > Preview Manager.",
        )
    try:
        settings.image.validate()
        settings.video.validate()
    except PreviewConfigError as exc:
        raise PreviewError(STAGE_CONFIGURATION, str(exc)) from exc
    return PreviewCache(root, settings.image, settings.video)


def _require_store(cache: PreviewCache) -> None:
    """A missing or disconnected preview root is an error, never an empty store (spec §22, §26)."""

    try:
        present = cache.root.is_dir()
    except OSError as exc:
        raise PreviewError(
            STAGE_PREVIEW_ROOT,
            f"The preview directory could not be reached: {cache.root}",
            detail=str(exc),
        ) from exc
    if not present:
        raise PreviewError(
            STAGE_PREVIEW_ROOT,
            f"The preview directory does not exist or is not reachable: {cache.root}",
            detail="Reconnect the drive or choose the preview directory again in Window > Preview Manager.",
        )


class _CancellableWorker(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()


class PreviewStoreStatisticsWorker(_CancellableWorker):
    """Count previews and bytes in the store (spec §22); cancellation yields partial figures."""

    progress = Signal(int, str)
    finished = Signal(object, object)
    failed = Signal(str)

    def __init__(self, settings: PreviewSettings) -> None:
        super().__init__()
        self.settings = settings

    def _report_progress(self, count: int, directory: str) -> None:
        self.progress.emit(int(count), f"Scanning {directory}")

    @Slot()
    def run(self) -> None:
        try:
            cache = _cache_for(self.settings)
            _require_store(cache)
            statistics = cache.store_statistics(
                cancel_callback=self.is_cancelled,
                progress_callback=self._report_progress,
            )
            usage = cache.free_space()
            free_bytes = usage[1] if usage is not None else None
        except Exception as exc:
            self.failed.emit(_describe_exception(exc))
            return
        self.finished.emit(statistics, free_bytes)


class UnreferencedPreviewWorker(_CancellableWorker):
    """List previews under the current profiles whose content hash this catalogue does not record.

    The catalogue is opened read-only and its hashes are indexed in a temporary
    SQLite table, so neither the store nor the catalogue is loaded into memory.
    The emitted list is capped at ``MAX_UNREFERENCED_ROWS``; ``total_found``
    is always the full count.
    """

    progress = Signal(int, str)
    finished = Signal(list, int)
    failed = Signal(str)

    def __init__(self, settings: PreviewSettings, db_path: Path) -> None:
        super().__init__()
        self.settings = settings
        self.db_path = Path(db_path)

    @Slot()
    def run(self) -> None:
        entries: list[PreviewEntry] = []
        total_found = 0
        examined = 0
        cancelled = False
        try:
            cache = _cache_for(self.settings)
            _require_store(cache)
            # Read-only: the catalogue is never modified. The content hash lookup is a
            # connection-local TEMP table that Database manages itself.
            db = Database(self.db_path, initialize=False, create=False, read_only=True)
            try:
                for media_kind, extensions in (
                    ("image", IMAGE_EXTENSIONS),
                    ("video", VIDEO_EXTENSIONS),
                ):
                    if self.is_cancelled():
                        cancelled = True
                        break
                    profile_id = cache.profile_id(media_kind)
                    db.prepare_content_hash_lookup(extensions)
                    try:
                        for entry in cache.iter_previews(
                            media_kind,
                            profile_id,
                            cancel_callback=self.is_cancelled,
                        ):
                            examined += 1
                            if not db.content_hash_referenced(bytes.fromhex(entry.sha256), entry.hash_algorithm):
                                total_found += 1
                                if len(entries) < MAX_UNREFERENCED_ROWS:
                                    entries.append(entry)
                            if examined % WORKER_PROGRESS_INTERVAL == 0:
                                self.progress.emit(
                                    examined,
                                    f"{total_found:,} unreferenced so far — {entry.path.parent}",
                                )
                    finally:
                        db.drop_content_hash_lookup()
            finally:
                db.close()
        except PreviewCancelled:
            cancelled = True
        except Exception as exc:
            self.failed.emit(_describe_exception(exc))
            return
        if cancelled:
            self.progress.emit(examined, "Cancelled — the list is partial.")
        self.finished.emit(entries, total_found)


class DeleteTemporariesWorker(_CancellableWorker):
    """Remove leftover temporary files older than a day; recent ones are kept."""

    progress = Signal(int, str)
    finished = Signal(int, int, list)  # deleted, kept (too recent), error texts
    failed = Signal(str)

    def __init__(self, settings: PreviewSettings) -> None:
        super().__init__()
        self.settings = settings

    @Slot()
    def run(self) -> None:
        deleted = kept = examined = 0
        errors: list[str] = []
        try:
            cache = _cache_for(self.settings)
            _require_store(cache)
            for path in cache.iter_temporary_files(cancel_callback=self.is_cancelled):
                examined += 1
                try:
                    if cache.remove_stale_temporary(path):
                        deleted += 1
                    else:
                        kept += 1
                except PreviewError as exc:
                    errors.append(f"{path}: {exc}")
                if examined % DELETE_PROGRESS_INTERVAL == 0:
                    self.progress.emit(examined, f"Deleted {deleted:,} temporary files")
        except PreviewCancelled:
            errors.append("Cancelled before every temporary file was examined.")
        except Exception as exc:
            self.failed.emit(_describe_exception(exc))
            return
        self.finished.emit(deleted, kept, errors)


class DeletePreviewsWorker(_CancellableWorker):
    """Delete selected previews one by one; a failure never stops the others."""

    progress = Signal(int, str)
    finished = Signal(int, list, list)
    failed = Signal(str)

    def __init__(self, settings: PreviewSettings, paths: list[str]) -> None:
        super().__init__()
        self.settings = settings
        self.paths = [str(path) for path in paths]

    @Slot()
    def run(self) -> None:
        deleted: list[str] = []
        errors: list[str] = []
        try:
            cache = _cache_for(self.settings)
        except Exception as exc:
            self.failed.emit(_describe_exception(exc))
            return
        total = len(self.paths)
        for index, path in enumerate(self.paths, 1):
            if self.is_cancelled():
                errors.append(f"{path}: not deleted because the operation was cancelled.")
                continue
            try:
                cache.remove_preview(Path(path))
            except PreviewError as exc:
                errors.append(f"{path}: {exc}")
            except Exception as exc:  # defensive: keep going, report it
                errors.append(f"{path}: {_describe_exception(exc)}")
            else:
                deleted.append(path)
            if index % DELETE_PROGRESS_INTERVAL == 0:
                self.progress.emit(index, f"Deleted {len(deleted):,} of {total:,} previews")
        self.finished.emit(len(deleted), deleted, errors)


__all__ = [
    "DeletePreviewsWorker",
    "DeleteTemporariesWorker",
    "FAILURE_COLUMNS",
    "MAX_UNREFERENCED_ROWS",
    "OfflinePreviewSettingsWidget",
    "PreviewCacheDialog",
    "PreviewFailuresDialog",
    "PreviewStoreStatisticsWorker",
    "PreviewValidationReportDialog",
    "UNREFERENCED_COLUMNS",
    "UNREFERENCED_HEADING_TEXT",
    "UnreferencedPreviewWorker",
    "preview_summary_text",
]
