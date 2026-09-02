from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from jvvv.app import (
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
    assert display.text == "Strong metadata · 1 other drive"
    assert "AID-002 - Offsite" in display.tooltip
    assert "same relative path" in display.tooltip
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
    assert display.text == "Possible · 50% files · 50% data"
    assert "spread across drives" in display.tooltip
    assert "Best single matching drive: AID-002" in display.tooltip
    assert "Best single-drive file coverage: 50%" in display.tooltip


def test_common_and_system_metadata_states_are_explicitly_grey():
    common = item_backup_display(
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

    assert (common.state, common.text) == ("unknown", "Too common")
    assert "too common" in common.tooltip.casefold()
    assert (excluded.state, excluded.text) == (
        "unknown",
        "N/A · system metadata",
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

    assert display.text == "Complete · 1 other drive"
    assert "Complete structure drives: AID-002" in display.tooltip
    assert "Possible or partial drives: AID-003" in display.tooltip


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

    assert display.text == "Strong metadata · 1 other drive"
    assert "Strong metadata-only drives: AID-002" in display.tooltip
    assert "Possible-only drives: AID-003" in display.tooltip


def test_single_catalogue_copy_is_red_but_stale_evidence_is_grey():
    single = item_backup_display(SimpleNamespace(status="single", is_stale=False))
    stale = item_backup_display(
        SimpleNamespace(
            status="likely",
            is_stale=True,
            stale_reason="A successfully applied scan changed the catalogue.",
        )
    )

    assert (single.state, single.text) == ("none", "None found")
    assert (stale.state, stale.text) == ("unknown", "Outdated")
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

    assert display.text == "Outdated"
    assert "Last-analysed strong metadata-only drives:" in display.tooltip
    assert "Last-analysed possible-only drives:" in display.tooltip
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
    assert "SHA-256" in model.headerData(
        1,
        Qt.Orientation.Horizontal,
        Qt.ItemDataRole.ToolTipRole,
    )
    assert model.data(model.index(0, 1)) == "None found"
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


def test_hash_verified_file_is_distinct_from_metadata_only_targets():
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

    assert display.text == "Hash verified · 1 other drive"
    assert "Hash-verified drives: HASH" in display.tooltip
    assert "Strong metadata-only drives: META" in display.tooltip
    assert "Possible-only drives: POSSIBLE" in display.tooltip


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
    assert dialog.volume_table.item(0, 8).text() == "Outdated"
    assert dialog.mirror_table.item(0, 4).text() == "Outdated"
    assert dialog.mirror_table.item(0, 5).text().startswith("OUTDATED")
    assert "last completed analysis" in dialog.analysis_summary_label.text()
    assert app is not None
