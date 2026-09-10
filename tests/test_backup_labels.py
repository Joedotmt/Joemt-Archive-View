from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QLabel

from jvvv import backup_analysis as ba
from jvvv.app import (
    BACKUP_COLUMN_HEADER_TOOLTIP,
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

REFS = {2: "AID-002", 3: "AID-003", 4: "AID-004"}


def folder_status(**overrides):
    base = dict(
        status="possible",
        item_type="folder",
        is_stale=False,
        other_drive_count=1,
        other_volume_ids=(2,),
        strong_volume_ids=(),
        possible_volume_ids=(2,),
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


def file_status(**overrides):
    base = dict(
        status="likely",
        item_type="file",
        is_stale=False,
        other_drive_count=1,
        other_volume_ids=(2,),
        strong_volume_ids=(2,),
        possible_volume_ids=(),
        verified_volume_ids=(),
        evidence_text=ba.FILE_EVIDENCE_TIME_MATCH,
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
        ba.FOLDER_EVIDENCE_FULL_MATCH_HASHED: "full_match_hashed",
        ba.FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED: "full_match_partly_hashed",
        ba.FOLDER_EVIDENCE_FULL_MATCH_LEGACY: "full_match_legacy",
        ba.FOLDER_EVIDENCE_ALL_FILES_HASHED: "all_files_hashed",
        ba.FOLDER_EVIDENCE_ALL_FILES_FOUND: "all_files_found",
        ba.FOLDER_EVIDENCE_ALL_FILES_WEAK: "all_files_weak",
        ba.FOLDER_EVIDENCE_PARTIAL: "partial",
        ba.FOLDER_EVIDENCE_SINGLE: "single",
        ba.FOLDER_EVIDENCE_EMPTY: "empty",
        ba.FOLDER_EVIDENCE_EXCLUDED: "excluded",
        ba.FOLDER_EVIDENCE_UNKNOWN_SCAN: "unknown_scan",
    }
    for text, key in expected.items():
        assert ba.folder_reason_key(text) == key
    repeats = ba.FOLDER_EVIDENCE_STRUCTURE_REPEATS_PREFIX + " 40 folders across 12 drives."
    assert ba.folder_reason_key(repeats) == "structure_repeats"
    assert ba.evidence_counts(repeats) == (40, 12)
    flagged = (
        ba.FOLDER_EVIDENCE_FULL_MATCH_LEGACY
        + ba.FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS
        + ba.FOLDER_EVIDENCE_SUFFIX_RENAMED
        + ba.FOLDER_EVIDENCE_SUPPRESSED_SUFFIX
    )
    assert ba.folder_reason_key(flagged) == "full_match_legacy"
    assert ba.folder_evidence_flags(flagged) == {"scan_errors", "renamed", "suppressed"}
    assert ba.folder_reason_key("free text from an older run") == ""
    assert ba.folder_reason_key(None) == ""

    hashed = ba.FILE_EVIDENCE_HASH_VERIFIED + ba.FILE_EVIDENCE_PAIRED_SUFFIX
    assert ba.file_reason_key(hashed) == "hash_verified"
    generic = ba.FILE_EVIDENCE_GENERIC_PREFIX + " 12 records across 4 drives."
    assert ba.file_reason_key(generic) == "generic"
    assert ba.evidence_counts(generic) == (12, 4)
    assert ba.file_reason_key(ba.FILE_EVIDENCE_TIME_MATCH) == "time_match"
    assert ba.file_reason_key(ba.FILE_EVIDENCE_HASH_CONFLICT) == "hash_conflict"
    assert ba.file_reason_key(ba.FILE_EVIDENCE_NAME_SIZE_ONLY) == "name_size_only"
    noted = ba.FILE_EVIDENCE_TIME_MATCH + ba.FILE_EVIDENCE_CONFLICT_NOTE
    assert ba.file_reason_key(noted) == "time_match"
    assert ba.file_evidence_flags(noted) == {"conflict"}


def test_full_match_label_says_how_much_was_compared():
    def full_match(evidence):
        return item_backup_display(
            folder_status(
                status="likely",
                strong_volume_ids=(2,),
                possible_volume_ids=(),
                evidence_text=evidence,
            ),
            REFS,
        )

    hashed = full_match(ba.FOLDER_EVIDENCE_FULL_MATCH_HASHED)
    partly = full_match(ba.FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED)
    legacy = full_match(ba.FOLDER_EVIDENCE_FULL_MATCH_LEGACY)

    assert (hashed.state, hashed.text) == ("verified", "Full match (hashed) · 1 other drive")
    assert (partly.state, partly.text) == (
        "strong",
        "Full match (partly hashed) · 1 other drive",
    )
    assert (legacy.state, legacy.text) == (
        "strong",
        "Full match (no hashes) · 1 other drive",
    )
    assert "contents were not compared" in legacy.tooltip
    assert "only partly compared" in partly.tooltip
    assert hashed.sort_rank > legacy.sort_rank
    for display in (hashed, partly, legacy):
        assert "%" not in display.text
        assert "Full-match drives: AID-002" in display.tooltip


def test_full_match_flags_stay_explicit_in_the_cell():
    errors = item_backup_display(
        folder_status(
            status="likely",
            strong_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FOLDER_EVIDENCE_FULL_MATCH_HASHED
            + ba.FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS,
        ),
        REFS,
    )
    renamed = item_backup_display(
        folder_status(
            status="likely",
            strong_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FOLDER_EVIDENCE_FULL_MATCH_LEGACY
            + ba.FOLDER_EVIDENCE_SUFFIX_RENAMED,
        ),
        REFS,
    )
    both = item_backup_display(
        folder_status(
            status="likely",
            strong_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED
            + ba.FOLDER_EVIDENCE_SUFFIX_RENAMED
            + ba.FOLDER_EVIDENCE_SUFFIX_SCAN_ERRORS,
        ),
        REFS,
    )

    assert errors.text == "Full match (hashed) · 1 other drive · scan errors"
    assert errors.state == "verified"
    assert "What would confirm it: a clean, complete re-scan of AID-002" in errors.tooltip
    assert renamed.text == "Full match (no hashes) · 1 other drive · different name there"
    assert "named differently" in renamed.tooltip
    assert both.text == (
        "Full match (partly hashed) · 1 other drive · scan errors · different name there"
    )
    assert OtherCopiesDelegate.short_text(both.text) == "Full match (partly hashed)"


def test_every_folder_reason_key_has_a_state_and_text():
    cases = {
        ba.FOLDER_EVIDENCE_FULL_MATCH_HASHED: (
            "verified",
            "Full match (hashed) · 1 other drive",
        ),
        ba.FOLDER_EVIDENCE_FULL_MATCH_PARTLY_HASHED: (
            "strong",
            "Full match (partly hashed) · 1 other drive",
        ),
        ba.FOLDER_EVIDENCE_FULL_MATCH_LEGACY: (
            "strong",
            "Full match (no hashes) · 1 other drive",
        ),
        ba.FOLDER_EVIDENCE_ALL_FILES_HASHED: (
            "verified",
            "All files hash-verified · AID-002",
        ),
        ba.FOLDER_EVIDENCE_ALL_FILES_FOUND: ("strong", "All files found · AID-002"),
        ba.FOLDER_EVIDENCE_ALL_FILES_WEAK: (
            "possible",
            "Found by name & size only · AID-002",
        ),
        ba.FOLDER_EVIDENCE_PARTIAL: ("possible", "Partial · 60% files · 60% data"),
        ba.FOLDER_EVIDENCE_STRUCTURE_REPEATS_PREFIX
        + " 40 folders across 12 drives.": (
            "possible",
            "Not confirmed · structure repeats on 11 other drives",
        ),
        ba.FOLDER_EVIDENCE_SINGLE: ("none", "No copy found"),
    }
    for evidence, (state, text) in cases.items():
        likely = state in {"verified", "strong"}
        display = item_backup_display(
            folder_status(
                status="likely" if likely else "single" if state == "none" else "possible",
                strong_volume_ids=(2,) if likely else (),
                possible_volume_ids=() if likely or state == "none" else (2,),
                other_volume_ids=() if state == "none" else (2,),
                other_drive_count=0 if state == "none" else 1,
                matched_files=0 if state == "none" else 6 if "PARTIAL" in text.upper() else 10,
                matched_bytes=0 if state == "none" else 600 if "PARTIAL" in text.upper() else 1000,
                best_coverage_files_percent=60.0 if "Partial" in text else 100.0,
                best_coverage_bytes_percent=60.0 if "Partial" in text else 100.0,
                best_target_volume_id=None if state == "none" else 2,
                evidence_text=evidence,
            ),
            REFS,
        )
        assert (display.state, display.text) == (state, text), evidence
        if state == "possible":
            assert BACKUP_FOLDER_RULE_SENTENCE in display.tooltip
        assert display.reason == ba.folder_reason_key(evidence)


def test_single_file_folder_found_by_time_is_green_not_hedged():
    display = item_backup_display(
        folder_status(
            status="likely",
            strong_volume_ids=(2,),
            possible_volume_ids=(),
            evidence_text=ba.FOLDER_EVIDENCE_ALL_FILES_FOUND,
            matched_files=1,
            total_files=1,
            matched_bytes=5,
            total_bytes=5,
        ),
        REFS,
    )

    assert (display.state, display.text) == ("strong", "All files found · AID-002")
    assert "Contents were not compared" in display.tooltip
    assert "does not say whether the other drive's folder also holds items" in (
        display.tooltip
    )
    assert "Not confirmed" not in display.text
    assert "%" not in display.text
    # Coverage verdicts list drives holding every file, not whole-folder matches.
    assert "Drives holding every file: AID-002" in display.tooltip
    assert "Full-match drives" not in display.tooltip


def test_all_files_weak_is_amber_and_never_says_verified():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_ALL_FILES_WEAK,
            matched_files=0,
            matched_bytes=0,
            best_coverage_files_percent=0.0,
            best_coverage_bytes_percent=0.0,
        ),
        REFS,
    )

    assert (display.state, display.text) == (
        "possible",
        "Found by name & size only · AID-002",
    )
    assert "verified" not in display.text.casefold()
    assert "leads rather than confirmed copies" in display.tooltip
    assert "coverage: 0%" not in display.tooltip
    assert backup_filter_matches(display, "possible")
    assert not backup_filter_matches(display, "strong")


