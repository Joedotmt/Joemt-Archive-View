from __future__ import annotations

import builtins
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path, PurePosixPath
import sqlite3

import pytest

import jvvv.backup_analysis as backup_analysis
from jvvv.backup_analysis import (
    FILE_EVIDENCE_PAIRED_SUFFIX,
    FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX,
    MAX_STORED_OTHER_VOLUME_IDS,
    AnalysisOptions,
    BackupAnalysisEngine,
    _canonical_modified_time,
    evidence_counts,
    file_evidence_flags,
    file_reason_key,
    folder_evidence_flags,
    folder_reason_key,
    modified_times_match,
)
from jvvv.app import BackupAnalysisWorker
from jvvv.database import Database


SCANNED_AT = "2026-08-20T10:00:00.000000+0000"
LATER_SCAN = "2026-08-21T10:00:00.000000+0000"
MODIFIED_AT = "2026-08-19T09:00:00.000000+0000"
MODIFIED_PLUS_2S = "2026-08-19T09:00:02.000000+0000"
MODIFIED_PLUS_3S = "2026-08-19T09:00:03.000000+0000"
MODIFIED_PLUS_1H = "2026-08-19T10:00:00.000000+0000"
HASH_A = b"a" * 32
HASH_B = b"b" * 32


def add_catalogued_volume(
    db: Database,
    tmp_path: Path,
    *,
    name: str,
    drive_id: str,
    files: dict[str, tuple[int, str | None]],
    scanned_at: str = SCANNED_AT,
    scan_errors: int = 0,
    hash_errors: int = 0,
    scan_status: str | None = "completed",
    content_hashes: dict[str, bytes | tuple[str, bytes]] | None = None,
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

            saved_hash = (content_hashes or {}).get(relative_path)
            if isinstance(saved_hash, tuple):
                hash_algorithm, content_hash = saved_hash
            else:
                hash_algorithm = "sha256" if saved_hash is not None else None
                content_hash = saved_hash
            file_ids[relative_path] = db.upsert_file(
                volume_id=volume_id,
                folder_id=parent_id,
                name=path.name,
                relative_path=relative_path,
                extension=path.suffix.lstrip("."),
                size_bytes=size_bytes,
                modified_at=modified_at,
                scanned_at=scanned_at,
                content_hash=content_hash,
                content_hash_algorithm=hash_algorithm,
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
            hash_errors=hash_errors,
        )
    return volume_id, file_ids, folder_ids


def status_text(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold()


def test_analysis_distinguishes_strong_weak_and_same_drive_only_matches(tmp_path):
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
                "Loose/retimed.mov": (400, MODIFIED_AT),
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
                "Elsewhere/retimed.mov": (400, LATER_SCAN),
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

        # A relocated file with the same name, size and time still exists elsewhere.
        relocated = engine.file_status(first_files["Loose/clip.mov"])
        assert relocated.status == "likely"
        assert file_reason_key(relocated.evidence_text) == "time_match"
        assert {
            match.target_volume_id
            for match in engine.file_matches(first_files["Loose/clip.mov"])
        } == {second_volume_id}

        # Name and size alone, with a different modified time, is only a lead.
        retimed = engine.file_status(first_files["Loose/retimed.mov"])
        assert retimed.status == "possible"
        assert file_reason_key(retimed.evidence_text) == "name_size_only"
        assert retimed.possible_volume_ids == (second_volume_id,)

        assert engine.file_status(first_files["Loose/source-only.txt"]).status == "single"
        # Repeated metadata on one physical catalogue volume is not a backup copy.
        assert engine.file_status(first_files["Duplicate A/local.bin"]).status == "single"
        assert engine.file_status(first_files["Duplicate B/local.bin"]).status == "single"

        project = engine.folder_status(first_folders["Project"])
        assert project.status == "likely"
        assert folder_reason_key(project.evidence_text) == "all_files_found"
        assert {
            match.target_volume_id
            for match in engine.folder_matches(first_folders["Project"])
        } == {second_volume_id}
        assert first_volume_id != second_volume_id
        assert second_files["Project/report.psd"] > 0
    finally:
        db.close()


def test_exact_hash_verifies_renamed_and_relocated_file(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Original/report.psd": (100, MODIFIED_AT)},
            content_hashes={"Original/report.psd": HASH_A},
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Backup",
            drive_id="AID-002",
            files={"Elsewhere/renamed-copy.bin": (100, LATER_SCAN)},
            content_hashes={"Elsewhere/renamed-copy.bin": HASH_A},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Original/report.psd"])

        assert status.status == "likely"
        assert status.verified_volume_ids == (target_id,)
        assert status.strong_volume_ids == (target_id,)
        assert status.possible_volume_ids == ()
        assert "exact sha-256 content hash" in status.evidence_text.casefold()
        matches = engine.file_matches(source_files["Original/report.psd"])
        assert len(matches) == 1
        assert "exact sha-256" in matches[0].evidence_text.casefold()
    finally:
        db.close()


def test_exact_hash_remains_authoritative_when_saved_size_metadata_disagrees(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Original.bin": (100, MODIFIED_AT)},
            content_hashes={"Original.bin": HASH_A},
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Backup",
            drive_id="AID-002",
            files={"Renamed.bin": (101, LATER_SCAN)},
            content_hashes={"Renamed.bin": HASH_A},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Original.bin"])

        assert status.status == "likely"
        assert status.verified_volume_ids == (target_id,)
    finally:
        db.close()


def test_comparable_hash_conflict_vetoes_identical_metadata(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
            content_hashes={"Project/report.psd": HASH_A},
        )
        _, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Other",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
            content_hashes={"Project/report.psd": HASH_B},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Project/report.psd"])

        assert status.status == "ambiguous"
        assert status.other_volume_ids == ()
        assert status.verified_volume_ids == ()
        assert engine.file_matches(source_files["Project/report.psd"]) == []
        assert "hashes differ" in status.evidence_text.casefold()
    finally:
        db.close()


def test_metadata_fallback_remains_explicit_when_one_hash_is_missing(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
            content_hashes={"Project/report.psd": HASH_A},
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Legacy backup",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Project/report.psd"])

        assert status.status == "likely"
        assert status.strong_volume_ids == (target_id,)
        assert status.verified_volume_ids == ()
        assert file_reason_key(status.evidence_text) == "time_match"
        assert "contents were not compared" in status.evidence_text.casefold()
    finally:
        db.close()


def test_hash_match_is_not_downgraded_by_common_metadata_guard(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Project/common.bin": (100, MODIFIED_AT)},
            content_hashes={"Project/common.bin": HASH_A},
        )
        verified_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Verified",
            drive_id="AID-002",
            files={"Project/common.bin": (100, MODIFIED_AT)},
            content_hashes={"Project/common.bin": HASH_A},
        )
        metadata_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Legacy",
            drive_id="AID-003",
            files={"Project/common.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_records_per_key=1),
        )
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Project/common.bin"])

        assert status.status == "likely"
        assert status.verified_volume_ids == (verified_id,)
        assert set(status.strong_volume_ids) == {verified_id, metadata_id}
        # The hash is authoritative: a generic name never hides a verified copy.
        assert file_reason_key(status.evidence_text) == "hash_verified"
        assert file_evidence_flags(status.evidence_text) == {"extra_drives"}
    finally:
        db.close()


def test_swapped_hashes_block_the_structure_match_but_every_file_still_verifies(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
            content_hashes={
                "Project/one.bin": HASH_A,
                "Project/two.bin": HASH_B,
            },
        )
        legacy_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Legacy",
            drive_id="AID-002",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )
        conflicting_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Conflicting",
            drive_id="AID-003",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
            content_hashes={
                "Project/one.bin": HASH_B,
                "Project/two.bin": HASH_A,
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        folder = engine.folder_status(source_folders["Project"])

        # No drive matches the folder as a unit (hashes are part of the layout
        # fingerprint), yet every file exists elsewhere: hash-verified on the
        # swapped drive, by name, size and time on the legacy drive.
        assert folder.status == "likely"
        assert folder_reason_key(folder.evidence_text) == "all_files_hashed"
        assert folder.best_target_volume_id == conflicting_id
        assert folder.best_coverage_files_percent == 100.0
        assert set(folder.strong_volume_ids) == {legacy_id, conflicting_id}
        for file_id in source_files.values():
            status = engine.file_status(file_id)
            assert status.status == "likely"
            assert status.verified_volume_ids == (conflicting_id,)
            assert status.strong_volume_ids == (legacy_id, conflicting_id)
            assert status.possible_volume_ids == ()
            # The same-name record with different content is noted, not hidden.
            assert "conflict" in file_evidence_flags(status.evidence_text)
    finally:
        db.close()


def test_fully_hashed_matching_folder_can_be_complete(tmp_path):
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
            content_hashes={
                "Project/one.bin": HASH_A,
                "Project/two.bin": HASH_B,
            },
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Backup",
            drive_id="AID-002",
            files={
                "Project/one.bin": (100, LATER_SCAN),
                "Project/two.bin": (200, LATER_SCAN),
            },
            content_hashes={
                "Project/one.bin": HASH_A,
                "Project/two.bin": HASH_B,
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        folder = engine.folder_status(source_folders["Project"])

        assert folder.status == "likely"
        assert folder.strong_volume_ids == (target_id,)
        assert folder_reason_key(folder.evidence_text) == "full_match_hashed"
        assert folder_evidence_flags(folder.evidence_text) == frozenset()
        assert folder.best_coverage_files_percent == 100.0
    finally:
        db.close()


def test_partially_hashed_matching_structure_is_a_full_match_partly_hashed(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        files = {
            "Project/one.bin": (100, MODIFIED_AT),
            "Project/two.bin": (200, MODIFIED_AT),
        }
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files=files,
            content_hashes={"Project/one.bin": HASH_A},
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Backup",
            drive_id="AID-002",
            files=files,
            content_hashes={"Project/one.bin": HASH_A},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        folder = engine.folder_status(source_folders["Project"])

        assert folder.status == "likely"
        assert folder.strong_volume_ids == (target_id,)
        assert folder.possible_volume_ids == ()
        assert folder.best_coverage_files_percent == 100.0
        assert folder_reason_key(folder.evidence_text) == "full_match_partly_hashed"
        matches = engine.folder_matches(source_folders["Project"])
        assert [match.evidence_text for match in matches] == [
            "Whole folder matches here (contents not compared)."
        ]
    finally:
        db.close()


def test_overly_common_exact_hash_is_bounded_but_still_verified(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        file_ids = []
        folder_ids = []
        for index in range(3):
            _, files, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={f"Different-{index}/copy-{index}.bin": (100, MODIFIED_AT)},
                content_hashes={f"Different-{index}/copy-{index}.bin": HASH_A},
            )
            file_ids.extend(files.values())
            folder_ids.append(folders[f"Different-{index}"])

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_hash_volumes_per_signature=2),
        )
        assert engine.analyse().status == "completed"

        # Existence is still recorded for every member; only the one-to-one
        # coverage pairing is skipped, and the evidence says so.
        for file_id in file_ids:
            status = engine.file_status(file_id)
            assert status.status == "likely"
            assert len(status.verified_volume_ids) == 2
            assert status.strong_volume_ids == status.verified_volume_ids
            assert status.other_drive_count == 2
            assert len(engine.file_matches(file_id)) == 2
            assert file_reason_key(status.evidence_text) == "hash_verified"
            assert "repeated_content" in file_evidence_flags(status.evidence_text)
            assert evidence_counts(status.evidence_text) == (3, 3)
        # Folder coverage still sees the content on both other drives, even
        # though existence was recorded once per hash and drive, not per record.
        for folder_id in folder_ids:
            folder = engine.folder_status(folder_id)
            assert folder.status == "likely"
            assert folder_reason_key(folder.evidence_text) == "all_files_hashed"
            assert folder.matched_files == folder.total_files == 1
            assert len(folder.strong_volume_ids) == 2
        # Nothing was paired, so no drive pair reaches the mirror threshold.
        assert engine.mirror_candidates() == []
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
            files={"Elsewhere/report.psd": (100, LATER_SCAN)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Project/report.psd"])
        matches = engine.file_matches(source_files["Project/report.psd"])

        assert status.status == "likely"
        assert status.other_drive_count == 2
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
        summaries = {summary.volume_id: summary for summary in engine.volume_summaries()}

        clean = summaries[clean_id]
        assert clean.total_files == 0
        assert clean.coverage_eligible is False
        assert status_text(clean.health_status) == "empty"
        assert clean.likely_files_percent is None

        errored = summaries[errored_id]
        assert status_text(errored.health_status) == "completed_with_errors"
        assert errored.coverage_eligible is False

        never_scanned = summaries[never_scanned_id]
        assert status_text(never_scanned.health_status) == "not_scanned"
        assert never_scanned.coverage_eligible is False
    finally:
        db.close()


def test_volume_results_group_rows_once_and_preserve_all_aggregate_values(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        first_id, first_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="First",
            drive_id="AID-001",
            files={
                "likely.bin": (100, MODIFIED_AT),
                "possible.bin": (50, MODIFIED_AT),
                "ambiguous.bin": (25, MODIFIED_AT),
                "excluded.bin": (10, MODIFIED_AT),
                "single.bin": (5, MODIFIED_AT),
            },
        )
        second_id, second_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Second",
            drive_id="AID-002",
            files={"unknown.bin": (7, MODIFIED_AT)},
            scan_status=None,
        )
        empty_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Empty",
            drive_id="AID-003",
            files={},
        )

        engine = BackupAnalysisEngine(db)
        engine._create_work_tables()
        db.connection.executemany(
            "INSERT INTO backup_work_files VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, '')",
            [
                (first_files["likely.bin"], first_id, b"l", b"l", b"", MODIFIED_AT, 100, 1),
                (first_files["possible.bin"], first_id, b"p", b"p", b"", MODIFIED_AT, 50, 1),
                (first_files["ambiguous.bin"], first_id, b"a", b"a", b"", MODIFIED_AT, 25, 1),
                (first_files["excluded.bin"], first_id, b"e", b"e", b"", MODIFIED_AT, 10, 0),
                (first_files["single.bin"], first_id, b"s", b"s", b"", MODIFIED_AT, 5, 1),
                (second_files["unknown.bin"], second_id, b"u", b"u", b"", MODIFIED_AT, 7, 1),
            ],
        )
        db.connection.executemany(
            "INSERT INTO backup_stage_file_results VALUES (?, ?, ?, '[]', '', '[]', '[]', '[]')",
            [
                (first_files["likely.bin"], first_id, "likely"),
                (first_files["possible.bin"], first_id, "possible"),
                (first_files["ambiguous.bin"], first_id, "ambiguous"),
            ],
        )

        traced_sql: list[str] = []
        progress = []
        db.connection.set_trace_callback(traced_sql.append)
        try:
            engine._build_volume_results(progress.append, None)
        finally:
            db.connection.set_trace_callback(None)

        rows = {
            int(row["volume_id"]): dict(row)
            for row in db.connection.execute(
                "SELECT * FROM backup_stage_volume_results ORDER BY volume_id"
            )
        }
        assert rows[first_id] == {
            "volume_id": first_id,
            "status": "possible",
            "health_status": "completed",
            "coverage_eligible": 1,
            "total_files": 5,
            "total_bytes": 190,
            "coverage_files": 4,
            "coverage_bytes": 180,
            "likely_files": 1,
            "likely_bytes": 100,
            "possible_files": 1,
            "possible_bytes": 50,
            "ambiguous_files": 1,
            "ambiguous_bytes": 25,
            "excluded_files": 1,
            "excluded_bytes": 10,
            "single_files": 1,
            "single_bytes": 5,
            "likely_files_percent": 25.0,
            "likely_bytes_percent": pytest.approx(55.55555555555556),
            "latest_scan_status": "completed",
            "latest_scan_errors": 0,
        }
        assert rows[second_id]["status"] == "unknown"
        assert rows[second_id]["health_status"] == "not_scanned"
        assert rows[second_id]["coverage_eligible"] == 0
        assert rows[second_id]["total_files"] == 1
        assert rows[second_id]["total_bytes"] == 7
        assert rows[second_id]["single_files"] == 1
        assert rows[second_id]["single_bytes"] == 7
        assert rows[empty_id]["status"] == "empty"
        assert rows[empty_id]["health_status"] == "empty"
        assert rows[empty_id]["total_files"] == 0
        assert rows[empty_id]["total_bytes"] == 0
        assert [event.completed for event in progress] == [0, 1, 2, 3]
        assert all(event.phase == "volume_coverage" for event in progress)
        assert all(event.total == 3 for event in progress)

        normalized_sql = [" ".join(statement.casefold().split()) for statement in traced_sql]
        total_queries = [
            statement
            for statement in normalized_sql
            if "from backup_work_files" in statement
            and "as coverage_files" in statement
        ]
        matched_queries = [
            statement
            for statement in normalized_sql
            if "from backup_stage_file_results r" in statement
            and "as likely_files" in statement
        ]
        assert len(total_queries) == 1
        assert "group by volume_id" in total_queries[0]
        assert len(matched_queries) == 1
        assert "group by r.volume_id" in matched_queries[0]
    finally:
        db.close()


def test_analysis_can_defer_persistent_indexes_without_changing_the_default(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    analysis_indexes = {
        "idx_backup_file_results_item",
        "idx_backup_folder_results_item",
        "idx_backup_volume_results_volume",
        "idx_backup_folder_matches_item",
    }
    try:
        with db.transaction():
            for index_name in analysis_indexes:
                db.connection.execute(f"DROP INDEX IF EXISTS {index_name}")

        engine = BackupAnalysisEngine(db)
        assert engine.analyse(defer_persistent_indexes=True).status == "completed"
        existing = {
            str(row["name"])
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert analysis_indexes.isdisjoint(existing)

        engine.ensure_schema()
        existing = {
            str(row["name"])
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert analysis_indexes <= existing
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
        records = {record.volume_id: record for record in engine.scan_records()}
        record = records[volume_id]
        assert status_text(record.latest_attempt_status) == "failed"
        assert record.last_applied_at
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
            if row.volume_id == volume_id
        )
        scan = next(
            row
            for row in engine.scan_records()
            if row.volume_id == volume_id
        )

        assert status_text(summary.health_status) == "empty"
        assert status_text(scan.health_status) == "empty"
        assert status_text(scan.latest_attempt_status) == "failed"
        assert scan.last_applied_at
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
            if row.volume_id == volume_id
        )

        assert status_text(summary.health_status) == "completed_with_errors"
        assert summary.coverage_eligible is False
        assert status_text(summary.status) == "unknown"
    finally:
        db.close()


def test_hash_only_scan_error_keeps_applied_metadata_coverage_eligible(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Hash Gap",
            drive_id="AID-001",
            files={"Known/report.psd": (100, MODIFIED_AT)},
            scan_errors=1,
            hash_errors=1,
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
            row for row in engine.volume_summaries() if row.volume_id == volume_id
        )
        scan = next(row for row in engine.scan_records() if row.volume_id == volume_id)

        assert scan.health_status == "completed"
        assert scan.latest_attempt_errors == 1
        assert scan.latest_attempt_hash_errors == 1
        assert scan.last_applied_hash_errors == 1
        assert summary.health_status == "completed"
        assert summary.coverage_eligible is True
        assert summary.status == "likely"
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
                candidate.source_volume_id,
                candidate.target_volume_id,
            }
            == {first_id, second_id}
        )
        assert candidate.complete_structure is True
        assert candidate.source_coverage_percent == 100.0
        assert candidate.target_coverage_percent == 100.0

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
            if row.volume_id == volume_id
        )

        assert status_text(summary.health_status) == "empty"
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
            if row.volume_id == volume_id
        )
        scan = next(
            row
            for row in engine.scan_records()
            if row.volume_id == volume_id
        )

        assert status_text(summary.health_status) == "empty"
        assert status_text(scan.health_status) == "empty"
        assert scan.latest_attempt_errors == 1
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
            if row.volume_id == volume_id
        )
        scan = next(
            row
            for row in engine.scan_records()
            if row.volume_id == volume_id
        )

        assert status_text(summary.health_status) == "completed_with_errors"
        assert status_text(scan.health_status) == "completed_with_errors"
        assert scan.latest_attempt_errors == 1
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


