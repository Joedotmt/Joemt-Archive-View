"""MainWindow / PreferencesDialog integration tests for offline previews.

Covers spec §49 "UI tests" together with the scan-start preflight (§27),
end-of-scan reporting (§15/§16/§17), external-only opening (§19/§44/§45),
Properties (§20), cache integrity when opening (§46) and the backup policy
(§23).  Pure window logic is driven through the SimpleNamespace-window style
of ``tests/test_app.py``; behaviour that needs real widgets uses an offscreen
``MainWindow`` whose settings are redirected to an INI file under ``tmp_path``.
"""

from __future__ import annotations

import functools
import hashlib
import os
import pathlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget  # noqa: E402

from preview_fixtures import write_test_image, write_tiny_mp4  # noqa: E402

import jvvv.app as app_module  # noqa: E402
from jvvv.app import (  # noqa: E402
    CatalogueItemRef,
    HelpDialog,
    MainWindow,
    PreferencesDialog,
    SEARCH_INCLUDE_PATHS_SETTING,
    ScanWorker,
    SearchResultItem,
    display_db_time,
    load_preview_settings,
    preview_open_label,
    preview_property_rows,
    save_preview_settings,
)
from jvvv.catalogue_backup import BackupResult  # noqa: E402
from jvvv.database import Database, utc_now  # noqa: E402
from jvvv.preview_cache import (  # noqa: E402
    PreviewCache,
    PreviewEntry,
    PreviewFailure,
    PreviewStoreStatistics,
)
from jvvv.preview_config import (  # noqa: E402
    BACKUP_POLICY_TEXT,
    PREVIEW_SETTING_KEYS,
    ImagePreviewProfile,
    PreviewSettings,
    VideoPreviewProfile,
)
from jvvv.preview_service import (  # noqa: E402
    MODE_DISABLED,
    MODE_ENABLED,
    MODE_SKIPPED_PREFLIGHT,
    PreviewFileInfo,
    PreviewStatistics,
    PreviewValidationReport,
    ValidationStep,
    scan_outcome,
    skipped_preflight_statistics,
)
from jvvv.preview_ui import OfflinePreviewSettingsWidget, PreviewFailuresDialog  # noqa: E402
from jvvv.theme import (  # noqa: E402
    ADOBE_THEME,
    DARK_MODE,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_COLOR_MODE,
    DEFAULT_THEME_STYLE,
)
from jvvv.utils import format_size  # noqa: E402


FAILED_STEP_LABEL = "FFmpeg executable"
FAILED_STEP_DETAIL = "FFmpeg could not be found on PATH."
GENERIC_MISSING_REASON = (
    "No preview exists at the expected location. Rescan the volume to generate it."
)
DEFAULT_PREVIEW_BUTTON_TOOLTIP = (
    "Open the offline preview in the operating system's default application"
)
INVALID_PREVIEW_REASON_PREFIX = "The preview file exists but is not valid: "


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def main_window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "open_last_catalogue", lambda self: None)
    window = MainWindow()
    # Everything the test persists must land under tmp_path, never in the
    # user's real settings store.
    window.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    try:
        yield window
    finally:
        window.close()


