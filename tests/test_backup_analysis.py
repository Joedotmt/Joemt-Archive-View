from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from jvvv.backup_analysis import DETAIL_PAGE_SIZE, BackupAnalysisEngine
from jvvv.database import Database

SCANNED_AT = "2026-08-20T10:00:00.000000+0000"
HASH_A = b"a" * 32
HASH_B = b"b" * 32
HASH_C = b"c" * 32


def add_catalogued_volume(
    db: Database, tmp_path: Path, name: str, files: dict[str, bytes | None], *,
    empty_dirs: tuple[str, ...] = (), errors: tuple[tuple[str, str], ...] = (),
    scanned: bool = True, unrecorded_errors: int = 0,
):
    """Create saved scan records with intentionally disconnected drives."""
    identity = f"AID-{len(db.list_volumes()) + 1:03d}"
    volume = db.create_volume(name, str(tmp_path / identity), {"drive_id": identity})
    file_ids, folder_ids = {}, {}

    def ensure(path):
        if path not in folder_ids:
            parent, _, basename = path.rpartition("/")
            folder_ids[path] = db.ensure_folder(
                volume_id=volume, parent_id=ensure(parent) if path else None,
                name=basename if path else name, relative_path=path, scanned_at=SCANNED_AT,
            )
        return folder_ids[path]

    with db.transaction():
        ensure("")
        for path in empty_dirs:
            ensure(path)
        for path, digest in files.items():
            parent, _, name = path.rpartition("/")
            file_ids[path] = db.upsert_file(
                volume_id=volume, folder_id=ensure(parent), name=name,
                relative_path=path, extension=PurePosixPath(path).suffix.lstrip("."),
                size_bytes=1, modified_at=SCANNED_AT, scanned_at=SCANNED_AT,
                content_hash=digest, content_hash_algorithm="sha256" if digest is not None else None,
            )
        db.rebuild_folder_statistics(volume, SCANNED_AT)
        db.refresh_volume_counts(volume, SCANNED_AT if scanned else None)
    if scanned:
        scan = db.start_scan(volume)
        for path, message in errors:
            db.add_scan_error(scan, volume, path, message)
        db.finish_scan(scan, "completed", len(files), len(folder_ids), len(errors) + unrecorded_errors)
    return volume, file_ids, folder_ids


@pytest.fixture
def catalogue(tmp_path):
    db = Database(tmp_path / "Copies.jvvv")
    try:
        yield db
    finally:
        db.close()


def test_exact_and_content_matches_show_the_actual_offline_paths(catalogue, tmp_path):
    source, files, folders = add_catalogued_volume(
        catalogue, tmp_path, "Editing", {"Photos/a.jpg": HASH_A, "Photos/raw/b.raw": HASH_B},
        empty_dirs=("Photos/empty",),
    )
    exact, _, _ = add_catalogued_volume(
        catalogue, tmp_path, "Exact", {
            "Archive/Photos backup/raw/b.raw": HASH_B, "Archive/Photos backup/a.jpg": HASH_A,
        }, empty_dirs=("Archive/Photos backup/empty",),
    )
    loose, _, _ = add_catalogued_volume(
        catalogue, tmp_path, "Loose", {"Other/c.jpg": HASH_A, "Other/d.raw": HASH_B},
    )
    partial, _, _ = add_catalogued_volume(catalogue, tmp_path, "Partial", {"a.jpg": HASH_A})
    zero, _, _ = add_catalogued_volume(catalogue, tmp_path, "Zero", {"same-size.jpg": HASH_C})
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    assert run.status == "completed"
    folder = engine.folder_status(folders["Photos"])
    assert (folder.status, folder.matched_files, folder.total_files) == ("full_match", 2, 2)
    assert folder.best_target_volume_id == exact
    drives = engine.drives_for(folder, source, run.run_id)
    assert [(drive.volume_id, drive.matched_files, drive.full_match) for drive in drives] == [
        (exact, 2, True), (loose, 2, False), (partial, 1, False), (zero, 0, False),
    ]
    matches = engine.locations("folder", folder.item_id, exact, run.run_id)
    assert [match.relative_path for match in matches] == ["Archive/Photos backup"]
    rows = engine.folder_files(folder.item_id, exact, run.run_id, exact_folder_id=matches[0].item_id)
    assert [(row.source_path, row.target.relative_path) for row in rows] == [
        ("a.jpg", "Archive/Photos backup/a.jpg"), ("raw/b.raw", "Archive/Photos backup/raw/b.raw"),
    ]
    assert engine.locations("file", files["Photos/a.jpg"], loose, run.run_id)[0].relative_path == "Other/c.jpg"
    summaries = {summary.volume_id: summary.item for summary in engine.volume_summaries()}
    assert summaries[loose].status == "all_contents"