def test_generic_name_and_size_is_a_lead_with_counts_not_a_false_backup(tmp_path):
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

        assert summary.possible_files == 3
        assert summary.ambiguous_files == 0
        for file_id, volume_id in zip(file_ids, volume_ids):
            status = engine.file_status(file_id)
            assert status.status == "possible"
            assert set(status.other_volume_ids) == set(volume_ids) - {volume_id}
            assert status.other_drive_count == 2
            assert status.strong_volume_ids == ()
            assert file_reason_key(status.evidence_text) == "generic"
            assert evidence_counts(status.evidence_text) == (3, 3)
            assert {match.status for match in engine.file_matches(file_id)} == {"possible"}
        summaries = {row.volume_id: row for row in engine.volume_summaries()}
        assert all(summaries[volume_id].possible_files == 1 for volume_id in volume_ids)
        # Generic groups are never paired, so they add nothing to drive coverage.
        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_generic_group_with_shared_time_is_likely_and_counts_survive_the_id_cap(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        drives = MAX_STORED_OTHER_VOLUME_IDS + 2
        file_ids = []
        for index in range(drives):
            _, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={f"Folder-{index}/common.bin": (4096, MODIFIED_AT)},
            )
            file_ids.append(files[f"Folder-{index}/common.bin"])

        # With the default strong-subgroup bounds this many drives sharing one
        # modified time are too common to call strong: the lead stays possible.
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(file_ids[0])
        assert status.status == "possible"
        assert status.strong_volume_ids == ()
        assert file_reason_key(status.evidence_text) == "generic"
        assert evidence_counts(status.evidence_text) == (drives, drives)
        assert len(status.other_volume_ids) == MAX_STORED_OTHER_VOLUME_IDS
        assert status.other_drive_count == drives - 1

        # Within the bounds the shared time makes every copy strong; stored ids
        # stay capped while the true drive count travels in the evidence.
        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(
                max_strong_volumes_per_signature=drives,
                max_strong_edges_per_signature=drives * drives,
            ),
        )
        assert engine.analyse().status == "completed"
        status = engine.file_status(file_ids[0])
        assert status.status == "likely"
        assert file_reason_key(status.evidence_text) == "generic"
        assert evidence_counts(status.evidence_text) == (drives, drives)
        assert len(status.other_volume_ids) == MAX_STORED_OTHER_VOLUME_IDS
        assert len(status.strong_volume_ids) == MAX_STORED_OTHER_VOLUME_IDS
        assert status.other_drive_count == drives - 1
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