def digest_for(seed: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8")).digest()


def enabled_settings(tmp_path: Path, **overrides: object) -> PreviewSettings:
    values: dict[str, object] = {
        "enabled": True,
        "root_directory": str(tmp_path / "previews"),
    }
    values.update(overrides)
    return PreviewSettings(**values)  # type: ignore[arg-type]


def cache_for(settings: PreviewSettings) -> PreviewCache:
    assert settings.root_path is not None
    return PreviewCache(settings.root_path, settings.image, settings.video)


def write_image_preview(
    settings: PreviewSettings, digest: bytes, width: int = 640, height: int = 480
) -> Path:
    path = cache_for(settings).preview_path("image", digest)
    write_test_image(path, width, height, "jpeg")
    return path


def write_video_preview(settings: PreviewSettings, digest: bytes) -> Path:
    path = cache_for(settings).preview_path("video", digest)
    write_tiny_mp4(path)
    return path


def write_corrupt_preview(settings: PreviewSettings, media_kind: str, digest: bytes) -> Path:
    """Put unreadable bytes at the exact path a valid preview would occupy (spec §11, §46)."""

    path = cache_for(settings).preview_path(media_kind, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a valid preview file at all")
    return path


def make_report(
    settings: PreviewSettings, *, passed: bool, include_encode_tests: bool = True
) -> PreviewValidationReport:
    common = (
        ValidationStep("configuration", "Configuration values", True, "ok"),
        ValidationStep("preview-root", "Preview storage directory", True, "writable"),
        ValidationStep("image-backend", "Image preview backend", True, "Qt"),
    )
    if passed:
        steps = common + (
            ValidationStep("ffmpeg-found", "FFmpeg executable", True, "C:/fake/ffmpeg.exe"),
            ValidationStep("ffmpeg-version", "FFmpeg version", True, "ffmpeg version 6.0"),
            ValidationStep("ffmpeg-encoder", "H.264 encoder (libx264)", True, "available"),
        )
    else:
        steps = common + (
            ValidationStep(
                "ffmpeg-found", FAILED_STEP_LABEL, False, FAILED_STEP_DETAIL, "ffmpeg-start"
            ),
            ValidationStep("ffmpeg-version", "FFmpeg version", False, "Not run", None, True),
        )
    return PreviewValidationReport(
        passed=passed,
        steps=steps,
        settings=settings,
        root=settings.root_directory,
        free_bytes=5_400_000_000_000 if passed else None,
        total_bytes=8_000_000_000_000 if passed else None,
        ffmpeg_path="C:/fake/ffmpeg.exe" if passed else None,
        ffmpeg_version="ffmpeg version 6.0" if passed else None,
        encoder_available=True if passed else None,
        image_backend="Qt image reader/writer",
        image_profile_id=settings.image.profile_id,
        video_profile_id=settings.video.profile_id,
        include_encode_tests=include_encode_tests,
    )


def make_failure(index: int, media_kind: str = "image") -> PreviewFailure:
    video = media_kind == "video"
    name = f"camera{index:03d}.mov" if video else f"photo{index:03d}.tif"
    return PreviewFailure(
        source_name=name,
        relative_path=f"{'Videos' if video else 'Photos'}/{name}",
        volume_id=3,
        volume_label="AID-003 - Archive",
        media_kind=media_kind,
        sha256=digest_for(f"failure-{index}").hex(),
        preview_path=f"E:\\JVVV Previews\\{index}",
        profile_id="h264-1fps-240p-crf35-veryfast" if video else "jpeg-max1600-q82",
        stage="ffmpeg-exit" if video else "image-decode",
        message="FFmpeg exited with code 1." if video else "Image decoder could not read the file.",
        detail="Invalid data found when processing input." if video else "",
    )


def bind(window: SimpleNamespace, *names: str) -> SimpleNamespace:
    """Attach real MainWindow methods to a SimpleNamespace window."""

    for name in names:
        setattr(window, name, functools.partial(getattr(MainWindow, name), window))
    return window


def file_info(
    exists: bool,
    *,
    valid: bool = True,
    message: str = "",
    media_kind: str = "image",
) -> PreviewFileInfo:
    return PreviewFileInfo(
        media_kind,
        "jpeg-max1600-q82" if media_kind == "image" else "h264-1fps-240p-crf35-veryfast",
        Path("E:/JVVV Previews/images/jpeg-max1600-q82/ab/preview.jpg"),
        exists,
        valid,
        10 if exists else 0,
        None,
        None,
        None,
        message,
    )


class Catalogue:
    """A real catalogue with one volume; files are added on demand."""

    def __init__(self, tmp_path: Path) -> None:
        self.db = Database(tmp_path / "catalogue.jvvv")
        self.volume_id = self.db.create_volume("Archive", str(tmp_path / "drive"))
        with self.db.transaction():
            self.root_id = self.db.ensure_folder(self.volume_id, None, "Archive", "", utc_now())

    def add_file(self, relative_path: str, digest: bytes | None, size: int = 1234) -> int:
        parts = relative_path.split("/")
        now = utc_now()
        with self.db.transaction():
            folder_id = self.root_id
            if len(parts) > 1:
                folder_id = self.db.ensure_folder(
                    self.volume_id, self.root_id, parts[-2], "/".join(parts[:-1]), now
                )
            return self.db.upsert_file(
                self.volume_id,
                folder_id,
                parts[-1],
                relative_path,
                Path(parts[-1]).suffix.lstrip("."),
                size,
                None,
                now,
                content_hash=digest,
                content_hash_algorithm="sha256" if digest is not None else None,
            )

    def set_status(self, file_id: int, **values: object) -> None:
        with self.db.transaction():
            self.db.replace_file_preview_status(file_id, values)

    def target(self, file_id: int, relative_path: str) -> CatalogueItemRef:
        return CatalogueItemRef(
            item_type="file",
            item_id=file_id,
            volume_id=self.volume_id,
            relative_path=relative_path,
        )

    def search_item(self, file_id: int, relative_path: str, item_type: str = "file") -> SearchResultItem:
        return SearchResultItem(
            item_type=item_type,
            item_id=file_id,
            name=relative_path.rsplit("/", 1)[-1],
            volume_id=self.volume_id,
            drive_id=None,
            volume_name="Archive",
            relative_path=relative_path,
            size_bytes=1,
            modified_at=None,
            missing=False,
            source_path="",
            connected=False,
        )

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def catalogue(tmp_path: Path):
    store = Catalogue(tmp_path)
    try:
        yield store
    finally:
        store.close()


# -- fakes -------------------------------------------------------------------
class FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *values) -> None:
        for callback in list(self.callbacks):
            callback(*values)


class FakeAction:
    def __init__(self, text: str) -> None:
        self.text = text
        self.enabled = True
        self.tooltip = ""
        self.status_tip = ""
        self.triggered = FakeSignal()

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def setToolTip(self, text: str) -> None:
        self.tooltip = text

    def setStatusTip(self, text: str) -> None:
        self.status_tip = text

    def trigger(self) -> None:
        self.triggered.emit()


class FakeMenu:
    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.items: list[FakeAction | None] = []

    def addAction(self, text: str) -> FakeAction:
        action = FakeAction(text)
        self.items.append(action)
        return action

    def addSeparator(self) -> None:
        self.items.append(None)

    def texts(self) -> list[str | None]:
        return [item.text if item is not None else None for item in self.items]

    def action(self, text: str) -> FakeAction:
        return next(item for item in self.items if item is not None and item.text == text)


class FakeButton:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self._enabled = True
        self._tooltip = ""

    def setText(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def isEnabled(self) -> bool:
        return self._enabled

    def setToolTip(self, text: str) -> None:
        self._tooltip = text

    def toolTip(self) -> str:
        return self._tooltip


class FakeProgress:
    def __init__(self) -> None:
        self._range = (0, 1)
        self.value = 0
        self._format = ""
        self.tooltip = ""

    def setRange(self, low: int, high: int) -> None:
        self._range = (low, high)

    def minimum(self) -> int:
        return self._range[0]

    def maximum(self) -> int:
        return self._range[1]

    def setValue(self, value: int) -> None:
        self.value = value

    def setFormat(self, text: str) -> None:
        self._format = text

    def format(self) -> str:
        return self._format

    def setToolTip(self, text: str) -> None:
        self.tooltip = text


class FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []

    def showMessage(self, message: str, timeout: int = 0) -> None:
        self.messages.append((message, timeout))


class RecordingSettings:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.values: dict[str, object] = {}

    def setValue(self, key: str, value) -> None:
        self.events.append(("setting", key, value))
        self.values[key] = value

    def sync(self) -> None:
        self.events.append(("sync",))

    def value(self, key: str, default=None):
        return self.values.get(key, default)

    def written_keys(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "setting"]


class FakeLog:
    def __init__(self) -> None:
        self.text = "unset"
        self.appended: list[str] = []

    def clear(self) -> None:
        self.text = ""

    def setPlainText(self, text: str) -> None:
        self.text = text

    def appendPlainText(self, text: str) -> None:
        self.appended.append(text)


class MessageBoxCapture:
    """Auto-answer ``QMessageBox.exec()`` and pretend a named button was clicked."""

    def __init__(self, monkeypatch, click_text: str | None = None) -> None:
        self.boxes: list[QMessageBox] = []
        self.click_text = click_text
        capture = self

        def fake_exec(box):
            capture.boxes.append(box)
            return 0

        def fake_clicked(box):
            if capture.click_text is None:
                return None
            for button in box.buttons():
                if button.text().replace("&", "") == capture.click_text:
                    return button
            return None

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        monkeypatch.setattr(QMessageBox, "clickedButton", fake_clicked)


def button_texts(box: QMessageBox) -> set[str]:
    return {button.text().replace("&", "") for button in box.buttons()}


def make_preferences_window(**extra):
    status_bar = FakeStatusBar()
    window = SimpleNamespace(
        search_include_paths=False,
        theme_style=ADOBE_THEME,
        color_mode=DARK_MODE,
        accent_color=DEFAULT_ACCENT_COLOR,
        ui_zoom=1.0,
        settings=RecordingSettings(),
        statusBar=lambda: status_bar,
        status_bar=status_bar,
    )
    for name, value in extra.items():
        setattr(window, name, value)
    return window


def scan_result(statistics: PreviewStatistics, status: str = "completed") -> dict:
    """Mirror VolumeScanner: a completed scan with preview failures is completed_with_warnings."""

    return {
        "status": scan_outcome(status, statistics) if status == "completed" else status,
        "files_seen": 5,
        "folders_seen": 2,
        "errors_count": 0,
        "message": "",
        "changes": {},
        "files_hashed": 5,
        "bytes_hashed": 1000,
        "hash_errors": 0,
        "media_files": 3,
        "media_metadata_collected": 3,
        "preview": statistics.as_dict(),
    }


# ---------------------------------------------------------------------------
# Settings persistence (spec §1 "Settings persistence", §49 settings tests)
# ---------------------------------------------------------------------------
def test_load_preview_settings_defaults_to_disabled_when_nothing_is_stored(tmp_path):
    settings = QSettings(str(tmp_path / "empty.ini"), QSettings.Format.IniFormat)

    loaded = load_preview_settings(settings)

    assert loaded == PreviewSettings()
    assert loaded.enabled is False
    assert loaded.root_directory == ""
    assert loaded.image.profile_id == "jpeg-max1600-q82"
    assert loaded.video.profile_id == "h264-1fps-240p-crf35-veryfast"


def test_save_and_load_preview_settings_round_trip_through_an_ini_file(tmp_path):
    ini = tmp_path / "jvvv.ini"
    stored = PreviewSettings(
        enabled=True,
        root_directory=str(tmp_path / "Preview Store"),
        ffmpeg_path=str(tmp_path / "bin" / "ffmpeg.exe"),
        image=ImagePreviewProfile(max_dimension=4096, jpeg_quality=92),
        video=VideoPreviewProfile(fps=0.5, max_height=720, crf=28, preset="fast"),
    )

    writer = QSettings(str(ini), QSettings.Format.IniFormat)
    save_preview_settings(writer, stored)
    del writer

    assert ini.is_file()
    reader = QSettings(str(ini), QSettings.Format.IniFormat)
    assert all(reader.contains(key) for key in PREVIEW_SETTING_KEYS)
    loaded = load_preview_settings(reader)
    assert loaded == stored
    assert loaded.image.profile_id == "jpeg-max4096-q92"
    assert loaded.video.profile_id == "h264-0.5fps-720p-crf28-fast"

    # Disabling persists too, and the other values survive it.
    save_preview_settings(reader, stored.with_enabled(False))
    reloaded = load_preview_settings(QSettings(str(ini), QSettings.Format.IniFormat))
    assert reloaded.enabled is False
    assert reloaded.output_signature() == stored.output_signature()


def test_preview_open_label_distinguishes_images_from_videos():
    assert preview_open_label("image") == "Open Preview"
    assert preview_open_label("video") == "Play Preview"


# ---------------------------------------------------------------------------
# PreferencesDialog (spec §1, §2, §35)
# ---------------------------------------------------------------------------
def test_preferences_dialog_exposes_the_preview_widget_and_its_settings(app, tmp_path):
    initial = enabled_settings(
        tmp_path, video=VideoPreviewProfile(fps=2.0, max_height=720, crf=28, preset="fast")
    )
    dialog = PreferencesDialog(
        False, DEFAULT_THEME_STYLE, DEFAULT_COLOR_MODE, DEFAULT_ACCENT_COLOR, None, initial
    )
    try:
        assert isinstance(dialog.preview_widget, OfflinePreviewSettingsWidget)
        assert dialog.preview_settings() == initial
        assert dialog.preview_widget.enable_check.isChecked()
        assert dialog.preview_widget.video_profile_label.text() == (
            "Current video profile: h264-2fps-720p-crf28-fast"
        )

        replacement = PreviewSettings(root_directory=str(tmp_path / "other"))
        dialog.set_preview_settings(replacement)
        assert dialog.preview_settings() == replacement
        assert not dialog.preview_widget.enable_check.isChecked()
        assert dialog.preview_widget.image_profile_label.text() == (
            "Current image profile: jpeg-max1600-q82"
        )
    finally:
        dialog.close()

    # Spec §49: the checkbox defaults off when no configuration is supplied.
    plain = PreferencesDialog(False, DEFAULT_THEME_STYLE, DEFAULT_COLOR_MODE, DEFAULT_ACCENT_COLOR)
    try:
        assert not plain.preview_widget.enable_check.isChecked()
        assert plain.preview_settings() == PreviewSettings()
    finally:
        plain.close()


class SaveValidationWidget:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def validate_for_save(self):
        self.calls += 1
        return self.outcome


def test_preferences_dialog_accept_keeps_the_dialog_open_when_save_validation_fails(
    app, tmp_path, monkeypatch
):
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda parent, title, text, *args: warnings.append((parent, title, text)),
    )
    settings = enabled_settings(tmp_path)
    dialog = PreferencesDialog(False, DEFAULT_THEME_STYLE, DEFAULT_COLOR_MODE, DEFAULT_ACCENT_COLOR)
    try:
        dialog.show()

        failing = SaveValidationWidget((settings, make_report(settings, passed=False)))
        dialog.preview_widget = failing
        dialog.accept()
        assert failing.calls == 1
        assert dialog.isVisible()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert len(warnings) == 1
        parent, title, text = warnings[0]
        assert parent is dialog
        assert title == "Offline Previews"
        assert "last known-good configuration was restored" in text
        assert "Validation failed." in text
        assert FAILED_STEP_LABEL in text and FAILED_STEP_DETAIL in text
        assert "Offline previews could not be enabled." not in text

        passing = SaveValidationWidget((settings, make_report(settings, passed=True)))
        dialog.preview_widget = passing
        dialog.accept()
        assert passing.calls == 1
        assert not dialog.isVisible()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert len(warnings) == 1
    finally:
        dialog.close()

    # No validation needed (disabled or unchanged) also closes.
    dialog = PreferencesDialog(False, DEFAULT_THEME_STYLE, DEFAULT_COLOR_MODE, DEFAULT_ACCENT_COLOR)
    try:
        dialog.show()
        dialog.preview_widget = SaveValidationWidget((PreviewSettings(), None))
        dialog.accept()
        assert not dialog.isVisible()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert len(warnings) == 1
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# MainWindow.show_preferences / persist_preview_settings
# ---------------------------------------------------------------------------
def test_show_preferences_persists_changed_preview_settings(monkeypatch, tmp_path):
    new_settings = enabled_settings(tmp_path)

    class FakePreferencesDialog:
        def __init__(self, include_paths, theme_style, color_mode, accent_color, parent):
            self.preview_widget = None

        def exec(self):
            return QDialog.DialogCode.Accepted

        def include_paths(self):
            return False

        def theme_style(self):
            return ADOBE_THEME

        def color_mode(self):
            return DARK_MODE

        def accent_color(self):
            return DEFAULT_ACCENT_COLOR

        def preview_settings(self):
            return new_settings

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    window = make_preferences_window(preview_settings=PreviewSettings())

    MainWindow.show_preferences(window)

    assert window.preview_settings == new_settings
    expected = new_settings.as_mapping()
    for key in PREVIEW_SETTING_KEYS:
        assert ("setting", key, expected[key]) in window.settings.events
    assert ("sync",) in window.settings.events
    assert set(window.settings.written_keys()) == set(PREVIEW_SETTING_KEYS)
    assert window.status_bar.messages == [("Preferences saved.", 3000)]


def test_show_preferences_does_not_persist_unchanged_preview_settings(monkeypatch, tmp_path):
    current = enabled_settings(tmp_path)

    class FakePreferencesDialog:
        def __init__(self, *args):
            self.preview_widget = None

        def exec(self):
            return QDialog.DialogCode.Accepted

        def include_paths(self):
            return False

        def theme_style(self):
            return ADOBE_THEME

        def color_mode(self):
            return DARK_MODE

        def accent_color(self):
            return DEFAULT_ACCENT_COLOR

        def preview_settings(self):
            # An equal copy, not the same object: equality decides, not identity.
            return PreviewSettings.from_mapping(current.as_mapping())

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    window = make_preferences_window(preview_settings=current)

    MainWindow.show_preferences(window)

    assert window.preview_settings == current
    assert window.settings.events == []
    assert window.status_bar.messages == []


def test_show_preferences_still_works_with_a_dialog_that_has_no_preview_support(monkeypatch):
    events = []

    class FakePreferencesDialog:
        def __init__(self, include_paths, theme_style, color_mode, accent_color, parent):
            events.append(("dialog", include_paths))

        def exec(self):
            return QDialog.DialogCode.Accepted

        def include_paths(self):
            return True

        def theme_style(self):
            return ADOBE_THEME

        def color_mode(self):
            return DARK_MODE

        def accent_color(self):
            return DEFAULT_ACCENT_COLOR

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    window = make_preferences_window(
        search_edit=SimpleNamespace(text=lambda: "", setPlaceholderText=lambda text: None),
        db=None,
        perform_search=lambda: events.append(("search",)),
        search_placeholder_text=lambda: "Search with paths",
    )

    MainWindow.show_preferences(window)

    assert events == [("dialog", False)]
    assert window.search_include_paths is True
    assert ("setting", SEARCH_INCLUDE_PATHS_SETTING, True) in window.settings.events
    assert not any(key.startswith("previews/") for key in window.settings.written_keys())
    assert not hasattr(window, "preview_settings")


def test_show_preferences_seeds_the_widget_and_persists_a_validated_enable_immediately(
    monkeypatch, tmp_path
):
    validated_settings = enabled_settings(tmp_path)
    dialogs = []

    class FakeWidget:
        def __init__(self):
            self.seeded = []
            self.validated = FakeSignal()

        def set_settings(self, settings):
            self.seeded.append(settings)

    class FakePreferencesDialog:
        def __init__(self, *args):
            self.preview_widget = FakeWidget()
            self.appearance_changed = FakeSignal()
            dialogs.append(self)

        def exec(self):
            # The user ticks the box, validation passes, then cancels the dialog.
            self.preview_widget.validated.emit(validated_settings)
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    monkeypatch.setattr(MainWindow, "apply_appearance", lambda self, *appearance: None)
    current = PreviewSettings(root_directory=str(tmp_path / "old"))
    window = make_preferences_window(preview_settings=current)

    MainWindow.show_preferences(window)

    assert dialogs[0].preview_widget.seeded == [current]
    # Spec §2: a successful enable-validation is persisted even though the
    # dialog was later cancelled.
    assert window.preview_settings == validated_settings
    assert ("setting", "previews/enabled", True) in window.settings.events
    assert ("setting", "previews/root_directory", validated_settings.root_directory) in window.settings.events
    assert ("sync",) in window.settings.events


def test_persist_preview_settings_writes_qsettings_and_updates_an_open_cache_dialog(tmp_path):
    ini = tmp_path / "jvvv.ini"

    class FakeCacheDialog:
        def __init__(self):
            self.settings = []

        def set_settings(self, settings):
            self.settings.append(settings)

    dialog = FakeCacheDialog()
    refreshed = []
    window = SimpleNamespace(
        settings=QSettings(str(ini), QSettings.Format.IniFormat),
        preview_settings=PreviewSettings(),
        preview_cache_dialog=dialog,
        on_search_selection_changed=lambda: refreshed.append(True),
    )
    new_settings = enabled_settings(
        tmp_path, image=ImagePreviewProfile(max_dimension=1024, jpeg_quality=75)
    )

    MainWindow.persist_preview_settings(window, new_settings)

    assert window.preview_settings == new_settings
    assert dialog.settings == [new_settings]
    assert refreshed == [True]
    window.settings = None
    assert load_preview_settings(QSettings(str(ini), QSettings.Format.IniFormat)) == new_settings

    # Without an open cache dialog (or a search tab) it still persists.
    bare = SimpleNamespace(
        settings=QSettings(str(ini), QSettings.Format.IniFormat),
        preview_settings=new_settings,
        preview_cache_dialog=None,
    )
    MainWindow.persist_preview_settings(bare, PreviewSettings())
    bare.settings = None
    assert load_preview_settings(QSettings(str(ini), QSettings.Format.IniFormat)) == PreviewSettings()


# ---------------------------------------------------------------------------
# Context menu (spec §19, §45)
# ---------------------------------------------------------------------------
def make_menu_window(catalogue: Catalogue, settings: PreviewSettings):
    opened = []
    window = SimpleNamespace(
        db=catalogue.db,
        preview_settings=settings,
        catalogue_item_real_path=lambda target: None,
        open_catalogue_item=lambda target: None,
        open_catalogue_location_for_item=lambda target: None,
        open_catalogue_item_in_file_manager=lambda target: None,
        copy_catalogue_item_path=lambda target: None,
        show_browser_item_properties=lambda item_type, item_id: None,
        open_preview_for_target=lambda target, reveal=False: opened.append((target, reveal)),
    )
    bind(window, "catalogue_item_media_kind", "preview_info_for_target", "preview_unavailable_reason")
    return window, opened


def test_context_menu_for_an_image_offers_open_and_reveal_preview_when_it_exists(
    monkeypatch, catalogue, tmp_path
):
    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    settings = enabled_settings(tmp_path)
    digest = digest_for("alpha")
    file_id = catalogue.add_file("Photos/alpha.jpg", digest)
    write_image_preview(settings, digest)
    window, opened = make_menu_window(catalogue, settings)
    target = catalogue.target(file_id, "Photos/alpha.jpg")

    menu = MainWindow.build_catalogue_item_context_menu(window, target)

    assert menu.texts() == [
        "Open",
        "Open File Location",
        "Copy Path",
        "Open Preview",
        "Reveal Preview",
        None,
        "Properties",
    ]
    open_action = menu.action("Open Preview")
    reveal_action = menu.action("Reveal Preview")
    assert open_action.enabled and reveal_action.enabled
    assert open_action.tooltip == "" and reveal_action.tooltip == ""
    # Disconnected source volume (spec §19): the original stays unavailable
    # while the preview is available.
    assert not menu.action("Open").enabled
    assert not menu.action("Open File Location").enabled

    open_action.trigger()
    reveal_action.trigger()
    assert opened == [(target, False), (target, True)]


def test_context_menu_disables_preview_actions_and_explains_when_the_preview_is_missing(
    monkeypatch, catalogue, tmp_path
):
    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    settings = enabled_settings(tmp_path)
    file_id = catalogue.add_file("Photos/beta.png", digest_for("beta"))
    window, _opened = make_menu_window(catalogue, settings)

    menu = MainWindow.build_catalogue_item_context_menu(
        window, catalogue.target(file_id, "Photos/beta.png")
    )

    open_action = menu.action("Open Preview")
    reveal_action = menu.action("Reveal Preview")
    assert not open_action.enabled and not reveal_action.enabled
    assert open_action.tooltip == GENERIC_MISSING_REASON
    assert open_action.status_tip == GENERIC_MISSING_REASON
    assert reveal_action.tooltip == GENERIC_MISSING_REASON
    # Spec §19 "explain why": Fusion never shows tooltips on disabled menu
    # items, so the reason is also a visible (disabled) menu line.
    note = menu.action(f"Preview not available: {GENERIC_MISSING_REASON}")
    assert not note.enabled


def test_context_menu_disables_preview_actions_for_a_corrupt_preview_file(
    monkeypatch, catalogue, tmp_path
):
    """Spec §11/§46: a file at the preview path that fails validation is not 'available'."""

    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    settings = enabled_settings(tmp_path)
    image_digest = digest_for("corrupt-image")
    video_digest = digest_for("corrupt-video")
    image_id = catalogue.add_file("Photos/corrupt.jpg", image_digest)
    video_id = catalogue.add_file("Videos/corrupt.mp4", video_digest)
    write_corrupt_preview(settings, "image", image_digest)
    write_corrupt_preview(settings, "video", video_digest)
    window, opened = make_menu_window(catalogue, settings)

    image_menu = MainWindow.build_catalogue_item_context_menu(
        window, catalogue.target(image_id, "Photos/corrupt.jpg")
    )
    open_action = image_menu.action("Open Preview")
    reveal_action = image_menu.action("Reveal Preview")
    assert not open_action.enabled and not reveal_action.enabled
    assert open_action.tooltip.startswith(INVALID_PREVIEW_REASON_PREFIX)
    # The validator's own explanation follows the prefix, and every disabled
    # action carries the same reason.
    assert len(open_action.tooltip) > len(INVALID_PREVIEW_REASON_PREFIX)
    assert open_action.status_tip == open_action.tooltip
    assert reveal_action.tooltip == open_action.tooltip

    video_menu = MainWindow.build_catalogue_item_context_menu(
        window, catalogue.target(video_id, "Videos/corrupt.mp4")
    )
    play_action = video_menu.action("Play Preview")
    assert not play_action.enabled and not video_menu.action("Reveal Preview").enabled
    assert play_action.tooltip.startswith(INVALID_PREVIEW_REASON_PREFIX)
    assert opened == []


def test_context_menu_for_a_video_offers_play_preview(monkeypatch, catalogue, tmp_path):
    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    settings = enabled_settings(tmp_path)
    digest = digest_for("clip")
    file_id = catalogue.add_file("Videos/clip.mp4", digest)
    write_video_preview(settings, digest)
    window, opened = make_menu_window(catalogue, settings)
    target = catalogue.target(file_id, "Videos/clip.mp4")

    menu = MainWindow.build_catalogue_item_context_menu(window, target, include_catalogue_location=True)

    assert menu.texts() == [
        "Open",
        "View in Catalogue",
        "Open File Location",
        "Copy Path",
        "Play Preview",
        "Reveal Preview",
        None,
        "Properties",
    ]
    play_action = menu.action("Play Preview")
    assert play_action.enabled
    play_action.trigger()
    assert opened == [(target, False)]


def test_context_menu_for_a_text_file_has_no_preview_actions(monkeypatch, catalogue, tmp_path):
    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    file_id = catalogue.add_file("Projects/report.txt", digest_for("report"))
    window, _opened = make_menu_window(catalogue, enabled_settings(tmp_path))
    window.preview_info_for_target = lambda target: pytest.fail("no preview lookup for .txt")

    menu = MainWindow.build_catalogue_item_context_menu(
        window, catalogue.target(file_id, "Projects/report.txt")
    )

    assert menu.texts() == ["Open", "Open File Location", "Copy Path", None, "Properties"]


def test_catalogue_item_media_kind_uses_the_extension_and_ignores_folders():
    def ref(item_type: str, relative_path: str) -> CatalogueItemRef:
        return CatalogueItemRef(item_type=item_type, item_id=1, volume_id=1, relative_path=relative_path)

    window = SimpleNamespace()
    assert MainWindow.catalogue_item_media_kind(window, ref("folder", "Photos")) is None
    assert MainWindow.catalogue_item_media_kind(window, ref("file", "Photos/a.JPG")) == "image"
    assert MainWindow.catalogue_item_media_kind(window, ref("file", "Videos/a.mov")) == "video"
    assert MainWindow.catalogue_item_media_kind(window, ref("file", "Audio/a.wav")) == "audio"
    assert MainWindow.catalogue_item_media_kind(window, ref("file", "Docs/a.txt")) is None


# ---------------------------------------------------------------------------
# preview_unavailable_reason wording (spec §19 "The UI should explain why")
# ---------------------------------------------------------------------------
def reason_window(settings: PreviewSettings, status=None) -> SimpleNamespace:
    return SimpleNamespace(
        preview_settings=settings,
        db=SimpleNamespace(get_file_preview_status=lambda file_id: status),
    )


def test_preview_unavailable_reason_explains_each_situation(tmp_path):
    enabled = enabled_settings(tmp_path)
    hashed_row = {"id": 1, "content_hash": digest_for("x")}

    assert MainWindow.preview_unavailable_reason(
        reason_window(PreviewSettings()), "image", None, hashed_row
    ) == "No preview storage directory is configured in Settings > Preferences."

    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled), "image", None, {"id": 1, "content_hash": None}
    ) == "No SHA-256 hash is recorded for this file, so no preview path can be derived."

    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled), "image", None, hashed_row
    ) == "The offline preview location could not be determined."

    failed = {"status": "failed", "error_message": "FFmpeg exited with code 1."}
    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled, failed), "video", file_info(False, media_kind="video"), hashed_row
    ) == "Preview generation failed during the last scan: FFmpeg exited with code 1."
    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled, {"status": "failed", "error_message": ""}),
        "video",
        file_info(False, media_kind="video"),
        hashed_row,
    ) == "Preview generation failed during the last scan."

    disabled = enabled.with_enabled(False)
    assert MainWindow.preview_unavailable_reason(
        reason_window(disabled), "image", file_info(False), hashed_row
    ) == (
        "No preview exists at the expected location and offline preview generation "
        "is disabled in Settings."
    )

    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled), "image", file_info(False), hashed_row
    ) == GENERIC_MISSING_REASON

    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled), "image", file_info(True, valid=False, message="Bad JPEG"), hashed_row
    ) == "The preview file exists but is not valid: Bad JPEG"

    assert MainWindow.preview_unavailable_reason(
        reason_window(enabled), "image", file_info(True), hashed_row
    ) == ""

    # No catalogue row at all: still a sentence, never an exception.
    assert MainWindow.preview_unavailable_reason(
        SimpleNamespace(preview_settings=enabled, db=None), "image", file_info(False), None
    ) == GENERIC_MISSING_REASON


