from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from jvvv import backup_analysis as ba
from jvvv.app import (
    BACKUP_FILTER_OPTIONS,
    BACKUP_METADATA_DISCLAIMER,
    BackupStatusIconProvider,
    BackupEvidenceDialog,
    BrowserItem,
    BrowserTableModel,
    MainWindow,
    ResponsiveStatusDelegate,
    SearchResultsTableModel,
    VolumeTableModel,
    backup_filter_matches,
    item_backup_display,
    volume_backup_display,
)


def test_item_copy_evidence_uses_words_and_explains_metadata_limit():
    status = SimpleNamespace(
        status="likely",
        item_type="file",
        is_stale=False,
        other_drive_count=1,
        other_volume_ids=(2,),
        evidence_text="same relative path, exact size, and modified time",
        analysed_at="2026-08-26T10:00:00.000000+0000",
    )

    display = item_backup_display(status, {2: "AID-002 - Offsite"})

    assert display.state == "strong"
    assert display.text == "Same size & time · 1 other drive"
    assert "AID-002 - Offsite" in display.tooltip
    assert "same relative path" in display.tooltip
    assert "not a byte-for-byte check" in display.tooltip
    assert BACKUP_METADATA_DISCLAIMER in display.tooltip


def test_partial_folder_says_best_single_drive_and_scattered_is_not_green():
    status = SimpleNamespace(
        status="possible",
        item_type="folder",
        is_stale=False,
        other_drive_count=2,
        other_volume_ids=(2, 3),
        evidence_text="some child files match",
        best_target_volume_id=2,
        best_coverage_files_percent=50.0,
        best_coverage_bytes_percent=50.0,
        scattered=True,
    )

    display = item_backup_display(status, {2: "AID-002", 3: "AID-003"})

    assert display.state == "possible"
    assert display.text == "Partial · spread over 2 drives"
    assert "spread across drives" in display.tooltip
    assert "Best single matching drive: AID-002" in display.tooltip
    assert "Best single-drive file coverage: 50%" in display.tooltip


def test_conflict_is_amber_and_system_metadata_is_grey():
    conflict = item_backup_display(
        SimpleNamespace(
            status="ambiguous",
            is_stale=False,
            evidence_text="100 records across 10 drives",
        )
    )
    excluded = item_backup_display(
        SimpleNamespace(
            status="excluded",
            is_stale=False,
            evidence_text="Known operating-system metadata",
        )
    )

    assert (conflict.state, conflict.text) == (
        "possible",
        "Content differs · same name and size",
    )
    assert (excluded.state, excluded.text) == (
        "unknown",
        "N/A · system files only",
    )
    assert "excluded" in excluded.tooltip.casefold()


def test_complete_folder_distinguishes_complete_and_partial_drives():
    display = item_backup_display(
        SimpleNamespace(
            status="likely",
            item_type="folder",
            is_stale=False,
            other_drive_count=2,
            other_volume_ids=(2, 3),
            strong_volume_ids=(2,),
            possible_volume_ids=(3,),
            evidence_text="complete on one drive; partial on another",
        ),
        {2: "AID-002", 3: "AID-003"},
    )

    # Unrecognised evidence text: report the verdict, never claim the structure.
    assert (display.state, display.text) == ("strong", "Copy found · 1 other drive")
    assert "not one this version recognises" in display.tooltip
    assert "layout" not in display.tooltip.casefold()
    assert "Full-match drives: AID-002" in display.tooltip
    assert "Partial or lead drives: AID-003" in display.tooltip


def test_excluded_tooltip_names_the_item_type():
    folder = item_backup_display(
        SimpleNamespace(status="excluded", item_type="folder", is_stale=False),
    )
    file = item_backup_display(
        SimpleNamespace(status="excluded", item_type="file", is_stale=False),
    )

    assert folder.text == file.text == "N/A · system files only"
    assert folder.tooltip.startswith("This folder holds only system files")
    assert file.tooltip.startswith("This is a known system file")
    assert "excluded from copy coverage" in file.tooltip