def test_renamed_complete_subtree_is_a_full_match_flagged_renamed(tmp_path):
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

        assert status.status == "likely"
        assert folder_reason_key(status.evidence_text) == "full_match_legacy"
        assert folder_evidence_flags(status.evidence_text) == {"renamed"}
        assert status.best_coverage_files_percent == 100.0
        assert status.best_target_volume_id == target_id
        assert status.other_volume_ids == (target_id,)
        assert status.strong_volume_ids == (target_id,)
        assert len(matches) == 1
        assert matches[0].status == "likely"
        assert matches[0].matched_files == matches[0].total_files == 2
        assert matches[0].evidence_text == (
            "Whole folder matches here under a different folder name (contents not compared)."
        )
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


def test_repeated_records_get_identical_labels_and_drive_pairing_counts_once(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        source_id, source_files, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/First/same.bin": (100, MODIFIED_AT),
                "Project/Second/same.bin": (100, MODIFIED_AT),
            },
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Target",
            drive_id="AID-002",
            files={"Elsewhere/same.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        first = engine.file_status(source_files["Project/First/same.bin"])
        second = engine.file_status(source_files["Project/Second/same.bin"])
        project = engine.folder_status(source_folders["Project"])

        # Both copies exist on the target by name, size and time: same label.
        assert first.status == second.status == "likely"
        assert first.strong_volume_ids == second.strong_volume_ids == (target_id,)
        assert first.evidence_text == second.evidence_text
        assert FILE_EVIDENCE_PAIRED_SUFFIX not in first.evidence_text
        # The folder follows what exists elsewhere: every file is on the target.
        assert project.status == "likely"
        assert folder_reason_key(project.evidence_text) == "all_files_found"
        assert project.best_target_volume_id == target_id
        assert project.matched_files == project.total_files == 2
        assert project.best_coverage_files_percent == 100.0
        assert project.scattered is False
        # Only the drive-pair overlap counts the lone target record once.
        candidate = next(
            row
            for row in engine.mirror_candidates()
            if {row.source_volume_id, row.target_volume_id} == {source_id, target_id}
        )
        assert candidate.matched_files == 1
        assert candidate.complete_structure is False
    finally:
        db.close()


def test_two_copies_on_one_drive_and_six_single_copies_elsewhere_match_identically(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="AID-140",
            drive_id="AID-140",
            files={
                "Project/Renders/clip.mov": (5_000, MODIFIED_AT),
                "Project/Delivery/clip.mov": (5_000, MODIFIED_AT),
            },
        )
        other_ids = []
        for index in range(6):
            volume_id, _, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Backup {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={f"Clips-{index}/clip.mov": (5_000, MODIFIED_AT)},
            )
            other_ids.append(volume_id)

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        renders = engine.file_status(source_files["Project/Renders/clip.mov"])
        delivery = engine.file_status(source_files["Project/Delivery/clip.mov"])

        assert renders.status == delivery.status == "likely"
        assert renders.strong_volume_ids == delivery.strong_volume_ids == tuple(sorted(other_ids))
        assert renders.possible_volume_ids == delivery.possible_volume_ids == ()
        assert renders.other_drive_count == delivery.other_drive_count == 6
        assert renders.evidence_text == delivery.evidence_text
        assert file_reason_key(renders.evidence_text) == "time_match"

        # Folder verdicts follow existence too: every file in Renders, Delivery
        # and Project is on each of the six drives, however pairing went.
        for path in ("Project/Renders", "Project/Delivery", "Project"):
            folder = engine.folder_status(source_folders[path])
            assert folder.status == "likely", path
            assert folder_reason_key(folder.evidence_text) == "all_files_found", path
            assert folder.matched_files == folder.total_files
            assert folder.best_coverage_files_percent == 100.0
            assert set(folder.strong_volume_ids) == set(other_ids)
            assert folder.possible_volume_ids == ()
    finally:
        db.close()


def test_duplicates_on_one_drive_do_not_hide_copies_on_other_drives(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        source_id, source_files, _ = add_catalogued_volume(
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

        for file_id in source_files.values():
            duplicated = engine.file_status(file_id)
            assert duplicated.status == "likely"
            assert duplicated.strong_volume_ids == (second_id, third_id)
        second = engine.file_status(second_files["B/same.bin"])
        third = engine.file_status(third_files["C/same.bin"])
        assert second.status == third.status == "likely"
        assert second.strong_volume_ids == (source_id, third_id)
        assert third.strong_volume_ids == (source_id, second_id)
        folder = engine.folder_status(second_folders["B"])
        assert folder.status == "likely"
        assert folder_reason_key(folder.evidence_text) == "all_files_found"
    finally:
        db.close()


def test_unequal_casefolded_duplicates_pair_once_but_all_exist_elsewhere(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        source_id, source_files, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Case-sensitive source",
            drive_id="AID-001",
            files={
                "Project/Same.bin": (100, MODIFIED_AT),
                "Project/same.bin": (100, MODIFIED_AT),
            },
        )
        target_id, target_files, _ = add_catalogued_volume(
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
            engine.file_status(file_id).status == "likely"
            for file_id in all_file_ids
        )
        project = engine.folder_status(source_folders["Project"])
        assert project.status == "likely"
        assert folder_reason_key(project.evidence_text) == "all_files_found"
        assert project.best_target_volume_id == target_id
        assert project.matched_files == project.total_files == 2
        assert project.best_coverage_files_percent == 100.0
        # Drive-pair overlap still pairs the single target record once.
        candidates = engine.mirror_candidates()
        assert all(not row.complete_structure for row in candidates)
        candidate = next(
            row
            for row in candidates
            if {row.source_volume_id, row.target_volume_id} == {source_id, target_id}
        )
        assert candidate.matched_files == 1
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


def test_overly_common_folder_structure_is_still_a_lead_from_its_files(tmp_path):
    """A repeated structure never hides that every file exists elsewhere.

    Nine drives exceed the folder-fingerprint bound, so no structure match is
    paired; the files carry no modified time, so each is only a name-and-size
    lead on the other eight drives. The folder follows its files (amber lead),
    never "No copy found". Only the best weak-only drive is persisted as a
    drive-match row; the full drive set stays in other_volume_ids.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        folder_ids = []
        volume_ids = []
        for index in range(9):
            volume_id, _, folders = add_catalogued_volume(
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
            volume_ids.append(volume_id)

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        full_match_keys = {"full_match_hashed", "full_match_partly_hashed", "full_match_legacy"}
        for folder_id, volume_id in zip(folder_ids, volume_ids):
            status = engine.folder_status(folder_id)
            assert status.status == "possible"
            assert folder_reason_key(status.evidence_text) == "all_files_weak"
            assert folder_reason_key(status.evidence_text) not in full_match_keys
            assert set(status.other_volume_ids) == set(volume_ids) - {volume_id}
            assert status.other_drive_count == 8
            assert status.strong_volume_ids == ()
            assert status.best_target_volume_id in set(volume_ids) - {volume_id}
            assert status.possible_volume_ids == (status.best_target_volume_id,)
            assert status.matched_files == 0
            assert status.total_files == 2
            matches = engine.folder_matches(folder_id)
            assert len(matches) == 1
            assert matches[0].status == "possible"
            assert matches[0].target_volume_id == status.best_target_volume_id
        # Name-and-size-only pairs never count towards drive-pair overlap.
        assert engine.mirror_candidates() == []
    finally:
        db.close()


def test_repeated_folder_structure_is_found_everywhere_but_drive_pairing_counts_once(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        source_id, _, source_folders = add_catalogued_volume(
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
        target_id, _, target_folders = add_catalogued_volume(
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
        x = engine.folder_status(source_folders["X/Project"])
        y = engine.folder_status(source_folders["Y/Project"])
        z = engine.folder_status(target_folders["Z/Project"])

        # Two same-name folders cannot both claim the one target folder as a
        # structure match, so none of the three is a full match...
        full_match_keys = {"full_match_hashed", "full_match_partly_hashed", "full_match_legacy"}
        assert all(
            folder_reason_key(status.evidence_text) not in full_match_keys
            for status in (x, y, z)
        )
        # ...but every file of each folder exists on the other drive, so all
        # three get the same verdict regardless of how pairing went.
        for status in (x, y, z):
            assert status.status == "likely"
            assert folder_reason_key(status.evidence_text) == "all_files_found"
            assert status.matched_files == status.total_files == 2
        assert x.best_target_volume_id == y.best_target_volume_id == target_id
        assert z.best_target_volume_id == source_id
        # The drive-pair overlap pairs the target's two files once each.
        candidate = next(
            row
            for row in engine.mirror_candidates()
            if {row.source_volume_id, row.target_volume_id} == {source_id, target_id}
        )
        assert candidate.matched_files == 2
        assert candidate.complete_structure is False
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
        # The folder itself still matches; the scan problem stays an explicit flag.
        folder = engine.folder_status(first_folders["Project"])
        assert folder.status == "likely"
        assert folder_reason_key(folder.evidence_text) == "full_match_legacy"
        assert folder_evidence_flags(folder.evidence_text) == {"scan_errors"}
        assert folder.strong_volume_ids == (second_id,)
    finally:
        db.close()


def test_modified_time_tolerance_is_two_seconds_or_an_exact_hour(tmp_path):
    assert modified_times_match(MODIFIED_AT, MODIFIED_AT)
    assert modified_times_match(MODIFIED_AT, MODIFIED_PLUS_2S)
    assert not modified_times_match(MODIFIED_AT, MODIFIED_PLUS_3S)
    assert modified_times_match(MODIFIED_AT, MODIFIED_PLUS_1H)
    assert not modified_times_match(MODIFIED_AT, None)
    # Two unusable values never "match" each other just by being equal text.
    assert not modified_times_match("", "")
    assert not modified_times_match("garbage", "garbage")
    assert not modified_times_match(None, None)

    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
        )
        within_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Two seconds later",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_PLUS_2S)},
        )
        beyond_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Three seconds later",
            drive_id="AID-003",
            files={"Project/report.psd": (100, MODIFIED_PLUS_3S)},
        )
        dst_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="FAT clock offset",
            drive_id="AID-004",
            files={"Project/report.psd": (100, MODIFIED_PLUS_1H)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        status = engine.file_status(source_files["Project/report.psd"])

        assert status.status == "likely"
        assert status.strong_volume_ids == (within_id, dst_id)
        assert status.possible_volume_ids == (beyond_id,)
        assert status.other_volume_ids == (within_id, beyond_id, dst_id)
        assert file_reason_key(status.evidence_text) == "time_match"
    finally:
        db.close()


def test_hash_conflict_is_ambiguous_until_another_drive_verifies_the_content(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, source_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"Project/report.psd": (100, MODIFIED_AT)},
            content_hashes={"Project/report.psd": HASH_A},
        )
        _, other_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Different content",
            drive_id="AID-002",
            files={"Project/report.psd": (100, MODIFIED_AT)},
            content_hashes={"Project/report.psd": HASH_B},
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        status = engine.file_status(source_files["Project/report.psd"])
        assert status.status == "ambiguous"
        assert file_reason_key(status.evidence_text) == "hash_conflict"
        assert status.other_volume_ids == ()
        assert status.strong_volume_ids == status.possible_volume_ids == ()
        assert engine.file_matches(source_files["Project/report.psd"]) == []
        assert engine.analyse().ambiguous_files == 2

        verified_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Same content",
            drive_id="AID-003",
            files={"Project/report.psd": (100, LATER_SCAN)},
            content_hashes={"Project/report.psd": HASH_A},
        )
        assert engine.analyse().status == "completed"

        status = engine.file_status(source_files["Project/report.psd"])
        assert status.status == "likely"
        assert file_reason_key(status.evidence_text) == "hash_verified"
        assert "conflict" in file_evidence_flags(status.evidence_text)
        assert status.verified_volume_ids == (verified_id,)
        assert status.strong_volume_ids == (verified_id,)
        # The conflicting drive is a warning, never a lead.
        assert status.possible_volume_ids == ()
        assert status.other_volume_ids == (verified_id,)
        # The odd one out still conflicts with everything else.
        odd = engine.file_status(other_files["Project/report.psd"])
        assert odd.status == "ambiguous"
        assert file_reason_key(odd.evidence_text) == "hash_conflict"
    finally:
        db.close()


def test_folder_found_only_by_name_and_size_is_a_weak_lead(tmp_path):
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
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Retimed and scattered",
            drive_id="AID-002",
            files={
                "Loose/one.bin": (100, LATER_SCAN),
                "Other/two.bin": (200, LATER_SCAN),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        folder = engine.folder_status(source_folders["Project"])
        matches = engine.folder_matches(source_folders["Project"])

        assert folder.status == "possible"
        assert folder_reason_key(folder.evidence_text) == "all_files_weak"
        assert folder.best_target_volume_id == target_id
        assert folder.strong_volume_ids == ()
        assert folder.possible_volume_ids == (target_id,)
        # Weak matches are leads, not coverage.
        assert folder.matched_files == 0
        assert folder.total_files == 2
        assert folder.best_coverage_files_percent == 0.0
        assert len(matches) == 1
        assert matches[0].status == "possible"
        assert "name and size only" in matches[0].evidence_text
    finally:
        db.close()


def test_generic_files_make_their_folder_and_its_parent_a_lead_not_missing(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        folder_ids: list[tuple[int, int]] = []
        file_ids = []
        volume_ids = []
        for index in range(10):
            volume_id, files, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Camera card {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    "Media/Shoot/IMG_0001.JPG": (4096, f"2026-08-{index + 1:02d}T09:00:00.000000+0000"),
                    "Media/Shoot/IMG_0002.JPG": (4096, f"2026-08-{index + 1:02d}T09:05:00.000000+0000"),
                },
            )
            volume_ids.append(volume_id)
            folder_ids.append((folders["Media/Shoot"], folders["Media"]))
            file_ids.extend(files.values())

        # Ten drives exceed this generic bound: every file is an amber lead.
        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=8),
        )
        assert engine.analyse().status == "completed"

        for file_id in file_ids:
            status = engine.file_status(file_id)
            assert status.status == "possible"
            assert file_reason_key(status.evidence_text) == "generic"
            assert status.strong_volume_ids == ()
            assert status.other_drive_count == 9
        for (shoot_id, media_id), volume_id in zip(folder_ids, volume_ids):
            for folder_id in (shoot_id, media_id):
                folder = engine.folder_status(folder_id)
                assert folder.status == "possible"
                assert folder_reason_key(folder.evidence_text) == "all_files_weak"
                assert folder.strong_volume_ids == ()
                # Every other drive is a lead; only the best one is persisted
                # as a drive-match row because none holds strong evidence.
                assert set(folder.other_volume_ids) == set(volume_ids) - {volume_id}
                assert folder.best_target_volume_id in set(volume_ids) - {volume_id}
                assert folder.possible_volume_ids == (folder.best_target_volume_id,)
                assert folder.matched_files == 0
                assert folder.total_files == 2
                assert folder.best_coverage_files_percent == 0.0
                assert {match.status for match in engine.folder_matches(folder_id)} == {"possible"}
    finally:
        db.close()


def test_coverage_verdicts_keep_scan_errors_explicit_on_either_side(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, errored_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Errored source",
            drive_id="AID-001",
            files={"Project/one.bin": (100, MODIFIED_AT)},
            scan_errors=1,
        )
        clean_id, _, clean_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Clean target",
            drive_id="AID-002",
            files={"Elsewhere/one.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        # The folder's own scan had errors.
        errored = engine.folder_status(errored_folders["Project"])
        assert errored.status == "likely"
        assert folder_reason_key(errored.evidence_text) == "all_files_found"
        assert folder_evidence_flags(errored.evidence_text) == {"scan_errors"}
        assert errored.best_target_volume_id == clean_id
        # The best target drive's scan had errors.
        clean = engine.folder_status(clean_folders["Elsewhere"])
        assert clean.status == "likely"
        assert folder_reason_key(clean.evidence_text) == "all_files_found"
        assert folder_evidence_flags(clean.evidence_text) == {"scan_errors"}
    finally:
        db.close()


def test_full_match_with_a_never_scanned_target_is_not_flagged_as_scan_errors(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        files = {
            "Project/one.bin": (100, MODIFIED_AT),
            "Project/two.bin": (200, MODIFIED_AT),
        }
        _, _, source_folders = add_catalogued_volume(
            db, tmp_path, name="Source", drive_id="AID-001", files=files
        )
        target_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Never scanned",
            drive_id="AID-002",
            files=files,
            scan_status=None,
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        folder = engine.folder_status(source_folders["Project"])

        assert folder.status == "likely"
        assert folder_reason_key(folder.evidence_text) == "full_match_legacy"
        # "reported access errors" is only true of a completed scan with errors.
        assert folder_evidence_flags(folder.evidence_text) == frozenset()
        assert folder.strong_volume_ids == (target_id,)
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


def test_generic_keys_shared_by_many_drives_still_feed_mirror_candidates(tmp_path):
    """Nine drives sharing three same-time files: every drive pair is a 100% mirror candidate.

    The keys take the generic path (bound lowered to 8 drives), which never
    pairs for coverage, but the bounded same-time bucket still feeds the
    drive-pair overlap. The root structure repeats on too many drives to be
    called a complete copy.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        file_ids = []
        for index in range(9):
            _, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    "P/a.mov": (10, MODIFIED_AT),
                    "P/b.mov": (20, MODIFIED_AT),
                    "c.mov": (30, MODIFIED_AT),
                },
            )
            file_ids.extend(files.values())

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=8),
        )
        assert engine.analyse().status == "completed"

        candidates = engine.mirror_candidates()
        assert len(candidates) == 36
        for candidate in candidates:
            assert candidate.source_coverage_percent == 100.0
            assert candidate.target_coverage_percent == 100.0
            assert candidate.matched_files == 3
            assert candidate.complete_structure is False
            assert "modified time" in candidate.evidence_text
        for file_id in file_ids:
            status = engine.file_status(file_id)
            assert status.status == "likely"
            assert file_reason_key(status.evidence_text) == "generic"
            assert len(status.strong_volume_ids) == 8
    finally:
        db.close()