def test_partial_folder_floors_percentages_and_shows_counts():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
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
    assert (
        "1 file has no name, size and time match on it; files present there by name "
        "and size only are not counted."
    ) in display.tooltip
    assert "not found on it" not in display.tooltip
    assert "Drives holding every file" not in display.tooltip
    assert "name, size and modified time" in display.tooltip
    assert "Best single-drive file coverage: 99.9% (1,207 of 1,208 files)." in (
        display.tooltip
    )
    assert display.coverage_fraction < 1.0


def test_scattered_folder_says_spread_over_drives():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
            other_drive_count=3,
            other_volume_ids=(2, 3, 4),
            possible_volume_ids=(2, 3, 4),
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


def test_structure_repeats_counts_drives_from_the_evidence():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_STRUCTURE_REPEATS_PREFIX
            + " 1,200 folders across 300 drives.",
            other_drive_count=299,
            other_volume_ids=tuple(range(2, 66)),
            possible_volume_ids=tuple(range(2, 66)),
            best_target_volume_id=None,
        ),
        REFS,
    )

    # "Other drives" convention: the group's 300 drives include this one.
    assert (display.state, display.text) == (
        "possible",
        "Not confirmed · structure repeats on 299 other drives",
    )
    assert "1,200 folders across 300 drives" in display.tooltip
    assert BACKUP_FOLDER_RULE_SENTENCE in display.tooltip


