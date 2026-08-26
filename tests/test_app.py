from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QDialog

from jvvv.app import (
    ACCENT_COLOR_SETTING,
    CatalogueInfoWorker,
    CatalogueOpenWorker,
    CATALOGUE_PROBE_INVALID,
    CATALOGUE_PROBE_OK,
    CATALOGUE_PROBE_UNAVAILABLE,
    COLOR_MODE_SETTING,
    MainWindow,
    PreferencesDialog,
    SEARCH_INCLUDE_PATHS_SETTING,
    SearchWorker,
    THEME_STYLE_SETTING,
    connected_volume_signature,
    DriveIdDialog,
    format_exception_diagnostics,
    include_content_timestamp,
    probe_catalogue_location,
    suggested_new_volume_drive_id,
)
from jvvv.database import CatalogueError, create_catalogue
from jvvv.theme import (
    ADOBE_ACCENT_COLOR,
    ADOBE_THEME,
    DARK_MODE,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_COLOR_MODE,
    DEFAULT_THEME_STYLE,
    FUSION_THEME,
    LIGHT_MODE,
)
from jvvv.utils import VolumeSnapshot


def test_content_date_guess_skips_invalid_timestamps():
    assert include_content_timestamp("2024-01-01", "2024-01-02", float("nan")) == (
        "2024-01-01",
        "2024-01-02",
    )


def test_new_volume_drive_id_prefers_aid_volume_label():
    assert suggested_new_volume_drive_id("AID-001", "AID-999") == "AID-001"
    assert suggested_new_volume_drive_id("aid-042", "AID-999") == "AID-042"
    assert suggested_new_volume_drive_id("Archive Drive", "AID-999") == "AID-999"
    assert suggested_new_volume_drive_id("AID-42", "AID-999") == "AID-999"