# ---------------------------------------------------------------------------
# open_preview_for_target (spec §19, §44, §45, §46)
# ---------------------------------------------------------------------------
def make_open_window(catalogue: Catalogue, settings: PreviewSettings):
    refreshed = []
    window = SimpleNamespace(
        db=catalogue.db,
        preview_settings=settings,
        on_search_selection_changed=lambda: refreshed.append(True),
    )
    bind(
        window,
        "catalogue_item_media_kind",
        "preview_info_for_target",
        "preview_unavailable_reason",
        "_mark_preview_missing",
    )
    return window, refreshed


def test_open_preview_launches_the_existing_preview_externally(monkeypatch, catalogue, tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("alpha")
    file_id = catalogue.add_file("Photos/alpha.jpg", digest)
    preview = write_image_preview(settings, digest)
    opened = []
    monkeypatch.setattr(
        "jvvv.app.open_in_file_manager",
        lambda path, reveal=False: opened.append((path, reveal)),
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **kwargs: pytest.fail("no dialog expected")
    )
    window, refreshed = make_open_window(catalogue, settings)
    target = catalogue.target(file_id, "Photos/alpha.jpg")

    MainWindow.open_preview_for_target(window, target)
    MainWindow.open_preview_for_target(window, target, reveal=True)

    assert opened == [(preview, False), (preview, True)]
    assert refreshed == []


def test_open_preview_for_a_vanished_file_explains_and_marks_the_status_missing(
    monkeypatch, catalogue, tmp_path
):
    settings = enabled_settings(tmp_path)
    digest = digest_for("gone")
    file_id = catalogue.add_file("Photos/gone.jpg", digest)
    catalogue.set_status(
        file_id,
        media_kind="image",
        profile_id=settings.image.profile_id,
        status="available",
        source_hash=digest,
        preview_size=4321,
        preview_width=640,
        preview_height=480,
        generated_at=utc_now(),
    )
    infos = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda parent, title, text, *args: infos.append((parent, title, text)),
    )
    monkeypatch.setattr(
        "jvvv.app.open_in_file_manager", lambda *args, **kwargs: pytest.fail("must not open")
    )
    window, refreshed = make_open_window(catalogue, settings)

    MainWindow.open_preview_for_target(window, catalogue.target(file_id, "Photos/gone.jpg"))

    assert infos == [
        (
            window,
            "Preview Not Available",
            f"The offline preview is not available.\n\n{GENERIC_MISSING_REASON}",
        )
    ]
    row = catalogue.db.get_file_preview_status(file_id)
    assert row["status"] == "missing"
    assert row["error_stage"] == "preview-missing"
    assert "no longer present" in row["error_message"]
    assert row["media_kind"] == "image"
    assert row["profile_id"] == settings.image.profile_id
    assert bytes(row["source_hash"]) == digest
    assert refreshed == [True]

    # A recorded failure is kept as a failure and its reason is shown.
    failed_id = catalogue.add_file("Videos/broken.mov", digest_for("broken"))
    catalogue.set_status(
        failed_id,
        media_kind="video",
        profile_id=settings.video.profile_id,
        status="failed",
        source_hash=digest_for("broken"),
        error_stage="ffmpeg-exit",
        error_message="FFmpeg exited with code 1.",
    )
    MainWindow.open_preview_for_target(window, catalogue.target(failed_id, "Videos/broken.mov"))
    assert infos[-1][2] == (
        "The offline preview is not available.\n\n"
        "Preview generation failed during the last scan: FFmpeg exited with code 1."
    )
    assert catalogue.db.get_file_preview_status(failed_id)["status"] == "failed"

    # A file without a stored status gets the message and no bookkeeping row.
    bare_id = catalogue.add_file("Photos/bare.png", digest_for("bare"))
    MainWindow.open_preview_for_target(window, catalogue.target(bare_id, "Photos/bare.png"))
    assert infos[-1][2].endswith(GENERIC_MISSING_REASON)
    assert catalogue.db.get_file_preview_status(bare_id) is None
    assert refreshed == [True, True, True]