def test_partial_folder_with_only_leads_does_not_say_zero_percent():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
            matched_files=0,
            total_files=10,
            matched_bytes=0,
            total_bytes=1000,
            best_coverage_files_percent=0.0,
            best_coverage_bytes_percent=0.0,
        ),
        REFS,
    )

    assert (display.state, display.text) == ("possible", "Partial · leads only · AID-002")
    assert display.tooltip.startswith(
        "No file in this folder matches by name, size and modified time on AID-002; "
        "some files are present there by name and size only, so treat this drive as "
        "a lead, not a copy."
    )
    assert "0%" not in display.tooltip
    assert BACKUP_FOLDER_RULE_SENTENCE in display.tooltip
    assert display.coverage_fraction == 0.0
    assert backup_filter_matches(display, "possible")


def test_all_files_weak_notes_files_that_also_matched_by_time():
    display = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_ALL_FILES_WEAK,
            matched_files=3,
            matched_bytes=300,
            best_coverage_files_percent=30.0,
            best_coverage_bytes_percent=30.0,
        ),
        REFS,
    )

    assert display.text == "Found by name & size only · AID-002"
    assert "3 of 10 files also match by modified time on AID-002." in display.tooltip
    assert "too many drives to single out copies" in display.tooltip


def test_full_match_names_drives_that_hold_every_file_loose():
    # Round-3 engine: strong_volume_ids holds whole-folder targets only, while
    # other_volume_ids keeps every drive with evidence. A drive in neither the
    # strong nor the possible tier holds every file, just not as one folder.
    def full_match(possible_ids):
        return item_backup_display(
            folder_status(
                status="likely",
                evidence_text=ba.FOLDER_EVIDENCE_FULL_MATCH_LEGACY,
                other_drive_count=3,
                other_volume_ids=(2, 3, 4),
                strong_volume_ids=(2,),
                possible_volume_ids=possible_ids,
            ),
            REFS,
        )

    loose = full_match((3,))
    assert loose.text == "Full match (no hashes) · 1 other drive"
    assert "Also holds every file, but not as one folder: AID-004." in loose.tooltip
    assert "Full-match drives: AID-002." in loose.tooltip
    assert "Partial or lead drives: AID-003." in loose.tooltip

    paired = full_match((3, 4))
    assert "not as one folder" not in paired.tooltip


