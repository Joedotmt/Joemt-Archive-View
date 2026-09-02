from __future__ import annotations

import functools
import hashlib
import os
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jvvv import preview_ui  # noqa: E402
from jvvv.database import Database, utc_now  # noqa: E402
from jvvv.preview_cache import (  # noqa: E402
    PreviewCache,
    PreviewEntry,
    PreviewFailure,
    PreviewStoreStatistics,
    ProfileStatistics,
)
from jvvv.preview_config import (  # noqa: E402
    BACKUP_POLICY_TEXT,
    ROOT_CHANGE_WARNING_TEXT,
    STORAGE_TRADEOFF_TEXT,
    VIDEO_PRESETS,
    ImagePreviewProfile,
    PreviewSettings,
    VideoPreviewProfile,
)
from jvvv.preview_service import (  # noqa: E402
    PreviewStatistics,
    PreviewValidationReport,
    ValidationStep,
    validate_preview_configuration,
)
from jvvv.preview_ui import (  # noqa: E402
    FAILURE_COLUMNS,
    MAX_UNREFERENCED_ROWS,
    UNREFERENCED_COLUMNS,
    DeletePreviewsWorker,
    OfflinePreviewSettingsWidget,
    PreviewCacheDialog,
    PreviewFailuresDialog,
    PreviewStoreStatisticsWorker,
    PreviewValidationReportDialog,
    UnreferencedPreviewWorker,
    preview_summary_text,
)
from jvvv.video_preview import FfmpegCapabilities  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def sha_hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def enabled_settings(tmp_path: Path, **overrides: object) -> PreviewSettings:
    values: dict[str, object] = {
        "enabled": True,
        "root_directory": str(tmp_path / "previews"),
    }
    values.update(overrides)
    return PreviewSettings(**values)  # type: ignore[arg-type]


def passing_report(settings: PreviewSettings, include_encode_tests: bool = True) -> PreviewValidationReport:
    steps = (
        ValidationStep("configuration", "Configuration values", True, "ok"),
        ValidationStep("preview-root", "Preview storage directory", True, "writable"),
        ValidationStep("image-backend", "Image preview backend", True, "Qt"),
        ValidationStep("image-test", "Image preview test encode", True, "ok"),
        ValidationStep("ffmpeg-found", "FFmpeg executable", True, "C:/fake/ffmpeg.exe"),
        ValidationStep("ffmpeg-version", "FFmpeg version", True, "ffmpeg version 6.0"),
        ValidationStep("ffmpeg-encoder", "H.264 encoder (libx264)", True, "available"),
        ValidationStep("video-test", "Video preview test encode", True, "ok"),
    )
    return PreviewValidationReport(
        passed=True,
        steps=steps,
        settings=settings,
        root=settings.root_directory,
        free_bytes=5_400_000_000_000,
        total_bytes=8_000_000_000_000,
        ffmpeg_path="C:/fake/ffmpeg.exe",
        ffmpeg_version="ffmpeg version 6.0",
        encoder_available=True,
        image_backend="Qt image reader/writer",
        image_profile_id=settings.image.profile_id,
        video_profile_id=settings.video.profile_id,
        include_encode_tests=include_encode_tests,
    )


FAILED_STEP_LABEL = "FFmpeg executable"
FAILED_STEP_DETAIL = "FFmpeg could not be found on PATH."


def failing_report(settings: PreviewSettings, include_encode_tests: bool = True) -> PreviewValidationReport:
    steps = (
        ValidationStep("configuration", "Configuration values", True, "ok"),
        ValidationStep("preview-root", "Preview storage directory", True, "writable"),
        ValidationStep("image-backend", "Image preview backend", True, "Qt"),
        ValidationStep("ffmpeg-found", FAILED_STEP_LABEL, False, FAILED_STEP_DETAIL, "ffmpeg-start"),
        ValidationStep("ffmpeg-version", "FFmpeg version", False, "Not run", None, True),
    )
    return PreviewValidationReport(
        passed=False,
        steps=steps,
        settings=settings,
        root=settings.root_directory,
        free_bytes=None,
        total_bytes=None,
        ffmpeg_path=None,
        ffmpeg_version=None,
        encoder_available=None,
        image_backend="Qt image reader/writer",
        image_profile_id=settings.image.profile_id,
        video_profile_id=settings.video.profile_id,
        include_encode_tests=include_encode_tests,
    )


class RecordingValidator:
    """Injected validator that records calls and returns scripted outcomes."""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = list(outcomes) or [True]
        self.calls: list[tuple[PreviewSettings, dict[str, object]]] = []

    def __call__(self, settings: PreviewSettings, **kwargs: object) -> PreviewValidationReport:
        self.calls.append((settings, dict(kwargs)))
        passed = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        include = bool(kwargs.get("include_encode_tests", True))
        return passing_report(settings, include) if passed else failing_report(settings, include)


class RecordingErrors:
    def __init__(self) -> None:
        self.shown: list[tuple[object, str, str]] = []

    def __call__(self, parent, title: str, text: str) -> None:
        self.shown.append((parent, title, text))


def make_widget(settings: PreviewSettings, validator=None, errors=None) -> OfflinePreviewSettingsWidget:
    return OfflinePreviewSettingsWidget(
        settings,
        validator=validator or RecordingValidator(True),
        show_error=errors or RecordingErrors(),
    )


def fake_real_validator(*, find_ffmpeg: bool = True):
    """The real validate_preview_configuration with FFmpeg replaced by fakes."""

    def finder(explicit):
        return "C:/fake/ffmpeg.exe" if find_ffmpeg else None

    def prober(path):
        return FfmpegCapabilities(path=path, version="ffmpeg version 6.0-fake", encoders=frozenset({"libx264"}))

    def video_tester(path, cache):
        return "Encoded a fake 64x48 test video"

    return functools.partial(
        validate_preview_configuration,
        ffmpeg_finder=finder,
        ffmpeg_prober=prober,
        video_tester=video_tester,
    )


