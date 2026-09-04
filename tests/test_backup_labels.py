from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from jvvv import backup_analysis as ba
from jvvv.app import (
    BACKUP_FOLDER_RULE_SENTENCE,
    BackupDisplay,
    BackupEvidenceDialog,
    BackupStatusIconProvider,
    OtherCopiesDelegate,
    backup_filter_matches,
    browser_status_strip_text,
    coverage_percent_text,
    item_backup_display,
)

REFS = {2: "AID-002", 3: "AID-003"}


def folder_status(**overrides):
    base = dict(
        status="possible",
        item_type="folder",
        is_stale=False,
        other_drive_count=1,
        other_volume_ids=(2,),
        best_target_volume_id=2,
        matched_files=10,
        total_files=10,
        matched_bytes=1000,
        total_bytes=1000,
        best_coverage_files_percent=100.0,
        best_coverage_bytes_percent=100.0,
        scattered=False,
        analysed_at="2026-09-01T10:00:00.000000+0000",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_coverage_percent_text_never_rounds_up():
    assert coverage_percent_text(1208, 1208) == "100%"
    assert coverage_percent_text(1207, 1208) == "99.9%"
    assert coverage_percent_text(996, 1000) == "99.6%"
    assert coverage_percent_text(62, 100) == "62%"
    assert coverage_percent_text(1, 100000) == "<0.1%"
    assert coverage_percent_text(0, 10) == "0%"
    # Stored percentages are the fallback when counts are missing.
    assert coverage_percent_text(None, None, 99.96) == "99.9%"
    assert coverage_percent_text(None, None, 100.0) == "100%"
    assert coverage_percent_text(None, None, None) is None
    assert coverage_percent_text(None, 0, None) is None


def test_every_engine_evidence_constant_maps_to_a_reason_key():
    expected = {
        ba.FOLDER_EVIDENCE_TRUSTED_HASHED: "trusted_hashed",
        ba.FOLDER_EVIDENCE_TRUSTED_LEGACY: "trusted_legacy",
        ba.FOLDER_EVIDENCE_MIXED_HASH: "mixed_hash",
        ba.FOLDER_EVIDENCE_UNTRUSTED_SCAN: "untrusted_scan",
        ba.FOLDER_EVIDENCE_UNTRUSTED_AND_RENAMED: "untrusted_and_renamed",
        ba.FOLDER_EVIDENCE_RENAMED: "renamed",
        ba.FOLDER_EVIDENCE_COVERAGE: "coverage",
        ba.FOLDER_EVIDENCE_AMBIGUOUS: "ambiguous",
        ba.FOLDER_EVIDENCE_SINGLE: "single",
        ba.FOLDER_EVIDENCE_EMPTY: "empty",
        ba.FOLDER_EVIDENCE_EXCLUDED: "excluded",
        ba.FOLDER_EVIDENCE_UNKNOWN_SCAN: "unknown_scan",
    }
    for text, key in expected.items():
        assert ba.folder_reason_key(text) == key
    suppressed = ba.FOLDER_EVIDENCE_TRUSTED_LEGACY + ba.FOLDER_EVIDENCE_SUPPRESSED_SUFFIX
    assert ba.folder_reason_key(suppressed) == "trusted_legacy"
    assert ba.folder_reason_key("free text from an older run") == ""
    assert ba.folder_reason_key(None) == ""

    hashed = ba.FILE_EVIDENCE_HASH_VERIFIED + ba.FILE_EVIDENCE_PAIRED_SUFFIX
    assert ba.file_reason_key(hashed) == "hash_verified"
    too_common = ba.FILE_EVIDENCE_TOO_COMMON_PREFIX + " 12 records across 4 drives."
    assert ba.file_reason_key(too_common) == "too_common"
    assert ba.file_reason_key(ba.FILE_EVIDENCE_HASH_CONFLICT) == "hash_conflict"
    assert ba.file_reason_key(ba.FILE_EVIDENCE_NAME_SIZE_ONLY) == "name_size_only"


def test_full_match_label_says_whether_hashes_were_compared():
    hashed = item_backup_display(
        folder_status(
            status="likely",
            strong_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FOLDER_EVIDENCE_TRUSTED_HASHED,
        ),
        REFS,
    )
    legacy = item_backup_display(
        folder_status(
            status="likely",
            strong_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FOLDER_EVIDENCE_TRUSTED_LEGACY,
        ),
        REFS,
    )

    assert (hashed.state, hashed.text) == ("strong", "Full match (hashed) · 1 other drive")
    assert (legacy.state, legacy.text) == (
        "strong",
        "Full match (no hashes) · 1 other drive",
    )
    assert "not a byte-for-byte check" in legacy.tooltip
    assert "%" not in hashed.text and "%" not in legacy.text


def test_structural_reasons_lead_with_the_hedge_and_never_print_a_percentage():
    cases = {
        ba.FOLDER_EVIDENCE_UNTRUSTED_SCAN: "Not confirmed · a scan had errors",
        ba.FOLDER_EVIDENCE_RENAMED: "Not confirmed · different folder name there",
        ba.FOLDER_EVIDENCE_MIXED_HASH: "Not confirmed · some files lack saved hashes",
        ba.FOLDER_EVIDENCE_UNTRUSTED_AND_RENAMED: (
            "Not confirmed · scan had errors, name differs"
        ),
    }
    for evidence, expected in cases.items():
        display = item_backup_display(folder_status(evidence_text=evidence), REFS)
        assert display.state == "possible"
        assert display.text == expected
        assert "%" not in display.text
        assert "Why:" in display.tooltip
        assert BACKUP_FOLDER_RULE_SENTENCE in display.tooltip
        assert "Best single matching drive: AID-002" in display.tooltip
        # The engine hard-codes 100/100 for these; repeating it would only
        # recreate the "100% but not complete" confusion.
        assert "coverage: 100%" not in display.tooltip

    errors = item_backup_display(
        folder_status(evidence_text=ba.FOLDER_EVIDENCE_UNTRUSTED_SCAN), REFS
    )
    assert "What would confirm it: A clean, complete re-scan" in errors.tooltip
    assert errors.reason == "untrusted_scan"


def test_single_file_folder_is_explained_instead_of_showing_100_percent():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_COVERAGE,
            matched_files=1,
            total_files=1,
            matched_bytes=5,
            total_bytes=5,
        ),
        REFS,
    )

    assert display.text == "Not confirmed · single-file folder"
    assert "at least two files" in display.tooltip
    assert "%" not in display.text