def test_generic_group_strong_tier_tolerates_two_second_fat_rounding(tmp_path):
    """Half the drives saved T, half T+2 s: one bucket, every copy strong on 11 drives.

    A thirteenth drive saved T+1 h. The one-hour FAT/DST rule is deliberately
    not applied in generic groups, so that copy stays a name-and-size lead.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        file_ids = []
        for index in range(12):
            _, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    "Clips/clip.mov": (
                        5_000,
                        MODIFIED_AT if index % 2 == 0 else MODIFIED_PLUS_2S,
                    )
                },
            )
            file_ids.extend(files.values())
        hour_id, hour_files, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Clock offset",
            drive_id="AID-013",
            files={"Clips/clip.mov": (5_000, MODIFIED_PLUS_1H)},
        )

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=8),
        )
        assert engine.analyse().status == "completed"

        for file_id in file_ids:
            status = engine.file_status(file_id)
            assert status.status == "likely"
            assert file_reason_key(status.evidence_text) == "generic"
            assert len(status.strong_volume_ids) == 11
            assert status.possible_volume_ids == (hour_id,)
            assert status.other_drive_count == 12
        offset = engine.file_status(hour_files["Clips/clip.mov"])
        assert offset.status == "possible"
        assert offset.strong_volume_ids == ()
        assert len(offset.possible_volume_ids) == 12
        # The same-time bucket also feeds the drive-pair overlap: 12 choose 2;
        # the clock-offset drive shares no strong pair with anyone.
        candidates = engine.mirror_candidates()
        assert len(candidates) == 66
        assert all(
            hour_id not in (row.source_volume_id, row.target_volume_id)
            for row in candidates
        )
    finally:
        db.close()


def test_capped_generic_ids_keep_strong_drives_before_leads(tmp_path):
    """When a generic group's drive list is truncated, strong drives survive the cut."""
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        drives = MAX_STORED_OTHER_VOLUME_IDS + 2
        strong_drives = 40
        strong_ids = []
        file_ids = []
        for index in range(drives):
            volume_id, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    f"Folder-{index}/common.bin": (
                        4096,
                        MODIFIED_AT
                        if index < strong_drives
                        else f"2027-01-{index - strong_drives + 1:02d}T09:00:00.000000+0000",
                    )
                },
            )
            file_ids.append(files[f"Folder-{index}/common.bin"])
            if index < strong_drives:
                strong_ids.append(volume_id)

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(
                max_strong_volumes_per_signature=drives,
                max_strong_edges_per_signature=drives * drives,
            ),
        )
        assert engine.analyse().status == "completed"

        status = engine.file_status(file_ids[0])
        assert status.status == "likely"
        assert file_reason_key(status.evidence_text) == "generic"
        assert set(status.strong_volume_ids) == set(strong_ids) - {strong_ids[0]}
        assert len(status.other_volume_ids) == MAX_STORED_OTHER_VOLUME_IDS
        assert set(status.strong_volume_ids) <= set(status.other_volume_ids)
        assert status.other_drive_count == drives - 1
    finally:
        db.close()


