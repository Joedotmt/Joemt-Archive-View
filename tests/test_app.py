from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QDialog

from jvvv.app import (
    CatalogueOpenWorker,
    MainWindow,
    SEARCH_INCLUDE_PATHS_SETTING,
    SearchWorker,
    connected_volume_signature,
    format_exception_diagnostics,
    include_content_timestamp,
    suggested_new_volume_drive_id,
)
from jvvv.database import CatalogueError, create_catalogue
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


def test_preferences_persist_path_search_and_refresh_current_results(monkeypatch):
    events = []

    class FakePreferencesDialog:
        def __init__(self, include_paths, parent):
            events.append(("dialog", include_paths, parent))

        def exec(self):
            return QDialog.DialogCode.Accepted

        def include_paths(self):
            return True

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
    assert ("placeholder", "Search with paths") in events
    assert ("search",) in events
    assert ("sync",) in events


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
        lambda db, items, snapshots: completed.append((db, items, snapshots))
    )
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert progress[0] == (0, 0, "Opening and checking catalogue...")
    assert progress[-1] == (1, 1, "Catalogue ready")
    db, items, snapshots = completed[0]
    assert items == []
    assert isinstance(snapshots, list)
    assert db.get_catalogue_info()["volume_count"] == 0
    db.close()