def test_all_files_found_one_by_one_is_not_confirmed():
    display = item_backup_display(
        folder_status(evidence_text=ba.FOLDER_EVIDENCE_COVERAGE), REFS
    )

    assert display.text == "Not confirmed · files found one by one"
    assert "Best single-drive file coverage: 100% (10 of 10 files)." in display.tooltip
    assert "does not say whether the other drive's folder also holds items" in (
        display.tooltip
    )
    assert BACKUP_FOLDER_RULE_SENTENCE in display.tooltip


def test_partial_folder_floors_percentages_and_shows_counts():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_COVERAGE,
            matched_files=1207,
            total_files=1208,
            matched_bytes=999,
            total_bytes=1000,
            best_coverage_files_percent=99.917,
            best_coverage_bytes_percent=99.9,
        ),
        REFS,
    )

    assert display.text == "Partial · 99.9% files · 99.9% data"
    assert "1 file was not found on it" in display.tooltip
    assert display.coverage_fraction < 1.0


def test_scattered_folder_says_spread_over_drives():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_COVERAGE,
            other_drive_count=3,
            other_volume_ids=(2, 3, 4),
            matched_files=87,
            total_files=100,
            matched_bytes=870,
            total_bytes=1000,
            best_coverage_files_percent=87.0,
            best_coverage_bytes_percent=87.0,
            scattered=True,
        ),
        REFS,
    )

    assert display.text == "Partial · spread over 3 drives"
    assert "87%" in display.tooltip
    assert "no single drive holds the whole folder" in display.tooltip