def test_header_tooltip_lists_every_kind_of_amber_lead():
    assert (
        "Amber (dash): a lead to check: partial, name and size only, a generic name, "
        "a folder structure that repeats too often to pair, or content that differs."
    ) in BACKUP_COLUMN_HEADER_TOOLTIP


def test_hash_conflict_file_is_amber_content_differs():
    display = item_backup_display(
        file_status(
            status="ambiguous",
            other_drive_count=0,
            other_volume_ids=(),
            strong_volume_ids=(),
            evidence_text=ba.FILE_EVIDENCE_HASH_CONFLICT,
        )
    )

    assert (display.state, display.text) == (
        "possible",
        "Content differs · same name and size",
    )
    assert "different version or a damaged copy" in display.tooltip
    assert backup_filter_matches(display, "possible")
    assert backup_filter_matches(display, "attention")
    assert not backup_filter_matches(display, "strong")
    assert "conflict" not in BackupStatusIconProvider.COLORS


def test_file_tiers_follow_evidence_not_the_status_word():
    verified = item_backup_display(
        file_status(
            other_drive_count=3,
            other_volume_ids=(2, 3, 4),
            strong_volume_ids=(2, 3),
            verified_volume_ids=(2,),
            possible_volume_ids=(4,),
            evidence_text=ba.FILE_EVIDENCE_HASH_VERIFIED
            + ba.FILE_EVIDENCE_HASH_VERIFIED_EXTRA_DRIVES,
        ),
        REFS,
    )
    timed = item_backup_display(
        file_status(
            other_drive_count=2,
            other_volume_ids=(2, 3),
            strong_volume_ids=(2,),
            possible_volume_ids=(3,),
        ),
        REFS,
    )
    lead = item_backup_display(
        file_status(
            status="possible",
            other_drive_count=2,
            other_volume_ids=(2, 3),
            strong_volume_ids=(),
            possible_volume_ids=(2, 3),
            evidence_text=ba.FILE_EVIDENCE_NAME_SIZE_ONLY,
        ),
        REFS,
    )
    none = item_backup_display(
        file_status(
            status="single",
            other_drive_count=0,
            other_volume_ids=(),
            strong_volume_ids=(),
            evidence_text="",
        )
    )

    assert (verified.state, verified.text) == ("verified", "Hash verified · 1 other drive")
    assert "proven the same" in verified.tooltip
    assert "Also found by name, size and time on 1 more drive" in verified.tooltip
    assert "Also found by name and size only on 1 more drive" in verified.tooltip
    assert (timed.state, timed.text) == ("strong", "Same size & time · 1 other drive")
    assert "Contents were not compared" in timed.tooltip
    assert "Name and size also match on 1 more drive" in timed.tooltip
    assert (lead.state, lead.text) == ("possible", "Name and size · 2 other drives")
    assert "verified" not in lead.text.casefold()
    assert "lead, not a confirmed copy" in lead.tooltip
    assert (none.state, none.text) == ("none", "No copy found")
    assert none.coverage_fraction == 0.0
    assert verified.sort_rank > timed.sort_rank > lead.sort_rank > none.sort_rank