def test_open_preview_refuses_a_corrupt_preview_file_and_explains(monkeypatch, catalogue, tmp_path):
    """Spec §46: a corrupt preview is never launched externally, for Open or Reveal."""

    settings = enabled_settings(tmp_path)
    digest = digest_for("corrupt")
    file_id = catalogue.add_file("Photos/corrupt.jpg", digest)
    catalogue.set_status(
        file_id,
        media_kind="image",
        profile_id=settings.image.profile_id,
        status="available",
        source_hash=digest,
        preview_size=4321,
        preview_width=640,
        preview_height=480,
        generated_at=utc_now(),
    )
    write_corrupt_preview(settings, "image", digest)
    infos = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda parent, title, text, *args: infos.append((parent, title, text)),
    )
    monkeypatch.setattr(
        "jvvv.app.open_in_file_manager", lambda *args, **kwargs: pytest.fail("must not open")
    )
    window, refreshed = make_open_window(catalogue, settings)
    target = catalogue.target(file_id, "Photos/corrupt.jpg")

    MainWindow.open_preview_for_target(window, target)
    MainWindow.open_preview_for_target(window, target, reveal=True)

    assert len(infos) == 2
    for parent, title, text in infos:
        assert parent is window
        assert title == "Preview Not Available"
        assert text.startswith(
            "The offline preview is not available.\n\n" + INVALID_PREVIEW_REASON_PREFIX
        )
    # The file is present, just unreadable, so it is not recorded as missing:
    # the next scan detects the corruption and regenerates it (spec §11).
    row = catalogue.db.get_file_preview_status(file_id)
    assert row["status"] == "available"
    assert refreshed == [True, True]

    # Same for a video whose .mp4 is garbage, with or without a stored status.
    video_digest = digest_for("corrupt-clip")
    video_id = catalogue.add_file("Videos/corrupt.mp4", video_digest)
    write_corrupt_preview(settings, "video", video_digest)
    MainWindow.open_preview_for_target(window, catalogue.target(video_id, "Videos/corrupt.mp4"))
    assert infos[-1][2].startswith(
        "The offline preview is not available.\n\n" + INVALID_PREVIEW_REASON_PREFIX
    )
    assert catalogue.db.get_file_preview_status(video_id) is None


def test_open_preview_ignores_non_media_targets_and_reports_launch_failures(
    monkeypatch, catalogue, tmp_path
):
    settings = enabled_settings(tmp_path)
    text_id = catalogue.add_file("Docs/notes.txt", digest_for("notes"))
    monkeypatch.setattr(
        "jvvv.app.open_in_file_manager", lambda *args, **kwargs: pytest.fail("must not open")
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **kwargs: pytest.fail("no dialog for .txt")
    )
    window, refreshed = make_open_window(catalogue, settings)

    MainWindow.open_preview_for_target(window, catalogue.target(text_id, "Docs/notes.txt"))
    assert refreshed == []

    digest = digest_for("alpha")
    image_id = catalogue.add_file("Photos/alpha.jpg", digest)
    write_image_preview(settings, digest)

    def failing_open(path, reveal=False):
        raise OSError("No application is associated with this file.")

    warnings = []
    monkeypatch.setattr("jvvv.app.open_in_file_manager", failing_open)
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, text, *args: warnings.append((title, text))
    )
    MainWindow.open_preview_for_target(window, catalogue.target(image_id, "Photos/alpha.jpg"))
    assert warnings == [("Open Preview Failed", "No application is associated with this file.")]


def test_open_selected_search_preview_opens_the_selected_file_only(catalogue):
    opened = []
    file_item = catalogue.search_item(5, "Photos/alpha.jpg")
    folder_item = catalogue.search_item(6, "Photos", item_type="folder")
    window = SimpleNamespace(
        selected_search_item=lambda: file_item,
        open_preview_for_target=lambda target: opened.append(target),
    )
    bind(window, "catalogue_ref_for_search_item")

    MainWindow.open_selected_search_preview(window)
    assert opened == [catalogue.target(5, "Photos/alpha.jpg")]

    window.selected_search_item = lambda: folder_item
    MainWindow.open_selected_search_preview(window)
    window.selected_search_item = lambda: None
    MainWindow.open_selected_search_preview(window)
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# preview_property_rows (spec §20, §33, §46)
# ---------------------------------------------------------------------------
NOT_CONFIGURED_TEXT = (
    "Not configured — choose a preview storage directory in Settings > Preferences "
    "to generate previews while scanning"
)


def test_preview_property_rows_without_a_configured_root_say_so():
    record = {"content_hash": digest_for("x")}

    assert preview_property_rows(record, None, "image") == [("Offline preview", NOT_CONFIGURED_TEXT)]
    assert preview_property_rows(record, PreviewSettings(), "video") == [
        ("Offline preview", NOT_CONFIGURED_TEXT)
    ]
    assert preview_property_rows(record, PreviewSettings(), "audio") == []
    assert preview_property_rows(record, None, "document") == []