def test_strong_file_distinguishes_strong_and_possible_only_drives():
    display = item_backup_display(
        SimpleNamespace(
            status="likely",
            item_type="file",
            is_stale=False,
            other_drive_count=2,
            other_volume_ids=(2, 3),
            strong_volume_ids=(2,),
            possible_volume_ids=(3,),
            evidence_text="exact on one drive; name and size only on another",
        ),
        {2: "AID-002", 3: "AID-003"},
    )

    assert display.text == "Same size & time · 1 other drive"
    assert "Name and size also match on 1 more drive" in display.tooltip
    assert "Same-size-and-time drives (no hash): AID-002" in display.tooltip
    assert "Name-and-size-only drives: AID-003" in display.tooltip


def test_single_catalogue_copy_is_red_but_stale_evidence_is_grey():
    single = item_backup_display(SimpleNamespace(status="single", is_stale=False))
    stale = item_backup_display(
        SimpleNamespace(
            status="likely",
            is_stale=True,
            stale_reason="A successfully applied scan changed the catalogue.",
        )
    )

    assert (single.state, single.text) == ("none", "No copy found")
    assert (stale.state, stale.text) == ("unknown", "Outdated · re-run analysis")
    assert backup_filter_matches(single, "attention")
    assert backup_filter_matches(stale, "unknown")


def test_stale_tooltip_qualifies_old_targets_and_reports_omitted_drives():
    references = {volume_id: f"Drive {volume_id}" for volume_id in range(2, 14)}
    display = item_backup_display(
        SimpleNamespace(
            status="likely",
            item_type="file",
            is_stale=True,
            stale_reason="A new scan changed the catalogue.",
            strong_volume_ids=tuple(range(2, 8)),
            possible_volume_ids=tuple(range(8, 14)),
            evidence_text="Saved metadata matched at analysis time.",
        ),
        references,
    )

    assert display.text == "Outdated · re-run analysis"
    assert "Last-analysed same-size-and-time drives (no hash):" in display.tooltip
    assert "Last-analysed name-and-size-only drives:" in display.tooltip
    assert display.tooltip.count("(+1 more)") == 2
    assert "Last-analysed evidence:" in display.tooltip


def test_browser_other_copy_column_has_header_and_row_explanations():
    icons = SimpleNamespace(icon_for=lambda _item: None)
    model = BrowserTableModel(icons)
    item = BrowserItem(
        item_type="file",
        item_id=1,
        name="report.psd",
        relative_path="Project/report.psd",
        type_label="PSD file",
        backup=item_backup_display(SimpleNamespace(status="single", is_stale=False)),
    )
    model.set_items([item])

    assert model.headerData(1, Qt.Orientation.Horizontal) == "Other copies"
    header_tooltip = model.headerData(
        1,
        Qt.Orientation.Horizontal,
        Qt.ItemDataRole.ToolTipRole,
    )
    assert "SHA-256" in header_tooltip
    assert "Aqua (double tick)" in header_tooltip
    assert model.data(model.index(0, 1)) == "No copy found"
    assert "No matching copy" in model.data(
        model.index(0, 1),
        Qt.ItemDataRole.ToolTipRole,
    )


