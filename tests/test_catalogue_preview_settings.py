"""Catalogue-owned preview preferences and snapshot portability."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from jvvv.database import CatalogueError, Database, create_catalogue, open_catalogue
from jvvv.preview_config import (
    CATALOGUE_PREVIEW_SETTING_KEYS,
    PREVIEWS_FFMPEG_PATH_SETTING,
    PREVIEWS_ROOT_SETTING,
    PREVIEWS_VIDEO_CRF_SETTING,
    ImagePreviewProfile,
    PreviewSettings,
    VideoPreviewProfile,
    catalogue_preview_mapping,
)
from jvvv.snapshot_repository import InvalidRepositoryError, SnapshotManifest, SnapshotRepository


def configured(root):
    return PreviewSettings(
        enabled=True,
        root_directory=str(root),
        ffmpeg_path="local-ffmpeg.exe",
        image=ImagePreviewProfile(max_dimension=1920, jpeg_quality=87),
        video=VideoPreviewProfile(fps=0.333, max_height=720, crf=28, preset="fast"),
    )


def test_new_catalogues_have_independent_preferences_and_local_ffmpeg(tmp_path):
    first = create_catalogue(tmp_path / "First")
    second = create_catalogue(tmp_path / "Second")
    try:
        assert first.has_preview_settings() and second.has_preview_settings()
        assert second.preview_settings() == PreviewSettings()
        preferences = configured(tmp_path / "previews")
        first.set_preview_settings(preferences)
        assert first.preview_settings(ffmpeg_path="machine-b-ffmpeg") == replace(
            preferences, ffmpeg_path="machine-b-ffmpeg"
        )
        assert second.preview_settings() == PreviewSettings()
        assert PREVIEWS_FFMPEG_PATH_SETTING not in first.catalogue_meta()
        assert set(CATALOGUE_PREVIEW_SETTING_KEYS) <= first.catalogue_meta().keys()
        first.close()
        first = open_catalogue(tmp_path / "First.jvvv")
        assert first.preview_settings() == replace(preferences, ffmpeg_path="")
    finally:
        first.close()
        second.close()


def test_existing_catalogues_without_preferences_remain_detectable_on_open(tmp_path):
    db = create_catalogue(tmp_path / "Legacy")
    path = db.path
    db.close()
    with sqlite3.connect(path / "catalogue.sqlite") as registry:
        registry.executemany("DELETE FROM catalogue_meta WHERE key = ?", [(key,) for key in CATALOGUE_PREVIEW_SETTING_KEYS])
    db = open_catalogue(path)
    try:
        assert not db.has_preview_settings()
        assert db.preview_settings() == PreviewSettings()
        db.set_preview_settings(configured(tmp_path / "legacy-previews"))
        assert db.has_preview_settings()
    finally:
        db.close()


def test_configuration_write_is_atomic_and_read_only_catalogues_are_unchanged(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    path = db.path
    original = db.catalogue_meta()
    registry = db._registry()
    with registry:
        registry.execute(
            "CREATE TRIGGER reject_preview_update BEFORE UPDATE ON catalogue_meta "
            f"WHEN NEW.key = '{PREVIEWS_VIDEO_CRF_SETTING}' "
            "BEGIN SELECT RAISE(ABORT, 'preview write rejected'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="preview write rejected"):
            db.set_preview_settings(configured(tmp_path / "previews"))
        assert db.catalogue_meta() == original
    finally:
        db.close()
    db = Database(path, initialize=False, create=False, read_only=True)
    try:
        with pytest.raises(CatalogueError, match="read-only"):
            db.set_preview_settings(configured(tmp_path / "previews"))
        assert db.catalogue_meta() == original
    finally:
        db.close()


def test_relative_preview_association_follows_a_moved_catalogue_and_directory(tmp_path):
    original_parent = tmp_path / "Original"
    db = create_catalogue(original_parent / "Archive")
    preferences = configured(original_parent / "Previews")
    db.set_preview_settings(preferences)
    assert not Path(db.catalogue_meta()[PREVIEWS_ROOT_SETTING]).is_absolute()
    db.close()
    moved_parent = tmp_path / "Moved"
    original_parent.rename(moved_parent)
    db = open_catalogue(moved_parent / "Archive.jvvv")
    try:
        assert db.preview_settings().root_path == moved_parent / "Previews"
        assert db.preview_settings().video == preferences.video
    finally:
        db.close()


def test_snapshots_include_preview_preferences_and_detect_configuration_only_changes(tmp_path):
    db = create_catalogue(tmp_path / "Archive")
    path = db.path
    db.create_volume("Drive", str(tmp_path))
    repository = SnapshotRepository.create(tmp_path / "History.jvvvrepo")
    before = repository.create_snapshot(path).manifest
    assert not repository.has_changes(path)
    preferences = configured(path / "Previews")
    db.set_preview_settings(preferences)
    assert repository.has_changes(path)
    after = repository.create_snapshot(path)
    assert after.blobs_added == 0 and after.volumes_written == 0
    assert SnapshotRepository.delta(after.manifest, before).preview_settings_changed
    assert SnapshotRepository.delta(after.manifest, before).changed_count == 1
    assert not repository.has_changes(path)
    assert after.manifest.preview_settings[PREVIEWS_ROOT_SETTING] == "Previews"
    assert PREVIEWS_FFMPEG_PATH_SETTING not in after.manifest.preview_settings
    assert "local-ffmpeg.exe" not in json.dumps(after.manifest.to_json())
    db.close()

    restored = repository.restore_snapshot(after.manifest.snapshot_id, tmp_path / "Restored")
    db = open_catalogue(restored.catalogue_path)
    try:
        assert db.has_preview_settings()
        assert db.preview_settings(ffmpeg_path="other-ffmpeg") == replace(
            preferences, root_directory=str(restored.catalogue_path / "Previews"), ffmpeg_path="other-ffmpeg"
        )
        assert not repository.has_changes(restored.catalogue_path)
    finally:
        db.close()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_snapshots_restore_explicit_safe_preview_defaults(tmp_path, version):
    db = create_catalogue(tmp_path / "Archive")
    db.set_preview_settings(configured(tmp_path / "Previews"))
    repository = SnapshotRepository.create(tmp_path / "History.jvvvrepo")
    manifest = repository.create_snapshot(db.path).manifest
    db.close()
    data = manifest.to_json()
    data["format_version"] = version
    data["catalogue"].pop("preview_settings")
    repository.snapshot_path(manifest.snapshot_id).write_text(json.dumps(data), encoding="utf-8")
    restored = repository.restore_snapshot(manifest.snapshot_id, tmp_path / "Restored")
    db = open_catalogue(restored.catalogue_path)
    try:
        assert db.has_preview_settings()
        assert db.preview_settings() == PreviewSettings()
        assert not repository.has_changes(restored.catalogue_path)
    finally:
        db.close()


def test_snapshot_configuration_mapping_is_validated_and_ignores_ffmpeg(tmp_path):
    assert catalogue_preview_mapping({PREVIEWS_FFMPEG_PATH_SETTING: "private-path"}) == catalogue_preview_mapping()
    db = create_catalogue(tmp_path / "Archive")
    repository = SnapshotRepository.create(tmp_path / "History.jvvvrepo")
    manifest = repository.create_snapshot(db.path).manifest
    db.close()
    data = manifest.to_json()
    data["catalogue"]["preview_settings"] = "not a mapping"
    with pytest.raises(InvalidRepositoryError, match="key/value mapping"):
        SnapshotManifest.from_json(data)