def test_preview_property_rows_describe_an_available_image_preview(tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("alpha")
    preview = write_image_preview(settings, digest, 640, 480)

    rows = preview_property_rows({"content_hash": digest}, settings, "image")

    assert [label for label, _value in rows] == [
        "Offline preview",
        "Preview profile",
        "Preview dimensions",
        "Preview size",
        "Preview stored at",
    ]
    values = dict(rows)
    assert values["Offline preview"] == "Available"
    assert values["Preview profile"] == " · ".join(settings.image.describe())
    assert values["Preview profile"] == "JPEG · Max dimension: 1600 px · Quality: 82"
    assert values["Preview dimensions"] == "640 × 480"
    assert values["Preview size"] == format_size(preview.stat().st_size)
    assert values["Preview stored at"] == str(preview)


def test_preview_property_rows_describe_an_available_video_preview(tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("clip")
    preview = write_video_preview(settings, digest)

    values = dict(preview_property_rows({"content_hash": digest}, settings, "video"))

    assert values["Offline preview"] == "Available"
    assert values["Preview profile"] == (
        "H.264 MP4 · 1 fps · Max height 240 px · CRF 35 · Preset veryfast · No audio"
    )
    assert values["Preview dimensions"] == "64 × 48"
    assert values["Preview duration"] == "0:03.000"
    assert values["Preview size"] == format_size(preview.stat().st_size)
    assert values["Preview stored at"] == str(preview)


def test_preview_property_rows_report_a_recorded_preview_that_has_disappeared(tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("gone")
    record = {
        "content_hash": digest,
        "preview_status": "available",
        "preview_profile_id": settings.image.profile_id,
        "preview_source_hash": digest,
        "preview_updated_at": "2026-08-27T10:00:00.000000+0000",
    }

    values = dict(preview_property_rows(record, settings, "image"))

    assert values["Offline preview"] == (
        "Missing — the last scan recorded a preview, but the file is no longer present "
        "at the expected location"
    )
    assert values["Preview stored at"] == str(cache_for(settings).preview_path("image", digest))
    assert "Preview size" not in values
    assert values["Preview status recorded"] == (
        f"available · {display_db_time('2026-08-27T10:00:00.000000+0000')}"
    )


def test_preview_property_rows_explain_a_failed_generation(tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("broken")
    record = {
        "content_hash": digest,
        "preview_status": "failed",
        "preview_profile_id": settings.video.profile_id,
        "preview_source_hash": digest,
        "preview_error_stage": "ffmpeg-exit",
        "preview_error_message": "FFmpeg exited with code 1.",
    }

    values = dict(preview_property_rows(record, settings, "video"))

    assert values["Offline preview"] == (
        "Preview generation failed during the last scan: FFmpeg exited with code 1. "
        "(stage: ffmpeg-exit)"
    )
    assert values["Preview status recorded"] == "failed"


def test_preview_property_rows_when_nothing_was_stored_depend_on_the_enabled_flag(tmp_path):
    settings = enabled_settings(tmp_path)
    record = {"content_hash": digest_for("new")}

    disabled = dict(preview_property_rows(record, settings.with_enabled(False), "image"))
    assert disabled["Offline preview"] == (
        "Not generated — offline preview generation is disabled in Settings"
    )
    assert "Preview status recorded" not in disabled

    enabled = dict(preview_property_rows(record, settings, "image"))
    assert enabled["Offline preview"] == (
        "Not generated — rescan this volume with offline previews enabled"
    )

    unhashed = dict(preview_property_rows({"content_hash": None}, settings, "image"))
    assert unhashed["Offline preview"].startswith("Not available — no SHA-256 hash is recorded")
    assert "Preview stored at" not in unhashed


def test_preview_property_rows_flag_a_status_recorded_for_previous_content(tmp_path):
    settings = enabled_settings(tmp_path)
    record = {
        "content_hash": digest_for("new-content"),
        "preview_status": "failed",
        "preview_profile_id": "jpeg-max1024-q75",
        "preview_source_hash": digest_for("old-content"),
        "preview_error_stage": "image-decode",
        "preview_error_message": "Image decoder could not read the file.",
    }

    values = dict(preview_property_rows(record, settings, "image"))

    # The stale failure must not be presented as this content's failure.
    assert values["Offline preview"] == (
        "Not generated — rescan this volume with offline previews enabled"
    )
    note = values["Preview status recorded"]
    assert note.startswith("failed")
    assert "recorded for previous file content" in note
    assert "recorded for profile jpeg-max1024-q75, current profile is jpeg-max1600-q82" in note


def test_preview_property_rows_report_a_present_but_invalid_preview(tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("corrupt")
    path = cache_for(settings).preview_path("image", digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a jpeg at all")

    values = dict(preview_property_rows({"content_hash": digest}, settings, "image"))

    assert values["Offline preview"].startswith("Present but invalid — ")
    assert values["Preview stored at"] == str(path)
    assert "Preview size" not in values


# ---------------------------------------------------------------------------
# catalogue_item_property_rows integration (spec §20)
# ---------------------------------------------------------------------------
def properties_window(**extra) -> SimpleNamespace:
    window = SimpleNamespace(
        db=SimpleNamespace(get_volume=lambda _volume_id: {"name": "Archive"}),
        current_source_path_for_volume=lambda _volume: None,
        physical_path_for_source=lambda _source, _relative: None,
        current_item_exists_text=lambda _path, _connected: "Unavailable",
        catalogue_item_display_name=lambda value: value["name"],
        catalogue_item_type_label=lambda _value: "MP4 video",
        parent_folder_display=lambda _value: "Videos",
        backup_engine=None,
        backup_volume_references={},
        _display_time=lambda value: value or "-",
        _display_optional_count=lambda value: str(value),
        _display_unknown_time=lambda value: value or "Unknown",
    )
    for name, value in extra.items():
        setattr(window, name, value)
    return window


def media_record(name: str, relative_path: str, extension: str, digest: bytes | None) -> dict:
    return {
        "item_type": "file",
        "item_id": 7,
        "volume_id": 3,
        "name": name,
        "relative_path": relative_path,
        "extension": extension,
        "size_bytes": 1234,
        "modified_at": None,
        "missing": 0,
        "scanned_at": "2026-08-27T10:00:00.000000+0000",
        "volume_name": "Archive",
        "parent_id": 4,
        "parent_relative_path": "Videos",
        "content_hash": digest,
        "content_hash_algorithm": "sha256" if digest is not None else None,
        "media_status": "complete",
        "media_source": "ffprobe",
        "media_container": "mov,mp4",
        "media_duration_ms": 2500,
        "media_width": 1920,
        "media_height": 1080,
        "video_codecs": "h264",
        "audio_codecs": "aac",
        "media_sample_rate_hz": 48000,
        "media_channels": 2,
        "media_bit_rate": 7500000,
        "media_message": "",
        "media_probed_at": "2026-08-27T10:00:00.000000+0000",
    }


def test_file_properties_add_offline_preview_rows_for_media_records(tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("clip")
    preview = write_video_preview(settings, digest)
    record = media_record("clip.mp4", "Videos/clip.mp4", "mp4", digest)

    rows = MainWindow.catalogue_item_property_rows(properties_window(preview_settings=settings), record)
    labels = [label for label, _value in rows]
    values = dict(rows)

    # Existing media rows are untouched...
    assert values["Content hash"] == f"SHA-256 · {digest.hex()}"
    assert values["Media details"] == "Collected · ffprobe"
    assert values["Duration"] == "0:02.500"
    assert values["Dimensions"] == "1,920 × 1,080"
    # ...and the offline preview block follows them.
    assert values["Offline preview"] == "Available"
    assert values["Preview duration"] == "0:03.000"
    assert values["Preview stored at"] == str(preview)
    assert labels.index("Offline preview") > labels.index("Media details recorded")
    assert labels.index("Preview stored at") < labels.index("Modified")

    # The fake window from test_app.py has no preview_settings attribute at all.
    legacy = dict(MainWindow.catalogue_item_property_rows(properties_window(), record))
    assert legacy["Offline preview"] == NOT_CONFIGURED_TEXT
    assert legacy["Media details"] == "Collected · ffprobe"

    # Images get the block too; other files do not.
    image_record = media_record("photo.jpg", "Photos/photo.jpg", "jpg", digest_for("photo"))
    image_rows = dict(
        MainWindow.catalogue_item_property_rows(properties_window(preview_settings=settings), image_record)
    )
    assert image_rows["Offline preview"] == "Not generated — rescan this volume with offline previews enabled"
    assert image_rows["Preview profile"] == "JPEG · Max dimension: 1600 px · Quality: 82"

    text_record = media_record("notes.txt", "Docs/notes.txt", "txt", digest_for("notes"))
    text_rows = dict(
        MainWindow.catalogue_item_property_rows(properties_window(preview_settings=settings), text_record)
    )
    assert "Offline preview" not in text_rows
    assert "Media details" not in text_rows


# ---------------------------------------------------------------------------
# Search tab preview button (spec §19)
# ---------------------------------------------------------------------------
def make_search_window(
    catalogue: Catalogue,
    settings: PreviewSettings,
    item,
    *,
    with_preview_button: bool = True,
) -> SimpleNamespace:
    window = SimpleNamespace(
        db=catalogue.db,
        preview_settings=settings,
        selected_search_item=lambda: item,
        selected_search_real_path=lambda: None,
        open_file_button=FakeButton("Open"),
        reveal_file_button=FakeButton("Reveal"),
    )
    if with_preview_button:
        window.open_preview_button = FakeButton("Open Preview")
    bind(
        window,
        "catalogue_ref_for_search_item",
        "catalogue_item_media_kind",
        "preview_info_for_target",
        "preview_unavailable_reason",
    )
    return window


def test_search_selection_enables_play_preview_for_a_video_with_a_preview(catalogue, tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("clip")
    file_id = catalogue.add_file("Videos/clip.mp4", digest)
    write_video_preview(settings, digest)
    window = make_search_window(catalogue, settings, catalogue.search_item(file_id, "Videos/clip.mp4"))

    MainWindow.on_search_selection_changed(window)

    button = window.open_preview_button
    assert button.text() == "Play Preview"
    assert button.isEnabled()
    assert button.toolTip() == DEFAULT_PREVIEW_BUTTON_TOOLTIP
    # Offline original (spec §19): Open/Reveal stay disabled while the preview works.
    assert not window.open_file_button.isEnabled()
    assert not window.reveal_file_button.isEnabled()


def test_search_selection_disables_the_preview_button_with_a_reason_when_missing(
    catalogue, tmp_path
):
    settings = enabled_settings(tmp_path)
    file_id = catalogue.add_file("Photos/alpha.jpg", digest_for("alpha"))
    window = make_search_window(catalogue, settings, catalogue.search_item(file_id, "Photos/alpha.jpg"))

    MainWindow.on_search_selection_changed(window)

    button = window.open_preview_button
    assert button.text() == "Open Preview"
    assert not button.isEnabled()
    assert button.toolTip() == GENERIC_MISSING_REASON

    unhashed_id = catalogue.add_file("Photos/unhashed.png", None)
    window.selected_search_item = lambda: catalogue.search_item(unhashed_id, "Photos/unhashed.png")
    MainWindow.on_search_selection_changed(window)
    assert not button.isEnabled()
    assert button.toolTip() == (
        "No SHA-256 hash is recorded for this file, so no preview path can be derived."
    )


def test_search_selection_disables_the_preview_button_for_a_corrupt_preview_file(
    catalogue, tmp_path
):
    """Spec §46: the search tab's button treats an unreadable preview as unavailable."""

    settings = enabled_settings(tmp_path)
    digest = digest_for("corrupt-clip")
    file_id = catalogue.add_file("Videos/corrupt.mp4", digest)
    write_corrupt_preview(settings, "video", digest)
    window = make_search_window(catalogue, settings, catalogue.search_item(file_id, "Videos/corrupt.mp4"))
    window.open_preview_button.setEnabled(True)

    MainWindow.on_search_selection_changed(window)

    button = window.open_preview_button
    assert button.text() == "Play Preview"
    assert not button.isEnabled()
    assert button.toolTip().startswith(INVALID_PREVIEW_REASON_PREFIX)
    assert len(button.toolTip()) > len(INVALID_PREVIEW_REASON_PREFIX)

    # Replacing the corrupt file with a valid preview makes the button live again.
    write_video_preview(settings, digest)
    MainWindow.on_search_selection_changed(window)
    assert button.isEnabled()
    assert button.toolTip() == DEFAULT_PREVIEW_BUTTON_TOOLTIP


def test_search_selection_disables_the_preview_button_for_folders_and_other_files(
    catalogue, tmp_path
):
    settings = enabled_settings(tmp_path)
    text_id = catalogue.add_file("Docs/notes.txt", digest_for("notes"))
    folder_item = catalogue.search_item(catalogue.root_id, "Docs", item_type="folder")
    window = make_search_window(catalogue, settings, folder_item)

    MainWindow.on_search_selection_changed(window)
    button = window.open_preview_button
    assert not button.isEnabled()
    assert button.text() == "Open Preview"
    assert button.toolTip() == DEFAULT_PREVIEW_BUTTON_TOOLTIP
    assert window.open_file_button.isEnabled()  # folders open in the catalogue

    window.selected_search_item = lambda: catalogue.search_item(text_id, "Docs/notes.txt")
    MainWindow.on_search_selection_changed(window)
    assert not button.isEnabled()
    assert button.toolTip() == "Offline previews are available for images and videos only."

    window.selected_search_item = lambda: None
    button.setEnabled(True)
    MainWindow.on_search_selection_changed(window)
    assert not button.isEnabled()
    assert button.toolTip() == DEFAULT_PREVIEW_BUTTON_TOOLTIP


def test_search_selection_without_a_preview_button_still_updates_the_other_buttons(
    catalogue, tmp_path
):
    folder_item = catalogue.search_item(catalogue.root_id, "Docs", item_type="folder")
    window = make_search_window(
        catalogue, enabled_settings(tmp_path), folder_item, with_preview_button=False
    )
    window.open_file_button.setEnabled(False)

    MainWindow.on_search_selection_changed(window)

    assert window.open_file_button.isEnabled()
    assert not window.reveal_file_button.isEnabled()
    assert not hasattr(window, "open_preview_button")


# ---------------------------------------------------------------------------
# Scan progress and log relay (spec §30, §31)
# ---------------------------------------------------------------------------
def test_scan_progress_relays_preview_phases_and_encode_percentages():
    progress = FakeProgress()
    progress.setRange(0, 10)
    status_bar = FakeStatusBar()
    window = SimpleNamespace(
        scan_cancel_requested=False, scan_progress=progress, statusBar=lambda: status_bar
    )
    message = "Creating video preview · Holiday-2008.mov · 42% of preview encode"

    MainWindow.on_scan_progress(window, 1284, 37, message)

    assert progress.maximum() == 0
    assert progress.format() == f"{message} · 1,284 files catalogued"
    # A busy QProgressBar paints no text, so the phase must reach the status bar (spec §30/§31).
    assert status_bar.messages[-1] == (f"{message} · 1,284 files catalogued", 0)

    MainWindow.on_scan_progress(window, 1285, 37, "Creating image preview · IMG_0001.jpg")
    assert progress.format() == "Creating image preview · IMG_0001.jpg · 1,285 files catalogued"

    MainWindow.on_scan_progress(window, 1286, 37, "E:/Photos")
    assert progress.format() == "Scanning... 1,286 files, 37 folders - E:/Photos"
    assert status_bar.messages[-1][0] == "Scanning... 1,286 files, 37 folders - E:/Photos"

    window.scan_cancel_requested = True
    MainWindow.on_scan_progress(window, 1287, 37, "Creating video preview · x")
    assert progress.format() == "Scanning... 1,286 files, 37 folders - E:/Photos"


def test_scan_log_messages_are_appended_only_when_a_log_widget_exists():
    log = FakeLog()

    MainWindow.on_scan_log_message(SimpleNamespace(scan_log=log), "Offline preview failed (image-decode) for Photos/a.tif")
    assert log.appended == ["Offline preview failed (image-decode) for Photos/a.tif"]

    MainWindow.on_scan_log_message(SimpleNamespace(), "ignored")  # no scan_log: no error


# ---------------------------------------------------------------------------
# End-of-scan reporting (spec §15, §16, §17) - real offscreen MainWindow
# ---------------------------------------------------------------------------
def test_scan_finished_with_preview_failures_reports_every_failure(main_window, monkeypatch, tmp_path):
    window = main_window
    window.preview_settings = enabled_settings(tmp_path)
    monkeypatch.setattr(MainWindow, "refresh_after_catalogue_write", lambda self: None)
    failures = [make_failure(0), make_failure(1, "video"), make_failure(2)]
    statistics = PreviewStatistics(
        mode=MODE_ENABLED,
        image_generated=4,
        image_reused=2,
        image_failed=2,
        video_generated=1,
        video_failed=1,
        bytes_written=18_700_000,
        failures=failures,
    )
    boxes = MessageBoxCapture(monkeypatch, click_text="View Preview Failures")
    dialogs = []
    monkeypatch.setattr(PreviewFailuresDialog, "exec", lambda self: dialogs.append(self) or 0)

    window.on_scan_finished(scan_result(statistics))

    assert window.scan_progress.format() == "Completed with preview errors"
    status_message = window.statusBar().currentMessage()
    assert status_message.startswith("Scan completed with warnings:")
    assert "Offline previews: 5 generated, 2 reused, 3 failed." in status_message

    assert len(boxes.boxes) == 1
    box = boxes.boxes[0]
    assert box.windowTitle() == "Scan Completed with Preview Errors"
    assert "Scan completed with preview errors." in box.text()
    assert "Catalogue indexing succeeded, but some offline previews were not created." in box.text()
    summary = box.informativeText()
    assert summary.startswith("Offline Preview Summary")
    assert str(tmp_path / "previews") in summary
    assert format_size(18_700_000) in summary
    assert button_texts(box) == {"View Preview Failures", "Close"}

    assert len(dialogs) == 1
    dialog = dialogs[0]
    assert dialog.parent() is window
    assert dialog.failures == failures
    assert dialog.table.rowCount() == len(failures)
    assert dialog.heading_label.text() == "3 preview failure(s)"
    assert window.last_preview_statistics.total_failed == 3
    assert window.last_preview_statistics.failures == failures


def test_scan_finished_with_storage_unavailable_offers_the_failure_list(main_window, monkeypatch, tmp_path):
    window = main_window
    window.preview_settings = enabled_settings(tmp_path)
    monkeypatch.setattr(MainWindow, "refresh_after_catalogue_write", lambda self: None)
    statistics = PreviewStatistics(
        mode=MODE_ENABLED,
        image_generated=10,
        storage_skipped=4,
        storage_unavailable_reason="Could not write preview. — No space left on device",
    )
    boxes = MessageBoxCapture(monkeypatch, click_text="View Preview Failures")
    dialogs = []
    monkeypatch.setattr(PreviewFailuresDialog, "exec", lambda self: dialogs.append(self) or 0)

    window.on_scan_finished(scan_result(statistics))

    assert window.scan_progress.format() == "Completed with preview errors"
    assert "4 skipped (storage unavailable)" in window.statusBar().currentMessage()
    box = boxes.boxes[0]
    assert box.windowTitle() == "Scan Completed with Preview Errors"
    assert "Preview generation stopped because preview storage became unavailable." in box.informativeText()
    assert "No space left on device" in box.informativeText()
    assert len(dialogs) == 1
    assert dialogs[0].table.rowCount() == 0
    assert "No space left on device" in dialogs[0].storage_label.text()


def test_scan_finished_with_previews_disabled_shows_no_preview_dialog(main_window, monkeypatch):
    window = main_window
    monkeypatch.setattr(MainWindow, "refresh_after_catalogue_write", lambda self: None)
    boxes = MessageBoxCapture(monkeypatch)
    monkeypatch.setattr(PreviewFailuresDialog, "exec", lambda self: pytest.fail("no failures dialog"))

    window.on_scan_finished(scan_result(PreviewStatistics(mode=MODE_DISABLED)))

    assert boxes.boxes == []
    assert window.scan_progress.format() == "Completed"
    status_message = window.statusBar().currentMessage()
    assert status_message.startswith("Scan completed:")
    assert "warnings" not in status_message
    assert "Offline previews" not in status_message
    assert window.last_preview_statistics.mode == MODE_DISABLED

    # A cancelled scan with nothing attempted is not worth a dialog either.
    window.on_scan_finished(scan_result(PreviewStatistics(mode=MODE_ENABLED), status="cancelled"))
    assert boxes.boxes == []
    assert window.scan_progress.format() == "Cancelled"


def test_scan_finished_with_all_previews_ok_shows_the_summary(main_window, monkeypatch, tmp_path):
    window = main_window
    window.preview_settings = enabled_settings(tmp_path)
    monkeypatch.setattr(MainWindow, "refresh_after_catalogue_write", lambda self: None)
    boxes = MessageBoxCapture(monkeypatch)
    monkeypatch.setattr(PreviewFailuresDialog, "exec", lambda self: pytest.fail("no failures dialog"))
    statistics = PreviewStatistics(mode=MODE_ENABLED, image_generated=2, video_reused=1, bytes_written=2048)

    window.on_scan_finished(scan_result(statistics))

    assert window.scan_progress.format() == "Completed"
    assert "Offline previews: 2 generated, 1 reused, 0 failed." in window.statusBar().currentMessage()
    assert len(boxes.boxes) == 1
    box = boxes.boxes[0]
    assert box.windowTitle() == "Offline Preview Summary"
    assert box.text() == "Scan completed. All offline previews were generated or reused."
    assert box.informativeText() == statistics.summary_text(str(tmp_path / "previews"))
    assert button_texts(box) == {"Close"}


def test_scan_finished_after_a_skipped_preflight_says_so_without_a_dialog(main_window, monkeypatch):
    window = main_window
    monkeypatch.setattr(MainWindow, "refresh_after_catalogue_write", lambda self: None)
    boxes = MessageBoxCapture(monkeypatch)

    window.on_scan_finished(
        scan_result(skipped_preflight_statistics("FFmpeg executable: FFmpeg could not be found on PATH."))
    )

    assert boxes.boxes == []
    assert window.scan_progress.format() == "Completed"
    assert "Offline previews skipped (preflight failed)." in window.statusBar().currentMessage()
    assert window.last_preview_statistics.mode == MODE_SKIPPED_PREFLIGHT


def test_show_preview_failures_falls_back_to_the_last_statistics(main_window, monkeypatch):
    window = main_window
    dialogs = []
    monkeypatch.setattr(PreviewFailuresDialog, "exec", lambda self: dialogs.append(self) or 0)

    window.last_preview_statistics = None
    window.show_preview_failures()
    assert dialogs == []

    failures = [make_failure(0, "video")]
    window.last_preview_statistics = PreviewStatistics(mode=MODE_ENABLED, video_failed=1, failures=failures)
    window.show_preview_failures()
    assert len(dialogs) == 1
    assert dialogs[0].failures == failures


# ---------------------------------------------------------------------------
# Scan-start preflight (spec §26, §27)
# ---------------------------------------------------------------------------
def test_plan_scan_previews_does_nothing_when_previews_are_disabled(tmp_path):
    window = SimpleNamespace(
        preview_settings=PreviewSettings(root_directory=str(tmp_path / "previews")),
        run_preview_preflight=lambda settings: pytest.fail("preflight must not run"),
        ask_preview_preflight_decision=lambda report: pytest.fail("no dialog"),
        show_preferences=lambda: pytest.fail("no settings"),
    )

    assert MainWindow.plan_scan_previews(window) == (None, None)


def test_plan_scan_previews_with_a_passing_preflight_scans_with_previews(app, monkeypatch, tmp_path):
    settings = enabled_settings(tmp_path)
    calls = []

    def fake_preflight(candidate):
        calls.append(candidate)
        return make_report(candidate, passed=True, include_encode_tests=False)

    monkeypatch.setattr("jvvv.app.preflight_preview_configuration", fake_preflight)
    window = SimpleNamespace(
        preview_settings=settings,
        ask_preview_preflight_decision=lambda report: pytest.fail("no dialog"),
        show_preferences=lambda: pytest.fail("no settings"),
    )
    bind(window, "run_preview_preflight")

    assert MainWindow.plan_scan_previews(window) == (settings, None)
    assert calls == [settings]
    assert QApplication.overrideCursor() is None


def plan_window(settings: PreviewSettings, preflight_outcomes: list[bool], decisions: list[str]):
    asked = []
    opened_settings = []
    outcomes = list(preflight_outcomes)

    def run_preflight(candidate):
        return make_report(candidate, passed=outcomes.pop(0), include_encode_tests=False)

    def ask(report):
        asked.append(report)
        return decisions.pop(0)

    window = SimpleNamespace(
        preview_settings=settings,
        run_preview_preflight=run_preflight,
        ask_preview_preflight_decision=ask,
        show_preferences=lambda: opened_settings.append(True),
    )
    return window, asked, opened_settings


def test_plan_scan_previews_scan_without_previews_records_the_failed_step(tmp_path):
    settings = enabled_settings(tmp_path)
    window, asked, opened_settings = plan_window(settings, [False], ["without"])

    plan = MainWindow.plan_scan_previews(window)

    assert plan == (None, f"{FAILED_STEP_LABEL}: {FAILED_STEP_DETAIL}")
    assert len(asked) == 1 and not asked[0].passed
    assert opened_settings == []
    # The one scan proceeds without previews; the setting itself is untouched.
    assert window.preview_settings.enabled is True


def test_plan_scan_previews_cancel_scan_returns_none(tmp_path):
    window, asked, opened_settings = plan_window(enabled_settings(tmp_path), [False], ["cancel"])

    assert MainWindow.plan_scan_previews(window) is None
    assert len(asked) == 1
    assert opened_settings == []


def test_plan_scan_previews_open_settings_re_evaluates_the_fixed_configuration(tmp_path):
    broken = enabled_settings(tmp_path, ffmpeg_path=str(tmp_path / "missing" / "ffmpeg.exe"))
    fixed = enabled_settings(tmp_path)
    window, asked, opened_settings = plan_window(broken, [False, True], ["settings"])

    def show_preferences():
        opened_settings.append(True)
        window.preview_settings = fixed

    window.show_preferences = show_preferences

    assert MainWindow.plan_scan_previews(window) == (fixed, None)
    assert len(asked) == 1
    assert opened_settings == [True]


def test_plan_scan_previews_open_settings_then_disabling_previews_scans_without_them(tmp_path):
    window, asked, opened_settings = plan_window(enabled_settings(tmp_path), [False], ["settings"])

    def show_preferences():
        opened_settings.append(True)
        window.preview_settings = window.preview_settings.with_enabled(False)

    window.show_preferences = show_preferences

    assert MainWindow.plan_scan_previews(window) == (None, None)
    assert opened_settings == [True]


def test_preflight_decision_dialog_explains_the_problem_and_maps_its_buttons(app, monkeypatch, tmp_path):
    parent = QWidget()
    settings = enabled_settings(tmp_path)
    report = make_report(settings, passed=False, include_encode_tests=False)
    try:
        for click, expected in (
            ("Open Settings", "settings"),
            ("Scan Without Previews", "without"),
            ("Cancel Scan", "cancel"),
            (None, "cancel"),
        ):
            capture = MessageBoxCapture(monkeypatch, click_text=click)

            assert MainWindow.ask_preview_preflight_decision(parent, report) == expected

            assert len(capture.boxes) == 1
            box = capture.boxes[0]
            assert box.parent() is parent
            assert box.windowTitle() == "Offline Preview Preflight Failed"
            assert box.text() == (
                "Offline preview generation is enabled, but the configuration is not working."
            )
            assert box.informativeText() == (
                f"Problem:\n{FAILED_STEP_LABEL} failed.\n{FAILED_STEP_DETAIL}"
            )
            assert box.detailedText() == report.report_text()
            # setDetailedText() adds Qt's own "Show Details..." toggle; the
            # three decision buttons are exactly the ones the spec names.
            decision_buttons = button_texts(box) - {"Show Details...", "Hide Details..."}
            assert decision_buttons == {"Open Settings", "Scan Without Previews", "Cancel Scan"}
            assert box.escapeButton().text() == "Cancel Scan"
            assert box.defaultButton().text() == "Cancel Scan"
    finally:
        parent.deleteLater()


# ---------------------------------------------------------------------------
# ScanWorker plumbing
# ---------------------------------------------------------------------------
def fake_scan_result(preview: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status=scan_outcome("completed", preview),
        files_seen=3,
        folders_seen=1,
        errors_count=0,
        message="",
        changes=None,
        files_hashed=3,
        bytes_hashed=30,
        hash_errors=0,
        media_files=2,
        media_metadata_collected=2,
        preview=preview,
    )


def test_scan_worker_passes_skipped_preflight_statistics_to_the_scanner(monkeypatch, tmp_path):
    reason = "FFmpeg executable: FFmpeg could not be found on PATH."
    preview = skipped_preflight_statistics(reason).as_dict()
    captured = {}

    class FakeVolumeScanner:
        def __init__(self, db, **kwargs):
            captured["db"] = db
            captured.update(kwargs)

        def scan(self, volume_id):
            captured["volume_id"] = volume_id
            return fake_scan_result(preview)

    monkeypatch.setattr("jvvv.app.VolumeScanner", FakeVolumeScanner)
    monkeypatch.setattr(
        "jvvv.app.PreviewService",
        lambda *args, **kwargs: pytest.fail("PreviewService must not be built for a skipped scan"),
    )
    worker = ScanWorker(
        tmp_path / "catalogue.jvvv", 7, preview_settings=None, preview_skip_reason=reason
    )
    finished, failed = [], []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert isinstance(captured["db"], Database)
    assert captured["volume_id"] == 7
    assert captured["preview_service"] is None
    statistics = captured["preview_statistics"]
    assert isinstance(statistics, PreviewStatistics)
    assert statistics.mode == MODE_SKIPPED_PREFLIGHT
    assert reason in statistics.message
    assert "user chose to continue" in statistics.message
    assert len(finished) == 1
    assert finished[0]["preview"] == preview
    assert finished[0]["status"] == "completed"
    assert "outcome" not in finished[0]
    assert finished[0]["changes"] == {}


def test_scan_worker_builds_a_preview_service_when_previews_are_enabled(monkeypatch, tmp_path):
    settings = enabled_settings(tmp_path)
    built = []
    captured = {}
    failure = make_failure(0)
    preview = PreviewStatistics(mode=MODE_ENABLED, image_failed=1, failures=[failure]).as_dict()

    class FakePreviewService:
        def __init__(self, given_settings, **kwargs):
            self.settings = given_settings
            self.kwargs = kwargs
            self.statistics = PreviewStatistics()
            built.append(self)

    class FakeVolumeScanner:
        def __init__(self, db, **kwargs):
            captured.update(kwargs)

        def scan(self, volume_id):
            return fake_scan_result(preview)

    monkeypatch.setattr("jvvv.app.PreviewService", FakePreviewService)
    monkeypatch.setattr("jvvv.app.VolumeScanner", FakeVolumeScanner)
    worker = ScanWorker(tmp_path / "catalogue.jvvv", 3, preview_settings=settings)
    finished, failed, logs = [], [], []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)
    worker.log_message.connect(logs.append)

    worker.run()

    assert failed == []
    assert len(built) == 1
    assert built[0].settings == settings
    assert captured["preview_service"] is built[0]
    assert captured["preview_statistics"] is None
    # The service's log lines reach the scan log through the worker signal.
    built[0].kwargs["log_callback"]("Offline preview failed (image-decode) for Photos/photo000.tif")
    assert logs == ["Offline preview failed (image-decode) for Photos/photo000.tif"]
    assert finished[0]["status"] == "completed_with_warnings"
    assert finished[0]["preview"]["failures"][0]["relative_path"] == failure.relative_path
    assert PreviewStatistics.from_dict(finished[0]["preview"]).failures == [failure]


# ---------------------------------------------------------------------------
# Scan log (spec §15, §27, §33)
# ---------------------------------------------------------------------------
def test_load_scan_log_lists_preview_results_skipped_preflights_and_failures(catalogue):
    db = catalogue.db
    enabled = PreviewStatistics(
        mode=MODE_ENABLED,
        image_generated=3,
        image_failed=1,
        video_generated=2,
        bytes_written=12345,
        storage_skipped=4,
        storage_unavailable_reason="No space left on device",
    )
    first_scan = db.start_scan(catalogue.volume_id)
    db.finish_scan(first_scan, "completed", 10, 2, 0, preview_summary=enabled.as_dict())
    skipped = skipped_preflight_statistics("FFmpeg executable: FFmpeg could not be found on PATH.")
    second_scan = db.start_scan(catalogue.volume_id)
    db.finish_scan(second_scan, "completed", 10, 2, 0, preview_summary=skipped.as_dict())
    file_id = catalogue.add_file("Photos/broken.tif", digest_for("broken"))
    catalogue.set_status(
        file_id,
        media_kind="image",
        profile_id="jpeg-max1600-q82",
        status="failed",
        source_hash=digest_for("broken"),
        error_stage="image-decode",
        error_message="Image decoder could not read the file.",
    )
    ok_id = catalogue.add_file("Photos/fine.jpg", digest_for("fine"))
    catalogue.set_status(
        ok_id,
        media_kind="image",
        profile_id="jpeg-max1600-q82",
        status="available",
        source_hash=digest_for("fine"),
    )
    log = FakeLog()
    window = SimpleNamespace(db=db, scan_log=log, _display_time=display_db_time)

    MainWindow.load_scan_log(window, catalogue.volume_id)

    text = log.text
    assert (
        "  Offline previews: images 3 generated, 0 reused, 1 failed; "
        "videos 2 generated, 0 reused, 0 failed; "
        f"{format_size(12345)} written; 4 not attempted (preview storage unavailable)"
    ) in text
    assert "  Offline previews: skipped because the preflight check failed" in text
    assert f"  {skipped.message}" in text
    assert "Offline preview failures recorded for indexed files (1):" in text
    assert "Photos/broken.tif [image-decode]: Image decoder could not read the file." in text
    assert "Photos/fine.jpg" not in text
    # Newest scan first.
    assert text.index("skipped because the preflight check failed") < text.index("images 3 generated")

    MainWindow.load_scan_log(window, None)
    assert log.text == ""
    MainWindow.load_scan_log(SimpleNamespace(db=None, scan_log=log), catalogue.volume_id)
    assert log.text == ""


# ---------------------------------------------------------------------------
# Backup policy (spec §23)
# ---------------------------------------------------------------------------
def test_backup_finished_message_states_the_preview_backup_policy(monkeypatch, tmp_path):
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text, *args: infos.append((title, text))
    )
    progress = FakeProgress()
    status_bar = FakeStatusBar()
    window = SimpleNamespace(scan_progress=progress, statusBar=lambda: status_bar)
    result = BackupResult(
        tmp_path / "archive.jvvv", tmp_path / "archive.zip", 1000, 250, 500, 750, 75.0, {"files": 2}
    )

    MainWindow.on_catalogue_backup_finished(window, result)

    assert len(infos) == 1
    title, text = infos[0]
    assert title == "Catalogue Backup Created"
    assert text.endswith(BACKUP_POLICY_TEXT)
    assert "JVVV catalogue backups do not include offline preview files." in text
    assert "copy the configured preview directory separately" in text
    assert str(tmp_path / "archive.zip") in text
    assert progress.format() == "Catalogue backup created"
    assert status_bar.messages == [("Catalogue backup created.", 5000)]


# ---------------------------------------------------------------------------
# Menu, help, cache manager plumbing (spec §22, §48)
# ---------------------------------------------------------------------------
def test_catalogue_menu_offers_the_preview_cache_manager(main_window):
    window = main_window

    texts = [action.text() for action in window.catalogue_menu.actions()]
    assert "Preview Cache…" in texts
    assert window.preview_cache_action in window.catalogue_actions
    assert "preview" in window.preview_cache_action.toolTip().casefold()
    # Catalogue actions follow the open/closed state like the other entries.
    assert not window.preview_cache_action.isEnabled()
    window._set_catalogue_open(True)
    assert window.preview_cache_action.isEnabled()
    window._set_catalogue_open(False)
    assert not window.preview_cache_action.isEnabled()

    assert isinstance(window.preview_settings, PreviewSettings)
    assert window.preview_cache_dialog is None
    assert window.last_preview_statistics is None
    assert window.open_preview_button.text() == "Open Preview"
    assert not window.open_preview_button.isEnabled()


def test_help_explains_offline_previews_and_the_backup_policy(app):
    dialog = HelpDialog()
    try:
        html = dialog._help_html()
    finally:
        dialog.close()

    assert "Offline previews" in html
    assert "Generate offline previews while scanning" in html
    assert "Open Preview" in html and "Play Preview" in html
    assert "Preview Cache" in html
    assert "do not include offline preview files" in html
    assert "never displays or plays media" in html


def test_show_preview_cache_builds_one_dialog_and_wires_its_requests(monkeypatch, tmp_path):
    created = []
    calls = []

    class FakeCacheDialog:
        def __init__(self, settings, parent=None):
            self.settings = [settings]
            self.parent = parent
            self.shown = 0
            for name in (
                "scan_requested",
                "unreferenced_requested",
                "delete_requested",
                "delete_temporaries_requested",
                "open_folder_requested",
                "cancel_requested",
            ):
                setattr(self, name, FakeSignal())
            created.append(self)

        def set_settings(self, settings):
            self.settings.append(settings)

        def show(self):
            self.shown += 1

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

    monkeypatch.setattr("jvvv.app.PreviewCacheDialog", FakeCacheDialog)
    first = enabled_settings(tmp_path)
    second = PreviewSettings(root_directory=str(tmp_path / "other"))
    window = SimpleNamespace(
        db=object(),
        preview_settings=first,
        preview_cache_dialog=None,
        start_preview_store_scan=lambda: calls.append("scan"),
        start_unreferenced_preview_scan=lambda: calls.append("unreferenced"),
        start_delete_previews=lambda paths: calls.append(("delete", paths)),
        start_delete_temporaries=lambda: calls.append("temporaries"),
        open_preview_folder=lambda: calls.append("open"),
        cancel_preview_store_operation=lambda: calls.append("cancel"),
    )

    MainWindow.show_preview_cache(window)

    dialog = window.preview_cache_dialog
    assert created == [dialog]
    assert dialog.parent is window
    assert dialog.settings == [first]
    assert dialog.shown == 1
    assert calls == ["raise", "activate"]
    calls.clear()
    dialog.scan_requested.emit()
    dialog.unreferenced_requested.emit()
    dialog.delete_requested.emit(["E:/JVVV Previews/images/x/ab/abc.jpg"])
    dialog.delete_temporaries_requested.emit()
    dialog.open_folder_requested.emit()
    dialog.cancel_requested.emit()
    assert calls == [
        "scan",
        "unreferenced",
        ("delete", ["E:/JVVV Previews/images/x/ab/abc.jpg"]),
        "temporaries",
        "open",
        "cancel",
    ]

    window.preview_settings = second
    MainWindow.show_preview_cache(window)
    assert created == [dialog]
    assert dialog.settings == [first, second]
    assert dialog.shown == 2

    closed = SimpleNamespace(db=None, preview_settings=first, preview_cache_dialog=None)
    MainWindow.show_preview_cache(closed)
    assert closed.preview_cache_dialog is None
    assert len(created) == 1


def test_preview_store_results_are_relayed_to_the_cache_dialog(monkeypatch, tmp_path):
    class RecordingCacheDialog:
        def __init__(self):
            self.calls = []

        def set_store_statistics(self, statistics, free_bytes):
            self.calls.append(("stats", statistics, free_bytes))

        def set_unreferenced(self, entries, total_found, *, partial=False):
            assert partial is False  # the worker finished normally in this test
            self.calls.append(("unreferenced", entries, total_found))

        def remove_entries(self, paths):
            self.calls.append(("remove", paths))

        def set_busy(self, busy, message=""):
            self.calls.append(("busy", busy, message))

        def set_progress(self, count, message):
            self.calls.append(("progress", count, message))

    dialog = RecordingCacheDialog()
    status_bar = FakeStatusBar()
    window = SimpleNamespace(
        preview_cache_dialog=dialog,
        statusBar=lambda: status_bar,
        preview_store_worker=object(),
        preview_store_thread=object(),
        preview_store_operation="scan",
    )
    statistics = PreviewStoreStatistics(
        image_count=128_401,
        video_count=18_420,
        image_bytes=2_000_000_000_000,
        video_bytes=700_000_000_000,
        total_bytes=2_700_000_000_000,
        temporary_files=0,
    )
    entry = PreviewEntry(
        path=tmp_path / "x.jpg",
        media_kind="image",
        profile_id="jpeg-max1600-q82",
        sha256="ab" * 32,
        size_bytes=10,
    )
    deleted = [str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")]
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, text, *args: warnings.append((title, text))
    )

    MainWindow.on_preview_store_progress(window, 1000, "images/jpeg-max1600-q82/8f")
    MainWindow.on_preview_store_statistics(window, statistics, 5_400_000_000_000)
    MainWindow.on_preview_store_statistics(window, "garbage", None)
    MainWindow.on_unreferenced_previews(window, [entry, "junk"], 20_001)
    MainWindow.on_previews_deleted(window, 2, deleted, ["c.jpg: Access is denied."])
    MainWindow.clear_preview_store_worker(window)

    assert dialog.calls == [
        ("progress", 1000, "images/jpeg-max1600-q82/8f"),
        ("stats", statistics, 5_400_000_000_000),
        ("stats", None, None),
        ("unreferenced", [entry], 20_001),
        ("remove", deleted),
        ("busy", False, "2 previews deleted; 1 could not be deleted."),
        ("busy", False, ""),
    ]
    assert warnings == [
        (
            "Some Previews Were Not Deleted",
            "2 previews were deleted. 1 could not be deleted:\n\nc.jpg: Access is denied.",
        )
    ]
    assert ("Preview store scanned.", 4000) in status_bar.messages
    assert ("20,001 previews are not referenced by this catalogue.", 6000) in status_bar.messages
    assert ("2 previews deleted.", 5000) in status_bar.messages
    assert window.preview_store_worker is None
    assert window.preview_store_thread is None
    assert window.preview_store_operation == ""

    # Without an open dialog the handlers only touch the status bar.
    bare = SimpleNamespace(preview_cache_dialog=None, statusBar=lambda: status_bar)
    MainWindow.on_preview_store_statistics(bare, statistics, None)
    MainWindow.on_unreferenced_previews(bare, [entry], 1)
    MainWindow.on_previews_deleted(bare, 1, deleted[:1], [])
    assert warnings[-1][0] == "Some Previews Were Not Deleted"  # unchanged: no new warning
    assert status_bar.messages[-1] == ("1 previews deleted.", 5000)


# ---------------------------------------------------------------------------
# No embedded viewer or player anywhere (non-negotiable rule 1)
# ---------------------------------------------------------------------------
def test_no_module_imports_qtmultimedia_and_the_app_has_no_player_widgets():
    package = Path(app_module.__file__).parent
    import_pattern = re.compile(r"^\s*(from|import)\s+PySide6\.QtMultimedia", re.MULTILINE)
    sources = {path.name: path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))}

    assert "app.py" in sources and "preview_ui.py" in sources
    offenders = [name for name, text in sources.items() if import_pattern.search(text)]
    assert offenders == []
    assert not [name for name in sys.modules if name.startswith("PySide6.QtMultimedia")]
    for name in ("QVideoWidget", "QMediaPlayer", "QAudioOutput", "QGraphicsVideoItem"):
        assert not hasattr(app_module, name)
        assert name not in sources["app.py"]
        assert name not in sources["preview_ui.py"]


# ---------------------------------------------------------------------------
# Properties dialog actions (spec §20) and partial unreferenced results (§22)
# ---------------------------------------------------------------------------
def test_preview_property_actions_offer_open_and_reveal_only_for_a_usable_preview(tmp_path):
    from dataclasses import replace

    calls: list[tuple[str, bool]] = []

    def make_window(info):
        return SimpleNamespace(
            preview_info_for_target=lambda target: (
                "video" if target.relative_path.endswith(".mp4") else "image",
                info,
                None,
            ),
            open_preview_for_target=lambda target, reveal=False: calls.append(
                (target.relative_path, reveal)
            ),
        )

    video_record = {
        "item_type": "file",
        "item_id": 7,
        "volume_id": 3,
        "relative_path": "Videos/clip.mp4",
        "missing": 0,
    }
    usable = PreviewFileInfo(
        "video", "h264-1fps-240p-crf35-veryfast", tmp_path / "p.mp4", True, True, 10, 64, 48, 3000, ""
    )

    actions = MainWindow.preview_property_actions(make_window(usable), video_record)
    assert [label for label, _callback in actions] == ["Play Preview", "Reveal Preview"]
    actions[0][1]()
    actions[1][1]()
    assert calls == [("Videos/clip.mp4", False), ("Videos/clip.mp4", True)]

    image_record = {**video_record, "relative_path": "Photos/shot.jpg"}
    image_info = PreviewFileInfo(
        "image", "jpeg-max1600-q82", tmp_path / "p.jpg", True, True, 10, 640, 480, None, ""
    )
    assert [label for label, _ in MainWindow.preview_property_actions(make_window(image_info), image_record)] == [
        "Open Preview",
        "Reveal Preview",
    ]

    # Spec §46: a present-but-invalid preview is not offered; neither is a missing one.
    corrupt = replace(usable, valid=False, message="The MP4 file has no video track.")
    assert MainWindow.preview_property_actions(make_window(corrupt), video_record) == []
    missing = replace(usable, exists=False, valid=False)
    assert MainWindow.preview_property_actions(make_window(missing), video_record) == []
    assert MainWindow.preview_property_actions(make_window(usable), {**video_record, "item_type": "folder"}) == []
    text_window = SimpleNamespace(
        preview_info_for_target=lambda target: (None, None, None),
        open_preview_for_target=lambda *args, **kwargs: calls.append(("unexpected", False)),
    )
    assert MainWindow.preview_property_actions(text_window, {**video_record, "relative_path": "notes.txt"}) == []
    assert ("unexpected", False) not in calls


def test_item_properties_dialog_adds_action_buttons_that_launch_callbacks(app):
    from PySide6.QtGui import QIcon

    from jvvv.app import ItemPropertiesDialog

    calls: list[str] = []
    dialog = ItemPropertiesDialog(
        None,
        QIcon(),
        "clip.mp4",
        "MP4 video",
        [("Name", "clip.mp4")],
        actions=[
            ("Play Preview", lambda: calls.append("play")),
            ("Reveal Preview", lambda: calls.append("reveal")),
        ],
    )
    try:
        assert [button.text() for button in dialog.action_buttons] == ["Play Preview", "Reveal Preview"]
        dialog.action_buttons[0].click()
        dialog.action_buttons[1].click()
        assert calls == ["play", "reveal"]
        assert dialog.details_edit.toPlainText() == "Name: clip.mp4"
    finally:
        dialog.close()

    plain = ItemPropertiesDialog(None, QIcon(), "notes.txt", "TXT file", [("Name", "notes.txt")])
    try:
        assert plain.action_buttons == []
    finally:
        plain.close()


def test_unreferenced_results_after_a_cancelled_comparison_are_marked_partial():
    received: list[tuple[list, int, bool]] = []
    dialog = SimpleNamespace(
        set_unreferenced=lambda entries, total, partial=False: received.append(
            (list(entries), total, partial)
        )
    )
    messages: list[str] = []
    status_bar = SimpleNamespace(showMessage=lambda message, timeout=0: messages.append(message))
    window = SimpleNamespace(
        preview_cache_dialog=dialog,
        preview_store_worker=SimpleNamespace(is_cancelled=lambda: True),
        statusBar=lambda: status_bar,
    )

    MainWindow.on_unreferenced_previews(window, [], 0)
    assert received == [([], 0, True)]
    assert "cancelled" in messages[-1].lower()

    window.preview_store_worker = SimpleNamespace(is_cancelled=lambda: False)
    MainWindow.on_unreferenced_previews(window, [], 3)
    assert received[-1] == ([], 3, False)
    assert messages[-1].startswith("3 previews")


# ---------------------------------------------------------------------------
# Audit follow-ups (spec §15 root in summary, §19 bookkeeping, §22 stale lists)
# ---------------------------------------------------------------------------


def test_context_menu_has_no_explanation_line_when_the_preview_is_usable(monkeypatch, catalogue, tmp_path):
    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    settings = enabled_settings(tmp_path)
    digest = digest_for("usable")
    file_id = catalogue.add_file("Photos/usable.jpg", digest)
    write_image_preview(settings, digest)
    window, _opened = make_menu_window(catalogue, settings)

    menu = MainWindow.build_catalogue_item_context_menu(window, catalogue.target(file_id, "Photos/usable.jpg"))

    assert not any(text and text.startswith("Preview not available") for text in menu.texts())


def test_open_preview_without_a_configured_root_does_not_rewrite_the_status(monkeypatch, catalogue, tmp_path):
    settings = PreviewSettings(enabled=False, root_directory="")
    digest = digest_for("unrooted")
    file_id = catalogue.add_file("Photos/unrooted.jpg", digest)
    catalogue.set_status(
        file_id,
        media_kind="image",
        profile_id=settings.image.profile_id,
        status="available",
        source_hash=digest,
        preview_size=4321,
        preview_width=640,
        preview_height=480,
        generated_at=utc_now(),
    )
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text, *args: infos.append((title, text))
    )
    monkeypatch.setattr(
        "jvvv.app.open_in_file_manager", lambda *args, **kwargs: pytest.fail("must not open")
    )
    window, _refreshed = make_open_window(catalogue, settings)

    MainWindow.open_preview_for_target(window, catalogue.target(file_id, "Photos/unrooted.jpg"))

    assert len(infos) == 1 and infos[0][0] == "Preview Not Available"
    row = catalogue.db.get_file_preview_status(file_id)
    assert row["status"] == "available", "nothing was checked, so nothing may be marked missing"


def test_refresh_after_catalogue_write_invalidates_a_displayed_unreferenced_list():
    class FakeCacheDialog:
        def __init__(self) -> None:
            self.invalidated = 0

        def invalidate_unreferenced(self) -> None:
            self.invalidated += 1

    dialog = FakeCacheDialog()
    window = SimpleNamespace(
        db=object(),
        refresh_volumes=lambda: None,
        perform_search=lambda: None,
        backup_evidence_dialog=None,
        preview_cache_dialog=dialog,
    )

    MainWindow.refresh_after_catalogue_write(window)
    assert dialog.invalidated == 1

    window.preview_cache_dialog = None
    MainWindow.refresh_after_catalogue_write(window)  # no dialog open: nothing to do


def test_scan_summary_names_the_root_the_scan_wrote_to_not_the_current_setting(
    main_window, monkeypatch, tmp_path
):
    window = main_window
    window.preview_settings = enabled_settings(tmp_path)  # root: tmp_path / "previews"
    monkeypatch.setattr(MainWindow, "refresh_after_catalogue_write", lambda self: None)
    scan_root = str(tmp_path / "root-used-by-the-scan")
    statistics = PreviewStatistics(
        mode=MODE_ENABLED, image_generated=3, bytes_written=1000, root=scan_root
    )
    boxes = MessageBoxCapture(monkeypatch, click_text="Close")

    window.on_scan_finished(scan_result(statistics))

    assert len(boxes.boxes) == 1
    summary = boxes.boxes[0].informativeText()
    assert scan_root in summary
    assert str(tmp_path / "previews") not in summary


def test_mark_preview_missing_reports_a_failed_catalogue_update(catalogue, monkeypatch, tmp_path):
    settings = enabled_settings(tmp_path)
    digest = digest_for("stuck")
    file_id = catalogue.add_file("Photos/stuck.jpg", digest)
    catalogue.set_status(
        file_id,
        media_kind="image",
        profile_id=settings.image.profile_id,
        status="available",
        source_hash=digest,
        preview_size=1,
        preview_width=1,
        preview_height=1,
        generated_at=utc_now(),
    )

    def failing_replace(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(catalogue.db, "replace_file_preview_status", failing_replace)
    status_bar = FakeStatusBar()
    window = SimpleNamespace(db=catalogue.db, statusBar=lambda: status_bar)

    MainWindow._mark_preview_missing(window, {"id": file_id})

    assert status_bar.messages, "a failed bookkeeping write must be visible"
    message, _timeout = status_bar.messages[-1]
    assert "could not be updated" in message and "database is locked" in message


def test_start_delete_temporaries_runs_the_worker_and_reports_the_outcome(tmp_path):
    from jvvv.preview_ui import DeleteTemporariesWorker

    settings = enabled_settings(tmp_path)
    started: list[tuple[object, str, str]] = []

    class FakeCacheDialog:
        def __init__(self) -> None:
            self.reports: list[tuple[int, int, list]] = []

        def set_temporaries_deleted(self, deleted, kept, errors):
            self.reports.append((deleted, kept, errors))

    dialog = FakeCacheDialog()
    status_bar = FakeStatusBar()
    window = SimpleNamespace(
        _preview_store_ready=lambda: True,
        preview_settings=settings,
        _start_preview_store_worker=lambda worker, operation, message: started.append((worker, operation, message)),
        preview_cache_dialog=dialog,
        statusBar=lambda: status_bar,
    )
    bind(window, "on_temporaries_deleted")

    MainWindow.start_delete_temporaries(window)

    assert len(started) == 1
    worker, operation, message = started[0]
    assert isinstance(worker, DeleteTemporariesWorker)
    assert operation == "temporaries" and "temporary" in message

    MainWindow.on_temporaries_deleted(window, 4, 1, [])

    assert dialog.reports == [(4, 1, [])]
    assert status_bar.messages[-1][0] == "4 temporary preview files deleted."

    window._preview_store_ready = lambda: False
    MainWindow.start_delete_temporaries(window)
    assert len(started) == 1, "nothing starts while another store operation or scan is running"