def test_drive_id_dialog_updates_and_unlocks_the_volume_label(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = DriveIdDialog(
        None,
        suggested_drive_id="AID-042",
        source_path="E:\\",
        volume_label="Archive",
        allow_volume_label_rename=True,
    )

    assert dialog.rename_volume_label_check.text() == "Rename volume label to match Drive ID"
    assert dialog.rename_volume_label_check.isChecked()
    assert dialog.volume_label_edit.text() == "AID-042"
    assert not dialog.volume_label_edit.isEnabled()

    dialog.drive_id_edit.setText("AID-043")
    assert dialog.volume_label_edit.text() == "AID-043"

    dialog.rename_volume_label_check.setChecked(False)
    assert dialog.volume_label_edit.text() == "Archive"
    assert dialog.volume_label_edit.isEnabled()

    dialog.volume_label_edit.setText("Custom Archive")
    dialog.rename_volume_label_check.setChecked(True)
    assert dialog.volume_label_edit.text() == "AID-043"
    assert not dialog.volume_label_edit.isEnabled()

    dialog.rename_volume_label_check.setChecked(False)
    assert dialog.volume_label_edit.text() == "Custom Archive"
    assert app is not None


def test_new_volume_drive_id_can_rename_the_actual_volume(monkeypatch):
    renamed = []

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            assert kwargs["allow_volume_label_rename"] is True

        def exec(self):
            return QDialog.DialogCode.Accepted

        def value(self):
            return "AID-042"

        def should_rename_volume_label(self):
            return True

        def volume_label_value(self):
            return "Archive"

    window = SimpleNamespace(
        db=SimpleNamespace(
            next_drive_id=lambda: "AID-042",
            list_volumes=lambda: [],
        )
    )
    snapshot = VolumeSnapshot(
        source_path="E:\\",
        mount_root="E:\\",
        source_relative_path="",
        identity_kind="windows-volume-guid",
        identity_token="volume-42",
        identity_label="Archive",
    )
    monkeypatch.setattr("jvvv.app.sys.platform", "win32")
    monkeypatch.setattr("jvvv.app.DriveIdDialog", FakeDialog)
    monkeypatch.setattr(
        "jvvv.app.rename_volume_label",
        lambda source_path, label: renamed.append((source_path, label)),
    )

    drive_id = MainWindow.choose_new_volume_drive_id(window, "E:\\", snapshot)

    assert drive_id == "AID-042"
    assert renamed == [("E:\\", "AID-042")]


def test_new_volume_drive_id_can_apply_an_edited_volume_label(monkeypatch):
    renamed = []

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def value(self):
            return "AID-042"

        def should_rename_volume_label(self):
            return False

        def volume_label_value(self):
            return "Custom Archive"

    window = SimpleNamespace(
        db=SimpleNamespace(
            next_drive_id=lambda: "AID-042",
            list_volumes=lambda: [],
        )
    )
    snapshot = VolumeSnapshot(
        source_path="E:\\",
        mount_root="E:\\",
        source_relative_path="",
        identity_kind="windows-volume-guid",
        identity_token="volume-42",
        identity_label="Archive",
    )
    monkeypatch.setattr("jvvv.app.sys.platform", "win32")
    monkeypatch.setattr("jvvv.app.DriveIdDialog", FakeDialog)
    monkeypatch.setattr(
        "jvvv.app.rename_volume_label",
        lambda source_path, label: renamed.append((source_path, label)),
    )

    drive_id = MainWindow.choose_new_volume_drive_id(window, "E:\\", snapshot)

    assert drive_id == "AID-042"
    assert renamed == [("E:\\", "Custom Archive")]


def test_connected_volume_signature_detects_identity_and_mount_root():
    snapshots = [
        VolumeSnapshot(
            source_path="E:\\",
            mount_root="E:\\",
            source_relative_path="",
            identity_kind="Windows-Volume-Guid",
            identity_token="\\\\?\\Volume{BBB}\\",
        ),
        VolumeSnapshot(
            source_path="D:\\",
            mount_root="D:\\",
            source_relative_path="",
            identity_kind="windows-volume-guid",
            identity_token="\\\\?\\Volume{AAA}\\",
        ),
        VolumeSnapshot(
            source_path="Z:\\",
            mount_root="Z:\\",
            source_relative_path="",
            identity_kind="",
            identity_token="",
        ),
    ]

    assert connected_volume_signature(snapshots) == (
        ("windows-volume-guid", "\\\\?\\volume{aaa}\\", "d:\\"),
        ("windows-volume-guid", "\\\\?\\volume{bbb}\\", "e:\\"),
    )


def test_exception_diagnostics_includes_database_context_and_cause():
    try:
        try:
            raise OSError("low-level failure")
        except OSError as cause:
            raise CatalogueError(
                "catalogue failed",
                diagnostic_details="Operation: setting SQLite journal mode to DELETE",
            ) from cause
    except CatalogueError as exc:
        details = format_exception_diagnostics(exc)

    assert "Operation: setting SQLite journal mode to DELETE" in details
    assert "OSError: low-level failure" in details
    assert "CatalogueError: catalogue failed" in details


def test_switching_volume_clears_and_repaints_browser_before_loading():
    events: list[str] = []

    class FakeDatabase:
        def get_volume(self, volume_id: int):
            events.append(f"get:{volume_id}")
            return {"id": volume_id}

    class FakeViewport:
        def __init__(self, name: str) -> None:
            self.name = name

        def repaint(self) -> None:
            events.append(f"repaint:{self.name}")

    folder_viewport = FakeViewport("folders")
    file_viewport = FakeViewport("files")
    window = SimpleNamespace(
        current_volume_id=None,
        db=FakeDatabase(),
        folder_tree=SimpleNamespace(viewport=lambda: folder_viewport),
        file_table=SimpleNamespace(viewport=lambda: file_viewport),
        clear_browser=lambda: events.append("clear"),
        show_volume_details=lambda volume: events.append(f"details:{volume['id']}"),
        load_volume_browser=lambda volume_id: events.append(f"browser:{volume_id}"),
        load_scan_log=lambda volume_id: events.append(f"log:{volume_id}"),
    )

    MainWindow.show_selected_volume(window, 42)

    assert window.current_volume_id == 42
    assert events == [
        "clear",
        "repaint:folders",
        "repaint:files",
        "get:42",
        "details:42",
        "browser:42",
        "log:42",
    ]


def test_perform_search_delegates_database_work_to_worker():
    requests = []

    class FakeDatabase:
        path = Path("catalogue.jvvv")

        def search(self, query: str):
            raise AssertionError("search must not run on the UI thread")

    window = SimpleNamespace(
        db=FakeDatabase(),
        search_edit=SimpleNamespace(text=lambda: " report "),
        search_model=SimpleNamespace(set_items=lambda items: None),
        on_search_selection_changed=lambda: None,
        search_include_paths=False,
        search_request_id=0,
        search_thread=None,
        pending_search_request=None,
        _start_search=requests.append,
    )

    MainWindow.perform_search(window)

    assert requests == [(1, Path("catalogue.jvvv"), "report", False)]


def test_search_empty_state_replaces_results_table():
    class FakeStack:
        current = None

        def setCurrentWidget(self, widget):
            self.current = widget

    table = object()
    empty_state = object()
    stack = FakeStack()
    window = SimpleNamespace(
        search_results_stack=stack,
        search_table=table,
        search_empty_state=empty_state,
    )

    MainWindow.set_search_empty_state(window, True)
    assert stack.current is empty_state

    MainWindow.set_search_empty_state(window, False)
    assert stack.current is table


def test_catalogue_write_refresh_reuses_open_database_connection():
    events = []

    class FakeDatabase:
        def close(self):
            raise AssertionError("refresh must not reopen the catalogue on the UI thread")

    database = FakeDatabase()
    window = SimpleNamespace(
        db=database,
        refresh_volumes=lambda: events.append("volumes"),
        perform_search=lambda: events.append("search"),
    )

    MainWindow.refresh_after_catalogue_write(window)

    assert window.db is database
    assert events == ["volumes", "search"]


def test_show_catalogue_info_delegates_database_work_to_worker():
    requests = []

    class FakeDatabase:
        path = Path("catalogue.jvvv")

        def get_catalogue_info(self):
            raise AssertionError("catalogue info must not run on the UI thread")

    window = SimpleNamespace(
        db=FakeDatabase(),
        catalogue_info_thread=None,
        _catalogue_job_running=lambda: False,
        _start_catalogue_info=requests.append,
    )

    MainWindow.show_catalogue_info(window)

    assert requests == [Path("catalogue.jvvv")]


def test_catalogue_info_worker_uses_a_separate_read_only_connection(monkeypatch):
    events = []
    expected_info = {"volume_count": 3}

    class FakeConnection:
        def set_progress_handler(self, callback, steps):
            events.append(("progress", callback is not None, steps))

        def interrupt(self):
            events.append(("interrupt",))

    class FakeDatabase:
        def __init__(self, path, *, initialize, create, read_only):
            events.append(("open", path, initialize, create, read_only))
            self.connection = FakeConnection()

        def get_catalogue_info(self):
            events.append(("info",))
            return expected_info

        def close(self):
            events.append(("close",))

    monkeypatch.setattr("jvvv.app.Database", FakeDatabase)
    completed = []
    failures = []
    worker = CatalogueInfoWorker(Path("catalogue.jvvv"))
    worker.finished.connect(completed.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert completed == [expected_info]
    assert failures == []
    assert events == [
        ("open", Path("catalogue.jvvv"), False, False, True),
        ("progress", True, 1000),
        ("info",),
        ("progress", False, 0),
        ("close",),
    ]


def test_preferences_persist_path_search_and_refresh_current_results(monkeypatch):
    events = []

    class FakePreferencesDialog:
        def __init__(self, include_paths, theme_style, color_mode, accent_color, parent):
            events.append(
                ("dialog", include_paths, theme_style, color_mode, accent_color, parent)
            )

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

    class FakeSettings:
        def setValue(self, key, value):
            events.append(("setting", key, value))

        def sync(self):
            events.append(("sync",))

    class FakeSearchEdit:
        def text(self):
            return "multigas"

        def setPlaceholderText(self, text):
            events.append(("placeholder", text))

    class FakeStatusBar:
        def showMessage(self, message, timeout):
            events.append(("status", message, timeout))

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    window = SimpleNamespace(
        search_include_paths=False,
        theme_style=ADOBE_THEME,
        color_mode=DARK_MODE,
        accent_color=DEFAULT_ACCENT_COLOR,
        ui_zoom=1.0,
        settings=FakeSettings(),
        search_edit=FakeSearchEdit(),
        db=object(),
        perform_search=lambda: events.append(("search",)),
        search_placeholder_text=lambda: "Search with paths",
        statusBar=lambda: FakeStatusBar(),
    )

    MainWindow.show_preferences(window)

    assert window.search_include_paths is True
    assert ("setting", SEARCH_INCLUDE_PATHS_SETTING, True) in events
    assert ("setting", THEME_STYLE_SETTING, ADOBE_THEME) in events
    assert ("setting", COLOR_MODE_SETTING, DARK_MODE) in events
    assert ("setting", ACCENT_COLOR_SETTING, DEFAULT_ACCENT_COLOR) in events
    assert ("placeholder", "Search with paths") in events
    assert ("search",) in events
    assert ("sync",) in events


def test_preferences_persist_appearance_choices(monkeypatch):
    events = []

    class FakePreferencesDialog:
        def __init__(self, include_paths, theme_style, color_mode, accent_color, parent):
            events.append(
                ("dialog", include_paths, theme_style, color_mode, accent_color, parent)
            )

        def exec(self):
            return QDialog.DialogCode.Accepted

        def include_paths(self):
            return False

        def theme_style(self):
            return FUSION_THEME

        def color_mode(self):
            return LIGHT_MODE

        def accent_color(self):
            return "#3366cc"

    class FakeSettings:
        def setValue(self, key, value):
            events.append(("setting", key, value))

        def sync(self):
            events.append(("sync",))

    class FakeStatusBar:
        def showMessage(self, message, timeout):
            events.append(("status", message, timeout))

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    window = SimpleNamespace(
        search_include_paths=False,
        theme_style=ADOBE_THEME,
        color_mode=DARK_MODE,
        accent_color=DEFAULT_ACCENT_COLOR,
        ui_zoom=1.0,
        settings=FakeSettings(),
        statusBar=lambda: FakeStatusBar(),
    )

    MainWindow.show_preferences(window)

    assert window.theme_style == FUSION_THEME
    assert window.color_mode == LIGHT_MODE
    assert window.accent_color == "#3366cc"
    assert ("setting", THEME_STYLE_SETTING, FUSION_THEME) in events
    assert ("setting", COLOR_MODE_SETTING, LIGHT_MODE) in events
    assert ("setting", ACCENT_COLOR_SETTING, "#3366cc") in events
    assert ("sync",) in events


def test_preferences_preview_is_rolled_back_when_cancelled(monkeypatch):
    events = []

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

        def emit(self, *values):
            self.callback(*values)

    class FakePreferencesDialog:
        def __init__(self, include_paths, theme_style, color_mode, accent_color, parent):
            self.appearance_changed = FakeSignal()

        def exec(self):
            self.appearance_changed.emit(FUSION_THEME, LIGHT_MODE, "#3366cc")
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("jvvv.app.PreferencesDialog", FakePreferencesDialog)
    monkeypatch.setattr(
        MainWindow,
        "apply_appearance",
        lambda self, *appearance: events.append(("appearance", *appearance)),
    )
    window = SimpleNamespace(
        search_include_paths=False,
        theme_style=ADOBE_THEME,
        color_mode=DARK_MODE,
        accent_color=DEFAULT_ACCENT_COLOR,
    )

    MainWindow.show_preferences(window)

    assert events == [
        ("appearance", FUSION_THEME, LIGHT_MODE, "#3366cc"),
        ("appearance", ADOBE_THEME, DARK_MODE, DEFAULT_ACCENT_COLOR),
    ]


def test_reset_theme_restores_all_appearance_defaults():
    events = []

    class FakeCombo:
        def __init__(self, values, current_index):
            self.values = values
            self.current_index = current_index
            self.signals_blocked = False

        def blockSignals(self, blocked):
            previous = self.signals_blocked
            self.signals_blocked = blocked
            return previous

        def findData(self, value):
            return self.values.index(value)

        def setCurrentIndex(self, index):
            self.current_index = index

    dialog = SimpleNamespace(
        theme_combo=FakeCombo([ADOBE_THEME, FUSION_THEME], 1),
        color_mode_combo=FakeCombo([DARK_MODE, LIGHT_MODE], 1),
        _accent_color="#3366cc",
        update_accent_button=lambda: events.append("button"),
        emit_appearance_changed=lambda: events.append("preview"),
    )

    PreferencesDialog.reset_theme(dialog)

    assert (
        dialog.theme_combo.values[dialog.theme_combo.current_index]
        == DEFAULT_THEME_STYLE
    )
    assert (
        dialog.color_mode_combo.values[dialog.color_mode_combo.current_index]
        == DEFAULT_COLOR_MODE
    )
    assert dialog._accent_color == DEFAULT_ACCENT_COLOR
    assert events == ["button", "preview"]


def test_switching_theme_selects_its_signature_accent():
    events = []
    dialog = SimpleNamespace(
        _last_theme_style=FUSION_THEME,
        _accent_color="#3366cc",
        theme_style=lambda: ADOBE_THEME,
        update_accent_button=lambda: events.append("button"),
        emit_appearance_changed=lambda: events.append("preview"),
    )

    PreferencesDialog.on_theme_changed(dialog)

    assert dialog._last_theme_style == ADOBE_THEME
    assert dialog._accent_color == ADOBE_ACCENT_COLOR
    assert events == ["button", "preview"]


def test_open_catalogue_location_reveals_catalogue_file(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "jvvv.app.open_in_file_manager",
        lambda path, reveal: opened.append((path, reveal)),
    )
    window = SimpleNamespace(catalogue_path=Path("archive.jvvv"))

    MainWindow.open_catalogue_location(window)

    assert opened == [(Path("archive.jvvv"), True)]


def test_search_worker_reuses_connected_state_for_results_on_same_volume(monkeypatch):
    events = []

    rows = [
        {
            "item_type": "file",
            "item_id": item_id,
            "name": name,
            "volume_id": 12,
            "drive_id": "AID-012",
            "volume_name": "Archive",
            "relative_path": name,
            "size_bytes": 10,
            "modified_at": None,
            "missing": 0,
            "source_path": "E:\\",
        }
        for item_id, name in (
            (item_id, f"report-{item_id:03d}.txt")
            for item_id in range(1, 502)
        )
    ]

    class FakeConnection:
        def set_progress_handler(self, callback, steps):
            events.append(("progress", callback is not None, steps))

    class FakeDatabase:
        def __init__(self, path, *, initialize, create, read_only):
            events.append(("open", path, initialize, create, read_only))
            self.connection = FakeConnection()

        def iter_search(self, query, *, include_paths):
            events.append(("search", query, include_paths))
            return iter(rows)

        def close(self):
            events.append(("close",))

    class FakeResolver:
        def __init__(self, snapshots=None, *, check_source_path=True):
            events.append(("resolver", snapshots, check_source_path))

        def resolve(self, result):
            events.append(("resolve", result["volume_id"]))
            return "E:\\"

    monkeypatch.setattr("jvvv.app.Database", FakeDatabase)
    monkeypatch.setattr("jvvv.app.ConnectedVolumeResolver", FakeResolver)
    batches = []
    completed = []
    worker = SearchWorker(
        Path("catalogue.jvvv"),
        "report",
        9,
        connected_volume_snapshots=[],
        include_paths=True,
    )
    worker.batch_ready.connect(
        lambda request_id, items: batches.append((request_id, items))
    )
    worker.finished.connect(
        lambda request_id, count: completed.append((request_id, count))
    )

    worker.run()

    assert completed[0][0] == 9
    assert completed[0][1] == 501
    assert [len(items) for _, items in batches] == [500, 1]
    assert batches[0][1][0].name == "report-001.txt"
    assert batches[1][1][0].name == "report-501.txt"
    assert [event for event in events if event[0] == "resolve"] == [("resolve", 12)]
    assert ("resolver", [], False) in events
    assert ("search", "report", True) in events
    assert ("open", Path("catalogue.jvvv"), False, False, True) in events


def test_catalogue_open_worker_opens_and_prepares_catalogue(tmp_path):
    path = tmp_path / "archive.jvvv"
    created = create_catalogue(path)
    created.close()

    progress = []
    completed = []
    failures = []
    worker = CatalogueOpenWorker(path)
    worker.progress.connect(
        lambda value, maximum, message: progress.append((value, maximum, message))
    )
    worker.finished.connect(
        lambda db, items, snapshots, lock: completed.append(
            (db, items, snapshots, lock)
        )
    )
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert progress[0] == (0, 0, "Acquiring catalogue lock...")
    assert progress[1] == (0, 0, "Opening and checking catalogue...")
    assert progress[-1] == (1, 1, "Catalogue ready")
    db, items, snapshots, lock = completed[0]
    assert items == []
    assert isinstance(snapshots, list)
    assert db.get_catalogue_info()["volume_count"] == 0
    db.close()
    lock.unlock()


def test_catalogue_location_probe_is_read_only_and_detects_unavailable_paths(tmp_path):
    path = tmp_path / "archive.jvvv"
    created = create_catalogue(path)
    created.close()

    assert probe_catalogue_location(path) == CATALOGUE_PROBE_OK
    assert probe_catalogue_location(tmp_path / "missing.jvvv") == CATALOGUE_PROBE_UNAVAILABLE

    invalid_path = tmp_path / "invalid.jvvv"
    invalid_path.write_text("not a catalogue")
    assert probe_catalogue_location(invalid_path) == CATALOGUE_PROBE_INVALID


def test_cancel_catalogue_open_kills_an_unresponsive_location_probe():
    events = []

    class FakeProcess:
        def kill(self):
            events.append("kill")

    class FakeControl:
        def setEnabled(self, enabled):
            events.append(("enabled", enabled))

        def setRange(self, minimum, maximum):
            events.append(("range", minimum, maximum))

        def setFormat(self, message):
            events.append(("format", message))

    class FakeStatusBar:
        def showMessage(self, message):
            events.append(("status", message))

    window = SimpleNamespace(
        catalogue_probe_process=FakeProcess(),
        catalogue_open_worker=None,
        catalogue_open_cancel_requested=False,
        catalogue_loading_cancel_button=FakeControl(),
        catalogue_loading_progress=FakeControl(),
        _catalogue_open_in_progress=lambda: True,
        statusBar=lambda: FakeStatusBar(),
    )

    MainWindow.cancel_catalogue_open(window)

    assert window.catalogue_open_cancel_requested is True
    assert events[-1] == "kill"


def test_catalogue_open_worker_can_be_cancelled_before_opening(monkeypatch):
    opened = []
    cancelled = []
    worker = CatalogueOpenWorker(Path("unavailable.jvvv"))
    monkeypatch.setattr("jvvv.app.acquire_catalogue_lock", lambda path: opened.append(path))
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.cancel()
    worker.run()

    assert opened == []
    assert cancelled == [True]


def test_cancel_scan_requests_cancellation_once_and_updates_controls():
    events = []

    class FakeWorker:
        def cancel(self):
            events.append("cancel")

    class FakeControl:
        def setEnabled(self, enabled):
            events.append(("enabled", enabled))

        def setFormat(self, message):
            events.append(("format", message))

    class FakeStatusBar:
        def showMessage(self, message):
            events.append(("status", message))

    window = SimpleNamespace(
        scan_worker=FakeWorker(),
        scan_cancel_requested=False,
        stop_scan_button=FakeControl(),
        scan_progress=FakeControl(),
        statusBar=lambda: FakeStatusBar(),
    )

    MainWindow.cancel_scan(window)
    MainWindow.cancel_scan(window)

    assert window.scan_cancel_requested is True
    assert events == [
        "cancel",
        ("enabled", False),
        ("format", "Cancelling scan..."),
        ("status", "Cancelling scan..."),
    ]


def test_scan_running_ui_only_enables_stop_button_during_active_scan():
    enabled_states = []

    class FakeControl:
        def setEnabled(self, enabled):
            enabled_states.append(enabled)

    window = SimpleNamespace(
        db=object(),
        scan_cancel_requested=False,
        scan_blocked_actions=[],
        scan_blocked_widgets=[],
        stop_scan_button=FakeControl(),
    )

    MainWindow._set_scan_running_ui(window, True)
    MainWindow._set_scan_running_ui(window, False)

    assert enabled_states == [True, False]


def test_catalogue_open_worker_cancel_interrupts_and_releases_resources(monkeypatch):
    events = []

    class FakeConnection:
        def interrupt(self):
            events.append("interrupt")

    class FakeDatabase:
        connection = FakeConnection()

        def list_volumes(self):
            worker.cancel()
            return []

        def close(self):
            events.append("close")

    class FakeLock:
        def unlock(self):
            events.append("unlock")

    worker = CatalogueOpenWorker(Path("catalogue.jvvv"))
    monkeypatch.setattr("jvvv.app.acquire_catalogue_lock", lambda path: FakeLock())
    monkeypatch.setattr("jvvv.app.open_catalogue", lambda path, **kwargs: FakeDatabase())
    monkeypatch.setattr("jvvv.app.list_connected_volume_snapshots", lambda: [])
    completed = []
    failures = []
    cancelled = []
    worker.finished.connect(lambda *args: completed.append(args))
    worker.failed.connect(failures.append)
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.run()

    assert completed == []
    assert failures == []
    assert cancelled == [True]
    assert events == ["interrupt", "close", "unlock"]


def test_cancelled_catalogue_open_ignores_a_late_success():
    events = []

    class FakeDatabase:
        def close(self):
            events.append("close")

    class FakeLock:
        def unlock(self):
            events.append("unlock")

    class FakeStatusBar:
        def showMessage(self, message, timeout):
            events.append(("status", message, timeout))

    window = SimpleNamespace(
        catalogue_open_path=Path("catalogue.jvvv"),
        catalogue_open_cancel_requested=True,
        _set_catalogue_loading=lambda loading: events.append(("loading", loading)),
        _set_catalogue_open=lambda is_open: events.append(("open", is_open)),
        statusBar=lambda: FakeStatusBar(),
    )

    MainWindow.on_catalogue_open_finished(
        window,
        FakeDatabase(),
        [],
        [],
        FakeLock(),
    )

    assert window.catalogue_open_path is None
    assert events == [
        "close",
        "unlock",
        ("loading", False),
        ("open", False),
        ("status", "Catalogue opening cancelled.", 3000),
    ]