@pytest.mark.parametrize("change", ["missing empty", "extra empty", "extra file", "rename", "case", "swap"])
def test_tree_changes_still_allow_all_contents_without_full_match(catalogue, tmp_path, change):
    _, _, folders = add_catalogued_volume(
        catalogue, tmp_path, "Source", {"Project/a.txt": HASH_A, "Project/b.txt": HASH_B},
        empty_dirs=("Project/empty",),
    )
    files = {"Copy/a.txt": HASH_A, "Copy/b.txt": HASH_B}
    directories = ["Copy/empty"]
    if change == "missing empty":
        directories.clear()
    elif change == "extra empty":
        directories.append("Copy/extra")
    elif change == "extra file":
        files["Copy/extra.txt"] = HASH_C
    elif change == "rename":
        files["Copy/renamed.txt"] = files.pop("Copy/a.txt")
    elif change == "case":
        files["Copy/A.txt"] = files.pop("Copy/a.txt")
    else:
        files["Copy/a.txt"], files["Copy/b.txt"] = HASH_B, HASH_A
    target, _, _ = add_catalogued_volume(catalogue, tmp_path, "Target", files, empty_dirs=tuple(directories))
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    item = engine.folder_status(folders["Project"])
    assert (item.status, item.matched_files) == ("all_contents", 2)
    assert engine.locations("folder", item.item_id, target, run.run_id) == []


def test_duplicate_bytes_count_for_coverage_and_scattered_matches_do_not_add_up(catalogue, tmp_path):
    _, files, folders = add_catalogued_volume(
        catalogue, tmp_path, "Source", {"a.txt": HASH_A, "b.txt": HASH_A, "nested/c.txt": HASH_B},
    )
    target, _, _ = add_catalogued_volume(catalogue, tmp_path, "Backup A", {"a.txt": HASH_A})
    add_catalogued_volume(catalogue, tmp_path, "Backup B", {"c.txt": HASH_B})
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    item = engine.folder_status(folders[""])
    assert (item.status, item.matched_files, item.total_files) == ("partial", 2, 3)
    assert engine.file_status(files["a.txt"]).other_drive_count == 1
    assert len(engine.folder_files(item.item_id, target, run.run_id, file_filter="no_match")) == 1
    matches = engine.folder_files(item.item_id, target, run.run_id)
    assert matches[0].target == matches[1].target
    assert matches[2].target is None


def test_hash_gaps_unavailable_scans_and_same_drive_are_explicit(catalogue, tmp_path):
    source, files, folders = add_catalogued_volume(
        catalogue, tmp_path, "Source", {"a.txt": HASH_A, "b.txt": HASH_A, "c.txt": None},
    )
    target, _, _ = add_catalogued_volume(catalogue, tmp_path, "Target", {"a.txt": HASH_B, "c.txt": None})
    unscanned, _, _ = add_catalogued_volume(catalogue, tmp_path, "Pending", {}, scanned=False)
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    assert engine.file_status(files["a.txt"]).status == "no_match"
    assert engine.file_status(files["c.txt"]).status == "needs_rescan"
    item = engine.folder_status(folders[""])
    assert (item.total_files, item.matched_files, item.missing_hashes) == (3, 0, 1)
    assert [row.source_path for row in engine.folder_files(
        item.item_id, target, run.run_id, file_filter="needs_rescan",
    )] == ["c.txt"]
    drives = {drive.volume_id: drive for drive in engine.drives_for(item, source, run.run_id)}
    assert drives[target].missing_hashes == 1
    assert drives[unscanned].scanned_at is None


@pytest.mark.parametrize("changed_side", ["source", "target"])
def test_incomplete_tree_blocks_full_match_only_in_affected_scope(catalogue, tmp_path, changed_side):
    source, _, folders = add_catalogued_volume(
        catalogue, tmp_path, "Source", {"Affected/a.txt": HASH_A, "Clean/b.txt": HASH_B},
        errors=(("Affected/skipped", "Access denied"),) if changed_side == "source" else (),
    )
    target, _, _ = add_catalogued_volume(
        catalogue, tmp_path, "Target", {"Affected/a.txt": HASH_A, "Clean/b.txt": HASH_B},
        errors=(("Affected/skipped", "Access denied"),) if changed_side == "target" else (),
    )
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    item = engine.folder_status(folders["Affected"])
    assert item.status == ("partial" if changed_side == "source" else "all_contents")
    assert item.scan_incomplete == (changed_side == "source")
    assert item.matched_files == item.total_files == 1
    assert not engine.drives_for(item, source, run.run_id)[0].full_match
    assert engine.folder_status(folders["Clean"]).status == "full_match"
    assert engine.locations("folder", folders["Affected"], target, run.run_id) == []


def test_protected_trees_are_outside_comparison_scope_but_extra_indexed_files_count(catalogue, tmp_path):
    _, _, folders = add_catalogued_volume(
        catalogue, tmp_path, "Source", {"a.txt": HASH_A, "System Volume Information/data": HASH_B},
        errors=(("System Volume Information", "Access denied"),),
    )
    add_catalogued_volume(catalogue, tmp_path, "Target", {"a.txt": HASH_A, "desktop.ini": HASH_C})
    engine = BackupAnalysisEngine(catalogue)
    engine.analyse()
    item = engine.folder_status(folders[""])
    assert (item.status, item.total_files) == ("all_contents", 1)


