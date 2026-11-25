from __future__ import annotations

import pytest

from jvvv.backup_analysis import ItemBackupStatus
from jvvv.backup_ui import backup_filter_matches, item_backup_display


@pytest.mark.parametrize(("kind", "counts", "label"), [
    ("full_match", (10, 10), "Full match · Backup A"),
    ("all_contents", (10, 10), "10/10 file contents found · Backup A"),
    ("partial", (7, 10), "7/10 file contents found · Backup A"),
    ("no_match", (0, 10), "No matching hashes"),
    ("no_files", (0, 0), "No files to compare"),
    ("outdated", (0, 0), "Outdated"),
    ("not_analyzed", (0, 0), "Not compared"),
    ("scan_incomplete", (0, 0), "Scan incomplete"),
    ("needs_rescan", (0, 0), "Needs rescan"),
])
def test_browser_verdicts_use_visible_words(kind, counts, label):
    matched, total = counts
    result = ItemBackupStatus(
        "folder", 1, kind, matched_files=matched, total_files=total,
        best_target_volume_id=2 if matched else None,
    )
    assert item_backup_display(result, {2: "Backup A"}).text == label


def test_hash_match_count_and_filters_use_typed_status():
    file_display = item_backup_display(ItemBackupStatus(
        "file", 1, "hash_match", total_files=1, matched_files=1, other_drive_count=2,
    ))
    folder_display = item_backup_display(ItemBackupStatus("folder", 1, "full_match", 10, 10))
    partial = item_backup_display(ItemBackupStatus("folder", 2, "partial", 10, 7))
    assert file_display.text == "Hash match on 2 drives"
    assert backup_filter_matches(file_display, "matches")
    assert not backup_filter_matches(file_display, "full_match")
    assert backup_filter_matches(folder_display, "full_match")
    assert backup_filter_matches(partial, "attention")
    assert backup_filter_matches(partial, "matches")