def write_preview(cache: PreviewCache, media_kind: str, digest: str, size: int, profile_id: str | None = None) -> Path:
    path = cache.preview_path(media_kind, digest, profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# module hygiene
# ---------------------------------------------------------------------------


def test_import_does_not_load_qtmultimedia():
    assert preview_ui is not None
    loaded = [name for name in sys.modules if name.startswith("PySide6.QtMultimedia")]
    assert loaded == []


def test_preview_summary_text_wraps_statistics():
    statistics = PreviewStatistics(image_generated=3, video_failed=1)
    text = preview_summary_text(statistics, "E:/Previews")
    assert text == statistics.summary_text("E:/Previews")
    assert "Offline Preview Summary" in text
    assert "E:/Previews" in text


# ---------------------------------------------------------------------------
# settings widget (spec §1, §2, §3, §34, §35, §37, §42, §49)
# ---------------------------------------------------------------------------


def test_checkbox_defaults_off_for_default_settings(app):
    validator = RecordingValidator(True)
    widget = make_widget(PreviewSettings(), validator)

    assert not widget.enable_check.isChecked()
    assert widget.enable_check.text() == "Generate offline previews while scanning"
    assert widget.last_known_good is None
    assert validator.calls == []
    assert "disabled" in widget.status_label.text().lower()
    assert widget.settings() == PreviewSettings()
    assert widget.image_profile_label.text() == "Current image profile: jpeg-max1600-q82"
    assert widget.video_profile_label.text() == "Current video profile: h264-1fps-240p-crf35-veryfast"


def test_enabling_runs_validator_once_and_success_keeps_box_checked(app, tmp_path):
    validator = RecordingValidator(True)
    errors = RecordingErrors()
    settings = PreviewSettings(root_directory=str(tmp_path / "root"))
    widget = make_widget(settings, validator, errors)
    validated: list[PreviewSettings] = []
    widget.validated.connect(validated.append)

    widget.enable_check.setChecked(True)

    assert len(validator.calls) == 1
    called_settings, kwargs = validator.calls[0]
    assert called_settings.enabled is True
    assert called_settings.root_directory == str(tmp_path / "root")
    assert kwargs == {"include_encode_tests": True}
    assert widget.enable_check.isChecked()
    assert validated == [settings.with_enabled(True)]
    assert validated[0].enabled is True
    assert widget.last_known_good == settings.with_enabled(True)
    assert "enabled" in widget.status_label.text().lower()
    assert errors.shown == []


def test_failed_validation_unchecks_box_and_names_failed_step(app, tmp_path):
    validator = RecordingValidator(False)
    errors = RecordingErrors()
    widget = make_widget(PreviewSettings(root_directory=str(tmp_path / "root")), validator, errors)
    validated: list[PreviewSettings] = []
    widget.validated.connect(validated.append)

    widget.enable_check.setChecked(True)

    assert len(validator.calls) == 1
    assert not widget.enable_check.isChecked()
    assert widget.settings().enabled is False
    assert validated == []
    assert widget.last_known_good is None
    assert len(errors.shown) == 1
    parent, title, text = errors.shown[0]
    assert parent is widget
    assert title == "Offline Previews"
    assert text.startswith("Offline previews could not be enabled.")
    assert FAILED_STEP_LABEL in text
    assert FAILED_STEP_DETAIL in text
    assert "fail" in widget.status_label.text().lower()
    assert FAILED_STEP_LABEL in widget.status_label.text()


def test_unchecking_reports_disabled_without_validation(app, tmp_path):
    validator = RecordingValidator(True)
    widget = make_widget(enabled_settings(tmp_path), validator)
    assert widget.enable_check.isChecked()

    widget.enable_check.setChecked(False)

    assert validator.calls == []
    assert widget.status_label.text() == "Offline previews disabled."
    assert widget.settings().enabled is False
    # The last configuration proven to work is remembered for spec §35.
    assert widget.last_known_good == enabled_settings(tmp_path)


def test_real_validator_with_fake_ffmpeg_enables_on_tmp_root_and_cleans_up(app, tmp_path):
    root = tmp_path / "store"
    errors = RecordingErrors()
    widget = make_widget(PreviewSettings(root_directory=str(root)), fake_real_validator(), errors)
    validated: list[PreviewSettings] = []
    widget.validated.connect(validated.append)

    widget.enable_check.setChecked(True)

    assert errors.shown == []
    assert widget.enable_check.isChecked()
    assert len(validated) == 1 and validated[0].enabled
    assert root.is_dir()
    assert list(root.rglob("*")) == []  # root check + image test files were removed


def test_real_validator_without_ffmpeg_rolls_the_checkbox_back(app, tmp_path):
    root = tmp_path / "store"
    errors = RecordingErrors()
    widget = make_widget(
        PreviewSettings(root_directory=str(root)),
        fake_real_validator(find_ffmpeg=False),
        errors,
    )

    widget.enable_check.setChecked(True)

    assert not widget.enable_check.isChecked()
    assert len(errors.shown) == 1
    assert "FFmpeg executable failed." in errors.shown[0][2]
    assert "could not be found" in errors.shown[0][2]
    assert list(root.rglob("*")) == []


def test_set_settings_round_trips_every_value_without_validating(app, tmp_path):
    validator = RecordingValidator(True)
    widget = make_widget(PreviewSettings(), validator)
    settings = PreviewSettings(
        enabled=True,
        root_directory=str(tmp_path / "Archive Proxy Media"),
        ffmpeg_path=str(tmp_path / "tools" / "ffmpeg.exe"),
        image=ImagePreviewProfile(max_dimension=4096, jpeg_quality=92),
        video=VideoPreviewProfile(fps=2.5, max_height=720, crf=28, preset="fast"),
    )
    changes: list[int] = []
    widget.settings_changed.connect(lambda: changes.append(1))

    widget.set_settings(settings)

    assert widget.settings() == settings
    assert widget.enable_check.isChecked()
    assert widget.root_edit.text() == settings.root_directory
    assert widget.ffmpeg_edit.text() == settings.ffmpeg_path
    assert widget.image_max_dimension_spin.value() == 4096
    assert widget.image_quality_spin.value() == 92
    assert widget.video_fps_spin.value() == 2.5
    assert widget.video_max_height_spin.value() == 720
    assert widget.video_crf_spin.value() == 28
    assert widget.video_preset_combo.currentData() == "fast"
    assert validator.calls == []
    assert changes == []
    assert widget.last_known_good == settings
    assert widget.image_profile_label.text() == "Current image profile: jpeg-max4096-q92"
    assert widget.video_profile_label.text() == "Current video profile: h264-2.5fps-720p-crf28-fast"


def test_spin_boxes_clamp_to_allowed_ranges(app):
    widget = make_widget(PreviewSettings())

    widget.image_max_dimension_spin.setValue(10)
    assert widget.image_max_dimension_spin.value() == 320
    widget.image_max_dimension_spin.setValue(9999)
    assert widget.image_max_dimension_spin.value() == 8192
    assert widget.image_max_dimension_spin.suffix() == " px"

    widget.image_quality_spin.setValue(1)
    assert widget.image_quality_spin.value() == 40
    widget.image_quality_spin.setValue(150)
    assert widget.image_quality_spin.value() == 100

    widget.video_fps_spin.setValue(0)
    assert widget.video_fps_spin.value() == pytest.approx(0.1)
    widget.video_fps_spin.setValue(20)
    assert widget.video_fps_spin.value() == pytest.approx(10.0)
    # Three decimals match preview_config.format_fps, so a persisted 0.333 fps
    # survives opening and accepting Preferences unchanged.
    assert widget.video_fps_spin.decimals() == 3
    widget.video_fps_spin.setValue(0.333)
    assert widget.settings().video.profile_id.startswith("h264-0.333fps-")

    widget.video_max_height_spin.setValue(10)
    assert widget.video_max_height_spin.value() == 120
    widget.video_max_height_spin.setValue(5000)
    assert widget.video_max_height_spin.value() == 2160
    assert widget.video_max_height_spin.suffix() == " px"

    widget.video_crf_spin.setValue(5)
    assert widget.video_crf_spin.value() == 18
    widget.video_crf_spin.setValue(60)
    assert widget.video_crf_spin.value() == 45

    # Whatever the controls show is always a valid configuration.
    widget.settings().validate(require_root=False)


def test_preset_combo_lists_presets_in_order_and_stores_stable_values(app):
    widget = make_widget(PreviewSettings())
    combo = widget.video_preset_combo

    assert combo.count() == len(VIDEO_PRESETS)
    assert [combo.itemData(index) for index in range(combo.count())] == list(VIDEO_PRESETS)
    for index, preset in enumerate(VIDEO_PRESETS):
        assert combo.itemText(index).startswith(f"{preset} — ")
    assert combo.currentData() == "veryfast"

    combo.setCurrentIndex(combo.findData("slow"))
    assert widget.settings().video.preset == "slow"
    assert widget.video_profile_label.text().endswith("-slow")


def test_profile_labels_update_live_on_every_output_affecting_control(app):
    widget = make_widget(PreviewSettings())
    changes: list[int] = []
    widget.settings_changed.connect(lambda: changes.append(1))

    widget.image_max_dimension_spin.setValue(1024)
    assert widget.image_profile_label.text() == "Current image profile: jpeg-max1024-q82"
    widget.image_quality_spin.setValue(75)
    assert widget.image_profile_label.text() == "Current image profile: jpeg-max1024-q75"

    widget.video_fps_spin.setValue(2.0)
    assert widget.video_profile_label.text() == "Current video profile: h264-2fps-240p-crf35-veryfast"
    widget.video_max_height_spin.setValue(720)
    assert widget.video_profile_label.text() == "Current video profile: h264-2fps-720p-crf35-veryfast"
    widget.video_crf_spin.setValue(28)
    assert widget.video_profile_label.text() == "Current video profile: h264-2fps-720p-crf28-veryfast"
    widget.video_preset_combo.setCurrentIndex(widget.video_preset_combo.findData("fast"))
    assert widget.video_profile_label.text() == "Current video profile: h264-2fps-720p-crf28-fast"
    widget.video_fps_spin.setValue(0.5)
    assert widget.video_profile_label.text() == "Current video profile: h264-0.5fps-720p-crf28-fast"

    assert len(changes) == 7


def test_validate_for_save_when_disabled_returns_current_without_validation(app, tmp_path):
    validator = RecordingValidator(True)
    widget = make_widget(PreviewSettings(root_directory=str(tmp_path)), validator)
    widget.video_crf_spin.setValue(30)

    result, report = widget.validate_for_save()

    assert report is None
    assert result == widget.settings()
    assert result.enabled is False
    assert result.video.crf == 30
    assert validator.calls == []


def test_validate_for_save_enabled_unchanged_returns_current_without_validation(app, tmp_path):
    validator = RecordingValidator(True)
    settings = enabled_settings(tmp_path)
    widget = make_widget(settings, validator)

    result, report = widget.validate_for_save()

    assert (result, report) == (settings, None)
    assert validator.calls == []


def test_validate_for_save_enabled_changed_failure_reverts_to_last_known_good(app, tmp_path):
    validator = RecordingValidator(False)
    settings = enabled_settings(tmp_path)
    widget = make_widget(settings, validator)
    widget.video_crf_spin.setValue(23)
    widget.image_max_dimension_spin.setValue(4096)
    assert widget.settings() != settings

    result, report = widget.validate_for_save()

    assert len(validator.calls) == 1
    assert validator.calls[0][0].video.crf == 23
    assert validator.calls[0][0].enabled is True
    assert report is not None and not report.passed
    assert result == settings
    assert result == widget.last_known_good
    assert widget.settings() == settings
    assert widget.video_crf_spin.value() == 35
    assert widget.image_max_dimension_spin.value() == 1600
    assert widget.enable_check.isChecked()
    assert "restored" in widget.status_label.text()
    assert FAILED_STEP_LABEL in widget.status_label.text()
    assert report.failure_summary(heading="Validation failed.").startswith("Validation failed.")


def test_validate_for_save_enabled_changed_success_updates_last_known_good(app, tmp_path):
    validator = RecordingValidator(True)
    settings = enabled_settings(tmp_path)
    widget = make_widget(settings, validator)
    widget.root_edit.setText(str(tmp_path / "other"))
    changed = widget.settings()

    result, report = widget.validate_for_save()

    assert len(validator.calls) == 1
    assert report is not None and report.passed
    assert result == changed
    assert widget.last_known_good == changed
    assert widget.settings() == changed

    # A second save with nothing new does not validate again.
    assert widget.validate_for_save() == (changed, None)
    assert len(validator.calls) == 1


def test_validate_for_save_enabled_without_known_good_disables_on_failure(app, tmp_path):
    validator = RecordingValidator(False)
    widget = make_widget(PreviewSettings(root_directory=str(tmp_path)), validator)
    widget.enable_check.blockSignals(True)
    widget.enable_check.setChecked(True)
    widget.enable_check.blockSignals(False)
    assert widget.last_known_good is None

    result, report = widget.validate_for_save()

    assert report is not None and not report.passed
    assert result.enabled is False
    assert not widget.enable_check.isChecked()


def test_test_button_runs_full_validation_and_shows_report_without_changing_state(app, tmp_path, monkeypatch):
    shown: list[PreviewValidationReport] = []

    def fake_exec(self):
        shown.append(self.report)
        return 0

    monkeypatch.setattr(PreviewValidationReportDialog, "exec", fake_exec)
    validator = RecordingValidator(True)
    widget = make_widget(PreviewSettings(root_directory=str(tmp_path)), validator)
    validated: list[PreviewSettings] = []
    widget.validated.connect(validated.append)

    widget.test_button.click()

    assert len(validator.calls) == 1
    called_settings, kwargs = validator.calls[0]
    assert kwargs == {"include_encode_tests": True}
    assert called_settings.enabled is True  # validated as if enabled
    assert not widget.enable_check.isChecked()  # ...but the setting is untouched
    assert widget.settings().enabled is False
    assert validated == []
    assert widget.last_known_good is None
    assert len(shown) == 1 and shown[0].passed
    assert "PASS" in widget.status_label.text()

    # Enabled state is likewise preserved when the test fails.
    failing = RecordingValidator(False)
    enabled_widget = make_widget(enabled_settings(tmp_path), failing)
    enabled_widget.test_button.click()
    assert enabled_widget.enable_check.isChecked()
    assert len(shown) == 2 and not shown[1].passed
    assert "FAIL" in enabled_widget.status_label.text()
    assert FAILED_STEP_LABEL in enabled_widget.status_label.text()


def test_browse_root_and_ffmpeg_use_injected_choosers(app, tmp_path):
    widget = make_widget(PreviewSettings())
    chooser_calls: list[tuple[object, str, str]] = []

    def choose_directory(parent, title, start):
        chooser_calls.append((parent, title, start))
        return str(tmp_path / "chosen")

    widget.directory_chooser = choose_directory
    widget.browse_root()
    assert widget.root_edit.text() == str(tmp_path / "chosen")
    assert chooser_calls[0][0] is widget
    assert "Preview Storage Directory" in chooser_calls[0][1]

    # Cancelling the dialog keeps the previous value.
    widget.directory_chooser = lambda parent, title, start: ""
    widget.browse_root()
    assert widget.root_edit.text() == str(tmp_path / "chosen")

    widget.file_chooser = lambda parent, title, start: str(tmp_path / "ffmpeg.exe")
    widget.browse_ffmpeg()
    assert widget.ffmpeg_edit.text() == str(tmp_path / "ffmpeg.exe")
    assert widget.settings().ffmpeg_path == str(tmp_path / "ffmpeg.exe")
    widget.file_chooser = lambda parent, title, start: ""
    widget.browse_ffmpeg()
    assert widget.ffmpeg_edit.text() == str(tmp_path / "ffmpeg.exe")

    widget.browse_button.click()  # the real button routes through the injected chooser too
    assert len(chooser_calls) == 1  # the injected chooser was replaced; ensure no crash


def test_storage_note_contains_tradeoff_root_change_and_backup_policy_text(app):
    widget = make_widget(PreviewSettings())
    text = widget.storage_note.text()

    assert widget.storage_note.wordWrap()
    assert (
        "Higher image dimensions, higher JPEG quality, higher video FPS, higher video "
        "resolution, and lower CRF values use more storage." in text
    )
    assert STORAGE_TRADEOFF_TEXT in text
    assert "does not move existing previews" in text
    assert ROOT_CHANGE_WARNING_TEXT in text
    assert BACKUP_POLICY_TEXT in text
    assert "JVVV catalogue backups do not include offline preview files." in text


def test_widget_never_instantiates_multimedia_widgets(app, tmp_path):
    make_widget(enabled_settings(tmp_path))
    PreviewCacheDialog(enabled_settings(tmp_path))
    PreviewFailuresDialog([])
    loaded = [name for name in sys.modules if name.startswith("PySide6.QtMultimedia")]
    assert loaded == []


# ---------------------------------------------------------------------------
# validation report dialog (spec §3)
# ---------------------------------------------------------------------------


def test_validation_report_dialog_shows_pass_or_fail_and_full_report(app, tmp_path):
    settings = enabled_settings(tmp_path)
    passing = PreviewValidationReportDialog(passing_report(settings))
    assert passing.windowTitle() == "Preview Configuration Test - PASS"
    assert passing.report_edit.isReadOnly()
    assert passing.report_edit.toPlainText() == passing_report(settings).report_text()
    assert "Overall: PASS" in passing.report_edit.toPlainText()
    assert "5.4 TB" in passing.report_edit.toPlainText()

    failing = PreviewValidationReportDialog(failing_report(settings))
    assert failing.windowTitle() == "Preview Configuration Test - FAIL"
    text = failing.report_edit.toPlainText()
    assert f"[FAIL] {FAILED_STEP_LABEL}" in text
    assert "[Not run] FFmpeg version" in text
    assert "Overall: FAIL" in text
    assert failing.copy_button.text() == "Copy"
    failing.copy_button.click()  # must not raise offscreen


# ---------------------------------------------------------------------------
# failure list (spec §15)
# ---------------------------------------------------------------------------


def make_failure(index: int) -> PreviewFailure:
    video = index % 2 == 0
    return PreviewFailure(
        source_name=f"camera{index:04d}.mov" if video else f"photo{index:04d}.tif",
        relative_path=(f"Videos\\camera{index:04d}.mov" if video else f"Photos\\photo{index:04d}.tif"),
        volume_id=7,
        volume_label="AID-007 - Archive",
        media_kind="video" if video else "image",
        sha256=sha_hex(f"failure-{index}"),
        preview_path=f"E:\\JVVV Previews\\{index}",
        profile_id="h264-1fps-240p-crf35-veryfast" if video else "jpeg-max1600-q82",
        stage="ffmpeg-exit" if video else "image-decode",
        message="FFmpeg exited with code 1." if video else "Image decoder could not read the file.",
        detail="Invalid data found when processing input." if video else "",
    )


def test_failures_dialog_lists_every_failure_without_truncation(app):
    failures = [make_failure(index) for index in range(2500)]

    dialog = PreviewFailuresDialog(failures, storage_unavailable_reason="No space left on device.")

    assert dialog.table.rowCount() == 2500
    assert dialog.table.columnCount() == len(FAILURE_COLUMNS)
    headers = [dialog.table.horizontalHeaderItem(i).text() for i in range(dialog.table.columnCount())]
    assert headers == ["#", "Path", "Type", "Profile", "Stage", "Error", "Detail"]
    assert "2,500" in dialog.heading_label.text()
    assert "preview failure(s)" in dialog.heading_label.text()

    first = [dialog.table.item(0, column).text() for column in range(7)]
    assert first == [
        "1",
        "Videos\\camera0000.mov",
        "Video",
        "h264-1fps-240p-crf35-veryfast",
        "ffmpeg-exit",
        "FFmpeg exited with code 1.",
        "Invalid data found when processing input.",
    ]
    second = [dialog.table.item(1, column).text() for column in range(7)]
    assert second == [
        "2",
        "Photos\\photo0001.tif",
        "Image",
        "jpeg-max1600-q82",
        "image-decode",
        "Image decoder could not read the file.",
        "",
    ]
    assert dialog.table.item(2499, 0).text() == "2500"
    assert dialog.table.item(2499, 1).text() == "Photos\\photo2499.tif"
    assert "AID-007 - Archive" in dialog.table.item(0, 1).toolTip()

    assert dialog.storage_label.isVisibleTo(dialog)
    assert "No space left on device." in dialog.storage_label.text()
    assert dialog.storage_label.objectName() == "offlineNotice"

    copy_text = dialog.copy_text
    assert copy_text.startswith("Preview Failures")
    assert "1. Videos\\camera0000.mov" in copy_text
    assert "   Type: Video" in copy_text
    assert "   Profile: h264-1fps-240p-crf35-veryfast" in copy_text
    assert "   Error: FFmpeg exited with code 1." in copy_text
    assert "   Detail: Invalid data found when processing input." in copy_text
    assert "2. Photos\\photo0001.tif" in copy_text
    assert "2500. Photos\\photo2499.tif" in copy_text
    assert "No space left on device." in copy_text
    # Image failure without detail has no Detail line.
    block = copy_text.split("2. Photos\\photo0001.tif", 1)[1].split("\n3. ", 1)[0]
    assert "Detail:" not in block
    assert dialog.copy_button.text() == "Copy All"


def test_failures_dialog_without_storage_reason_hides_notice(app):
    dialog = PreviewFailuresDialog([make_failure(1)])
    assert dialog.heading_label.text() == "1 preview failure(s)"
    assert not dialog.storage_label.isVisibleTo(dialog)
    assert dialog.table.rowCount() == 1


# ---------------------------------------------------------------------------
# preview cache manager (spec §22)
# ---------------------------------------------------------------------------


def test_cache_dialog_labels_from_settings_and_64_bit_statistics(app, tmp_path):
    settings = PreviewSettings(
        root_directory=str(tmp_path / "JVVV Previews"),
        image=ImagePreviewProfile(max_dimension=4096, jpeg_quality=90),
        video=VideoPreviewProfile(fps=1.0, max_height=720, crf=30, preset="fast"),
    )
    dialog = PreviewCacheDialog(PreviewSettings())
    assert dialog.root_label.text() == "Not configured"
    assert dialog.image_count_label.text() == "Not scanned"
    assert dialog.free_space_label.text() == "Unknown"
    assert not dialog.scan_button.isEnabled()

    dialog.set_settings(settings)
    assert dialog.root_label.text() == str(tmp_path / "JVVV Previews")
    # The directory does not exist yet, so free space cannot be read.
    assert dialog.free_space_label.text() == "Unknown"
    existing_root = tmp_path / "existing-root"
    existing_root.mkdir()
    dialog.set_settings(PreviewSettings(root_directory=str(existing_root)))
    # An existing directory reports its free space immediately, before any scan.
    assert dialog.free_space_label.text() not in {"Unknown", "—"}
    dialog.set_settings(settings)
    assert dialog.image_profile_label.text() == "jpeg-max4096-q90"
    assert dialog.video_profile_label.text() == "h264-1fps-720p-crf30-fast"
    assert dialog.scan_button.isEnabled()
    assert dialog.unreferenced_button.isEnabled()

    seven_tb = 7 * 10**12
    statistics = PreviewStoreStatistics(
        image_count=128_401,
        video_count=18_420,
        image_bytes=2 * 10**12,
        video_bytes=5 * 10**12,
        total_bytes=seven_tb,
        temporary_files=3,
        profiles={
            ("image", "jpeg-max4096-q90"): ProfileStatistics(128_401, 2 * 10**12),
            ("video", "h264-1fps-720p-crf30-fast"): ProfileStatistics(18_000, 4 * 10**12),
            ("video", "h264-1fps-240p-crf35-veryfast"): ProfileStatistics(420, 10**12),
        },
    )
    dialog.set_store_statistics(statistics, 5_400_000_000_000)

    assert dialog.image_count_label.text() == "128,401"
    assert dialog.video_count_label.text() == "18,420"
    assert dialog.total_storage_label.text().startswith("7.0 TB")
    assert "7,000,000,000,000 bytes" in dialog.total_storage_label.text()
    assert dialog.temporary_label.text() == "3"
    assert dialog.free_space_label.text() == "5.4 TB"
    assert "h264-1fps-240p-crf35-veryfast" in dialog.profiles_label.text()
    assert "(current)" in dialog.profiles_label.text()
    assert "128,401" in dialog.status_label.text()

    dialog.set_store_statistics(None, None)
    assert dialog.image_count_label.text() == "Not scanned"
    # Free space is a cheap disk query, so a value that is already known is kept
    # rather than reverting to "Unknown" until a full store walk finishes.
    assert dialog.free_space_label.text() == "5.4 TB"

    cancelled = PreviewStoreStatistics(1, 0, 10, 0, 10, 0, {}, cancelled=True)
    dialog.set_store_statistics(cancelled, 0)
    assert "cancelled" in dialog.status_label.text().lower()
    assert dialog.free_space_label.text() == "0 B"


def test_cache_dialog_buttons_emit_requests(app, tmp_path):
    dialog = PreviewCacheDialog(enabled_settings(tmp_path))
    received: list[str] = []
    dialog.scan_requested.connect(lambda: received.append("scan"))
    dialog.unreferenced_requested.connect(lambda: received.append("unreferenced"))
    dialog.open_folder_requested.connect(lambda: received.append("open"))
    dialog.cancel_requested.connect(lambda: received.append("cancel"))

    dialog.scan_button.click()
    dialog.unreferenced_button.click()
    dialog.open_folder_button.click()
    assert received == ["scan", "unreferenced", "open"]

    assert dialog.scan_button.text() == "Scan Preview Store"
    assert dialog.unreferenced_button.text() == "Show Unreferenced Previews"
    assert dialog.open_folder_button.text() == "Open Preview Folder"
    assert dialog.delete_button.text() == "Delete Selected Unreferenced Previews"

    dialog.set_busy(True, "Scanning the preview store…")
    assert not dialog.scan_button.isEnabled()
    assert not dialog.unreferenced_button.isEnabled()
    assert dialog.cancel_button.isEnabled()
    assert dialog.progress.minimum() == 0 and dialog.progress.maximum() == 0
    assert dialog.status_label.text() == "Scanning the preview store…"
    dialog.cancel_button.click()
    assert received[-1] == "cancel"

    dialog.set_progress(12_345, "Scanning E:\\previews\\images")
    assert "12,345" in dialog.status_label.text()
    assert "images" in dialog.status_label.text()

    dialog.set_busy(False, "Done.")
    assert dialog.scan_button.isEnabled()
    assert not dialog.cancel_button.isEnabled()
    assert dialog.progress.maximum() == 1
    assert dialog.status_label.text() == "Done."


def test_cache_dialog_unreferenced_table_and_delete_flow(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    entries = [
        PreviewEntry(cache.preview_path("image", sha_hex("a")), "image", settings.image.profile_id, sha_hex("a"), 1_500),
        PreviewEntry(cache.preview_path("image", sha_hex("b")), "image", settings.image.profile_id, sha_hex("b"), 2_500),
        PreviewEntry(cache.preview_path("video", sha_hex("c")), "video", settings.video.profile_id, sha_hex("c"), 3 * 10**9),
    ]
    dialog = PreviewCacheDialog(settings)
    deletions: list[list[str]] = []
    dialog.delete_requested.connect(deletions.append)

    assert "Not referenced by this catalogue" in dialog.unreferenced_heading.text()
    note = dialog.unreferenced_note.text().lower()
    assert "another catalogue" in note
    assert "same preview directory" in note
    assert "nothing is deleted automatically" in note
    headers = [dialog.unreferenced_table.horizontalHeaderItem(i).text() for i in range(dialog.unreferenced_table.columnCount())]
    assert headers == list(UNREFERENCED_COLUMNS)

    dialog.set_unreferenced(entries, 3)
    table = dialog.unreferenced_table
    assert table.rowCount() == 3
    assert table.item(0, 1).text() == "Image"
    assert table.item(2, 1).text() == "Video"
    assert table.item(0, 2).text() == settings.image.profile_id
    assert table.item(0, 3).text() == sha_hex("a")
    assert table.item(2, 4).text() == "3.0 GB"
    assert table.item(0, 5).text() == str(entries[0].path)
    assert table.item(0, 0).checkState() == Qt.CheckState.Unchecked
    assert "3 preview(s)" in dialog.unreferenced_count_label.text()
    assert not dialog.delete_button.isEnabled()
    assert dialog.selected_unreferenced_paths() == []

    # on_delete_clicked with nothing selected does nothing.
    dialog.confirm = lambda *args: True
    dialog.on_delete_clicked()
    assert deletions == []

    table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert dialog.delete_button.isEnabled()
    assert dialog.selected_unreferenced_paths() == [str(entries[0].path)]

    confirmations: list[tuple[str, str]] = []

    def refuse(parent, title, text):
        confirmations.append((title, text))
        return False

    dialog.confirm = refuse
    dialog.on_delete_clicked()
    assert deletions == []
    assert len(confirmations) == 1
    assert "not referenced by this catalogue" in confirmations[0][1].lower()
    assert "another catalogue" in confirmations[0][1].lower()

    dialog.confirm = lambda *args: True
    dialog.delete_button.click()
    assert deletions == [[str(entries[0].path)]]

    dialog.remove_entries([str(entries[0].path)])
    assert table.rowCount() == 2
    assert table.item(0, 3).text() == sha_hex("b")
    assert not dialog.delete_button.isEnabled()
    assert "2 preview(s)" in dialog.unreferenced_count_label.text()

    dialog.select_all_button.click()
    assert sorted(dialog.selected_unreferenced_paths()) == sorted(str(entry.path) for entry in entries[1:])
    assert dialog.delete_button.isEnabled()
    dialog.select_none_button.click()
    assert dialog.selected_unreferenced_paths() == []
    assert not dialog.delete_button.isEnabled()

    # A capped list reports how many were found in total.
    dialog.set_unreferenced(entries[:1], 25_000)
    assert "Showing 1 of 25,000" in dialog.unreferenced_count_label.text()
    assert f"{MAX_UNREFERENCED_ROWS:,}" in dialog.unreferenced_count_label.text()

    dialog.set_unreferenced([], 0)
    assert table.rowCount() == 0
    assert "no previews" in dialog.unreferenced_count_label.text().lower()


def test_cache_dialog_forgets_figures_when_root_changes(app, tmp_path):
    settings = enabled_settings(tmp_path)
    dialog = PreviewCacheDialog(settings)
    dialog.set_store_statistics(PreviewStoreStatistics(5, 1, 500, 100, 600, 0, {}), 10**9)
    assert dialog.image_count_label.text() == "5"

    dialog.set_settings(settings)  # same root: figures stay
    assert dialog.image_count_label.text() == "5"

    dialog.set_settings(PreviewSettings(enabled=True, root_directory=str(tmp_path / "elsewhere")))
    assert dialog.image_count_label.text() == "Not scanned"
    assert dialog.root_label.text() == str(tmp_path / "elsewhere")
    assert "changed" in dialog.status_label.text().lower()


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------


def test_statistics_worker_counts_store_and_reports_free_space(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    write_preview(cache, "image", sha_hex("1"), 100)
    write_preview(cache, "image", sha_hex("2"), 200)
    write_preview(cache, "image", sha_hex("3"), 300)
    write_preview(cache, "image", sha_hex("old"), 50, profile_id="jpeg-max1024-q75")
    write_preview(cache, "video", sha_hex("4"), 1000)
    write_preview(cache, "video", sha_hex("5"), 2000)
    image_dir = cache.preview_path("image", sha_hex("1")).parent
    (image_dir / f".{sha_hex('1')}.jpg.tmp-deadbeef").write_bytes(b"partial")
    (image_dir / "readme.txt").write_text("ignored", encoding="utf-8")

    worker = PreviewStoreStatisticsWorker(settings)
    results: list[tuple[object, object]] = []
    failures: list[str] = []
    worker.finished.connect(lambda stats, free: results.append((stats, free)))
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert len(results) == 1
    statistics, free_bytes = results[0]
    assert isinstance(statistics, PreviewStoreStatistics)
    assert statistics.image_count == 4
    assert statistics.video_count == 2
    assert statistics.image_bytes == 650
    assert statistics.video_bytes == 3000
    assert statistics.total_bytes == 3650
    assert statistics.temporary_files == 1
    assert statistics.cancelled is False
    assert statistics.profiles[("image", settings.image.profile_id)] == ProfileStatistics(3, 600)
    assert statistics.profiles[("image", "jpeg-max1024-q75")] == ProfileStatistics(1, 50)
    assert isinstance(free_bytes, int) and free_bytes > 0


def test_statistics_worker_cancel_and_missing_root_are_explicit(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    write_preview(cache, "image", sha_hex("1"), 100)
    worker = PreviewStoreStatisticsWorker(settings)
    results: list[tuple[object, object]] = []
    worker.finished.connect(lambda stats, free: results.append((stats, free)))
    worker.cancel()
    worker.run()
    assert len(results) == 1
    assert results[0][0].cancelled is True

    unconfigured = PreviewStoreStatisticsWorker(PreviewSettings())
    failures: list[str] = []
    finished: list[object] = []
    unconfigured.failed.connect(failures.append)
    unconfigured.finished.connect(lambda *args: finished.append(args))
    unconfigured.run()
    assert finished == []
    assert len(failures) == 1
    assert "preview storage directory" in failures[0].lower()


def test_unreferenced_worker_lists_only_current_profile_previews_not_in_catalogue(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    referenced_image = sha_hex("referenced image")
    unreferenced_image = sha_hex("unreferenced image")
    other_profile_image = sha_hex("other profile")
    referenced_video = sha_hex("referenced video")
    unreferenced_video = sha_hex("unreferenced video")

    db_path = tmp_path / "catalogue.jvvv"
    db = Database(db_path)
    now = utc_now()
    with db.transaction():
        volume_id = db.create_volume("AID-001", str(tmp_path / "source"))
        folder_id = db.ensure_folder(volume_id, None, "", "", now)
        db.upsert_file(
            volume_id, folder_id, "photo.jpg", "photo.jpg", "jpg", 123, None, now,
            content_hash=bytes.fromhex(referenced_image), content_hash_algorithm="sha256",
        )
        db.upsert_file(
            volume_id, folder_id, "clip.mov", "clip.mov", "mov", 456, None, now,
            content_hash=bytes.fromhex(referenced_video), content_hash_algorithm="sha256",
        )
        # A non-media file sharing a hash must not count as a preview reference.
        db.upsert_file(
            volume_id, folder_id, "notes.txt", "notes.txt", "txt", 7, None, now,
            content_hash=bytes.fromhex(unreferenced_video), content_hash_algorithm="sha256",
        )
    db.close()

    write_preview(cache, "image", referenced_image, 100)
    unreferenced_path = write_preview(cache, "image", unreferenced_image, 200)
    write_preview(cache, "image", other_profile_image, 300, profile_id="jpeg-max1024-q75")
    write_preview(cache, "video", referenced_video, 1000)
    unreferenced_video_path = write_preview(cache, "video", unreferenced_video, 2000)

    worker = UnreferencedPreviewWorker(settings, db_path)
    results: list[tuple[list, int]] = []
    failures: list[str] = []
    worker.finished.connect(lambda entries, total: results.append((list(entries), total)))
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert len(results) == 1
    entries, total_found = results[0]
    assert total_found == 2
    assert [entry.sha256 for entry in entries] == [unreferenced_image, unreferenced_video]
    assert entries[0] == PreviewEntry(unreferenced_path, "image", settings.image.profile_id, unreferenced_image, 200)
    assert entries[1].path == unreferenced_video_path
    assert entries[1].media_kind == "video"
    assert entries[1].profile_id == settings.video.profile_id
    # The catalogue was opened read-only and closed again: it is still intact.
    reopened = Database(db_path, initialize=False, create=False, read_only=True)
    try:
        assert reopened.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3
    finally:
        reopened.close()


def test_unreferenced_worker_reports_missing_catalogue(app, tmp_path):
    settings = enabled_settings(tmp_path)
    settings.root_path.mkdir(parents=True)  # the store exists; only the catalogue is missing
    worker = UnreferencedPreviewWorker(settings, tmp_path / "missing.jvvv")
    failures: list[str] = []
    finished: list[object] = []
    worker.failed.connect(failures.append)
    worker.finished.connect(lambda *args: finished.append(args))

    worker.run()

    assert finished == []
    assert len(failures) == 1
    assert "missing.jvvv" in failures[0]


def test_delete_worker_deletes_previews_and_refuses_paths_outside_root(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    first = write_preview(cache, "image", sha_hex("delete me"), 100)
    second = write_preview(cache, "video", sha_hex("delete me too"), 100)
    outside = tmp_path / "elsewhere" / f"{sha_hex('outside')}.jpg"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"keep")
    not_a_preview = settings.root_path / "images" / "notes.txt"
    not_a_preview.write_text("keep", encoding="utf-8")

    worker = DeletePreviewsWorker(settings, [str(first), str(outside), str(second), str(not_a_preview)])
    results: list[tuple[int, list, list]] = []
    worker.finished.connect(lambda count, deleted, errors: results.append((count, list(deleted), list(errors))))

    worker.run()

    assert len(results) == 1
    count, deleted, errors = results[0]
    assert count == 2
    assert deleted == [str(first), str(second)]
    assert not first.exists()
    assert not second.exists()
    assert outside.exists()
    assert not_a_preview.exists()
    assert len(errors) == 2
    assert str(outside) in errors[0]
    assert "outside the preview storage directory" in errors[0]
    assert str(not_a_preview) in errors[1]
    assert "not a preview" in errors[1]


def test_delete_worker_cancel_stops_remaining_deletions(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    first = write_preview(cache, "image", sha_hex("one"), 10)
    second = write_preview(cache, "image", sha_hex("two"), 10)
    worker = DeletePreviewsWorker(settings, [str(first), str(second)])
    results: list[tuple[int, list, list]] = []
    worker.finished.connect(lambda count, deleted, errors: results.append((count, list(deleted), list(errors))))
    worker.cancel()

    worker.run()

    count, deleted, errors = results[0]
    assert count == 0
    assert deleted == []
    assert first.exists() and second.exists()
    assert len(errors) == 2
    assert "cancelled" in errors[0]


def test_cache_dialog_marks_a_cancelled_unreferenced_comparison_as_partial(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    dialog = PreviewCacheDialog(settings)
    entry = PreviewEntry(
        cache.preview_path("image", sha_hex("a")),
        "image",
        settings.image.profile_id,
        sha_hex("a"),
        1_500,
    )

    dialog.set_unreferenced([entry], 1, partial=True)
    text = dialog.unreferenced_count_label.text()
    assert "cancelled" in text.lower()
    assert "1 preview" in text
    assert dialog.unreferenced_table.rowCount() == 1

    # A cancelled comparison with nothing found so far must not claim that
    # nothing is unreferenced.
    dialog.set_unreferenced([], 0, partial=True)
    assert "cancelled" in dialog.unreferenced_count_label.text().lower()
    assert not dialog.unreferenced_count_label.text().startswith("No previews")

    dialog.set_unreferenced([], 0)
    assert dialog.unreferenced_count_label.text().startswith("No previews")


# ---------------------------------------------------------------------------
# Audit follow-ups (spec §22: a missing root is an error; stale lists are dropped)
# ---------------------------------------------------------------------------


def test_store_workers_report_a_missing_preview_root_instead_of_an_empty_store(app, tmp_path):
    settings = enabled_settings(tmp_path)
    assert not settings.root_path.exists()

    statistics_worker = PreviewStoreStatisticsWorker(settings)
    finished: list[object] = []
    failed: list[str] = []
    statistics_worker.finished.connect(lambda *args: finished.append(args))
    statistics_worker.failed.connect(failed.append)
    statistics_worker.run()
    assert finished == []
    assert len(failed) == 1
    assert "does not exist or is not reachable" in failed[0]
    assert str(settings.root_path) in failed[0]

    db_path = tmp_path / "catalogue.jvvv"
    Database(db_path).close()
    unreferenced_worker = UnreferencedPreviewWorker(settings, db_path)
    finished_lists: list[object] = []
    failed_lists: list[str] = []
    unreferenced_worker.finished.connect(lambda *args: finished_lists.append(args))
    unreferenced_worker.failed.connect(failed_lists.append)
    unreferenced_worker.run()
    assert finished_lists == [], "a disconnected root must never read as 'nothing unreferenced'"
    assert len(failed_lists) == 1
    assert "does not exist or is not reachable" in failed_lists[0]


def test_cache_dialog_invalidate_unreferenced_clears_the_list_and_explains(app, tmp_path):
    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    dialog = PreviewCacheDialog(settings)
    entries = [
        PreviewEntry(cache.preview_path("image", sha_hex("a")), "image", settings.image.profile_id, sha_hex("a"), 1_500),
        PreviewEntry(cache.preview_path("video", sha_hex("b")), "video", settings.video.profile_id, sha_hex("b"), 2_500),
    ]
    dialog.set_unreferenced(entries, 2)
    assert dialog.unreferenced_table.rowCount() == 2

    dialog.invalidate_unreferenced()

    assert dialog.unreferenced_table.rowCount() == 0
    assert "run Show Unreferenced Previews again" in dialog.status_label.text()

    dialog.status_label.setText("untouched")
    dialog.invalidate_unreferenced()  # nothing displayed: nothing to say
    assert dialog.status_label.text() == "untouched"


# ---------------------------------------------------------------------------
# Delete Temporary Files (cache manager)
# ---------------------------------------------------------------------------


def test_delete_temporaries_worker_removes_old_leftovers_and_keeps_recent_ones(app, tmp_path):
    import time

    from jvvv.preview_ui import DeleteTemporariesWorker

    settings = enabled_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    digest = sha_hex("leftover")
    final = cache.preview_path("image", digest)
    cache.ensure_parent(final)
    old_temp = cache.temporary_path(final)
    old_temp.write_bytes(b"partial")
    stamp = time.time() - 3 * 86400
    os.utime(old_temp, (stamp, stamp))
    running_final = cache.preview_path("image", sha_hex("running"))
    cache.ensure_parent(running_final)
    recent_temp = cache.temporary_path(running_final)
    recent_temp.write_bytes(b"partial")
    write_preview(cache, "image", sha_hex("finished"), 100)

    worker = DeleteTemporariesWorker(settings)
    results: list[tuple[int, int, list]] = []
    failures: list[str] = []
    worker.finished.connect(lambda deleted, kept, errors: results.append((deleted, kept, errors)))
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert results == [(1, 1, [])]
    assert not old_temp.exists()
    assert recent_temp.exists()
    assert cache.preview_path("image", sha_hex("finished")).exists()

    missing_root = DeleteTemporariesWorker(PreviewSettings(enabled=True, root_directory=str(tmp_path / "gone")))
    gone: list[str] = []
    missing_root.failed.connect(gone.append)
    missing_root.run()
    assert len(gone) == 1 and "does not exist or is not reachable" in gone[0]


def test_cache_dialog_offers_delete_temporary_files_with_confirmation(app, tmp_path):
    settings = enabled_settings(tmp_path)
    dialog = PreviewCacheDialog(settings)
    requests: list[bool] = []
    dialog.delete_temporaries_requested.connect(lambda: requests.append(True))
    asked: list[str] = []

    def decline(parent, title, text):
        asked.append(title)
        return False

    dialog.confirm = decline
    dialog.temporaries_button.click()
    assert asked == ["Delete Temporary Files"] and requests == []

    dialog.confirm = lambda parent, title, text: "newer than 24 hours are kept" in text
    dialog.temporaries_button.click()
    assert requests == [True]

    dialog.set_temporaries_deleted(3, 1, ["x: locked"])
    assert dialog.temporary_label.text() == "2"
    assert "3 temporary file(s) deleted" in dialog.status_label.text()
    assert "1 newer than 24 hours kept" in dialog.status_label.text()
    assert "1 could not be deleted" in dialog.status_label.text()

    dialog.set_busy(True, "working")
    assert not dialog.temporaries_button.isEnabled()
    dialog.set_busy(False)
    assert dialog.temporaries_button.isEnabled()