def test_hash_conflict_file_is_amber_content_differs_not_grey_too_common():
    display = item_backup_display(
        SimpleNamespace(
            status="ambiguous",
            item_type="file",
            is_stale=False,
            evidence_text=ba.FILE_EVIDENCE_HASH_CONFLICT,
        )
    )

    assert (display.state, display.text) == (
        "conflict",
        "Content differs · same name and size",
    )
    assert backup_filter_matches(display, "possible")
    assert backup_filter_matches(display, "attention")
    assert not backup_filter_matches(display, "strong")
    assert BackupStatusIconProvider.COLORS["conflict"] == (
        BackupStatusIconProvider.COLORS["possible"]
    )


def test_name_and_size_only_file_never_says_verified():
    display = item_backup_display(
        SimpleNamespace(
            status="possible",
            item_type="file",
            is_stale=False,
            other_drive_count=2,
            other_volume_ids=(2, 3),
            evidence_text=ba.FILE_EVIDENCE_NAME_SIZE_ONLY,
        ),
        REFS,
    )

    assert display.text == "Name and size only · 2 other drives"
    assert "verified" not in display.text.casefold()
    assert "lead, not a confirmed copy" in display.tooltip


def test_unknown_scan_state_reads_as_cant_tell():
    display = item_backup_display(
        SimpleNamespace(
            status="unknown",
            item_type="folder",
            is_stale=False,
            analysed_at="2026-09-01T10:00:00.000000+0000",
            evidence_text=ba.FOLDER_EVIDENCE_UNKNOWN_SCAN,
        )
    )

    assert (display.state, display.text) == ("unknown", "Can't tell · scan incomplete")


def test_sort_key_orders_amber_rows_thinnest_first():
    thin = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_COVERAGE,
            matched_files=3,
            total_files=100,
            best_coverage_files_percent=3.0,
            best_coverage_bytes_percent=3.0,
        ),
        REFS,
    )
    thick = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_COVERAGE,
            matched_files=97,
            total_files=100,
            best_coverage_files_percent=97.0,
            best_coverage_bytes_percent=97.0,
        ),
        REFS,
    )
    none = item_backup_display(SimpleNamespace(status="single", is_stale=False))

    assert thin.sort_rank == thick.sort_rank
    assert (thin.sort_rank, thin.coverage_fraction) < (thick.sort_rank, thick.coverage_fraction)
    assert none.coverage_fraction == 0.0


def test_status_strip_explains_parent_amber_children_green():
    parent = item_backup_display(
        folder_status(evidence_text=ba.FOLDER_EVIDENCE_UNTRUSTED_SCAN), REFS
    )
    green = BackupDisplay("strong", "Full match (no hashes) · 1 other drive", "", 3)
    red = BackupDisplay("none", "No copy found", "", 0, 0.0)
    items = [
        SimpleNamespace(
            item_type="folder",
            is_parent_entry=True,
            name="..",
            backup=BackupDisplay("unknown", "Not analysed", "", 2),
        ),
        SimpleNamespace(item_type="folder", is_parent_entry=False, name="Day 1", backup=green),
        SimpleNamespace(item_type="folder", is_parent_entry=False, name="Day 2", backup=green),
        SimpleNamespace(
            item_type="file", is_parent_entry=False, name="Timings.xlsx", backup=red
        ),
        SimpleNamespace(item_type="file", is_parent_entry=False, name="Rider.pdf", backup=green),
    ]

    text = browser_status_strip_text(parent, items)

    assert text.startswith("This folder: Not confirmed · a scan had errors.")
    assert "Folders here: 2 of 2 are a Full match." in text
    assert "Files here: 1 of 2 has no copy found (Timings.xlsx)." in text
    assert text.endswith("Each folder is judged on its own, not added up from its contents.")