def test_search_volume_status_uses_matching_dots_and_hides_text_when_narrow(
    monkeypatch,
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    icons = SimpleNamespace(icon_for=lambda _item: None)
    status_icons = BackupStatusIconProvider()
    model = SearchResultsTableModel(icons, backup_icons=status_icons)
    model.set_items(
        [
            SimpleNamespace(
                name="connected.psd",
                is_folder=False,
                connected=True,
            ),
            SimpleNamespace(
                name="offline.psd",
                is_folder=False,
                connected=False,
            ),
        ]
    )

    connected_index = model.index(0, 8)
    offline_index = model.index(1, 8)
    assert model.data(connected_index) == "Connected"
    assert model.data(offline_index) == "Offline"
    assert model.data(connected_index, Qt.ItemDataRole.DecorationRole).cacheKey() == (
        status_icons.icon_for_state("strong").cacheKey()
    )
    assert model.data(offline_index, Qt.ItemDataRole.DecorationRole).cacheKey() == (
        status_icons.icon_for_state("none").cacheKey()
    )

    volume_model = VolumeTableModel(backup_icons=status_icons)
    volume_model.set_items(
        [SimpleNamespace(drive_id="AID-002", connected=False)]
    )
    volume_status_index = volume_model.index(0, 7)
    assert volume_model.data(volume_status_index) == "Offline"
    assert volume_model.data(
        volume_status_index,
        Qt.ItemDataRole.DecorationRole,
    ).cacheKey() == status_icons.icon_for_state("none").cacheKey()

    delegate = ResponsiveStatusDelegate()
    pixmap = QPixmap(32, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionViewItem()
    option.rect = pixmap.rect()
    option.decorationSize = QSize(18, 18)
    option.state = option.state | option.state.State_Enabled
    painter = QPainter(pixmap)
    delegate.paint(painter, option, connected_index)
    painter.end()
    dot_color = status_icons.COLORS["strong"]
    assert any(
        pixmap.toImage().pixelColor(x, y).name() == dot_color
        for x in range(pixmap.width())
        for y in range(pixmap.height())
    )
    assert app is not None


def test_verified_dot_is_aqua_with_its_own_icon(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    icons = BackupStatusIconProvider()

    assert BackupStatusIconProvider.COLORS["verified"] == "#1fa9b8"
    assert "conflict" not in BackupStatusIconProvider.COLORS
    verified = icons.icon_for_state("verified")
    strong = icons.icon_for_state("strong")
    assert verified.cacheKey() != strong.cacheKey()
    assert verified.cacheKey() == icons.icon_for_state("verified").cacheKey()

    image = verified.pixmap(16, 16).toImage()
    assert any(
        image.pixelColor(x, y).name() == "#1fa9b8"
        for x in range(image.width())
        for y in range(image.height())
    )
    # The double tick paints more white than the single tick does.
    strong_image = strong.pixmap(16, 16).toImage()

    def white_pixels(picture):
        return sum(
            1
            for x in range(picture.width())
            for y in range(picture.height())
            if picture.pixelColor(x, y).lightness() > 235
            and picture.pixelColor(x, y).alpha() > 0
        )

    assert white_pixels(image) > white_pixels(strong_image)
    assert app is not None


def test_hash_verified_file_is_aqua_and_distinct_from_metadata_only_targets():
    display = item_backup_display(
        SimpleNamespace(
            status="likely",
            item_type="file",
            is_stale=False,
            other_drive_count=3,
            other_volume_ids=(2, 3, 4),
            strong_volume_ids=(2, 3),
            verified_volume_ids=(2,),
            possible_volume_ids=(4,),
            evidence_text="SHA-256 on one drive; metadata fallbacks on two drives.",
        ),
        {2: "HASH", 3: "META", 4: "POSSIBLE"},
    )

    assert display.state == "verified"
    assert display.text == "Hash verified · 1 other drive"
    assert "Also found by name, size and time on 1 more drive" in display.tooltip
    assert "Also found by name and size only on 1 more drive" in display.tooltip
    assert "Hash-verified drives: HASH" in display.tooltip
    assert "Same-size-and-time drives (no hash): META" in display.tooltip
    assert "Name-and-size-only drives: POSSIBLE" in display.tooltip


def test_hash_verified_only_filter_excludes_time_matches():
    verified = item_backup_display(
        SimpleNamespace(
            status="likely",
            item_type="file",
            is_stale=False,
            strong_volume_ids=(2,),
            verified_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FILE_EVIDENCE_HASH_VERIFIED,
        )
    )
    timed = item_backup_display(
        SimpleNamespace(
            status="likely",
            item_type="file",
            is_stale=False,
            strong_volume_ids=(2,),
            verified_volume_ids=(),
            possible_volume_ids=(),
            evidence_text=ba.FILE_EVIDENCE_TIME_MATCH,
        )
    )

    assert ("Hash verified only", "verified") in BACKUP_FILTER_OPTIONS
    assert backup_filter_matches(verified, "verified")
    assert not backup_filter_matches(timed, "verified")
    assert backup_filter_matches(verified, "strong")
    assert backup_filter_matches(timed, "strong")
    assert not backup_filter_matches(verified, "attention")
    assert not backup_filter_matches(timed, "attention")
    assert not backup_filter_matches(timed, "possible")


def test_browser_attention_filter_keeps_parent_navigation_entry():
    parent = BrowserItem(
        item_type="folder",
        item_id=1,
        name="..",
        relative_path="",
        type_label="Folder",
        is_parent_entry=True,
    )
    strong = BrowserItem(
        item_type="file",
        item_id=2,
        name="safe.bin",
        relative_path="safe.bin",
        type_label="BIN file",
        backup=item_backup_display(
            SimpleNamespace(
                status="likely",
                item_type="file",
                is_stale=False,
                other_drive_count=1,
            )
        ),
    )
    single = BrowserItem(
        item_type="file",
        item_id=3,
        name="only.bin",
        relative_path="only.bin",
        type_label="BIN file",
        backup=item_backup_display(SimpleNamespace(status="single", is_stale=False)),
    )

    captured = []
    window = SimpleNamespace(
        current_directory_items=[parent, strong, single],
        browser_backup_filter_combo=SimpleNamespace(currentData=lambda: "attention"),
        browser_model=SimpleNamespace(set_items=captured.append),
    )
    MainWindow.apply_browser_backup_filter(window)

    assert captured == [[parent, single]]


def test_folder_properties_explain_the_v4_verdict():
    record = {
        "item_type": "folder",
        "item_id": 9,
        "volume_id": 1,
        "name": "Show",
        "relative_path": "Events/Show",
        "size_bytes": 5000,
        "recursive_file_count": 10,
        "recursive_subfolder_count": 2,
        "direct_file_count": 4,
        "direct_subfolder_count": 2,
        "stats_updated_at": None,
        "modified_at": None,
        "missing": 0,
        "scanned_at": "2026-09-01T10:00:00.000000+0000",
        "volume_name": "Archive",
        "parent_id": 3,
        "parent_relative_path": "Events",
    }

    def window_for(status):
        return SimpleNamespace(
            db=SimpleNamespace(get_volume=lambda _volume_id: {"name": "Archive"}),
            current_source_path_for_volume=lambda _volume: None,
            physical_path_for_source=lambda _source, _relative: None,
            current_item_exists_text=lambda _path, _connected: "Unavailable",
            catalogue_item_display_name=lambda value: value["name"],
            catalogue_item_type_label=lambda _value: "Folder",
            parent_folder_display=lambda _value: "Events",
            backup_engine=SimpleNamespace(
                folder_status=lambda _item_id: status,
                file_status=lambda _item_id: None,
            ),
            backup_volume_references={2: "AID-002", 3: "AID-003"},
            _display_time=lambda value: value or "-",
            _display_optional_count=lambda value: str(value),
            _display_unknown_time=lambda value: value or "Unknown",
        )

    full = dict(
        MainWindow.catalogue_item_property_rows(
            window_for(
                SimpleNamespace(
                    status="likely",
                    item_type="folder",
                    is_stale=False,
                    other_drive_count=2,
                    other_volume_ids=(2, 3),
                    strong_volume_ids=(2,),
                    possible_volume_ids=(3,),
                    best_target_volume_id=2,
                    matched_files=10,
                    total_files=10,
                    matched_bytes=5000,
                    total_bytes=5000,
                    evidence_text=ba.FOLDER_EVIDENCE_FULL_MATCH_HASHED
                    + ba.FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS,
                )
            ),
            record,
        )
    )
    partial = dict(
        MainWindow.catalogue_item_property_rows(
            window_for(
                SimpleNamespace(
                    status="possible",
                    item_type="folder",
                    is_stale=False,
                    other_drive_count=1,
                    other_volume_ids=(2,),
                    strong_volume_ids=(),
                    possible_volume_ids=(2,),
                    best_target_volume_id=2,
                    matched_files=6,
                    total_files=10,
                    matched_bytes=3000,
                    total_bytes=5000,
                    evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
                )
            ),
            record,
        )
    )

    assert full["Other-copy status"] == "Full match (hashed) · 1 other drive · scan errors"
    assert full["Why"].startswith("The whole folder matches on the other drive")
    assert "access errors" in full["Why"]
    assert full["Whole-folder structure"].startswith("Matches on another drive")
    assert full["Full-match drives"] == "AID-002"
    assert full["Partial or lead drives"] == "AID-003"
    assert "Found on best single drive" not in full

    assert partial["Other-copy status"] == "Partial · 60% files · 60% data"
    assert partial["Why"].startswith("Only part of this folder was found")
    assert partial["Drives with any evidence"] == "AID-002"
    assert "Matching catalogue drives" not in partial
    assert partial["Found on best single drive"] == "60% of files, 60% of bytes"
    assert "Whole-folder structure" not in partial

    # Coverage verdicts: strong_volume_ids are drives holding every file by
    # name, size and time, not whole-folder matches, so the row must not claim
    # a Full match.
    found = dict(
        MainWindow.catalogue_item_property_rows(
            window_for(
                SimpleNamespace(
                    status="likely",
                    item_type="folder",
                    is_stale=False,
                    other_drive_count=2,
                    other_volume_ids=(2, 3),
                    strong_volume_ids=(2,),
                    possible_volume_ids=(3,),
                    best_target_volume_id=2,
                    matched_files=10,
                    total_files=10,
                    matched_bytes=5000,
                    total_bytes=5000,
                    best_coverage_files_percent=100.0,
                    best_coverage_bytes_percent=100.0,
                    evidence_text=ba.FOLDER_EVIDENCE_ALL_FILES_FOUND,
                )
            ),
            record,
        )
    )
    assert found["Other-copy status"] == "All files found · AID-002"
    assert found["Drives holding every file"] == "AID-002"
    assert "Full-match drives" not in found
    assert found["Partial or lead drives"] == "AID-003"
    assert found["Found on best single drive"] == "100% of files, 100% of bytes"
    assert "Whole-folder structure" not in found

    # Weak (name-and-size-only) matches are not in matched_files, so the
    # percentage row would read 0%; the dialog says what was found instead.
    def weak_status(total_files):
        return SimpleNamespace(
            status="possible",
            item_type="folder",
            is_stale=False,
            other_drive_count=1,
            other_volume_ids=(2,),
            strong_volume_ids=(),
            possible_volume_ids=(2,),
            best_target_volume_id=2,
            matched_files=0,
            total_files=total_files,
            matched_bytes=0,
            total_bytes=5000 if total_files else None,
            best_coverage_files_percent=0.0,
            best_coverage_bytes_percent=0.0,
            evidence_text=ba.FOLDER_EVIDENCE_ALL_FILES_WEAK,
        )

    weak = dict(MainWindow.catalogue_item_property_rows(window_for(weak_status(10)), record))
    assert weak["Other-copy status"] == "Found by name & size only · AID-002"
    assert weak["Found by name & size only"] == "all 10 files on AID-002"
    assert "Found on best single drive" not in weak
    assert "0%" not in " ".join(str(value) for value in weak.values())

    weak_unknown_total = dict(
        MainWindow.catalogue_item_property_rows(window_for(weak_status(None)), record)
    )
    assert "Found by name & size only" not in weak_unknown_total
    assert "Found on best single drive" not in weak_unknown_total

    # Some weak-folder files also matched by time: say how many.
    weak_partly_timed = weak_status(10)
    weak_partly_timed.matched_files = 3
    partly = dict(
        MainWindow.catalogue_item_property_rows(window_for(weak_partly_timed), record)
    )
    assert partly["Found by name & size only"] == (
        "all 10 files on AID-002; 3 of them also by modified time"
    )

    # A partial folder whose best drive holds only name-and-size leads must
    # not read 0%.
    leads_only = dict(
        MainWindow.catalogue_item_property_rows(
            window_for(
                SimpleNamespace(
                    status="possible",
                    item_type="folder",
                    is_stale=False,
                    other_drive_count=1,
                    other_volume_ids=(2,),
                    strong_volume_ids=(),
                    possible_volume_ids=(2,),
                    best_target_volume_id=2,
                    matched_files=0,
                    total_files=10,
                    matched_bytes=0,
                    total_bytes=5000,
                    best_coverage_files_percent=0.0,
                    best_coverage_bytes_percent=0.0,
                    evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
                )
            ),
            record,
        )
    )
    assert leads_only["Other-copy status"] == "Partial · leads only · AID-002"
    assert leads_only["Found on best single drive"] == (
        "none by name, size and time; leads only"
    )
    assert "0%" not in " ".join(str(value) for value in leads_only.values())


def test_properties_say_when_the_listed_drives_are_a_sample():
    # The saved id list is capped at 64 while other_drive_count keeps the true
    # number, so a capped row must say it is the first 64 of N.
    references = {volume_id: f"AID-{volume_id:03d}" for volume_id in range(2, 66)}
    record = {
        "item_type": "file",
        "item_id": 11,
        "volume_id": 1,
        "name": "notes.txt",
        "relative_path": "Events/notes.txt",
        "extension": "txt",
        "size_bytes": 512,
        "content_hash": None,
        "content_hash_algorithm": None,
        "modified_at": None,
        "missing": 0,
        "scanned_at": "2026-09-01T10:00:00.000000+0000",
        "volume_name": "Archive",
        "parent_id": 3,
        "parent_relative_path": "Events",
    }

    def rows_for(status):
        window = SimpleNamespace(
            db=SimpleNamespace(get_volume=lambda _volume_id: {"name": "Archive"}),
            current_source_path_for_volume=lambda _volume: None,
            physical_path_for_source=lambda _source, _relative: None,
            current_item_exists_text=lambda _path, _connected: "Unavailable",
            catalogue_item_display_name=lambda value: value["name"],
            catalogue_item_type_label=lambda _value: "TXT file",
            parent_folder_display=lambda _value: "Events",
            backup_engine=SimpleNamespace(
                folder_status=lambda _item_id: None,
                file_status=lambda _item_id: status,
            ),
            backup_volume_references=references,
            _display_time=lambda value: value or "-",
            _display_optional_count=lambda value: str(value),
            _display_unknown_time=lambda value: value or "Unknown",
        )
        return dict(MainWindow.catalogue_item_property_rows(window, record))

    def file_status(other_drive_count, evidence_text):
        return SimpleNamespace(
            status="likely",
            item_type="file",
            is_stale=False,
            other_drive_count=other_drive_count,
            other_volume_ids=tuple(range(2, 66)),
            strong_volume_ids=tuple(range(2, 66)),
            possible_volume_ids=(),
            verified_volume_ids=tuple(range(2, 66)),
            evidence_text=evidence_text,
        )

    capped = rows_for(
        file_status(
            299,
            ba.FILE_EVIDENCE_HASH_VERIFIED
            + ba.FILE_EVIDENCE_REPEATED_CONTENT_NOTE
            + " 900 records across 299 drives.",
        )
    )
    assert capped["Other-copy status"] == "Hash verified · 299 other drives"
    assert capped["Drives with any evidence"].startswith("AID-002, AID-003, ")
    assert capped["Drives with any evidence"].endswith("AID-065 (first 64 of 299 drives)")
    assert capped["Hash-verified drives"].endswith("AID-065 (first 64 of 299 drives)")

    # Every drive listed: no sample note.
    complete = rows_for(file_status(64, ba.FILE_EVIDENCE_HASH_VERIFIED))
    assert "(first" not in complete["Drives with any evidence"]
    assert "(first" not in complete["Hash-verified drives"]


def test_empty_volume_ui_uses_applied_scan_health_not_latest_failed_attempt():
    clean = volume_backup_display(
        SimpleNamespace(health_status="empty"),
        0,
        SimpleNamespace(latest_attempt_status="failed", errors_count=1),
    )
    errored = volume_backup_display(
        SimpleNamespace(health_status="completed_with_errors"),
        0,
        SimpleNamespace(latest_attempt_status="completed", errors_count=3),
    )
    never = volume_backup_display(
        SimpleNamespace(health_status="not_scanned"),
        0,
        None,
    )
    before_analysis_with_system_warning = volume_backup_display(
        None,
        0,
        SimpleNamespace(
            health_status="empty",
            latest_attempt_status="completed",
            latest_attempt_errors=1,
            latest_attempt_ignored_errors=1,
        ),
    )
    before_analysis_with_hash_gap = volume_backup_display(
        None,
        0,
        SimpleNamespace(
            latest_attempt_status="completed",
            latest_attempt_errors=1,
            latest_attempt_hash_errors=1,
        ),
    )
    newly_errored = volume_backup_display(
        SimpleNamespace(health_status="empty"),
        0,
        SimpleNamespace(
            health_status="completed_with_errors",
            latest_attempt_status="completed",
            latest_attempt_errors=1,
        ),
    )
    newly_clean = volume_backup_display(
        SimpleNamespace(health_status="completed_with_errors"),
        0,
        SimpleNamespace(
            health_status="empty",
            latest_attempt_status="completed",
            latest_attempt_errors=0,
        ),
    )

    assert clean.text == "N/A · empty"
    assert errored.text == "Check scan"
    assert never.text == "Not scanned"
    assert before_analysis_with_system_warning.text == "N/A · empty"
    assert before_analysis_with_hash_gap.text == "N/A · empty"
    assert newly_errored.text == "Check scan"
    assert newly_clean.text == "N/A · empty"


def test_populated_volume_with_untrustworthy_denominator_withholds_percentages():
    errored = volume_backup_display(
        SimpleNamespace(
            health_status="completed_with_errors",
            coverage_eligible=False,
            total_files=10,
            total_bytes=1000,
            likely_files=10,
            likely_bytes=1000,
            likely_files_percent=100.0,
            likely_bytes_percent=100.0,
        ),
        10,
    )
    never = volume_backup_display(
        SimpleNamespace(
            health_status="not_scanned",
            coverage_eligible=False,
            total_files=10,
            likely_files_percent=100.0,
        ),
        10,
    )

    assert (errored.state, errored.text) == ("unknown", "Check scan")
    assert "percentages are withheld" in errored.tooltip
    assert (never.state, never.text) == ("unknown", "Not scanned")


def test_volume_coverage_tooltip_uses_the_v4_vocabulary():
    display = volume_backup_display(
        SimpleNamespace(
            status="likely",
            health_status="completed",
            coverage_eligible=True,
            total_files=10,
            total_bytes=1000,
            coverage_files=10,
            coverage_bytes=1000,
            likely_files=8,
            likely_bytes=800,
            possible_files=1,
            ambiguous_files=1,
            likely_files_percent=80.0,
            likely_bytes_percent=80.0,
        ),
        10,
    )

    assert display.text == "80% files · 80% data"
    assert "Hash verified or same size & time on another drive: 8 of 10" in display.tooltip
    assert "Name-and-size-only leads not counted as coverage: 1 files" in display.tooltip
    assert "Content differs (same name and size, different hash), not counted: 1" in (
        display.tooltip
    )
    assert "Too common" not in display.tooltip


def test_backup_report_before_first_analysis_explains_what_will_be_compared(
    monkeypatch,
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()
    dialog.set_records(
        SimpleNamespace(status="not_analyzed", analysed_at=None, is_stale=False),
        [],
        [],
        [
            SimpleNamespace(volume_id=1, indexed_file_count=12),
        ],
        {1: "AID-001"},
    )

    assert dialog.analysis_state_label.text() == "Not analysed"
    assert "12 indexed file records" in dialog.analysis_summary_label.text()
    assert "1 catalogue drive." in dialog.analysis_summary_label.text()
    assert dialog.volume_table.columnCount() == 9
    assert dialog.volume_table.horizontalHeaderItem(2).text() == (
        "Hash verified / same size & time"
    )
    assert dialog.volume_table.horizontalHeaderItem(4).text() == "Content differs"
    assert dialog.mirror_table.columnCount() == 6
    assert app is not None


def test_backup_report_separates_hash_gaps_from_access_errors(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()
    dialog.set_records(
        SimpleNamespace(status="not_analyzed", analysed_at=None, is_stale=False),
        [],
        [],
        [
            SimpleNamespace(
                volume_id=1,
                latest_attempt_status="completed",
                latest_attempt_files=2,
                latest_attempt_folders=1,
                latest_attempt_errors=1,
                latest_attempt_hash_errors=1,
                latest_attempt_ignored_errors=0,
                health_status="healthy",
                applied=True,
            )
        ],
        {1: "AID-001"},
    )

    assert dialog.scan_table.item(0, 2).text() == "Completed · hash gaps"
    assert dialog.scan_table.item(0, 6).text() == "0"
    assert dialog.scan_table.item(0, 7).text() == "1"
    assert "metadata fallback" in dialog.scan_table.item(0, 8).text()
    assert app is not None


def test_backup_report_marks_all_persisted_rows_outdated(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()
    state = SimpleNamespace(
        status="outdated",
        analysed_at="2026-08-20T10:00:00.000000+0000",
        is_stale=True,
        stale_reason="Catalogue contents changed after this analysis.",
    )
    summary = SimpleNamespace(
        volume_id=1,
        total_files=0,
        total_bytes=0,
        likely_files=0,
        possible_files=0,
        ambiguous_files=0,
        excluded_files=0,
        likely_files_percent=None,
        likely_bytes_percent=None,
        health_status="empty",
        is_stale=True,
    )
    mirror = SimpleNamespace(
        source_volume_id=1,
        target_volume_id=2,
        source_coverage_percent=100.0,
        target_coverage_percent=100.0,
        complete_structure=True,
        evidence_text="Complete metadata structure",
        manual_mirror_link=False,
    )
    dialog.set_records(
        state,
        [summary],
        [mirror],
        [],
        {1: "AID-001", 2: "AID-002"},
    )

    assert dialog.volume_table.item(0, 6).text() == "Outdated"
    assert dialog.volume_table.item(0, 8).text() == "Outdated · re-run analysis"
    assert dialog.mirror_table.item(0, 4).text() == "Outdated · re-run analysis"
    assert dialog.mirror_table.item(0, 5).text().startswith("OUTDATED")
    summary_text = dialog.analysis_summary_label.text()
    assert "last completed analysis" in summary_text
    assert "Same size & time" in summary_text
    assert "Too common" not in summary_text
    assert app is not None