def test_generic_folder_coverage_is_scattered_when_strong_files_sit_on_different_drives(
    tmp_path,
):
    """Key-level existence rows still add up per folder and per drive.

    The source folder holds a.bin and b.bin. Eight other drives hold only a.bin
    (one of them with the same time) and eight hold only b.bin (one with the
    same time), so each key is a generic group. The folder is partial on its
    best drive, scattered across two, and persists only the two drives holding
    strong evidence while every lead stays in other_volume_ids.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        source_id, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={"P/a.bin": (100, MODIFIED_AT), "P/b.bin": (200, MODIFIED_AT)},
        )
        other_ids = []
        for index in range(16):
            name = "a.bin" if index < 8 else "b.bin"
            size = 100 if index < 8 else 200
            same_time = index in {0, 8}
            volume_id, _, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 2:03d}",
                files={
                    f"Elsewhere/{name}": (
                        size,
                        MODIFIED_AT
                        if same_time
                        else f"2027-02-{index + 1:02d}T09:00:00.000000+0000",
                    )
                },
            )
            other_ids.append(volume_id)
        a_strong_id, b_strong_id = other_ids[0], other_ids[8]

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=8),
        )
        assert engine.analyse().status == "completed"

        folder = engine.folder_status(source_folders["P"])
        assert folder.status == "possible"
        assert folder_reason_key(folder.evidence_text) == "partial"
        assert folder.scattered is True
        assert folder.matched_files == 1
        assert folder.total_files == 2
        assert folder.best_coverage_files_percent == 50.0
        # Both strong drives hold one file; the tie goes to the larger bytes.
        assert folder.best_target_volume_id == b_strong_id
        assert folder.best_coverage_bytes_percent == pytest.approx(200 / 3)
        assert folder.other_drive_count == 16
        assert set(folder.other_volume_ids) == set(other_ids)
        matches = engine.folder_matches(source_folders["P"])
        assert {match.target_volume_id for match in matches} == {a_strong_id, b_strong_id}
        assert all(match.status == "possible" and match.matched_files == 1 for match in matches)
        # Drives holding the files by name and size alone are leads in
        # other_volume_ids but are not persisted as drive-match rows.
        assert set(folder.possible_volume_ids) == {a_strong_id, b_strong_id}
        assert source_id not in folder.other_volume_ids
    finally:
        db.close()


def test_generic_group_persists_few_drive_match_rows_per_folder(tmp_path):
    """60 drives x 200 records of one generic key: verdicts unchanged, fan-out gone.

    Drives are catalogued in pairs that share one modified time, so every
    record is strong on exactly one other drive and a lead on the other 58.
    Each folder persists only the drive holding strong evidence rather than
    a row for every drive the lead points at.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        file_ids = []
        folder_ids = []
        for drive in range(60):
            modified_at = (base + timedelta(days=drive // 2)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f%z"
            )
            _, files, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Card {drive}",
                drive_id=f"AID-{drive + 1:03d}",
                files={
                    f"Set-{index:03d}/common.bin": (4096, modified_at)
                    for index in range(200)
                },
            )
            file_ids.extend(files.values())
            folder_ids.extend(folders.values())

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        statuses = engine.item_statuses("file", file_ids)
        assert len(statuses) == len(file_ids) == 12_000
        for status in statuses.values():
            assert status.status == "likely"
            assert file_reason_key(status.evidence_text) == "generic"
            assert len(status.strong_volume_ids) == 1
            assert status.other_drive_count == 59
        folder_statuses = engine.item_statuses("folder", folder_ids)
        assert len(folder_statuses) == len(folder_ids)
        for status in folder_statuses.values():
            assert status.status == "likely"
            assert folder_reason_key(status.evidence_text) == "all_files_found"
            assert status.strong_volume_ids == (status.best_target_volume_id,)
            assert status.other_drive_count == 59

        run_id = engine.state().active_run_id
        rows_per_folder = db.connection.execute(
            """
            SELECT MAX(rows_for_folder)
            FROM (
                SELECT COUNT(*) AS rows_for_folder
                FROM backup_folder_drive_matches
                WHERE run_id = ?
                GROUP BY folder_id
            )
            """,
            (run_id,),
        ).fetchone()[0]
        assert rows_per_folder <= 2
    finally:
        db.close()


