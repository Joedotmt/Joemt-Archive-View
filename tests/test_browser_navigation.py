from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt

from jvvv.app import BrowserItem, BrowserTableModel, CatalogueItemRef, MainWindow


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class FakeAction:
    def __init__(self, text: str) -> None:
        self.text = text
        self.enabled = True
        self.triggered = FakeSignal()

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


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


class CapturingBrowserModel:
    def __init__(self) -> None:
        self.items: list[BrowserItem] = []

    def set_items(self, items: list[BrowserItem]) -> None:
        self.items = items


class ValueWidget:
    def __init__(self) -> None:
        self.value = None

    def setText(self, value: str) -> None:
        self.value = value

    def setEnabled(self, value: bool) -> None:
        self.value = value


def folder_record(
    folder_id: int,
    name: str,
    relative_path: str,
    parent_id: int | None,
) -> dict:
    return {
        "id": folder_id,
        "name": name,
        "relative_path": relative_path,
        "parent_id": parent_id,
        "recursive_size_bytes": 123,
        "modified_at": "2026-07-29T12:00:00.000000+0000",
        "missing": 0,
    }


def browser_window_for_directory(folder: dict, parent: dict | None):
    records = {folder["id"]: folder}
    if parent is not None:
        records[parent["id"]] = parent

    class FakeDatabase:
        def get_folder(self, folder_id: int):
            return records.get(folder_id)

        def list_child_folders(self, volume_id: int, folder_id: int):
            return []

        def list_files(self, volume_id: int, folder_id: int):
            return []

    model = CapturingBrowserModel()
    window = SimpleNamespace(
        db=FakeDatabase(),
        browser_model=model,
        current_path_label=ValueWidget(),
        up_button=ValueWidget(),
        file_table=object(),
        apply_table_default_columns=lambda *args, **kwargs: None,
    )
    return window, model


def test_nested_directory_prepends_parent_entry_for_actual_parent_record():
    parent = folder_record(10, "Projects", "Projects", 1)
    current = folder_record(11, "Multigas", "Projects/Multigas", 10)
    window, model = browser_window_for_directory(current, parent)

    MainWindow.load_directory_items(window, volume_id=3, folder_id=current["id"])

    assert len(model.items) == 1
    parent_entry = model.items[0]
    assert parent_entry.is_parent_entry
    assert parent_entry.item_type == "folder"
    assert parent_entry.item_id == parent["id"]
    assert parent_entry.name == ".."
    assert parent_entry.relative_path == parent["relative_path"]


def test_volume_root_does_not_add_parent_entry():
    root = folder_record(1, "Archive", "", None)
    window, model = browser_window_for_directory(root, parent=None)

    MainWindow.load_directory_items(window, volume_id=3, folder_id=root["id"])

    assert model.items == []


def test_parent_entry_stays_first_for_every_browser_sort():
    icons = SimpleNamespace(icon_for=lambda item: None)
    model = BrowserTableModel(icons)
    parent_entry = BrowserItem(
        item_type="folder",
        item_id=1,
        name="..",
        relative_path="",
        type_label="Folder",
        is_parent_entry=True,
    )
    folder = BrowserItem(
        item_type="folder",
        item_id=2,
        name="A folder",
        relative_path="A folder",
        type_label="Folder",
    )
    file_item = BrowserItem(
        item_type="file",
        item_id=3,
        name="z-file.txt",
        relative_path="z-file.txt",
        type_label="TXT file",
        extension="txt",
        size_bytes=999,
    )

    for column in range(model.columnCount()):
        for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
            model.set_items([file_item, folder, parent_entry])
            model.sort(column, order)
            assert model.items[0] is parent_entry