def test_status_strip_is_empty_before_any_analysis():
    not_analysed = BackupDisplay("unknown", "Not analysed", "", 2)
    items = [
        SimpleNamespace(item_type="file", is_parent_entry=False, name="a.mov", backup=not_analysed)
    ]

    assert browser_status_strip_text(None, items) == ""
    assert browser_status_strip_text(not_analysed, items) == ""
    assert browser_status_strip_text(None, []) == ""


def test_other_copies_delegate_keeps_the_verdict_word_when_narrow():
    assert OtherCopiesDelegate.short_text("Not confirmed · a scan had errors") == "Not confirmed"
    assert (
        OtherCopiesDelegate.short_text("Full match (no hashes) · 2 other drives")
        == "Full match (no hashes)"
    )
    assert OtherCopiesDelegate.short_text("No copy found") == "No copy found"


def test_status_strip_ignores_children_without_a_verdict():
    stale = BackupDisplay("unknown", "Outdated · re-run analysis", "", 2)
    cant_tell = BackupDisplay("unknown", "Can't tell · scan incomplete", "", 2)
    lead = BackupDisplay("possible", "Name and size only · 1 other drive", "", 1)
    green = BackupDisplay("strong", "Hash verified · 1 other drive", "", 3)

    stale_items = [
        SimpleNamespace(item_type="folder", is_parent_entry=False, name="A", backup=stale),
        SimpleNamespace(item_type="folder", is_parent_entry=False, name="B", backup=cant_tell),
        SimpleNamespace(item_type="file", is_parent_entry=False, name="a.mov", backup=stale),
    ]
    text = browser_status_strip_text(stale, stale_items)
    assert "0 of" not in text
    assert text.startswith("This folder: Outdated · re-run analysis.")

    lead_items = [
        SimpleNamespace(item_type="file", is_parent_entry=False, name="a.mov", backup=lead),
        SimpleNamespace(item_type="file", is_parent_entry=False, name="b.mov", backup=green),
    ]
    text = browser_status_strip_text(None, lead_items)
    assert (
        "Files here: 1 of 2 match on another drive; 1 is a name-and-size lead only."
        in text
    )


def test_missing_record_is_not_blamed_on_the_scan():
    for item_type in ("file", "folder"):
        display = item_backup_display(
            SimpleNamespace(
                status="unknown",
                item_type=item_type,
                is_stale=False,
                analysed_at="2026-09-01T10:00:00.000000+0000",
                evidence_text=ba.ITEM_EVIDENCE_MISSING,
            )
        )
        assert (display.state, display.text) == ("unknown", "Can't tell · record missing")
        assert "access errors" not in display.tooltip
        assert "no longer found this item" in display.tooltip


def test_backup_report_dialog_floors_coverage_like_the_main_window(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()
    state = SimpleNamespace(
        status="completed",
        analysed_at="2026-09-01T10:00:00.000000+0000",
        is_stale=False,
    )
    summary = SimpleNamespace(
        volume_id=1,
        status="likely",
        total_files=1000,
        total_bytes=1000,
        coverage_files=1000,
        coverage_bytes=1000,
        likely_files=996,
        likely_bytes=996,
        possible_files=0,
        ambiguous_files=0,
        excluded_files=0,
        likely_files_percent=99.6,
        likely_bytes_percent=99.6,
        health_status="completed",
        coverage_eligible=True,
        is_stale=False,
    )
    mirror = SimpleNamespace(
        source_volume_id=1,
        target_volume_id=2,
        source_coverage_percent=99.6,
        target_coverage_percent=100.0,
        complete_structure=False,
        evidence_text="partial",
        manual_mirror_link=False,
    )
    dialog.set_records(state, [summary], [mirror], [], {1: "AID-001", 2: "AID-002"})

    assert dialog.volume_table.item(0, 6).text() == "99.6%"
    assert dialog.volume_table.item(0, 7).text() == "99.6%"
    assert dialog.mirror_table.item(0, 2).text() == "99.6%"
    assert dialog.mirror_table.item(0, 3).text() == "100%"
    assert dialog.mirror_table.item(0, 4).text() == "Partial overlap"
    assert app is not None