def test_generic_name_with_a_time_match_is_green_and_notes_the_common_name():
    display = item_backup_display(
        file_status(
            status="likely",
            other_drive_count=300,
            other_volume_ids=tuple(range(2, 66)),
            strong_volume_ids=(2, 3, 4),
            possible_volume_ids=tuple(range(5, 66)),
            evidence_text=ba.FILE_EVIDENCE_GENERIC_PREFIX
            + " 1,800 records across 300 drives.",
        ),
        REFS,
    )

    assert (display.state, display.text) == ("strong", "Same size & time · 3 other drives")
    assert "generic name" not in display.text
    assert (
        "This name and size is common across the catalogue (1,800 records on 300 "
        "drives); the time match still counts."
    ) in display.tooltip
    assert "means little" not in display.tooltip
    assert "Same-size-and-time drives (no hash): AID-002, AID-003, AID-004" in (
        display.tooltip
    )
    assert "Only the first 64 drives are listed; 300 other drives" in display.tooltip
    assert backup_filter_matches(display, "strong")
    assert not backup_filter_matches(display, "attention")


def test_generic_name_without_a_time_match_stays_amber():
    display = item_backup_display(
        file_status(
            status="possible",
            other_drive_count=299,
            other_volume_ids=tuple(range(2, 66)),
            strong_volume_ids=(),
            possible_volume_ids=tuple(range(2, 66)),
            evidence_text=ba.FILE_EVIDENCE_GENERIC_PREFIX
            + " 1,800 records across 300 drives.",
        ),
        REFS,
    )

    # "Other drives" convention: the group's 300 drives include this one.
    assert (display.state, display.text) == (
        "possible",
        "Name and size · 299 other drives · generic name",
    )
    assert "means little" in display.tooltip
    # Too many drives share the time to single out copies: say so rather than
    # claiming the times differ.
    assert "single out copies" in display.tooltip
    assert "Only the first 64 drives are listed; 299 other drives" in display.tooltip
    assert "1,800 records across 300 drives" in display.tooltip
    assert "the time match still counts" not in display.tooltip
    assert not backup_filter_matches(display, "strong")
    assert backup_filter_matches(display, "attention")


def test_generic_name_with_a_hash_match_is_aqua():
    display = item_backup_display(
        file_status(
            status="likely",
            other_drive_count=300,
            other_volume_ids=tuple(range(2, 66)),
            strong_volume_ids=(2,),
            possible_volume_ids=tuple(range(3, 66)),
            verified_volume_ids=(2,),
            evidence_text=ba.FILE_EVIDENCE_GENERIC_PREFIX
            + " 1,800 records across 300 drives.",
        ),
        REFS,
    )

    assert (display.state, display.text) == ("verified", "Hash verified · 1 other drive")
    assert "the time match still counts" in display.tooltip


def test_conflict_note_is_added_to_a_matched_file():
    display = item_backup_display(
        file_status(evidence_text=ba.FILE_EVIDENCE_TIME_MATCH + ba.FILE_EVIDENCE_CONFLICT_NOTE),
        REFS,
    )

    assert (display.state, display.text) == ("strong", "Same size & time · 1 other drive")
    assert "different version of this file" in display.tooltip
    assert "hashes do not match" in display.tooltip


def test_repeated_content_hash_match_stays_aqua_and_reports_the_true_count():
    # The saved id list is capped at 64; the cell must show the real drive count.
    display = item_backup_display(
        file_status(
            other_drive_count=299,
            other_volume_ids=tuple(range(2, 66)),
            strong_volume_ids=tuple(range(2, 66)),
            verified_volume_ids=tuple(range(2, 66)),
            evidence_text=ba.FILE_EVIDENCE_HASH_VERIFIED
            + ba.FILE_EVIDENCE_REPEATED_CONTENT_NOTE
            + " 900 records across 299 drives.",
        ),
        REFS,
    )

    assert (display.state, display.text) == ("verified", "Hash verified · 299 other drives")
    assert "not paired for folder coverage" in display.tooltip
    assert "Only the first 64 drives are listed; 299 other drives" in display.tooltip


def test_hash_match_without_the_repeated_content_flag_counts_listed_drives():
    display = item_backup_display(
        file_status(
            other_drive_count=2,
            other_volume_ids=(2, 3),
            strong_volume_ids=(2, 3),
            verified_volume_ids=(2, 3),
            evidence_text=ba.FILE_EVIDENCE_HASH_VERIFIED,
        ),
        REFS,
    )

    assert display.text == "Hash verified · 2 other drives"
    assert "Only the first" not in display.tooltip