def test_drive_pair_overlap_counts_strong_pairs_only(tmp_path):
    """Name-and-size-only agreement never makes two drives look like mirrors."""
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        a_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="A",
            drive_id="AID-001",
            files={
                "Alpha/report.psd": (100, MODIFIED_AT),
                "Alpha/notes.txt": (50, MODIFIED_AT),
                "Alpha/extra.bin": (10, MODIFIED_AT),
            },
        )
        b_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="B",
            drive_id="AID-002",
            files={
                "Beta/report.psd": (100, LATER_SCAN),
                "Beta/notes.txt": (50, LATER_SCAN),
                "Beta/extra.bin": (10, MODIFIED_AT),
            },
        )
        c_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="C",
            drive_id="AID-003",
            files={
                "Gamma/report.psd": (100, MODIFIED_AT),
                "Gamma/notes.txt": (50, MODIFIED_AT),
                "Gamma/extra.bin": (10, LATER_SCAN),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        candidates = engine.mirror_candidates()

        # A-B share one file by time (33%), B-C none, A-C two (67%).
        assert [(row.source_volume_id, row.target_volume_id) for row in candidates] == [
            (a_id, c_id)
        ]
        candidate = candidates[0]
        assert candidate.matched_files == 2
        assert candidate.source_coverage_percent == pytest.approx(200 / 3)
        assert candidate.complete_structure is False
        assert "name, size and modified time" in candidate.evidence_text
        assert "filename-and-size" not in candidate.evidence_text
        assert b_id not in {
            volume
            for row in candidates
            for volume in (row.source_volume_id, row.target_volume_id)
        }
    finally:
        db.close()


def test_run_saved_under_older_rules_reads_as_outdated(tmp_path):
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
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"
        assert engine.state().is_stale is False

        # Simulate evidence persisted by the previous rules version.
        with db.transaction():
            db.connection.execute("UPDATE backup_analysis_runs SET rules_version = 3")

        state = engine.state()
        assert state.is_stale is True
        assert state.status == "outdated"
        assert state.rules_version == 3
        assert "rules" in state.stale_reason.casefold()
        status = engine.file_status(files["Project/report.psd"])
        assert status.is_stale is True
        assert status.stale_reason == state.stale_reason
    finally:
        db.close()


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


class _RecordingDict(dict):
    """A dict that remembers the most entries it ever held."""

    peak = 0

    def _note(self) -> None:
        type(self).peak = max(type(self).peak, len(self))

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._note()

    def setdefault(self, key, default=None):
        result = super().setdefault(key, default)
        self._note()
        return result


def test_pending_folder_coverage_holds_only_parents_awaiting_children(monkeypatch, tmp_path):
    """60 drives x 200 records of one generic key: coverage is streamed, not materialised.

    The coverage cursor is read in lockstep with the deepest-first folder
    loop, so the in-memory map never holds more than the folders whose
    children are still being judged (here the 60 roots), never every folder.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        folder_ids = []
        for drive in range(60):
            modified_at = (base + timedelta(days=drive // 2)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f%z"
            )
            _, _, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Card {drive}",
                drive_id=f"AID-{drive + 1:03d}",
                files={
                    f"Set-{index:03d}/common.bin": (4096, modified_at)
                    for index in range(200)
                },
            )
            folder_ids.extend(folders.values())
        non_leaf_folders = int(
            db.connection.execute(
                "SELECT COUNT(DISTINCT parent_id) FROM folders WHERE parent_id IS NOT NULL"
            ).fetchone()[0]
        )
        assert non_leaf_folders == 60

        _RecordingDict.peak = 0
        monkeypatch.setattr(
            BackupAnalysisEngine, "_pending_coverage_map", staticmethod(_RecordingDict)
        )
        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        assert 0 < _RecordingDict.peak <= non_leaf_folders
        statuses = engine.item_statuses("folder", folder_ids)
        assert len(statuses) == len(folder_ids) == 60 * 201
        assert {status.status for status in statuses.values()} == {"likely"}
        assert {
            folder_reason_key(status.evidence_text) for status in statuses.values()
        } == {"all_files_found"}
    finally:
        db.close()


def test_generic_bucket_strong_tier_is_per_record_not_per_chain(tmp_path):
    """Records 2 s apart chain into one bucket, but only neighbours within 2 s are strong.

    Seven drives saved T, T+2, ..., T+10 and T+13. The first six chain into
    one bucket; the copy at T is strong only on the drive at T+2, never on
    the drive at T+10, and the copy at T+13 has no strong drive at all.
    """
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        base = datetime(2026, 8, 19, 9, 0, 0, tzinfo=timezone.utc)
        offsets = [0, 2, 4, 6, 8, 10, 13]
        volume_ids = []
        file_ids = []
        for index, offset in enumerate(offsets):
            volume_id, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={
                    "Clips/clip.mov": (
                        5_000,
                        (base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.%f%z"),
                    )
                },
            )
            volume_ids.append(volume_id)
            file_ids.append(files["Clips/clip.mov"])

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=2),
        )
        assert engine.analyse().status == "completed"

        first = engine.file_status(file_ids[0])
        assert first.status == "likely"
        assert file_reason_key(first.evidence_text) == "generic"
        assert first.strong_volume_ids == (volume_ids[1],)
        assert set(first.possible_volume_ids) == set(volume_ids[2:])
        middle = engine.file_status(file_ids[2])
        assert set(middle.strong_volume_ids) == {volume_ids[1], volume_ids[3]}
        last_chained = engine.file_status(file_ids[5])
        assert last_chained.strong_volume_ids == (volume_ids[4],)
        apart = engine.file_status(file_ids[6])
        assert apart.status == "possible"
        assert apart.strong_volume_ids == ()
        assert set(apart.possible_volume_ids) == set(volume_ids[:6])
        # Drive-pair overlap follows the same 2 s rule: only neighbours pair.
        pairs = {
            frozenset((row.source_volume_id, row.target_volume_id))
            for row in engine.mirror_candidates()
        }
        assert pairs == {
            frozenset((volume_ids[index], volume_ids[index + 1])) for index in range(5)
        }
    finally:
        db.close()


def test_two_drive_mirror_of_many_identical_generic_files_keeps_strong_evidence(tmp_path):
    """The strong-records bound applies per drive: 2 x 300 same-time copies stay strong."""
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_ids = []
        file_ids = []
        folder_ids = []
        for drive in ("A", "B"):
            volume_id, files, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Mirror {drive}",
                drive_id=f"AID-{drive}",
                files={
                    f"Clips/{drive}-take-{index:03d}/clip.mov": (5_000, MODIFIED_AT)
                    for index in range(300)
                },
            )
            volume_ids.append(volume_id)
            file_ids.extend(files.values())
            folder_ids.extend(folders.values())

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        statuses = engine.item_statuses("file", file_ids)
        assert len(statuses) == 600
        for status in statuses.values():
            assert status.status == "likely"
            assert file_reason_key(status.evidence_text) == "generic"
            twin = volume_ids[1] if status.other_volume_ids == (volume_ids[1],) else volume_ids[0]
            assert status.strong_volume_ids == (twin,)
            assert status.possible_volume_ids == ()
        folder_statuses = engine.item_statuses("folder", folder_ids)
        assert len(folder_statuses) == len(folder_ids)
        for status in folder_statuses.values():
            assert status.status == "likely"
            assert folder_reason_key(status.evidence_text) == "all_files_found"
            assert status.matched_files == status.total_files > 0
    finally:
        db.close()


def test_same_time_generic_copies_on_too_many_drives_stay_a_lead(tmp_path):
    """33 drives sharing one modified time exceed the drive bound: possible, not strong."""
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        file_ids = []
        for index in range(33):
            _, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={f"Folder-{index}/common.bin": (4096, MODIFIED_AT)},
            )
            file_ids.append(files[f"Folder-{index}/common.bin"])

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        for file_id in file_ids:
            status = engine.file_status(file_id)
            assert status.status == "possible"
            assert file_reason_key(status.evidence_text) == "generic"
            assert status.strong_volume_ids == ()
            assert status.other_drive_count == 32
    finally:
        db.close()


def test_over_bound_hash_group_beyond_the_id_cap_never_counts_a_file_twice(monkeypatch, tmp_path):
    """Drives beyond the capped verified list are covered by hash rows already.

    Four drives hold the same content; the hash pass is over its bounds, so
    existence is written per (hash, drive) for every drive while the stored
    verified ids are capped at two. The last drive holds the content under
    another name (hashed) and the same-name file without a saved hash, so the
    metadata pass sees it only as a time match. It must not get a second,
    per-record coverage row: the folder also holds a file found nowhere, so
    it is partial, not complete.
    """
    monkeypatch.setattr(backup_analysis, "MAX_STORED_OTHER_VOLUME_IDS", 2)
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        volume_ids = []
        first_folders = None
        first_files = None
        for index in range(4):
            files = {f"Project-{index}/copy.bin": (100, MODIFIED_AT)}
            hashes = {f"Project-{index}/copy.bin": HASH_A}
            if index == 0:
                files["Project-0/unique.bin"] = (250, MODIFIED_AT)
            if index == 3:
                files[f"Project-{index}/renamed.bin"] = (100, MODIFIED_AT)
                hashes = {f"Project-{index}/renamed.bin": HASH_A}
            volume_id, file_ids, folders = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files=files,
                content_hashes=hashes,
            )
            volume_ids.append(volume_id)
            if index == 0:
                first_folders, first_files = folders, file_ids

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_hash_volumes_per_signature=2),
        )
        assert engine.analyse().status == "completed"

        copy = engine.file_status(first_files["Project-0/copy.bin"])
        assert copy.status == "likely"
        assert len(copy.verified_volume_ids) == 2
        assert copy.other_drive_count == 3
        assert "repeated_content" in file_evidence_flags(copy.evidence_text)
        folder = engine.folder_status(first_folders["Project-0"])
        assert folder.status == "possible"
        assert folder_reason_key(folder.evidence_text) == "partial"
        assert folder.matched_files == 1
        assert folder.total_files == 2
        assert folder.best_coverage_files_percent == 50.0
        matches = engine.folder_matches(first_folders["Project-0"])
        assert {match.target_volume_id for match in matches} == set(volume_ids[1:])
        assert all(match.matched_files == 1 and match.total_files == 2 for match in matches)
    finally:
        db.close()


def test_saved_modified_times_are_canonicalised_to_the_scanner_format():
    assert _canonical_modified_time(None) is None
    assert _canonical_modified_time(MODIFIED_AT) == MODIFIED_AT
    assert _canonical_modified_time("2026-08-19T09:00:00+00:00") == MODIFIED_AT
    assert _canonical_modified_time("2026-08-19T11:00:00+02:00") == MODIFIED_AT
    assert _canonical_modified_time("2026-08-19T09:00:00Z") == MODIFIED_AT
    assert _canonical_modified_time("2026-08-19T09:00:00") == MODIFIED_AT
    assert _canonical_modified_time("not a time") == "not a time"
    assert _canonical_modified_time("") == ""


def test_generic_bucket_sweep_matches_equal_instants_saved_with_different_offsets(tmp_path):
    """09:00+00:00 and 11:00+02:00 are the same instant; 10:00+00:00 is not."""
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        saved = [
            "2026-08-19T09:00:00+00:00",
            "2026-08-19T10:00:00+00:00",
            "2026-08-19T11:00:00+02:00",
        ]
        volume_ids = []
        file_ids = []
        for index, modified_at in enumerate(saved):
            volume_id, files, _ = add_catalogued_volume(
                db,
                tmp_path,
                name=f"Archive {index}",
                drive_id=f"AID-{index + 1:03d}",
                files={"Clips/clip.mov": (5_000, modified_at)},
            )
            volume_ids.append(volume_id)
            file_ids.append(files["Clips/clip.mov"])

        engine = BackupAnalysisEngine(
            db,
            AnalysisOptions(max_candidate_volumes_per_key=2),
        )
        assert engine.analyse().status == "completed"

        utc = engine.file_status(file_ids[0])
        offset = engine.file_status(file_ids[2])
        hour_later = engine.file_status(file_ids[1])
        assert file_reason_key(utc.evidence_text) == "generic"
        assert utc.status == offset.status == "likely"
        assert utc.strong_volume_ids == (volume_ids[2],)
        assert offset.strong_volume_ids == (volume_ids[0],)
        assert hour_later.status == "possible"
        assert hour_later.strong_volume_ids == ()
    finally:
        db.close()


def test_full_match_folder_counts_only_whole_folder_drives_as_strong(tmp_path):
    """A drive holding every file loose is a lead, not a "Full-match drive"."""
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
        structure_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Structure",
            drive_id="AID-002",
            files={
                "Project/one.bin": (100, MODIFIED_AT),
                "Project/two.bin": (200, MODIFIED_AT),
            },
        )
        loose_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Loose",
            drive_id="AID-003",
            files={
                "Loose/one.bin": (100, MODIFIED_AT),
                "Other/two.bin": (200, MODIFIED_AT),
            },
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        folder = engine.folder_status(source_folders["Project"])
        assert folder.status == "likely"
        assert folder_reason_key(folder.evidence_text) == "full_match_legacy"
        assert folder.strong_volume_ids == (structure_id,)
        assert folder.possible_volume_ids == ()
        assert folder.other_volume_ids == (structure_id, loose_id)
        matches = {match.target_volume_id: match for match in engine.folder_matches(folder.item_id)}
        assert set(matches) == {structure_id, loose_id}
        assert matches[structure_id].status == matches[loose_id].status == "likely"
        assert matches[structure_id].evidence_text.startswith(FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX)
        assert not matches[loose_id].evidence_text.startswith(FOLDER_MATCH_EVIDENCE_WHOLE_PREFIX)
        assert matches[loose_id].evidence_text == "Every file found here by name, size and time."
    finally:
        db.close()


def test_partial_drive_row_text_reports_weak_matches_when_they_exceed_strong_ones(tmp_path):
    db = Database(tmp_path / "catalogue.jvvv")
    try:
        _, _, source_folders = add_catalogued_volume(
            db,
            tmp_path,
            name="Source",
            drive_id="AID-001",
            files={
                "Project/a.bin": (100, MODIFIED_AT),
                "Project/b.bin": (200, MODIFIED_AT),
                "Project/c.bin": (300, MODIFIED_AT),
            },
        )
        mixed_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Mixed",
            drive_id="AID-002",
            files={
                "Elsewhere/a.bin": (100, MODIFIED_AT),
                "Elsewhere/b.bin": (200, "2027-01-01T09:00:00.000000+0000"),
            },
        )
        strong_only_id, _, _ = add_catalogued_volume(
            db,
            tmp_path,
            name="Strong only",
            drive_id="AID-003",
            files={"Elsewhere/a.bin": (100, MODIFIED_AT)},
        )

        engine = BackupAnalysisEngine(db)
        assert engine.analyse().status == "completed"

        folder = engine.folder_status(source_folders["Project"])
        assert folder.status == "possible"
        assert folder_reason_key(folder.evidence_text) == "partial"
        matches = {match.target_volume_id: match for match in engine.folder_matches(folder.item_id)}
        assert matches[mixed_id].status == "possible"
        assert matches[mixed_id].matched_files == 1
        assert matches[mixed_id].evidence_text == (
            "Part of this folder is here (1 of 3 files by name, size and time; "
            "2 by name and size)."
        )
        assert matches[strong_only_id].evidence_text == (
            "Part of this folder is here (1 of 3 files by name, size and time)."
        )
    finally:
        db.close()