def test_opening_parent_entry_uses_normal_folder_navigation():
    selected_paths = []
    window = SimpleNamespace(
        select_folder_path=selected_paths.append,
        open_real_browser_item=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("parent entry must not be opened as a real file")
        ),
    )
    parent_entry = BrowserItem(
        item_type="folder",
        item_id=10,
        name="..",
        relative_path="Projects",
        type_label="Folder",
        is_parent_entry=True,
    )

    MainWindow.open_browser_item(window, parent_entry)

    assert selected_paths == ["Projects"]


def catalogue_target(item_type: str, *, missing: bool = False) -> CatalogueItemRef:
    return CatalogueItemRef(
        item_type=item_type,
        item_id=7,
        volume_id=3,
        relative_path="Projects/report.txt" if item_type == "file" else "Projects",
        missing=missing,
    )


def build_fake_context_menu(
    monkeypatch,
    target: CatalogueItemRef,
    real_path: Path | None,
    *,
    include_catalogue_location: bool = False,
):
    monkeypatch.setattr("jvvv.app.QMenu", FakeMenu)
    window = SimpleNamespace(
        catalogue_item_real_path=lambda candidate: real_path,
        open_catalogue_item=lambda candidate: None,
        open_catalogue_location_for_item=lambda candidate: None,
        open_catalogue_item_in_file_manager=lambda candidate: None,
        copy_catalogue_item_path=lambda candidate: None,
        show_browser_item_properties=lambda item_type, item_id: None,
    )
    return MainWindow.build_catalogue_item_context_menu(
        window,
        target,
        include_catalogue_location=include_catalogue_location,
    )


