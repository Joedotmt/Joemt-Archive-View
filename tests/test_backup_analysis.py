from __future__ import annotations

import builtins
import os
from pathlib import Path, PurePosixPath
import sqlite3

import pytest

from jvvv.backup_analysis import AnalysisOptions, BackupAnalysisEngine
from jvvv.app import BackupAnalysisWorker
from jvvv.database import Database


SCANNED_AT = "2026-08-20T10:00:00.000000+0000"
LATER_SCAN = "2026-08-21T10:00:00.000000+0000"
MODIFIED_AT = "2026-08-19T09:00:00.000000+0000"


def add_catalogued_volume(
    db: Database,
    tmp_path: Path,
    *,
    name: str,
    drive_id: str,
    files: dict[str, tuple[int, str | None]],
    scanned_at: str = SCANNED_AT,
    scan_errors: int = 0,
    scan_status: str | None = "completed",
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Build saved catalogue records without requiring a connected source drive."""
    volume_id = db.create_volume(
        name,
        str(tmp_path / f"offline-{drive_id}"),
        {"drive_id": drive_id},
    )
    file_ids: dict[str, int] = {}
    folder_ids: dict[str, int] = {}
    with db.transaction():
        root_id = db.ensure_folder(
            volume_id=volume_id,
            parent_id=None,
            name=name,
            relative_path="",
            scanned_at=scanned_at,
            modified_at=MODIFIED_AT,
        )
        folder_ids[""] = root_id

        for relative_path, (size_bytes, modified_at) in files.items():
            path = PurePosixPath(relative_path)
            parent_id = root_id
            parent_path = ""
            for part in path.parts[:-1]:
                child_path = f"{parent_path}/{part}".strip("/")
                child_id = folder_ids.get(child_path)
                if child_id is None:
                    child_id = db.ensure_folder(
                        volume_id=volume_id,
                        parent_id=parent_id,
                        name=part,
                        relative_path=child_path,
                        scanned_at=scanned_at,
                        modified_at=MODIFIED_AT,
                    )
                    folder_ids[child_path] = child_id
                parent_path = child_path
                parent_id = child_id

            file_ids[relative_path] = db.upsert_file(
                volume_id=volume_id,
                folder_id=parent_id,
                name=path.name,
                relative_path=relative_path,
                extension=path.suffix.lstrip("."),
                size_bytes=size_bytes,
                modified_at=modified_at,
                scanned_at=scanned_at,
            )

        db.rebuild_folder_statistics(volume_id, scanned_at)
        db.refresh_volume_counts(
            volume_id,
            scanned_at if scan_status == "completed" else None,
        )

    if scan_status is not None:
        scan_id = db.start_scan(volume_id)
        db.finish_scan(
            scan_id,
            scan_status,
            len(files),
            len(folder_ids),
            scan_errors,
        )
    return volume_id, file_ids, folder_ids


def object_field(value, *names):
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
        if isinstance(value, dict) and name in value:
            return value[name]
    raise AssertionError(f"Expected one of these report fields: {', '.join(names)}")


def status_text(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold()


def test_analysis_distinguishes_strong_possible_and_same_drive_only_matches(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        first_volume_id, first_files, first_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={
                "Project/report.psd": (100, MODIFIED_AT),
                "Loose/clip.mov": (200, MODIFIED_AT),
                "Loose/source-only.txt": (25, MODIFIED_AT),
                "Duplicate A/local.bin": (300, MODIFIED_AT),
                "Duplicate B/local.bin": (300, MODIFIED_AT),
            },
        )
        second_volume_id, second_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={
                "Project/report.psd": (100, MODIFIED_AT),
                "Elsewhere/clip.mov": (200, MODIFIED_AT),
            },
        )

        engine = BackupAnalysisEngine(db)
        summary = engine.analyse()

        assert summary.status == "completed"
        assert engine.state().is_stale is False

        report = engine.file_status(first_files["Project/report.psd"])
        assert report.status == "likely"
        assert {
            match.target_volume_id
            for match in engine.file_matches(first_files["Project/report.psd"])
        } == {second_volume_id}

        relocated = engine.file_status(first_files["Loose/clip.mov"])
        assert relocated.status == "possible"
        assert {
            match.target_volume_id
            for match in engine.file_matches(first_files["Loose/clip.mov"])
        } == {second_volume_id}

        # Repeated metadata on one physical catalogue volume is not a backup copy.
        assert engine.file_status(first_files["Duplicate A/local.bin"]).status == "single"
        assert engine.file_status(first_files["Duplicate B/local.bin"]).status == "single"

        project = engine.folder_status(first_folders["Project"])
        assert project.status == "possible"
        assert {
            match.target_volume_id
            for match in engine.folder_matches(first_folders["Project"])
        } == {second_volume_id}
        assert first_volume_id != second_volume_id
        assert second_files["Project/report.psd"] > 0
    finally:
        db.close()


def test_missing_folder_does_not_reuse_old_backup_evidence(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="Backup",
            drive_id="AID-002",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        folder_id = source_folders["Project"]
        assert engine.folder_status(folder_id).strong_volume_ids
        assert engine.folder_matches(folder_id)

        with db.transaction():
            db.connection.execute(
                "UPDATE folders SET missing = 1 WHERE id = ?",
                (folder_id,),
            )

        status = engine.folder_status(folder_id)
        assert status.status == "unknown"
        assert status.strong_volume_ids == ()
        assert status.possible_volume_ids == ()
        assert status.best_target_volume_id is None
        assert status.matched_files == status.total_files == 0
        assert engine.folder_matches(folder_id) == []
    finally:
        db.close()


def test_file_status_keeps_strong_and_possible_target_drives_separate(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        strong_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Strong target",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        possible_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Possible target",
            drive_id="AID-003",
            files={"Elsewhere/report.psd": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Project/report.psd"])
        matches = engine.file_matches(source_files["Project/report.psd"])

        assert status.status == "likely"
        assert set(status.other_volume_ids) == {strong_id, possible_id}
        assert status.strong_volume_ids == (strong_id,)
        assert status.possible_volume_ids == (possible_id,)
        assert {(match.target_volume_id, match.status) for match in matches} == {
            (strong_id, "likely"),
            (possible_id, "possible"),
        }
    finally:
        db.close()


def test_analysis_uses_only_saved_catalogue_metadata_and_persists_results(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalogue.jvvv"
    db = Database(path)
    first_volume_id, first_files, _ = add_catalogued_volume(
        db,
        tmp_path,
        name="First",
        drive_id="AID-001",
        files={"Project/report.psd": (100, MODIFIED_AT)},
    )
    second_volume_id, _, _ = add_catalogued_volume(
        db,
        tmp_path,
        name="Second",
        drive_id="AID-002",
        files={"Project/report.psd": (100, MODIFIED_AT)},
    )

    def unexpected_filesystem_access(*args, **kwargs):
        raise AssertionError("backup analysis must not access a source drive")

    with monkeypatch.context() as patch:
        patch.setattr(os, "walk", unexpected_filesystem_access)
        patch.setattr(os, "scandir", unexpected_filesystem_access)
        patch.setattr(Path, "exists", unexpected_filesystem_access)
        patch.setattr(Path, "stat", unexpected_filesystem_access)
        patch.setattr(Path, "open", unexpected_filesystem_access)
        patch.setattr(builtins, "open", unexpected_filesystem_access)

        summary = BackupAnalysisEngine(db).analyse()

    assert summary.status == "completed"
    db.close()

    reopened = Database(path, create=False)
    try:
        engine = BackupAnalysisEngine(reopened)
        assert engine.state().is_stale is False
        assert engine.file_status(first_files["Project/report.psd"]).status == "likely"
        assert {
            match.target_volume_id
            for match in engine.file_matches(first_files["Project/report.psd"])
        } == {second_volume_id}
        assert first_volume_id != second_volume_id
    finally:
        reopened.close()


def test_freshness_tracks_applied_catalogue_scans_not_scan_attempts_or_locations(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        assert engine.state().is_stale is False

        # These attempts do not apply catalogue content and must not invalidate evidence.
        for status in ("failed", "cancelled", "discarded"):
            scan_id = db.start_scan(volume_id)
            db.finish_scan(scan_id, status, 0, 0, 0, f"{status} test")
            assert engine.state().is_stale is False

        # A mount/source-path update is not a content change either.
        db.update_volume_location(
            volume_id,
            str(tmp_path / "different-offline-location"),
            None,
        )
        assert engine.state().is_stale is False

        # VolumeScanner advances last_scan_at only inside its successfully applied
        # transaction. Even an unchanged completed scan therefore makes the saved
        # evidence explicitly outdated until it is analysed again.
        with db.transaction():
            db.refresh_volume_counts(volume_id, LATER_SCAN)

        assert engine.state().is_stale is True
        stale_status = engine.file_status(files["Project/report.psd"])
        stale_matches = engine.file_matches(files["Project/report.psd"])
        assert stale_status.is_stale is True
        assert stale_matches
        assert all(match.is_stale for match in stale_matches)
        assert all(match.analysed_at == stale_status.analysed_at for match in stale_matches)

        assert engine.analyse().status == "completed"
        assert engine.state().is_stale is False
    finally:
        db.close()


def test_cancelled_analysis_keeps_the_previous_published_result(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        second_volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        matches_before = engine.file_matches(files["Project/report.psd"])

        cancelled = engine.analyse(cancel_callback=lambda: True)

        assert cancelled.status == "cancelled"
        assert engine.state().is_stale is False
        assert engine.file_status(files["Project/report.psd"]).status == "likely"
        assert engine.file_matches(files["Project/report.psd"]) == matches_before
        assert {match.target_volume_id for match in matches_before} == {second_volume_id}
    finally:
        db.close()


def test_cancellation_during_a_build_keeps_the_previous_published_result(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={
                f"Project/file-{index:02d}.bin": (index + 1, MODIFIED_AT)
                for index in range(12)
            },
        )
        second_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={
                f"Project/file-{index:02d}.bin": (index + 1, MODIFIED_AT)
                for index in range(12)
            },
        )
        engine = BackupAnalysisEngine(db, AnalysisOptions(batch_size=1))
        assert engine.analyse().status == "completed"
        run_before = engine.state().active_run_id
        matches_before = engine.file_matches(files["Project/file-00.bin"])
        cancelled = {"requested": False}

        def on_progress(progress) -> None:
            if progress.phase == "index_files" and progress.completed >= 1:
                cancelled["requested"] = True

        result = engine.analyse(
            progress_callback=on_progress,
            cancel_callback=lambda: cancelled["requested"],
        )

        assert result.status == "cancelled"
        assert engine.state().active_run_id == run_before
        assert engine.state().is_stale is False
        assert engine.file_matches(files["Project/file-00.bin"]) == matches_before
        assert {match.target_volume_id for match in matches_before} == {second_id}
        assert db.connection.in_transaction is False
    finally:
        db.close()


def test_failed_analysis_build_keeps_the_previous_published_result(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db, AnalysisOptions(batch_size=1))
        assert engine.analyse().status == "completed"
        run_before = engine.state().active_run_id
        matches_before = engine.file_matches(files["Project/report.psd"])

        def fail_after_work_starts(progress) -> None:
            if progress.phase == "index_files" and progress.completed >= 1:
                raise RuntimeError("simulated analysis failure")

        with pytest.raises(RuntimeError, match="simulated analysis failure"):
            engine.analyse(progress_callback=fail_after_work_starts)

        assert engine.state().active_run_id == run_before
        assert engine.file_matches(files["Project/report.psd"]) == matches_before
        assert db.connection.in_transaction is False
    finally:
        db.close()


def test_volume_health_distinguishes_clean_empty_error_empty_and_not_scanned(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        clean_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Clean Empty",
            drive_id="AID-001",
            files={},
        )
        errored_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Errored Empty",
            drive_id="AID-002",
            files={},
            scan_errors=3,
        )
        never_scanned_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Never Scanned",
            drive_id="AID-003",
            files={},
            scan_status=None,
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        summaries = {
            int(object_field(summary, "volume_id")): summary
            for summary in engine.volume_summaries()
        }

        clean = summaries[clean_id]
        assert int(object_field(clean, "total_files", "indexed_files", "file_count")) == 0
        assert bool(object_field(clean, "coverage_eligible")) is False
        assert status_text(
            object_field(clean, "health_status", "status", "scan_health")
        ) in {"empty", "healthy_empty", "completed_empty"}
        assert object_field(
            clean,
            "strong_files_percent",
            "file_coverage_percent",
            "coverage_files_percent",
        ) is None

        errored = summaries[errored_id]
        assert status_text(
            object_field(errored, "health_status", "status", "scan_health")
        ) in {
            "unknown",
            "check_scan",
            "completed_with_errors",
            "scan_errors",
            "incomplete",
        }
        assert status_text(
            object_field(errored, "health_status", "status", "scan_health")
        ) not in {"empty", "healthy_empty", "completed_empty"}
        assert bool(object_field(errored, "coverage_eligible")) is False

        never_scanned = summaries[never_scanned_id]
        assert status_text(
            object_field(never_scanned, "health_status", "status", "scan_health")
        ) in {"not_scanned", "unknown", "no_applied_scan"}
        assert status_text(
            object_field(never_scanned, "health_status", "status", "scan_health")
        ) not in {"empty", "healthy_empty", "completed_empty"}
        assert bool(object_field(never_scanned, "coverage_eligible")) is False
    finally:
        db.close()


def test_scan_report_keeps_latest_attempt_separate_from_last_applied_data(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Archive",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        failed_scan_id = db.start_scan(volume_id)
        db.finish_scan(
            failed_scan_id,
            "failed",
            0,
            0,
            1,
            "Drive disconnected during scan",
        )

        assert engine.state().is_stale is False
        records = {
            int(object_field(record, "volume_id")): record
            for record in engine.scan_records()
        }
        record = records[volume_id]
        assert status_text(
            object_field(record, "latest_attempt_status", "status")
        ) == "failed"
        assert object_field(
            record,
            "last_applied_at",
            "applied_scan_at",
            "catalogue_scan_at",
        )
    finally:
        db.close()


def test_later_failed_attempt_does_not_reclassify_clean_applied_empty_drive(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Clean Empty",
            drive_id="AID-001",
            files={},
        )
        failed_scan_id = db.start_scan(volume_id)
        db.finish_scan(
            failed_scan_id,
            "failed",
            0,
            0,
            1,
            "Drive was disconnected",
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        summary = next(
            row
            for row in engine.volume_summaries()
            if int(object_field(row, "volume_id")) == volume_id
        )
        scan = next(
            row
            for row in engine.scan_records()
            if int(object_field(row, "volume_id")) == volume_id
        )

        assert status_text(object_field(summary, "health_status", "scan_health")) == "empty"
        assert status_text(object_field(scan, "health_status")) == "empty"
        assert status_text(object_field(scan, "latest_attempt_status", "status")) == "failed"
        assert object_field(scan, "last_applied_at", "applied_scan_at")
    finally:
        db.close()


def test_populated_volume_with_applied_scan_errors_has_unknown_coverage(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Incomplete Archive",
            drive_id="AID-001",
            files={"Known/report.psd": (100, MODIFIED_AT)},
            scan_errors=2,
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="Other Copy",
            drive_id="AID-002",
            files={"Known/report.psd": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        summary = next(
            row
            for row in engine.volume_summaries()
            if int(object_field(row, "volume_id")) == volume_id
        )

        assert status_text(object_field(summary, "health_status", "scan_health")) == (
            "completed_with_errors"
        )
        assert bool(object_field(summary, "coverage_eligible")) is False
        assert status_text(object_field(summary, "status")) == "unknown"
    finally:
        db.close()


def test_complete_drive_copy_report_does_not_change_manual_mirror_settings(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        first_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={
                "Project/report.psd": (100, MODIFIED_AT),
                "Project/notes.txt": (50, MODIFIED_AT),
            },
        )
        second_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={
                "Project/report.psd": (100, MODIFIED_AT),
                "Project/notes.txt": (50, MODIFIED_AT),
            },
        )
        before = {
            volume_id: (
                int(db.get_volume(volume_id)["is_mirror"]),
                db.get_volume(volume_id)["master_volume_id"],
            )
            for volume_id in (first_id, second_id)
        }

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        candidates = list(engine.mirror_candidates())
        candidate = next(
            candidate
            for candidate in candidates
            if {
                int(
                    object_field(
                        candidate,
                        "volume_a_id",
                        "first_volume_id",
                        "source_volume_id",
                    )
                ),
                int(
                    object_field(
                        candidate,
                        "volume_b_id",
                        "second_volume_id",
                        "target_volume_id",
                    )
                ),
            }
            == {first_id, second_id}
        )
        assert bool(object_field(candidate, "complete", "is_complete", "exact")) is True
        assert float(
            object_field(
                candidate,
                "a_on_b_percent",
                "first_on_second_percent",
                "source_coverage_percent",
            )
        ) == 100.0
        assert float(
            object_field(
                candidate,
                "b_on_a_percent",
                "second_on_first_percent",
                "target_coverage_percent",
            )
        ) == 100.0

        after = {
            volume_id: (
                int(db.get_volume(volume_id)["is_mirror"]),
                db.get_volume(volume_id)["master_volume_id"],
            )
            for volume_id in (first_id, second_id)
        }
        assert after == before == {
            first_id: (0, None),
            second_id: (0, None),
        }
    finally:
        db.close()


def test_folder_copies_scattered_across_drives_are_partial_not_complete(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (100, MODIFIED_AT),
            },
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="First Partial",
            drive_id="AID-002",
            files={"Project/one.bin": (100, MODIFIED_AT)},
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="Second Partial",
            drive_id="AID-003",
            files={"Project/two.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.folder_status(source_folders["Project"])

        assert status.status == "possible"
        assert status.scattered is True
        assert float(status.best_coverage_files_percent) == 50.0
        assert all(match.status != "likely" for match in engine.folder_matches(source_folders["Project"]))
    finally:
        db.close()


def test_old_scan_errors_do_not_taint_a_later_clean_empty_scan(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Empty Archive",
            drive_id="AID-001",
            files={},
            scan_errors=4,
        )
        with db.transaction():
            db.refresh_volume_counts(volume_id, LATER_SCAN)
        clean_scan_id = db.start_scan(volume_id)
        db.finish_scan(clean_scan_id, "completed", 0, 1, 0)

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        summary = next(
            row
            for row in engine.volume_summaries()
            if int(object_field(row, "volume_id")) == volume_id
        )

        assert status_text(
            object_field(summary, "health_status", "status", "scan_health")
        ) in {"empty", "healthy_empty", "completed_empty"}
    finally:
        db.close()


@pytest.mark.parametrize(
    "system_path",
    ["System Volume Information", "$RECYCLE.BIN"],
)
def test_protected_system_metadata_warning_does_not_make_empty_drive_unhealthy(
    tmp_path,
    system_path,
):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Intentionally Empty",
            drive_id="AID-001",
            files={},
            scan_errors=1,
        )
        scan_id = int(
            db.connection.execute(
                "SELECT MAX(id) FROM scan_history WHERE volume_id = ?",
                (volume_id,),
            ).fetchone()[0]
        )
        with db.transaction():
            db.add_scan_error(
                scan_id,
                volume_id,
                system_path,
                f"[WinError 5] Access is denied: {system_path}",
            )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        summary = next(
            row
            for row in engine.volume_summaries()
            if int(object_field(row, "volume_id")) == volume_id
        )
        scan = next(
            row
            for row in engine.scan_records()
            if int(object_field(row, "volume_id")) == volume_id
        )

        assert status_text(object_field(summary, "health_status", "scan_health")) == "empty"
        assert status_text(object_field(scan, "health_status")) == "empty"
        assert int(object_field(scan, "latest_attempt_errors")) == 1
    finally:
        db.close()


def test_user_tree_access_error_still_makes_empty_scan_health_unknown(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Possibly Incomplete",
            drive_id="AID-001",
            files={},
            scan_errors=1,
        )
        scan_id = int(
            db.connection.execute(
                "SELECT MAX(id) FROM scan_history WHERE volume_id = ?",
                (volume_id,),
            ).fetchone()[0]
        )
        with db.transaction():
            db.add_scan_error(
                scan_id,
                volume_id,
                "Client Projects/Restricted",
                "Access is denied",
            )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        summary = next(
            row
            for row in engine.volume_summaries()
            if int(object_field(row, "volume_id")) == volume_id
        )
        scan = next(
            row
            for row in engine.scan_records()
            if int(object_field(row, "volume_id")) == volume_id
        )

        assert status_text(object_field(summary, "health_status", "scan_health")) == (
            "completed_with_errors"
        )
        assert status_text(object_field(scan, "health_status")) == "completed_with_errors"
        assert int(object_field(scan, "latest_attempt_errors")) == 1
    finally:
        db.close()


def test_manual_mirror_relationship_is_not_used_as_content_evidence(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        master_id, master_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Master",
            drive_id="AID-001",
            files={"Only/master.bin": (100, MODIFIED_AT)},
        )
        mirror_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Declared Mirror",
            drive_id="AID-002",
            files={"Different/content.bin": (999, MODIFIED_AT)},
        )
        db.upsert_volume_register(
            mirror_id,
            {
                "drive_id": "AID-002",
                "is_mirror": True,
                "master_volume_id": master_id,
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        assert engine.file_status(master_files["Only/master.bin"]).status == "single"
        assert not engine.file_matches(master_files["Only/master.bin"])
        candidate = next(
            item
            for item in engine.mirror_candidates()
            if {item.source_volume_id, item.target_volume_id}
            == {master_id, mirror_id}
        )
        assert candidate.manual_mirror_link is True
        assert candidate.complete_structure is False
        assert candidate.source_coverage_percent == 0.0
        assert candidate.target_coverage_percent == 0.0
        assert "below the suggestion threshold" in candidate.evidence_text
    finally:
        db.close()


def test_manual_mirror_added_after_analysis_is_still_reported(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        master_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Master",
            drive_id="AID-001",
            files={"Only/master.bin": (100, MODIFIED_AT)},
        )
        mirror_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Declared Later",
            drive_id="AID-002",
            files={"Different/content.bin": (999, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        assert engine.mirror_candidates() == []

        db.upsert_volume_register(
            mirror_id,
            {
                "drive_id": "AID-002",
                "is_mirror": True,
                "master_volume_id": master_id,
            },
        )

        candidate = next(iter(engine.mirror_candidates()))
        assert {candidate.source_volume_id, candidate.target_volume_id} == {
            master_id,
            mirror_id,
        }
        assert candidate.manual_mirror_link is True
        assert candidate.source_coverage_percent is None
        assert candidate.target_coverage_percent is None
        assert "Update the analysis" in candidate.evidence_text
    finally:
        db.close()


def test_too_common_name_and_size_is_ambiguous_instead_of_false_backup(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        file_ids = []
        volume_ids = []
        for index in range(3):
            volume_id, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    f"Different-{index}/common.bin": (
                        4096,
                        f"2026-08-{index + 1:02d}T09:00:00.000000+0000",
                    )
                },
            )
            volume_ids.append(volume_id)
            file_ids.append(files[f"Different-{index}/common.bin"])

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=2),
        )
        summary = engine.analyse()

        assert summary.ambiguous_files == 3
        for file_id in file_ids:
            status = engine.file_status(file_id)
            assert status.status == "ambiguous"
            assert status.other_volume_ids == ()
            assert engine.file_matches(file_id) == []
        summaries = {row.volume_id: row for row in engine.volume_summaries()}
        assert all(summaries[volume_id].ambiguous_files == 1 for volume_id in volume_ids)
        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_strong_path_and_time_match_survives_common_key_guard(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        file_ids = []
        for index in range(3):
            _, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={"Project/common.bin": (4096, MODIFIED_AT)},
            )
            file_ids.append(files["Project/common.bin"])

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_records_per_key=1),
        )
        assert engine.analyse().status == "completed"

        assert all(engine.file_status(file_id).status == "likely" for file_id in file_ids)
        assert all(len(engine.file_matches(file_id)) == 2 for file_id in file_ids)
    finally:
        db.close()


def test_known_os_metadata_is_visible_but_excluded_from_coverage(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_ids = []
        file_ids = []
        folder_ids = []
        for index in range(2):
            volume_id, files, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    "Project/.DS_Store": (6148, MODIFIED_AT),
                    "System Volume Information/IndexerVolumeGuid": (76, MODIFIED_AT),
                },
            )
            volume_ids.append(volume_id)
            file_ids.extend(files.values())
            folder_ids.append(folders["Project"])

        engine = BackupAnalysisEngine(db)
        summary = engine.analyse()

        assert summary.excluded_files == 4
        assert all(engine.file_status(file_id).status == "excluded" for file_id in file_ids)
        assert all(engine.folder_status(folder_id).status == "excluded" for folder_id in folder_ids)
        summaries = {row.volume_id: row for row in engine.volume_summaries()}
        for volume_id in volume_ids:
            volume = summaries[volume_id]
            assert volume.status == "excluded"
            assert volume.coverage_files == 0
            assert volume.excluded_files == 2
            assert volume.likely_files_percent is None
        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_folder_depth_accepts_backslash_catalogue_paths(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id = db.create_volume(
            "Archive",
            str(tmp_path / "offline-AID-001"),
            {"drive_id": "AID-001"},
        )
        with db.transaction():
            root_id = db.ensure_folder(
                volume_id,
                None,
                "Archive",
                "",
                SCANNED_AT,
                MODIFIED_AT,
            )
            project_id = db.ensure_folder(
                volume_id,
                root_id,
                "Project",
                "Project",
                SCANNED_AT,
                MODIFIED_AT,
            )
            sub_id = db.ensure_folder(
                volume_id,
                project_id,
                "Sub",
                r"Project\Sub",
                SCANNED_AT,
                MODIFIED_AT,
            )
            db.upsert_file(
                volume_id,
                sub_id,
                "file.bin",
                r"Project\Sub\file.bin",
                "bin",
                100,
                MODIFIED_AT,
                SCANNED_AT,
            )
            db.rebuild_folder_statistics(volume_id, SCANNED_AT)
            db.refresh_volume_counts(volume_id, SCANNED_AT)
        scan_id = db.start_scan(volume_id)
        db.finish_scan(scan_id, "completed", 1, 3, 0)

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        assert engine.folder_status(project_id).status == "single"
        assert engine.folder_status(sub_id).status == "single"
    finally:
        db.close()


def test_renamed_complete_subtree_is_possible_not_green(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Original/one.bin": (100, MODIFIED_AT),
                "Original/two.bin": (200, MODIFIED_AT),
            },
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Target",
            drive_id="AID-002",
            files={
                "Renamed/one.bin": (100, MODIFIED_AT),
                "Renamed/two.bin": (200, MODIFIED_AT),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.folder_status(source_folders["Original"])
        matches = engine.folder_matches(source_folders["Original"])

        assert status.status == "possible"
        assert status.best_coverage_files_percent == 100.0
        assert status.other_volume_ids == (target_id,)
        assert len(matches) == 1
        assert matches[0].status == "possible"
        assert "folder name differs" in matches[0].evidence_text
    finally:
        db.close()


def test_exact_folder_match_is_retained_when_file_key_is_ambiguous(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/common-a.bin": (4096, "2026-08-01T09:00:00.000000+0000"),
                "Project/common-b.bin": (8192, "2026-08-01T09:00:00.000000+0000"),
            },
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Target",
            drive_id="AID-002",
            files={
                "Project/common-a.bin": (4096, "2026-08-02T09:00:00.000000+0000"),
                "Project/common-b.bin": (8192, "2026-08-02T09:00:00.000000+0000"),
            },
        )

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_records_per_key=1),
        )
        assert engine.analyse().status == "completed"
        status = engine.folder_status(source_folders["Project"])
        matches = engine.folder_matches(source_folders["Project"])

        assert status.status == "likely"
        assert status.other_volume_ids == (target_id,)
        assert len(matches) == 1
        assert matches[0].status == "likely"
        assert matches[0].matched_files == matches[0].total_files == 2
    finally:
        db.close()


def test_repeated_file_candidates_are_suppressed_instead_of_arbitrarily_paired(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/First/same.bin": (100, MODIFIED_AT),
                "Project/Second/same.bin": (100, MODIFIED_AT),
            },
        )
        _, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Target",
            drive_id="AID-002",
            files={"Elsewhere/same.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        source_statuses = [
            engine.file_status(file_id).status for file_id in source_files.values()
        ]
        project = engine.folder_status(source_folders["Project"])

        assert source_statuses == ["ambiguous", "ambiguous"]
        assert project.status == "ambiguous"
        assert project.best_target_volume_id is None
        assert project.matched_files == 0
        assert project.total_files == 2
        assert project.best_coverage_files_percent is None
    finally:
        db.close()


def test_valid_unique_pair_survives_competing_duplicates_on_a_third_drive(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Duplicated",
            drive_id="AID-001",
            files={
                "First/same.bin": (100, MODIFIED_AT),
                "Second/same.bin": (100, MODIFIED_AT),
            },
        )
        second_id, second_files, second_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Unique B",
            drive_id="AID-002",
            files={"B/same.bin": (100, MODIFIED_AT)},
        )
        third_id, third_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Unique C",
            drive_id="AID-003",
            files={"C/same.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        assert all(
            engine.file_status(file_id).status == "ambiguous"
            for file_id in source_files.values()
        )
        second = engine.file_status(second_files["B/same.bin"])
        third = engine.file_status(third_files["C/same.bin"])
        assert second.status == third.status == "possible"
        assert second.other_volume_ids == (third_id,)
        assert third.other_volume_ids == (second_id,)
        assert engine.folder_status(second_folders["B"]).status == "possible"
    finally:
        db.close()


def test_unequal_casefolded_strong_duplicates_are_not_arbitrarily_paired(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Case-sensitive source",
            drive_id="AID-001",
            files={
                "Project/Same.bin": (100, MODIFIED_AT),
                "Project/same.bin": (100, MODIFIED_AT),
            },
        )
        _, target_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Target",
            drive_id="AID-002",
            files={"Project/SAME.BIN": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        all_file_ids = [*source_files.values(), *target_files.values()]
        assert all(
            engine.file_status(file_id).status == "ambiguous"
            for file_id in all_file_ids
        )
        assert engine.folder_status(source_folders["Project"]).status == "ambiguous"
        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_folder_status_separates_complete_and_partial_target_drives(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )
        complete_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Complete",
            drive_id="AID-002",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )
        partial_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Partial",
            drive_id="AID-003",
            files={"Elsewhere/one.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.folder_status(source_folders["Project"])

        assert status.status == "likely"
        assert set(status.other_volume_ids) == {complete_id, partial_id}
        assert status.strong_volume_ids == (complete_id,)
        assert status.possible_volume_ids == (partial_id,)
    finally:
        db.close()


def test_overly_common_folder_fingerprint_never_becomes_complete(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        folder_ids = []
        for index in range(9):
            _, _, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    "Project/one.bin": (100, None),
                    "Project/two.bin": (200, None),
                },
            )
            folder_ids.append(folders["Project"])

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        assert all(
            engine.folder_status(folder_id).status == "ambiguous"
            for folder_id in folder_ids
        )
        assert all(engine.folder_matches(folder_id) == [] for folder_id in folder_ids)
        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_repeated_folder_structure_does_not_reuse_one_target_folder(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "X/Project/a.bin": (100, MODIFIED_AT),
                "X/Project/b.bin": (200, MODIFIED_AT),
                "Y/Project/a.bin": (100, MODIFIED_AT),
                "Y/Project/b.bin": (200, MODIFIED_AT),
            },
        )
        _, _, target_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Target",
            drive_id="AID-002",
            files={
                "Z/Project/a.bin": (100, MODIFIED_AT),
                "Z/Project/b.bin": (200, MODIFIED_AT),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        project_ids = [
            source_folders["X/Project"],
            source_folders["Y/Project"],
            target_folders["Z/Project"],
        ]

        assert all(
            engine.folder_status(folder_id).status == "ambiguous"
            for folder_id in project_ids
        )
        assert all(engine.folder_matches(folder_id) == [] for folder_id in project_ids)
    finally:
        db.close()


def test_complete_drive_copy_requires_trustworthy_applied_scans(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        first_id, _, first_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Incomplete scan",
            drive_id="AID-001",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
            scan_errors=1,
        )
        second_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Clean scan",
            drive_id="AID-002",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        candidate = next(
            row
            for row in engine.mirror_candidates()
            if {row.source_volume_id, row.target_volume_id} == {first_id, second_id}
        )

        assert candidate.complete_structure is False
        assert "not labelled a complete drive copy" in candidate.evidence_text
        folder = engine.folder_status(first_folders["Project"])
        assert folder.status == "possible"
        assert "scan" in folder.evidence_text.casefold()
    finally:
        db.close()


def test_removed_manual_mirror_does_not_leave_a_ghost_suggestion(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        master_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Master",
            drive_id="AID-001",
            files={"Only/master.bin": (100, MODIFIED_AT)},
        )
        mirror_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Mirror",
            drive_id="AID-002",
            files={"Different/content.bin": (999, MODIFIED_AT)},
        )
        db.upsert_volume_register(
            mirror_id,
            {
                "drive_id": "AID-002",
                "is_mirror": True,
                "master_volume_id": master_id,
            },
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        assert len(engine.mirror_candidates()) == 1

        db.upsert_volume_register(
            mirror_id,
            {"drive_id": "AID-002", "is_mirror": False},
        )

        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_catalogue_change_during_analysis_discards_new_generation(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db, AnalysisOptions(batch_size=1))
        assert engine.analyse().status == "completed"
        run_before = engine.state().active_run_id
        changed = False

        def invalidate_during_build(progress) -> None:
            nonlocal changed
            if not changed and progress.phase == "index_files" and progress.completed >= 1:
                changed = True
                engine.invalidate_all("Catalogue changed during the running analysis.")

        result = engine.analyse(progress_callback=invalidate_during_build)

        assert result.status == "discarded"
        assert engine.state().active_run_id == run_before
        assert engine.state().is_stale is True
        assert engine.file_status(files["Project/report.psd"]).status == "likely"
        assert db.connection.execute(
            "SELECT COUNT(*) FROM backup_analysis_invalidations"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_cleanup_error_after_publish_does_not_misreport_analysis(monkeypatch, tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        add_catalogued_volume(
            db,
            tmp_path,
            name="Archive",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        engine = BackupAnalysisEngine(db)
        original_drop = engine._drop_work_tables
        calls = 0

        def fail_only_after_publication() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("simulated temporary-table cleanup failure")
            original_drop()

        monkeypatch.setattr(engine, "_drop_work_tables", fail_only_after_publication)
        result = engine.analyse()

        assert result.status == "completed"
        assert engine.state().active_run_id == result.run_id
    finally:
        db.close()


def test_published_analysis_can_be_queried_through_a_read_only_connection(tmp_path):
    path = tmp_path / "catalogue.jvvv"
    db = Database(path)
    _, files, _ = add_catalogued_volume(
        db,
        tmp_path,
        name="First",
        drive_id="AID-001",
        files={"Project/report.psd": (100, MODIFIED_AT)},
    )
    second_id, _, _ = add_catalogued_volume(
        db,
        tmp_path,
        name="Second",
        drive_id="AID-002",
        files={"Project/report.psd": (100, MODIFIED_AT)},
    )
    original_schema_version = db.connection.execute("PRAGMA user_version").fetchone()[0]
    assert BackupAnalysisEngine(db).analyse().status == "completed"
    assert db.connection.execute("PRAGMA user_version").fetchone()[0] == original_schema_version
    db.close()

    reader = Database(
        path,
        initialize=False,
        create=False,
        read_only=True,
    )
    try:
        engine = BackupAnalysisEngine(reader)
        assert engine.state().is_stale is False
        assert engine.file_status(files["Project/report.psd"]).status == "likely"
        assert {
            match.target_volume_id
            for match in engine.file_matches(files["Project/report.psd"])
        } == {second_id}
    finally:
        reader.close()


def test_backup_analysis_worker_interrupts_active_sqlite_work_when_cancelled(monkeypatch):
    events = []

    class FakeConnection:
        def set_progress_handler(self, callback, steps):
            events.append(("progress_handler", callback is not None, steps))

        def interrupt(self):
            events.append(("interrupt",))

    class FakeDatabase:
        def __init__(self, path, *, initialize, create, read_only):
            events.append(("open", path, initialize, create, read_only))
            self.connection = FakeConnection()

        def close(self):
            events.append(("close",))

    class FakeEngine:
        def __init__(self, db):
            events.append(("engine", db))

        def analyse(self, *, progress_callback, cancel_callback):
            worker.cancel()
            assert cancel_callback() is True
            raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr("jvvv.app.Database", FakeDatabase)
    monkeypatch.setattr("jvvv.app.BackupAnalysisEngine", FakeEngine)
    worker = BackupAnalysisWorker(Path("catalogue.jvvv"))
    cancelled = []
    failures = []
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.failed.connect(failures.append)

    worker.run()

    assert cancelled == [True]
    assert failures == []
    assert ("interrupt",) in events
    assert events[-2:] == [
        ("progress_handler", False, 0),
        ("close",),
    ]


def test_worker_does_not_report_cancelled_after_analysis_already_completed(monkeypatch):
    class FakeConnection:
        def set_progress_handler(self, callback, steps):
            pass

        def interrupt(self):
            pass

    class FakeDatabase:
        def __init__(self, *args, **kwargs):
            self.connection = FakeConnection()

        def close(self):
            pass

    class FakeEngine:
        def __init__(self, db):
            pass

        def analyse(self, *, progress_callback, cancel_callback):
            worker.cancel()
            return SimpleNamespace(status="completed")

    from types import SimpleNamespace

    monkeypatch.setattr("jvvv.app.Database", FakeDatabase)
    monkeypatch.setattr("jvvv.app.BackupAnalysisEngine", FakeEngine)
    worker = BackupAnalysisWorker(Path("catalogue.jvvv"))
    completed = []
    cancelled = []
    worker.finished.connect(completed.append)
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.run()

    assert len(completed) == 1
    assert completed[0].status == "completed"
    assert cancelled == []