def test_same_name_copies_on_one_drive_get_identical_labels():
    # AID-140 pattern: one drive holds two copies of a file in different
    # folders; six other drives hold one each with the same modified time.
    others = tuple(range(2, 8))
    references = {volume_id: f"AID-{volume_id:03d}" for volume_id in others}
    first = item_backup_display(
        SimpleNamespace(
            item_type="file",
            item_id=101,
            status="likely",
            is_stale=False,
            other_drive_count=6,
            other_volume_ids=others,
            strong_volume_ids=others,
            possible_volume_ids=(),
            verified_volume_ids=(),
            evidence_text=ba.FILE_EVIDENCE_TIME_MATCH,
        ),
        references,
    )
    second = item_backup_display(
        SimpleNamespace(
            item_type="file",
            item_id=202,
            status="likely",
            is_stale=False,
            other_drive_count=6,
            other_volume_ids=others,
            strong_volume_ids=others,
            possible_volume_ids=(),
            verified_volume_ids=(),
            evidence_text=ba.FILE_EVIDENCE_TIME_MATCH,
        ),
        references,
    )

    assert first.text == second.text == "Same size & time · 6 other drives"
    assert first.state == second.state == "strong"
    assert first.tooltip == second.tooltip
    assert "Can't tell" not in first.text


def test_legacy_v3_evidence_reads_as_outdated():
    legacy_folder = (
        ba.FOLDER_EVIDENCE_TRUSTED_HASHED,
        ba.FOLDER_EVIDENCE_TRUSTED_LEGACY,
        ba.FOLDER_EVIDENCE_MIXED_HASH,
        ba.FOLDER_EVIDENCE_UNTRUSTED_SCAN,
        ba.FOLDER_EVIDENCE_UNTRUSTED_AND_RENAMED,
        ba.FOLDER_EVIDENCE_RENAMED,
        ba.FOLDER_EVIDENCE_COVERAGE,
        ba.FOLDER_EVIDENCE_AMBIGUOUS,
    )
    for evidence in legacy_folder:
        display = item_backup_display(
            folder_status(status="likely", strong_volume_ids=(2,), evidence_text=evidence),
            REFS,
        )
        assert (display.state, display.text) == ("unknown", "Outdated · re-run analysis")
    legacy_file = (
        ba.FILE_EVIDENCE_METADATA_LIKELY,
        ba.FILE_EVIDENCE_COMPETING,
        ba.FILE_EVIDENCE_REPETITIVE_HASH,
        ba.FILE_EVIDENCE_TOO_COMMON_PREFIX + " 40 records across 12 drives.",
    )
    for evidence in legacy_file:
        display = item_backup_display(file_status(evidence_text=evidence), REFS)
        assert (display.state, display.text) == ("unknown", "Outdated · re-run analysis")
        assert "Too common" not in display.text


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
            evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
            matched_files=3,
            total_files=100,
            best_coverage_files_percent=3.0,
            best_coverage_bytes_percent=3.0,
        ),
        REFS,
    )
    thick = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
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


def test_status_strip_counts_verified_and_strong_as_copies():
    parent = item_backup_display(
        folder_status(
            evidence_text=ba.FOLDER_EVIDENCE_PARTIAL,
            matched_files=6,
            matched_bytes=600,
            best_coverage_files_percent=60.0,
            best_coverage_bytes_percent=60.0,
        ),
        REFS,
    )
    aqua = BackupDisplay("verified", "Full match (hashed) · 1 other drive", "", 4)
    green = BackupDisplay("strong", "Full match (no hashes) · 1 other drive", "", 3)
    red = BackupDisplay("none", "No copy found", "", 0, 0.0)
    items = [
        SimpleNamespace(
            item_type="folder",
            is_parent_entry=True,
            name="..",
            backup=BackupDisplay("unknown", "Not analysed", "", 2),
        ),
        SimpleNamespace(item_type="folder", is_parent_entry=False, name="Day 1", backup=aqua),
        SimpleNamespace(item_type="folder", is_parent_entry=False, name="Day 2", backup=green),
        SimpleNamespace(
            item_type="file", is_parent_entry=False, name="Timings.xlsx", backup=red
        ),
        SimpleNamespace(item_type="file", is_parent_entry=False, name="Rider.pdf", backup=aqua),
    ]

    text = browser_status_strip_text(parent, items)

    assert text.startswith("This folder: Partial · 60% files · 60% data.")
    assert "Folders here: 2 of 2 have a copy elsewhere." in text
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
    assert (
        OtherCopiesDelegate.short_text("Not confirmed · structure repeats on 11 other drives")
        == "Not confirmed"
    )
    assert (
        OtherCopiesDelegate.short_text("Full match (no hashes) · 2 other drives")
        == "Full match (no hashes)"
    )
    assert OtherCopiesDelegate.short_text("Same size & time · 3 other drives") == (
        "Same size & time"
    )
    assert OtherCopiesDelegate.short_text("No copy found") == "No copy found"


