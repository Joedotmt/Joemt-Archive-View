from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jvvv.app import (
    MainWindow,
    SearchWorker,
    connected_volume_signature,
    format_exception_diagnostics,
    include_content_timestamp,
    suggested_new_volume_drive_id,
)
from jvvv.database import CatalogueError
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
        search_request_id=0,
        search_thread=None,
        pending_search_request=None,
        _start_search=requests.append,
    )

    MainWindow.perform_search(window)

    assert requests == [(1, Path("catalogue.jvvv"), "report")]


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
        for item_id, name in ((1, "report-a.txt"), (2, "report-b.txt"))
    ]

    class FakeConnection:
        def set_progress_handler(self, callback, steps):
            events.append(("progress", callback is not None, steps))

    class FakeDatabase:
        def __init__(self, path, *, initialize, create):
            events.append(("open", path, initialize, create))
            self.connection = FakeConnection()

        def search(self, query):
            events.append(("search", query))
            return rows

        def close(self):
            events.append(("close",))

    class FakeResolver:
        def resolve(self, result):
            events.append(("resolve", result["volume_id"]))
            return "E:\\"

    monkeypatch.setattr("jvvv.app.Database", FakeDatabase)
    monkeypatch.setattr("jvvv.app.ConnectedVolumeResolver", FakeResolver)
    completed = []
    worker = SearchWorker(Path("catalogue.jvvv"), "report", 9)
    worker.finished.connect(lambda request_id, items: completed.append((request_id, items)))

    worker.run()

    assert completed[0][0] == 9
    assert [item.name for item in completed[0][1]] == ["report-a.txt", "report-b.txt"]
    assert [event for event in events if event[0] == "resolve"] == [("resolve", 12)]
    assert ("open", Path("catalogue.jvvv"), False, False) in events