def test_catalogue_browser_menu_omits_redundant_catalogue_location(monkeypatch, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("content")

    menu = build_fake_context_menu(monkeypatch, catalogue_target("file"), path)

    assert [item.text if item is not None else None for item in menu.items] == [
        "Open",
        "Open File Location",
        "Copy Path",
        None,
        "Properties",
    ]


def test_search_menu_includes_catalogue_location(monkeypatch, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("content")

    menu = build_fake_context_menu(
        monkeypatch,
        catalogue_target("file"),
        path,
        include_catalogue_location=True,
    )

    assert [item.text if item is not None else None for item in menu.items] == [
        "Open",
        "View in Catalogue",
        "Open File Location",
        "Copy Path",
        None,
        "Properties",
    ]


def test_catalogue_item_context_menu_enables_online_file_actions(monkeypatch, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("content")

    menu = build_fake_context_menu(monkeypatch, catalogue_target("file"), path)

    assert [item.enabled for item in menu.items if item is not None] == [
        True,
        True,
        True,
        True,
    ]


def test_catalogue_item_context_menu_disables_physical_actions_when_offline(monkeypatch):
    menu = build_fake_context_menu(monkeypatch, catalogue_target("file"), real_path=None)

    assert [item.enabled for item in menu.items if item is not None] == [
        False,
        False,
        False,
        True,
    ]


def test_catalogue_item_context_menu_keeps_index_actions_for_missing_item(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "report.txt"
    path.write_text("content")

    menu = build_fake_context_menu(
        monkeypatch,
        catalogue_target("file", missing=True),
        path,
    )

    assert [item.enabled for item in menu.items if item is not None] == [
        False,
        False,
        True,
        True,
    ]


def test_catalogue_item_context_menu_can_open_offline_folder_in_catalogue(monkeypatch):
    menu = build_fake_context_menu(monkeypatch, catalogue_target("folder"), real_path=None)

    assert [item.enabled for item in menu.items if item is not None] == [
        True,
        False,
        False,
        True,
    ]


def test_file_manager_opens_folders_and_reveals_files():
    calls = []
    window = SimpleNamespace(
        open_real_catalogue_item=lambda target, reveal: calls.append((target, reveal))
    )
    folder = catalogue_target("folder")
    file_item = catalogue_target("file")

    MainWindow.open_catalogue_item_in_file_manager(window, folder)
    MainWindow.open_catalogue_item_in_file_manager(window, file_item)

    assert calls == [(folder, False), (file_item, True)]


def test_search_open_keeps_offline_catalogue_folders_available():
    open_button = ValueWidget()
    reveal_button = ValueWidget()
    folder = SimpleNamespace(is_folder=True, missing=False)
    window = SimpleNamespace(
        selected_search_item=lambda: folder,
        selected_search_real_path=lambda: None,
        open_file_button=open_button,
        reveal_file_button=reveal_button,
    )

    MainWindow.on_search_selection_changed(window)

    assert open_button.value is True
    assert reveal_button.value is False


def test_catalogue_navigation_clears_volume_filter_before_switching_tabs():
    events = []
    select_results = iter((False, True))
    window = SimpleNamespace(
        current_volume_id=1,
        select_volume=lambda volume_id: events.append(("select", volume_id))
        or next(select_results),
        volume_filter_edit=SimpleNamespace(clear=lambda: events.append(("clear",))),
        tabs=SimpleNamespace(
            setCurrentWidget=lambda widget: events.append(("tab", widget))
        ),
        browser_tab="catalogue",
        select_folder_path=lambda path: events.append(("folder", path)),
        parent_catalogue_path=lambda path: str(Path(path).parent),
        select_browser_relative_path=lambda *args, **kwargs: None,
    )
    target = CatalogueItemRef(
        item_type="folder",
        item_id=7,
        volume_id=3,
        relative_path="Projects",
    )

    MainWindow.open_catalogue_location_for_item(window, target)

    assert events == [
        ("select", 3),
        ("clear",),
        ("select", 3),
        ("tab", "catalogue"),
        ("folder", "Projects"),
    ]


def test_failed_catalogue_navigation_does_not_leave_search_tab():
    events = []
    window = SimpleNamespace(
        current_volume_id=1,
        select_volume=lambda volume_id: events.append(("select", volume_id)) or False,
        volume_filter_edit=SimpleNamespace(clear=lambda: events.append(("clear",))),
        tabs=SimpleNamespace(
            setCurrentWidget=lambda widget: events.append(("tab", widget))
        ),
        browser_tab="catalogue",
        select_folder_path=lambda path: events.append(("folder", path)),
    )

    MainWindow.open_catalogue_location_for_item(window, catalogue_target("folder"))

    assert events == [("select", 3), ("clear",), ("select", 3)]


def test_search_double_click_uses_the_shared_open_action():
    item = SimpleNamespace(
        item_type="file",
        item_id=7,
        volume_id=3,
        relative_path="Projects/report.txt",
        missing=False,
    )
    opened = []
    window = SimpleNamespace(
        search_model=SimpleNamespace(item_at=lambda index: item),
        catalogue_ref_for_search_item=lambda candidate: MainWindow.catalogue_ref_for_search_item(
            window,
            candidate,
        ),
        open_catalogue_item=opened.append,
    )

    MainWindow.open_search_index(window, object())

    assert opened == [catalogue_target("file")]


def test_tree_context_menu_targets_clicked_folder_without_navigating():
    point = object()
    viewport = object()
    tree_item = SimpleNamespace(data=lambda column, role: 7)
    shown = []

    class FakeTree:
        def itemAt(self, candidate):
            assert candidate is point
            return tree_item

        def viewport(self):
            return viewport

        def setCurrentItem(self, item):
            raise AssertionError("right-click must not navigate the folder tree")

    window = SimpleNamespace(
        db=SimpleNamespace(
            get_folder=lambda folder_id: {
                "id": folder_id,
                "volume_id": 3,
                "relative_path": "Projects",
                "missing": 0,
            }
        ),
        folder_tree=FakeTree(),
        show_catalogue_item_context_menu=lambda target, owner, position: shown.append(
            (target, owner, position)
        ),
    )

    MainWindow.show_folder_tree_context_menu(window, point)

    assert shown == [(catalogue_target("folder"), viewport, point)]
