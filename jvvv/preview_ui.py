"""Qt widgets and background workers for the offline-preview feature.

Covers the Settings section (spec §1, §2, §3, §34, §35, §37, §42), the
end-of-scan failure list (spec §15), and the Preview Cache Manager (spec §22)
together with the workers the main window runs on ``QThread``s.

Nothing here displays or plays media: previews are always opened externally,
so this module never imports ``PySide6.QtMultimedia``.  Workers never touch
widgets; they only emit signals.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .database import Database
from .media_metadata import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .preview_cache import (
    STAGE_CONFIGURATION,
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
UNREFERENCED_COLUMNS: tuple[str, ...] = ("", "Type", "Profile", "SHA-256", "Size", "Path")

UNREFERENCED_HEADING_TEXT = "Not referenced by this catalogue"
UNREFERENCED_EXPLANATION_TEXT = (
    "These preview files are not referenced by any SHA-256 recorded in the open "
    "catalogue under the current preview profiles. Another catalogue sharing the "
    "same preview directory may still use these files, so nothing is deleted "
    "automatically: tick the previews you want to remove and confirm the deletion."
)
STATUS_ENABLED_TEXT = "Offline previews enabled — configuration verified."
STATUS_DISABLED_TEXT = "Offline previews disabled."
ENABLE_ERROR_TITLE = "Offline Previews"


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
    """The "Offline Previews" section of Preferences (spec §1, §2, §3, §34, §35, §37, §42).

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
    ) -> None:
        super().__init__("Offline Previews", parent)
        self._validator: Validator = validator
        self._show_error: ShowError = show_error or _default_show_error
        self._last_known_good: PreviewSettings | None = settings if settings.enabled else None
        self.directory_chooser: DirectoryChooser = _default_directory_chooser
        self.file_chooser: FileChooser = _default_file_chooser
        self._populating = False
        self._build_ui()
        self._connect_signals()
        self.set_settings(settings)

    # -- construction ---------------------------------------------------------
    def _build_ui(self) -> None:
        self.enable_check = QCheckBox("Generate offline previews while scanning")
        self.enable_check.setToolTip(
            "Ticking this immediately tests the preview directory, the image encoder, "
            "and FFmpeg. Previews stay disabled if any test fails."
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
        storage_form.addRow("Preview storage directory:", root_row)
        storage_form.addRow("FFmpeg executable (optional):", ffmpeg_row)

        image_group = QGroupBox("Image preview settings")
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
        image_form.addRow("Maximum dimension:", self.image_max_dimension_spin)
        image_form.addRow("JPEG quality:", self.image_quality_spin)

        video_group = QGroupBox("Video preview settings")
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
        video_form.addRow("Maximum height:", self.video_max_height_spin)
        video_form.addRow("CRF:", self.video_crf_spin)
        video_form.addRow("Encoder preset:", self.video_preset_combo)

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

        layout = QVBoxLayout(self)
        layout.addWidget(self.enable_check)
        layout.addWidget(self.status_label)
        layout.addLayout(storage_form)
        layout.addWidget(image_group)
        layout.addWidget(video_group)
        layout.addWidget(self.image_profile_label)
        layout.addWidget(self.video_profile_label)
        layout.addWidget(profile_note)
        layout.addLayout(test_row)
        layout.addWidget(self.storage_note)

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
            f"Offline previews could not be enabled — {_failed_step_label(report)} failed."
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
        self._set_status(f"Offline previews disabled — {failed_label} failed.")
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
    summary = report.failure_summary()
    if summary:
        return summary
    return "Offline previews could not be enabled.\n\n" + report.report_text()


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
                    tooltip_lines.append(f"SHA-256: {failure.sha256}")
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
# Preview Cache Manager (spec §22)
# ---------------------------------------------------------------------------


class PreviewCacheDialog(QDialog):
    """Store overview plus the "not referenced by this catalogue" list (spec §22).

    The dialog only requests work; the main window runs the workers and feeds
    results back through ``set_store_statistics`` / ``set_unreferenced``.
    """

    scan_requested = Signal()
    unreferenced_requested = Signal()
    delete_requested = Signal(list)
    open_folder_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, settings: PreviewSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview Cache")
        self.setMinimumSize(760, 560)
        self.resize(1040, 720)
        self.confirm: Confirm = _default_confirm
        self._settings = settings
        self._entries: list[PreviewEntry] = []
        self._total_found = 0
        self._busy = False
        self._build_ui()
        self.set_settings(settings)
        self.set_store_statistics(None, None)

    # -- construction ---------------------------------------------------------
    def _build_ui(self) -> None:
        overview = QGroupBox("Preview store")
        self.root_label = self._value_label()
        self.free_space_label = self._value_label()
        self.image_profile_label = self._value_label()
        self.video_profile_label = self._value_label()
        self.image_count_label = self._value_label()
        self.video_count_label = self._value_label()
        self.total_storage_label = self._value_label()
        self.temporary_label = self._value_label()
        self.profiles_label = _muted_label("")
        form = QFormLayout(overview)
        form.addRow("Preview root:", self.root_label)
        form.addRow("Free space:", self.free_space_label)
        form.addRow("Current image profile:", self.image_profile_label)
        form.addRow("Current video profile:", self.video_profile_label)
        form.addRow("Total image previews:", self.image_count_label)
        form.addRow("Total video previews:", self.video_count_label)
        form.addRow("Total preview storage:", self.total_storage_label)
        form.addRow("Temporary files:", self.temporary_label)
        form.addRow("Per profile:", self.profiles_label)

        self.open_folder_button = QPushButton("Open Preview Folder")
        self.scan_button = QPushButton("Scan Preview Store")
        self.scan_button.setToolTip(
            "Count previews and bytes per profile by walking the preview directory"
        )
        self.unreferenced_button = QPushButton("Show Unreferenced Previews")
        self.unreferenced_button.setToolTip(
            "List previews under the current profiles whose SHA-256 is not recorded "
            "in the open catalogue"
        )
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        actions = QHBoxLayout()
        actions.addWidget(self.open_folder_button)
        actions.addWidget(self.scan_button)
        actions.addWidget(self.unreferenced_button)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.status_label = _muted_label(
            "Use Scan Preview Store to count previews, or Show Unreferenced Previews "
            "to compare the store with this catalogue."
        )

        unreferenced_group = QGroupBox("Unreferenced previews")
        self.unreferenced_heading = QLabel(UNREFERENCED_HEADING_TEXT)
        self.unreferenced_heading.setObjectName("emptyStateTitle")
        self.unreferenced_note = QLabel(UNREFERENCED_EXPLANATION_TEXT)
        self.unreferenced_note.setObjectName("offlineNotice")
        self.unreferenced_note.setWordWrap(True)
        self.unreferenced_count_label = _muted_label("Not checked yet.")

        self.unreferenced_table = QTableWidget(0, len(UNREFERENCED_COLUMNS))
        self.unreferenced_table.setHorizontalHeaderLabels(list(UNREFERENCED_COLUMNS))
        self.unreferenced_table.verticalHeader().setVisible(False)
        self.unreferenced_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.unreferenced_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.unreferenced_table.setAlternatingRowColors(True)
        self.unreferenced_table.setWordWrap(False)
        header = self.unreferenced_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.unreferenced_table.setColumnWidth(0, 32)
        self.unreferenced_table.setColumnWidth(1, 70)
        self.unreferenced_table.setColumnWidth(2, 220)
        self.unreferenced_table.setColumnWidth(3, 200)
        self.unreferenced_table.setColumnWidth(4, 90)
        self.unreferenced_table.itemChanged.connect(self._on_table_item_changed)

        self.select_all_button = QPushButton("Select All")
        self.select_none_button = QPushButton("Clear Selection")
        self.delete_button = QPushButton("Delete Selected Unreferenced Previews")
        self.delete_button.setEnabled(False)
        self.delete_button.setToolTip(
            "Delete only the ticked previews after confirmation. Another catalogue "
            "may still use them."
        )
        selection_row = QHBoxLayout()
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.select_none_button)
        selection_row.addStretch(1)
        selection_row.addWidget(self.delete_button)

        unreferenced_layout = QVBoxLayout(unreferenced_group)
        unreferenced_layout.addWidget(self.unreferenced_heading)
        unreferenced_layout.addWidget(self.unreferenced_note)
        unreferenced_layout.addWidget(self.unreferenced_count_label)
        unreferenced_layout.addWidget(self.unreferenced_table, 1)
        unreferenced_layout.addLayout(selection_row)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(overview)
        layout.addLayout(actions)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addWidget(unreferenced_group, 1)
        layout.addLayout(bottom)

        self.open_folder_button.clicked.connect(self.open_folder_requested)
        self.scan_button.clicked.connect(self.scan_requested)
        self.unreferenced_button.clicked.connect(self.unreferenced_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.delete_button.clicked.connect(self.on_delete_clicked)
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))

    @staticmethod
    def _value_label() -> QLabel:
        label = QLabel("—")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    # -- data ----------------------------------------------------------------
    def set_settings(self, settings: PreviewSettings) -> None:
        previous_root = self._settings.root_directory.strip()
        self._settings = settings
        root_text = settings.root_directory.strip()
        self.root_label.setText(root_text or "Not configured")
        self.image_profile_label.setText(_profile_id_text(settings, "image"))
        self.video_profile_label.setText(_profile_id_text(settings, "video"))
        has_root = bool(root_text)
        if not self._busy:
            self.scan_button.setEnabled(has_root)
            self.unreferenced_button.setEnabled(has_root)
            self.open_folder_button.setEnabled(has_root)
        if root_text != previous_root and self.image_count_label.text() != "—":
            # Figures belong to the previous directory; do not present them as current.
            self.set_store_statistics(None, None)
            self.set_unreferenced([], 0)
            self.status_label.setText(
                "The preview directory changed. Scan the preview store to refresh the figures."
            )
        self._refresh_free_space()

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
        self.total_storage_label.setText(
            f"{format_size(int(statistics.total_bytes))} "
            f"({int(statistics.total_bytes):,} bytes)"
        )
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
            self.status_label.setText(
                f"Preview store scanned: {int(statistics.image_count):,} image and "
                f"{int(statistics.video_count):,} video previews, "
                f"{format_size(int(statistics.total_bytes))} in total."
            )

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
                table.setItem(row, 3, _readonly_item(entry.sha256, entry.sha256))
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

    def _refresh_unreferenced_count(self) -> None:
        shown = len(self._entries)
        total = self._total_found
        if getattr(self, "_partial", False):
            # A cancelled comparison must never read as "nothing is unreferenced".
            self.unreferenced_count_label.setText(
                f"The comparison was cancelled before it finished: {shown:,} preview(s) not "
                "referenced by this catalogue were found so far. Run Show Unreferenced "
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
        self.open_folder_button.setEnabled(not busy and has_root)
        self.select_all_button.setEnabled(not busy)
        self.select_none_button.setEnabled(not busy)
        self.cancel_button.setEnabled(bool(busy))
        if busy:
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setVisible(False)
        if message:
            self.status_label.setText(message)
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
            "No preview storage directory is configured in Settings > Preferences.",
        )
    try:
        settings.image.validate()
        settings.video.validate()
    except PreviewConfigError as exc:
        raise PreviewError(STAGE_CONFIGURATION, str(exc)) from exc
    return PreviewCache(root, settings.image, settings.video)


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
    """List previews under the current profiles whose SHA-256 this catalogue does not record.

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
            # Read-only: the catalogue is never modified. The SHA-256 lookup is a
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
                            if not db.content_hash_referenced(bytes.fromhex(entry.sha256)):
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