def test_unlocated_errors_block_exact_and_later_failed_attempt_keeps_applied_scan(catalogue, tmp_path):
    source, _, folders = add_catalogued_volume(catalogue, tmp_path, "Source", {"a.txt": HASH_A})
    target, _, _ = add_catalogued_volume(
        catalogue, tmp_path, "Target", {"a.txt": HASH_A}, unrecorded_errors=1,
    )
    scan = catalogue.start_scan(source)
    catalogue.finish_scan(scan, "failed", 0, 0, 1)
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    item = engine.folder_status(folders[""])
    assert item.status == "all_contents"
    assert not item.scan_incomplete
    assert engine.drives_for(item, source, run.run_id)[0].scan_incomplete
    assert engine.locations("folder", item.item_id, target, run.run_id) == []


def test_repeated_hash_groups_store_members_and_page_all_locations(catalogue, tmp_path):
    _, source_files, source_folders = add_catalogued_volume(
        catalogue, tmp_path, "Source", {f"Copy-{i:03d}/a.txt": HASH_A for i in range(160)},
    )
    target, _, _ = add_catalogued_volume(
        catalogue, tmp_path, "Target", {f"Backup-{i:03d}/a.txt": HASH_A for i in range(160)},
    )
    engine = BackupAnalysisEngine(catalogue)
    run = engine.analyse()
    assert catalogue.connection.execute("SELECT COUNT(*) FROM backup_hash_drives").fetchone()[0] == 2
    for kind, item_id in (("file", source_files["Copy-000/a.txt"]), ("folder", source_folders["Copy-000"])):
        first = engine.locations(kind, item_id, target, run.run_id)
        second = engine.locations(kind, item_id, target, run.run_id, DETAIL_PAGE_SIZE)
        assert len(first) == DETAIL_PAGE_SIZE + 1
        assert len(second) == 60
        assert first[DETAIL_PAGE_SIZE] == second[0]
    assert engine.folder_status(source_folders[""]).matched_files == 160


def test_previous_results_survive_cancellation_and_failure(catalogue, tmp_path):
    _, files, _ = add_catalogued_volume(catalogue, tmp_path, "Source", {"a.txt": HASH_A})
    add_catalogued_volume(catalogue, tmp_path, "Target", {"a.txt": HASH_A})
    engine = BackupAnalysisEngine(catalogue)
    previous = engine.analyse().run_id
    cancel = False

    def progress(update):
        nonlocal cancel
        cancel = update.phase == "publish"

    assert engine.analyse(progress, lambda: cancel).status == "cancelled"
    assert engine.state().active_run_id == previous

    def fail(update):
        if update.phase == "publish":
            raise RuntimeError("failed to publish")

    with pytest.raises(RuntimeError, match="failed to publish"):
        engine.analyse(fail)
    assert engine.state().active_run_id == previous
    assert engine.file_status(files["a.txt"]).status == "hash_match"


def test_catalogue_change_discards_staging_and_hides_outdated_results(catalogue, tmp_path):
    _, files, _ = add_catalogued_volume(catalogue, tmp_path, "Source", {"a.txt": HASH_A})
    target, _, _ = add_catalogued_volume(catalogue, tmp_path, "Target", {"a.txt": HASH_A})
    engine = BackupAnalysisEngine(catalogue)
    previous = engine.analyse().run_id

    def progress(update):
        if update.phase == "publish":
            engine.invalidate_all("A new scan was applied.")

    assert engine.analyse(progress).status == "discarded"
    assert engine.state().active_run_id == previous
    assert engine.file_status(files["a.txt"]).status == "outdated"
    assert engine.locations("file", files["a.txt"], target, previous) == []
    assert engine.analyse().status == "completed"
    assert engine.file_status(files["a.txt"]).status == "hash_match"


def test_older_evidence_prompts_recomparison_on_read_only_connection(catalogue, tmp_path):
    _, files, _ = add_catalogued_volume(catalogue, tmp_path, "Source", {"a.txt": HASH_A})
    engine = BackupAnalysisEngine(catalogue)
    run_id = engine.analyse().run_id
    with catalogue.transaction():
        catalogue.connection.execute("UPDATE backup_analysis_runs SET rules_version=4 WHERE id=?", (run_id,))
    read_only = Database(catalogue.path, initialize=False, create=False, read_only=True)
    try:
        reader = BackupAnalysisEngine(read_only)
        assert reader.state().status == "outdated"
        assert reader.file_status(files["a.txt"]).status == "outdated"
        assert engine.analyse().status == "completed"
        assert reader.file_status(files["a.txt"]).status == "no_match"
    finally:
        read_only.close()