def test_status_strip_ignores_children_without_a_verdict():
    stale = BackupDisplay("unknown", "Outdated · re-run analysis", "", 2)
    cant_tell = BackupDisplay("unknown", "Can't tell · scan incomplete", "", 2)
    lead = BackupDisplay("possible", "Name and size · 1 other drive", "", 1)
    aqua = BackupDisplay("verified", "Hash verified · 1 other drive", "", 4)
    green = BackupDisplay("strong", "Same size & time · 1 other drive", "", 3)

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
        SimpleNamespace(item_type="file", is_parent_entry=False, name="c.mov", backup=aqua),
    ]
    text = browser_status_strip_text(None, lead_items)
    assert "Files here: 2 of 3 match on another drive; 1 is a lead only." in text


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


def test_backup_report_mirror_structure_column_says_what_was_compared(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()
    state = SimpleNamespace(
        status="completed",
        analysed_at="2026-09-01T10:00:00.000000+0000",
        is_stale=False,
    )
    mirror = SimpleNamespace(
        source_volume_id=1,
        target_volume_id=2,
        source_coverage_percent=100.0,
        target_coverage_percent=100.0,
        complete_structure=True,
        evidence_text="whole drive",
        manual_mirror_link=False,
    )
    dialog.set_records(state, [], [mirror], [], {1: "AID-001", 2: "AID-002"})

    # A whole-drive structure match compares layout and sizes, not hashes; the
    # cell must not read as hash-verified.
    assert dialog.mirror_table.item(0, 4).text() == "Full match (layout & sizes)"
    assert app is not None


def test_backup_report_legend_green_line_covers_files_and_folders(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()

    legend_texts = [
        label.text() for label in dialog.findChildren(QLabel) if "Green (tick)" in label.text()
    ]
    assert len(legend_texts) == 1
    green_line = next(
        line for line in legend_texts[0].splitlines() if line.startswith("Green (tick)")
    )
    assert "contents not compared" in green_line
    assert "same name, size and modified time" in green_line
    assert "folder name, layout, file names and sizes" in green_line
    # The column header tooltip must tell the same story.
    assert "same name, size and modified time" in BACKUP_COLUMN_HEADER_TOOLTIP
    assert "folder name, layout, file names and sizes" in BACKUP_COLUMN_HEADER_TOOLTIP
    assert (
        "a folder whose every file was found elsewhere by name, size and modified time"
    ) in BACKUP_COLUMN_HEADER_TOOLTIP
    assert "contents were not compared" in BACKUP_COLUMN_HEADER_TOOLTIP
    # The dialog rules must agree with the cells: a generic name only stays
    # amber when no modified time agrees.
    rules_texts = [
        label.text()
        for label in dialog.findChildren(QLabel)
        if "What the labels mean" in label.text()
    ]
    assert len(rules_texts) == 1
    assert "only when no modified time agrees" in rules_texts[0]
    assert "stay amber even when times agree" not in rules_texts[0]
    assert app is not None


def test_backup_report_rules_explain_the_generic_time_bound(monkeypatch):
    # Generic names above the per-signature drive bound get no strong tier, so
    # the dialog must not claim their times differ, and must say the one-hour
    # offset is not applied to them.
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = BackupEvidenceDialog()

    rules = next(
        label.text()
        for label in dialog.findChildren(QLabel)
        if "What the labels mean" in label.text()
    )
    name_and_size = next(line for line in rules.splitlines() if "• Name and size:" in line)
    assert "shared by so many drives that it cannot single out copies" in name_and_size
    assert "bounded number of drives" in name_and_size
    assert "The one-hour clock offset is not applied to generic names." in name_and_size
    same_size = next(line for line in rules.splitlines() if "• Same size & time:" in line)
    assert "one-hour clock offset for names that are not generic" in same_size
    weak = next(line for line in rules.splitlines() if "• Found by name & size only:" in line)
    assert "too many drives to single out copies" in weak
    assert app is not None
