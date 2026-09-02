"""Scanner integration tests for offline previews (spec section 49, "Scanner tests").

These tests drive ``VolumeScanner`` end to end against a real SQLite
catalogue and a real preview root under ``tmp_path``.  Image happy paths use
the real Qt image generator; video happy paths use a fake generator that
writes the checked-in ``tiny_1fps_3s.mp4`` fixture through the cache's
temporary-file/publish protocol, so no FFmpeg is needed.  Failure paths use
injected fake generators, or the real ``VideoPreviewGenerator`` driven by a
fake ``subprocess.Popen`` that exits non-zero.  Two optional tests run the
real FFmpeg when ``JVVV_TEST_FFMPEG`` points at it.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from preview_fixtures import (  # noqa: E402
    real_ffmpeg_path,
    tiny_mp4_bytes,
    write_source_mp4,
    write_test_image,
    write_tiny_mp4,
)

from jvvv import video_preview as video_module  # noqa: E402
from jvvv.database import Database, count_rows  # noqa: E402
from jvvv.image_preview import ImagePreviewGenerator  # noqa: E402
from jvvv.media_metadata import (  # noqa: E402
    MediaMetadata,
    MediaMetadataExtractor,
    media_kind_for_extension,
)
from jvvv.preview_cache import (  # noqa: E402
    PREVIEW_GENERATED,
    STAGE_DISK_FULL,
    STAGE_FFMPEG_EXIT,
    STAGE_FFMPEG_START,
    STAGE_HASH_UNAVAILABLE,
    STAGE_IMAGE_DECODE,
    PreviewCache,
    PreviewCancelled,
    PreviewError,
    PreviewResult,
)
from jvvv.preview_config import PreviewSettings  # noqa: E402
from jvvv.preview_service import (  # noqa: E402
    MODE_DISABLED,
    MODE_ENABLED,
    MODE_SKIPPED_PREFLIGHT,
    SCAN_OUTCOME_COMPLETED_WITH_WARNINGS,
    STAGE_STORAGE_UNAVAILABLE,
    PreviewService,
    PreviewStatistics,
    skipped_preflight_statistics,
)
from jvvv.scanner import VolumeScanner  # noqa: E402
from jvvv.video_preview import validate_video_preview  # noqa: E402


FAKE_FFMPEG = "C:/Tools/ffmpeg/bin/ffmpeg.exe"
VIDEO_DURATION_MS = 3000


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class StubMediaExtractor:
    """Deterministic media details: videos report a 3 s duration, others nothing."""

    def __init__(self, video_duration_ms: int | None = VIDEO_DURATION_MS) -> None:
        self.video_duration_ms = video_duration_ms

    def inspect(self, path, *, extension=None, cancel_callback=None):
        suffix = extension if extension is not None else Path(path).suffix
        kind = media_kind_for_extension(suffix)
        if kind is None:
            return None
        if kind == "video":
            return MediaMetadata(
                status="complete",
                media_kind="video",
                source="stub",
                duration_ms=self.video_duration_ms,
                width=64,
                height=48,
                video_codecs=("h264",),
            )
        return MediaMetadata(
            status="unavailable",
            media_kind=kind,
            source="stub",
            message="Stubbed for the preview scanner tests.",
        )


class RecordingImageGenerator:
    """The real Qt image generator, recording every source it was asked for."""

    def __init__(self, cache: PreviewCache) -> None:
        self.inner = ImagePreviewGenerator(cache)
        self.calls: list[Path] = []

    def generate(self, source, destination, *, cancel_callback=None, source_stat=None):
        self.calls.append(Path(source))
        return self.inner.generate(
            source, destination, cancel_callback=cancel_callback, source_stat=source_stat
        )


class FailingImageGenerator:
    """Raises the configured ``PreviewError`` for every source."""

    def __init__(self, error: PreviewError) -> None:
        self.error = error
        self.calls: list[Path] = []

    def generate(self, source, destination, *, cancel_callback=None, source_stat=None):
        self.calls.append(Path(source))
        raise self.error


class CancellingImageGenerator:
    """Writes a temporary file, flips the scan's cancel flag, then honours it."""

    def __init__(self, cache: PreviewCache, cancel_state: dict[str, bool]) -> None:
        self.cache = cache
        self.cancel_state = cancel_state
        self.calls: list[Path] = []

    def generate(self, source, destination, *, cancel_callback=None, source_stat=None):
        self.calls.append(Path(source))
        self.cache.ensure_parent(destination)
        temp_path = self.cache.temporary_path(destination)
        temp_path.write_bytes(b"partial preview bytes")
        self.cancel_state["cancelled"] = True
        try:
            if cancel_callback is not None and cancel_callback():
                raise PreviewCancelled("Cancelled by the test generator.")
        finally:
            self.cache.discard_temporary(temp_path)
        raise AssertionError("the scanner's cancel callback did not report cancellation")


class FakeVideoGenerator:
    """Publishes the tiny MP4 fixture atomically and reports 50% progress once."""

    def __init__(self, cache: PreviewCache) -> None:
        self.cache = cache
        self.profile_id = cache.profile_id("video")
        self.calls: list[Path] = []
        self.expected_durations: list[int | None] = []

    def generate(
        self,
        source,
        destination,
        *,
        cancel_callback=None,
        progress_callback=None,
        expected_duration_ms=None,
        source_stat=None,
    ) -> PreviewResult:
        self.calls.append(Path(source))
        self.expected_durations.append(expected_duration_ms)
        destination = Path(destination)
        self.cache.ensure_parent(destination)
        temp_path = self.cache.temporary_path(destination)
        try:
            temp_path.write_bytes(tiny_mp4_bytes())
            if progress_callback is not None:
                progress_callback(0.5, 1_500_000)
            if cancel_callback is not None and cancel_callback():
                raise PreviewCancelled("Cancelled by the test generator.")
            self.cache.publish(temp_path, destination)
        except BaseException:
            self.cache.discard_temporary(temp_path)
            raise
        size = destination.stat().st_size
        return PreviewResult(
            status=PREVIEW_GENERATED,
            media_kind="video",
            profile_id=self.profile_id,
            path=destination,
            bytes_written=size,
            size_bytes=size,
            width=64,
            height=48,
            duration_ms=VIDEO_DURATION_MS,
        )


class NeverCalledVideoGenerator:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def generate(self, source, destination, **kwargs):
        self.calls.append(Path(source))
        raise AssertionError(f"video generator must not be called for {source}")


class FailingVideoGenerator:
    """Raises the configured ``PreviewError`` for every video, leaving nothing behind."""

    def __init__(self, error: PreviewError) -> None:
        self.error = error
        self.calls: list[Path] = []
        self.expected_durations: list[int | None] = []

    def generate(
        self,
        source,
        destination,
        *,
        cancel_callback=None,
        progress_callback=None,
        expected_duration_ms=None,
        source_stat=None,
    ):
        self.calls.append(Path(source))
        self.expected_durations.append(expected_duration_ms)
        raise self.error


class ExitingFfmpegProcess:
    """A ``subprocess.Popen`` stand-in that exits at once with a non-zero code.

    It writes ``partial_output`` to the destination argument (the last element
    of ``args``, which the real generator points at its temporary file) so the
    tests can prove the failed encode's leftovers are discarded.
    """

    def __init__(
        self,
        args,
        kwargs,
        *,
        returncode: int,
        stderr: bytes,
        partial_output: bytes = b"partial output",
    ) -> None:
        self.args = list(args)
        self.kwargs = kwargs
        self.stdin = None
        self.stdout = io.BytesIO(b"") if kwargs.get("stdout") is subprocess.PIPE else None
        self.stderr = io.BytesIO(stderr) if kwargs.get("stderr") is subprocess.PIPE else None
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        Path(self.args[-1]).write_bytes(partial_output)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def communicate(self, input=None, timeout=None):
        stdout = self.stdout.getvalue() if self.stdout is not None else None
        stderr = self.stderr.getvalue() if self.stderr is not None else None
        return stdout, stderr


class ExitingFfmpeg:
    """Installs itself as ``jvvv.video_preview``'s ``subprocess.Popen``."""

    def __init__(self, monkeypatch, *, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[ExitingFfmpegProcess] = []
        monkeypatch.setattr(video_module.subprocess, "Popen", self)

    def __call__(self, args, **kwargs):
        process = ExitingFfmpegProcess(args, kwargs, returncode=self.returncode, stderr=self.stderr)
        self.calls.append(process)
        return process


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def preview_settings(tmp_path: Path) -> PreviewSettings:
    return PreviewSettings(enabled=True, root_directory=str(tmp_path / "previews"))


def make_service(
    tmp_path: Path,
    *,
    image_generator=None,
    video_generator=None,
) -> tuple[PreviewService, PreviewCache]:
    settings = preview_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    service = PreviewService(
        settings,
        ffmpeg_path=FAKE_FFMPEG,
        cache=cache,
        image_generator=image_generator or RecordingImageGenerator(cache),
        video_generator=video_generator or FakeVideoGenerator(cache),
    )
    return service, cache


def make_scanner(db: Database, service: PreviewService | None = None, **kwargs) -> VolumeScanner:
    kwargs.setdefault("media_extractor", StubMediaExtractor())
    return VolumeScanner(db, preview_service=service, **kwargs)


def make_media_tree(root: Path) -> None:
    write_test_image(root / "Photos" / "alpha.png", 800, 600, "png")
    write_test_image(root / "Photos" / "beta.jpg", 640, 480, "jpeg")
    write_tiny_mp4(root / "Videos" / "clip.mp4")
    (root / "Docs").mkdir(parents=True, exist_ok=True)
    (root / "Docs" / "notes.txt").write_text("hello", encoding="utf-8")


def write_wave(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00" * 800)
    return path


def sha256_of(path: Path) -> bytes:
    return hashlib.sha256(path.read_bytes()).digest()


def file_rows(db: Database) -> dict[str, object]:
    rows = db.connection.execute(
        "SELECT id, relative_path, content_hash FROM files ORDER BY relative_path"
    ).fetchall()
    return {row["relative_path"]: row for row in rows}


def preview_rows(db: Database) -> dict[str, object]:
    rows = db.connection.execute(
        """
        SELECT f.relative_path AS relative_path, f.content_hash AS content_hash, p.*
        FROM files f
        JOIN file_preview_status p ON p.file_id = f.id
        ORDER BY f.relative_path
        """
    ).fetchall()
    return {row["relative_path"]: row for row in rows}


def temporaries_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and ".tmp-" in path.name]


def preview_files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and PreviewCache.parse_preview_name(path.name) is not None
    ]


@pytest.fixture
def catalogue(tmp_path: Path):
    db = Database(tmp_path / "catalogue.sqlite3")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "drive"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Previews disabled
# ---------------------------------------------------------------------------
def test_disabled_previews_attempt_nothing_and_record_disabled_mode(
    catalogue, source, tmp_path, monkeypatch
):
    make_media_tree(source)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no preview may be generated while previews are disabled")

    monkeypatch.setattr(ImagePreviewGenerator, "generate", forbidden)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue).scan(volume_id)

    assert result.status == "completed"
    assert result.files_seen == 4
    assert result.preview is not None
    assert result.preview["mode"] == MODE_DISABLED
    assert result.preview["image_generated"] == 0
    assert result.preview["video_generated"] == 0
    assert result.preview["failures"] == []
    assert result.preview_statistics.mode == MODE_DISABLED
    assert count_rows(catalogue, "file_preview_status") == 0
    assert not (tmp_path / "previews").exists()
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["preview_mode"] == "disabled"
    assert history["image_previews_generated"] == 0
    assert history["preview_bytes_written"] == 0
    assert history["preview_message"] == ""
    assert history["message"] is None


def test_scanner_uses_the_service_statistics_when_a_service_is_given(catalogue, tmp_path):
    service, _cache = make_service(tmp_path)
    ignored = PreviewStatistics(mode=MODE_DISABLED, message="ignored")

    scanner = make_scanner(catalogue, service, preview_statistics=ignored)

    assert scanner.preview_statistics is service.statistics
    assert scanner.preview_statistics.mode == MODE_ENABLED


# ---------------------------------------------------------------------------
# Previews enabled: generation, reuse, duplicates
# ---------------------------------------------------------------------------
def test_enabled_previews_generate_images_and_videos(catalogue, source, tmp_path):
    make_media_tree(source)
    service, cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == "completed"
    assert result.errors_count == 0
    assert result.preview["mode"] == MODE_ENABLED
    assert result.preview["image_generated"] == 2
    assert result.preview["image_reused"] == 0
    assert result.preview["image_failed"] == 0
    assert result.preview["video_generated"] == 1
    assert result.preview["video_reused"] == 0
    assert result.preview["video_failed"] == 0
    assert result.preview["storage_skipped"] == 0
    assert result.preview["bytes_written"] > 0
    assert result.preview["failures"] == []
    statistics = result.preview_statistics
    assert statistics.total_generated == 3
    assert statistics.bytes_written == result.preview["bytes_written"]

    rows = preview_rows(catalogue)
    assert set(rows) == {"Photos/alpha.png", "Photos/beta.jpg", "Videos/clip.mp4"}
    files = file_rows(catalogue)
    kinds = {"Photos/alpha.png": "image", "Photos/beta.jpg": "image", "Videos/clip.mp4": "video"}
    total_size = 0
    for relative_path, row in rows.items():
        expected_hash = sha256_of(source / relative_path)
        assert files[relative_path]["content_hash"] == expected_hash
        assert row["status"] == "available"
        assert row["source_hash"] == expected_hash
        assert row["media_kind"] == kinds[relative_path]
        assert row["profile_id"] == cache.profile_id(kinds[relative_path])
        assert row["error_stage"] is None
        assert row["error_message"] == ""
        assert row["generated_at"]
        preview_path = cache.preview_path(kinds[relative_path], expected_hash)
        assert preview_path.is_file()
        assert row["preview_size"] == preview_path.stat().st_size
        assert row["preview_width"] > 0 and row["preview_height"] > 0
        total_size += preview_path.stat().st_size
    assert rows["Videos/clip.mp4"]["preview_duration_ms"] == VIDEO_DURATION_MS
    assert rows["Photos/alpha.png"]["preview_duration_ms"] is None
    assert result.preview["bytes_written"] == total_size
    assert (source / "Docs" / "notes.txt").exists()
    assert "Docs/notes.txt" in files and "Docs/notes.txt" not in rows
    assert temporaries_under(cache.root) == []

    # The scanner passes the media duration so FFmpeg progress can be a percentage.
    assert service.video_generator.expected_durations == [VIDEO_DURATION_MS]
    assert [path.name for path in service.image_generator.calls] == ["alpha.png", "beta.jpg"]

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == "completed"
    assert history["preview_mode"] == "enabled"
    assert history["image_previews_generated"] == 2
    assert history["image_previews_reused"] == 0
    assert history["image_previews_failed"] == 0
    assert history["video_previews_generated"] == 1
    assert history["video_previews_reused"] == 0
    assert history["video_previews_failed"] == 0
    assert history["previews_storage_skipped"] == 0
    assert history["preview_bytes_written"] == total_size
    assert history["preview_bytes_written"] > 0
    assert history["preview_message"] == ""
    assert history["message"] is None
    assert history["errors_count"] == 0


def test_rescan_reuses_existing_previews_without_regenerating(catalogue, source, tmp_path):
    make_media_tree(source)
    volume_id = catalogue.create_volume("Drive", str(source))
    first_service, cache = make_service(tmp_path)
    first = make_scanner(catalogue, first_service).scan(volume_id)
    assert first.preview["image_generated"] == 2
    assert first.preview["video_generated"] == 1
    previews = preview_files_under(cache.root)
    assert len(previews) == 3
    before = {path: path.stat().st_mtime_ns for path in previews}
    before_bytes = {path: path.read_bytes() for path in previews}

    second_service, _ = make_service(tmp_path)
    second = make_scanner(catalogue, second_service).scan(volume_id)

    assert second.status == "completed"
    assert second.preview["image_generated"] == 0
    assert second.preview["image_reused"] == 2
    assert second.preview["video_generated"] == 0
    assert second.preview["video_reused"] == 1
    assert second.preview["image_failed"] == 0
    assert second.preview["video_failed"] == 0
    assert second.preview["bytes_written"] == 0
    assert second_service.image_generator.calls == []
    assert second_service.video_generator.calls == []
    assert preview_files_under(cache.root) == previews
    for path, mtime in before.items():
        assert path.stat().st_mtime_ns == mtime
        assert path.read_bytes() == before_bytes[path]
    rows = preview_rows(catalogue)
    assert {row["status"] for row in rows.values()} == {"available"}
    assert rows["Videos/clip.mp4"]["preview_duration_ms"] == VIDEO_DURATION_MS
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["preview_mode"] == "enabled"
    assert history["image_previews_reused"] == 2
    assert history["video_previews_reused"] == 1
    assert history["image_previews_generated"] == 0
    assert history["video_previews_generated"] == 0
    assert history["preview_bytes_written"] == 0


def test_duplicate_content_in_two_folders_generates_once_and_reuses_once(
    catalogue, source, tmp_path
):
    original = write_test_image(source / "Photos" / "holiday.png", 640, 400, "png")
    copy = source / "Archive" / "holiday-copy.png"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(original.read_bytes())
    digest = sha256_of(original)
    service, cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == "completed"
    assert result.preview["image_generated"] == 1
    assert result.preview["image_reused"] == 1
    assert result.preview["image_failed"] == 0
    assert len(service.image_generator.calls) == 1
    assert preview_files_under(cache.root) == [cache.preview_path("image", digest)]
    rows = preview_rows(catalogue)
    assert set(rows) == {"Archive/holiday-copy.png", "Photos/holiday.png"}
    for row in rows.values():
        assert row["status"] == "available"
        assert row["source_hash"] == digest
        assert row["preview_size"] == cache.preview_path("image", digest).stat().st_size
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["image_previews_generated"] == 1
    assert history["image_previews_reused"] == 1


# ---------------------------------------------------------------------------
# Scan-start preflight: user chose "Scan Without Previews"
# ---------------------------------------------------------------------------
def test_skipped_preflight_statistics_are_recorded_without_a_service(catalogue, source):
    make_media_tree(source)
    volume_id = catalogue.create_volume("Drive", str(source))
    statistics = skipped_preflight_statistics("FFmpeg could not be found.")

    result = make_scanner(catalogue, preview_statistics=statistics).scan(volume_id)

    assert result.status == "completed"
    assert result.files_seen == 4
    assert result.preview["mode"] == MODE_SKIPPED_PREFLIGHT
    assert result.preview["image_generated"] == 0
    assert result.preview["failures"] == []
    assert "FFmpeg could not be found." in result.preview["message"]
    assert result.message == statistics.message
    assert count_rows(catalogue, "file_preview_status") == 0
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == "completed"
    assert history["preview_mode"] == "skipped-preflight"
    assert history["message"] == statistics.message
    assert history["preview_message"] == statistics.message
    assert "skipped for this scan" in history["message"]
    assert "preflight" in history["message"]
    assert "FFmpeg could not be found." in history["message"]
    assert history["image_previews_generated"] == 0
    assert history["preview_bytes_written"] == 0


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
def test_corrupt_image_fails_preview_but_keeps_the_catalogue_record(catalogue, source, tmp_path):
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    broken = source / "Photos" / "broken.jpg"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a JPEG file at all " * 8)
    broken_hash = sha256_of(broken)
    service, cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert result.files_seen == 2
    assert result.errors_count == 0
    assert result.hash_errors == 0
    assert catalogue.list_scan_errors(volume_id) == []
    assert result.preview["image_generated"] == 1
    assert result.preview["image_failed"] == 1
    assert result.preview["video_failed"] == 0
    assert result.preview["storage_skipped"] == 0

    failures = result.preview["failures"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["relative_path"] == "Photos/broken.jpg"
    assert failure["source_name"] == "broken.jpg"
    assert failure["media_kind"] == "image"
    assert failure["stage"] == STAGE_IMAGE_DECODE
    assert failure["sha256"] == broken_hash.hex()
    assert failure["volume_id"] == volume_id
    assert failure["profile_id"] == cache.profile_id("image")
    assert failure["preview_path"] == str(cache.preview_path("image", broken_hash))
    assert failure["message"]
    typed = result.preview_statistics.failures
    assert [item.relative_path for item in typed] == ["Photos/broken.jpg"]
    assert typed[0].stage == STAGE_IMAGE_DECODE

    files = file_rows(catalogue)
    assert "Photos/broken.jpg" in files
    assert files["Photos/broken.jpg"]["content_hash"] == broken_hash
    rows = preview_rows(catalogue)
    assert rows["Photos/broken.jpg"]["status"] == "failed"
    assert rows["Photos/broken.jpg"]["error_stage"] == STAGE_IMAGE_DECODE
    assert rows["Photos/broken.jpg"]["error_message"]
    assert rows["Photos/broken.jpg"]["source_hash"] == broken_hash
    assert rows["Photos/broken.jpg"]["preview_size"] is None
    assert rows["Photos/alpha.png"]["status"] == "available"
    assert not cache.preview_path("image", broken_hash).exists()
    assert temporaries_under(cache.root) == []

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert history["message"].startswith("Catalogue indexing succeeded, but")
    assert "1 offline preview was not created" in history["message"]
    assert result.message == history["message"]
    assert history["image_previews_failed"] == 1
    assert history["image_previews_generated"] == 1
    assert history["errors_count"] == 0
    assert history["preview_mode"] == "enabled"


def test_failed_video_and_image_previews_are_both_counted_and_reported(
    catalogue, source, tmp_path
):
    """A video FFmpeg rejects and a corrupt image fail side by side (spec 14-16, 40)."""

    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    broken = source / "Photos" / "broken.jpg"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a JPEG file at all " * 8)
    clip = write_tiny_mp4(source / "Videos" / "clip.mp4")
    broken_hash = sha256_of(broken)
    clip_hash = sha256_of(clip)
    videos = FailingVideoGenerator(
        PreviewError(
            STAGE_FFMPEG_EXIT,
            "FFmpeg exited with code 1.",
            detail="Invalid data found when processing input.",
        )
    )
    service, cache = make_service(tmp_path, video_generator=videos)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert result.files_seen == 3
    assert result.errors_count == 0
    assert catalogue.list_scan_errors(volume_id) == []
    assert [path.name for path in videos.calls] == ["clip.mp4"]
    assert videos.expected_durations == [VIDEO_DURATION_MS]
    assert result.preview["image_generated"] == 1
    assert result.preview["image_failed"] == 1
    assert result.preview["video_generated"] == 0
    assert result.preview["video_reused"] == 0
    assert result.preview["video_failed"] == 1
    assert result.preview["storage_skipped"] == 0
    assert result.preview["storage_unavailable_reason"] is None
    assert result.preview_statistics.total_failed == 2

    # Every failed item is in the report data, in scan order, with its reason.
    failures = result.preview["failures"]
    assert [item["relative_path"] for item in failures] == ["Photos/broken.jpg", "Videos/clip.mp4"]
    image_failure, video_failure = failures
    assert image_failure["media_kind"] == "image"
    assert image_failure["stage"] == STAGE_IMAGE_DECODE
    assert image_failure["sha256"] == broken_hash.hex()
    assert video_failure["source_name"] == "clip.mp4"
    assert video_failure["media_kind"] == "video"
    assert video_failure["stage"] == STAGE_FFMPEG_EXIT
    assert video_failure["message"] == "FFmpeg exited with code 1."
    assert video_failure["detail"] == "Invalid data found when processing input."
    assert video_failure["sha256"] == clip_hash.hex()
    assert video_failure["volume_id"] == volume_id
    assert video_failure["profile_id"] == cache.profile_id("video")
    assert video_failure["preview_path"] == str(cache.preview_path("video", clip_hash))
    typed = result.preview_statistics.failures
    assert [(item.relative_path, item.media_kind, item.stage) for item in typed] == [
        ("Photos/broken.jpg", "image", STAGE_IMAGE_DECODE),
        ("Videos/clip.mp4", "video", STAGE_FFMPEG_EXIT),
    ]
    assert typed[1].display_lines() == [
        "Type: Video",
        f"Profile: {cache.profile_id('video')}",
        "Error: FFmpeg exited with code 1.",
        "Detail: Invalid data found when processing input.",
    ]

    # The catalogue records survive intact; only the preview status says "failed".
    files = file_rows(catalogue)
    assert set(files) == {"Photos/alpha.png", "Photos/broken.jpg", "Videos/clip.mp4"}
    assert files["Videos/clip.mp4"]["content_hash"] == clip_hash
    assert files["Photos/broken.jpg"]["content_hash"] == broken_hash
    rows = preview_rows(catalogue)
    clip_row = rows["Videos/clip.mp4"]
    assert clip_row["status"] == "failed"
    assert clip_row["media_kind"] == "video"
    assert clip_row["profile_id"] == cache.profile_id("video")
    assert clip_row["error_stage"] == STAGE_FFMPEG_EXIT
    assert "FFmpeg exited with code 1." in clip_row["error_message"]
    assert "Invalid data found when processing input." in clip_row["error_message"]
    assert clip_row["source_hash"] == clip_hash
    assert clip_row["preview_size"] is None
    assert clip_row["preview_width"] is None
    assert clip_row["preview_height"] is None
    assert clip_row["preview_duration_ms"] is None
    assert clip_row["generated_at"] is None
    assert rows["Photos/broken.jpg"]["status"] == "failed"
    assert rows["Photos/alpha.png"]["status"] == "available"
    assert [row["relative_path"] for row in catalogue.list_preview_failures(volume_id)] == [
        "Photos/broken.jpg",
        "Videos/clip.mp4",
    ]
    assert catalogue.count_preview_statuses(volume_id) == {"available": 1, "failed": 2}
    assert not cache.preview_path("video", clip_hash).exists()
    assert preview_files_under(cache.root) == [
        cache.preview_path("image", sha256_of(source / "Photos" / "alpha.png"))
    ]
    assert temporaries_under(cache.root) == []

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert history["image_previews_generated"] == 1
    assert history["image_previews_failed"] == 1
    assert history["video_previews_generated"] == 0
    assert history["video_previews_reused"] == 0
    assert history["video_previews_failed"] == 1
    assert history["previews_storage_skipped"] == 0
    assert history["message"].startswith("Catalogue indexing succeeded, but")
    assert "2 offline previews were not created" in history["message"]
    assert result.message == history["message"]
    assert history["errors_count"] == 0
    assert history["preview_mode"] == "enabled"


def test_real_video_generator_nonzero_ffmpeg_exit_reaches_the_scan_report(
    catalogue, source, tmp_path, monkeypatch
):
    """The real ``VideoPreviewGenerator`` (fake FFmpeg process) fails visibly through the scanner."""

    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    clip = write_tiny_mp4(source / "Videos" / "clip.mp4")
    clip_hash = sha256_of(clip)
    ffmpeg = ExitingFfmpeg(
        monkeypatch,
        returncode=1,
        stderr=b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] moov atom not found\n"
        b"Invalid data found when processing input\n",
    )
    settings = preview_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    # No injected video generator: the service builds the real one for FAKE_FFMPEG.
    service = PreviewService(
        settings,
        ffmpeg_path=FAKE_FFMPEG,
        cache=cache,
        image_generator=RecordingImageGenerator(cache),
    )
    assert isinstance(service.video_generator, video_module.VideoPreviewGenerator)
    service.video_generator.poll_interval_seconds = 0.01
    volume_id = catalogue.create_volume("Drive", str(source))
    messages: list[str] = []

    result = make_scanner(
        catalogue,
        service,
        progress_callback=lambda _files, _folders, message: messages.append(message),
    ).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert result.errors_count == 0
    assert len(ffmpeg.calls) == 1
    process = ffmpeg.calls[0]
    assert process.args[0] == FAKE_FFMPEG
    assert str(clip) in process.args
    assert cache.contains(Path(process.args[-1]))
    assert PreviewCache.is_temporary_name(Path(process.args[-1]).name)
    assert process.kwargs["shell"] is False
    assert process.kwargs["stdin"] is subprocess.DEVNULL
    assert not process.terminated and not process.killed
    assert "Creating video preview · Videos/clip.mp4" in messages

    assert result.preview["image_generated"] == 1
    assert result.preview["image_failed"] == 0
    assert result.preview["video_generated"] == 0
    assert result.preview["video_failed"] == 1
    assert result.preview["bytes_written"] > 0
    failures = result.preview["failures"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["relative_path"] == "Videos/clip.mp4"
    assert failure["media_kind"] == "video"
    assert failure["stage"] == STAGE_FFMPEG_EXIT
    assert failure["message"] == "FFmpeg exited with code 1."
    assert "Invalid data found when processing input" in failure["detail"]
    assert "\n" not in failure["detail"]
    assert failure["sha256"] == clip_hash.hex()
    assert failure["preview_path"] == str(cache.preview_path("video", clip_hash))

    assert file_rows(catalogue)["Videos/clip.mp4"]["content_hash"] == clip_hash
    rows = preview_rows(catalogue)
    assert rows["Videos/clip.mp4"]["status"] == "failed"
    assert rows["Videos/clip.mp4"]["error_stage"] == STAGE_FFMPEG_EXIT
    assert "Invalid data found when processing input" in rows["Videos/clip.mp4"]["error_message"]
    assert rows["Videos/clip.mp4"]["preview_duration_ms"] is None
    assert rows["Photos/alpha.png"]["status"] == "available"
    # The failed encode's partial output was discarded, not published.
    assert not cache.preview_path("video", clip_hash).exists()
    assert not Path(process.args[-1]).exists()
    assert temporaries_under(cache.root) == []
    assert len(preview_files_under(cache.root)) == 1

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["video_previews_failed"] == 1
    assert history["video_previews_generated"] == 0
    assert history["image_previews_generated"] == 1
    assert "1 offline preview was not created" in history["message"]
    assert history["errors_count"] == 0


def test_videos_fail_visibly_when_ffmpeg_disappeared_after_validation(
    catalogue, source, tmp_path
):
    """FFmpeg removed since Settings validation: every video is a reported failure (spec 26)."""

    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    write_tiny_mp4(source / "Videos" / "clip.mp4")
    write_tiny_mp4(source / "Videos" / "second.mov")
    missing_ffmpeg = tmp_path / "gone" / "ffmpeg.exe"
    settings = PreviewSettings(
        enabled=True,
        root_directory=str(tmp_path / "previews"),
        ffmpeg_path=str(missing_ffmpeg),
    )
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    service = PreviewService(settings, cache=cache, image_generator=RecordingImageGenerator(cache))
    assert service.ffmpeg_path is None
    assert service.video_generator is None
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert result.errors_count == 0
    assert result.files_seen == 3
    assert result.preview["image_generated"] == 1
    assert result.preview["image_failed"] == 0
    assert result.preview["video_generated"] == 0
    assert result.preview["video_failed"] == 2
    assert result.preview["storage_skipped"] == 0
    failures = result.preview["failures"]
    assert [item["relative_path"] for item in failures] == ["Videos/clip.mp4", "Videos/second.mov"]
    for item in failures:
        assert item["media_kind"] == "video"
        assert item["stage"] == STAGE_FFMPEG_START
        assert "FFmpeg is not available" in item["message"]
        assert str(missing_ffmpeg) in item["detail"]
        assert item["sha256"] == sha256_of(source / item["relative_path"]).hex()

    rows = preview_rows(catalogue)
    for relative_path in ("Videos/clip.mp4", "Videos/second.mov"):
        assert rows[relative_path]["status"] == "failed"
        assert rows[relative_path]["error_stage"] == STAGE_FFMPEG_START
        assert rows[relative_path]["source_hash"] == sha256_of(source / relative_path)
        assert file_rows(catalogue)[relative_path]["content_hash"] == sha256_of(source / relative_path)
    assert rows["Photos/alpha.png"]["status"] == "available"
    assert len(preview_files_under(cache.root)) == 1
    assert temporaries_under(cache.root) == []

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["video_previews_failed"] == 2
    assert history["image_previews_generated"] == 1
    assert "2 offline previews were not created" in history["message"]
    assert history["errors_count"] == 0


def test_disk_full_stops_further_preview_generation(catalogue, source, tmp_path):
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    write_test_image(source / "Photos" / "beta.jpg", 320, 240, "jpeg")
    write_test_image(source / "Photos" / "gamma.png", 320, 240, "png")
    write_tiny_mp4(source / "Videos" / "clip.mp4")
    failing = FailingImageGenerator(
        PreviewError(STAGE_DISK_FULL, "Could not write preview.", detail="No space left on device.")
    )
    videos = NeverCalledVideoGenerator()
    service, cache = make_service(tmp_path, image_generator=failing, video_generator=videos)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert result.errors_count == 0
    assert result.files_seen == 4
    assert [path.name for path in failing.calls] == ["alpha.png"]
    assert videos.calls == []
    assert result.preview["image_failed"] == 1
    assert result.preview["image_generated"] == 0
    assert result.preview["video_failed"] == 0
    assert result.preview["storage_skipped"] == 3
    assert "No space left on device." in result.preview["storage_unavailable_reason"]
    assert [item["relative_path"] for item in result.preview["failures"]] == ["Photos/alpha.png"]
    assert result.preview["failures"][0]["stage"] == STAGE_DISK_FULL

    rows = preview_rows(catalogue)
    assert rows["Photos/alpha.png"]["status"] == "failed"
    assert rows["Photos/alpha.png"]["error_stage"] == STAGE_DISK_FULL
    for relative_path in ("Photos/beta.jpg", "Photos/gamma.png", "Videos/clip.mp4"):
        assert rows[relative_path]["status"] == "missing"
        assert rows[relative_path]["error_stage"] == STAGE_STORAGE_UNAVAILABLE
        assert "unavailable" in rows[relative_path]["error_message"]
        assert rows[relative_path]["source_hash"] == sha256_of(source / relative_path)
    assert preview_files_under(cache.root) == []

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["previews_storage_skipped"] == 3
    assert history["image_previews_failed"] == 1
    assert "unavailable" in history["preview_message"]
    assert "No space left on device." in history["preview_message"]
    assert history["message"].startswith("Catalogue indexing succeeded, but")
    assert "1 offline preview was not created" in history["message"]
    assert "3 previews were not attempted because preview storage became unavailable" in history["message"]
    assert history["errors_count"] == 0
    assert catalogue.list_scan_errors(volume_id) == []


def test_hash_unavailable_records_missing_status_without_calling_generators(
    catalogue, source, tmp_path, monkeypatch
):
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    write_tiny_mp4(source / "Videos" / "clip.mp4")

    def fail_hash(*_args, **_kwargs):
        raise PermissionError("content read denied")

    monkeypatch.setattr(VolumeScanner, "_hash_file", fail_hash)
    failing = FailingImageGenerator(PreviewError(STAGE_IMAGE_DECODE, "must not run"))
    videos = NeverCalledVideoGenerator()
    service, cache = make_service(tmp_path, image_generator=failing, video_generator=videos)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == "completed"
    assert result.files_seen == 2
    assert result.files_hashed == 0
    # The unreadable content is a scan (hash) error, not a preview failure.
    assert result.hash_errors == 2
    assert result.errors_count == 2
    assert failing.calls == []
    assert videos.calls == []
    assert result.preview["image_failed"] == 0
    assert result.preview["video_failed"] == 0
    assert result.preview["image_generated"] == 0
    assert result.preview["storage_skipped"] == 0
    assert result.preview["failures"] == []

    files = file_rows(catalogue)
    assert files["Photos/alpha.png"]["content_hash"] is None
    rows = preview_rows(catalogue)
    assert set(rows) == {"Photos/alpha.png", "Videos/clip.mp4"}
    for relative_path, kind in (("Photos/alpha.png", "image"), ("Videos/clip.mp4", "video")):
        row = rows[relative_path]
        assert row["status"] == "missing"
        assert row["error_stage"] == STAGE_HASH_UNAVAILABLE
        assert row["source_hash"] is None
        assert row["media_kind"] == kind
        assert row["profile_id"] == cache.profile_id(kind)
        assert "SHA-256" in row["error_message"]
    assert preview_files_under(cache.root) == []
    assert temporaries_under(cache.root) == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_cancellation_inside_a_generator_rolls_back_and_leaves_no_temporaries(
    catalogue, source, tmp_path
):
    write_test_image(source / "Photos" / "zeta.png", 320, 240, "png")
    (source / "Docs").mkdir()
    (source / "Docs" / "notes.txt").write_text("hello", encoding="utf-8")
    volume_id = catalogue.create_volume("Drive", str(source))
    first = make_scanner(catalogue).scan(volume_id)
    assert first.status == "completed"
    files_before = count_rows(catalogue, "files")
    assert files_before == 2
    assert count_rows(catalogue, "file_preview_status") == 0

    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    write_test_image(source / "Photos" / "beta.png", 320, 240, "png")
    cancel_state = {"cancelled": False}
    settings = preview_settings(tmp_path)
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    generator = CancellingImageGenerator(cache, cancel_state)
    service = PreviewService(
        settings,
        ffmpeg_path=FAKE_FFMPEG,
        cache=cache,
        image_generator=generator,
        video_generator=NeverCalledVideoGenerator(),
    )

    result = make_scanner(
        catalogue,
        service,
        cancel_callback=lambda: cancel_state["cancelled"],
    ).scan(volume_id)

    assert result.status == "cancelled"
    assert result.message == "Scan cancelled."
    assert [path.name for path in generator.calls] == ["alpha.png"]
    assert temporaries_under(cache.root) == []
    assert preview_files_under(cache.root) == []
    assert count_rows(catalogue, "files") == files_before
    assert set(file_rows(catalogue)) == {"Docs/notes.txt", "Photos/zeta.png"}
    assert count_rows(catalogue, "file_preview_status") == 0
    assert result.preview["image_generated"] == 0
    assert result.preview["image_failed"] == 0
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == "cancelled"
    assert history["preview_mode"] == "enabled"
    assert history["image_previews_generated"] == 0


def test_cancellation_during_real_image_encoding_discards_the_temporary_file(
    catalogue, source, tmp_path
):
    write_test_image(source / "Photos" / "alpha.png", 640, 480, "png")
    write_test_image(source / "Photos" / "beta.png", 640, 480, "png")
    alpha_hash = sha256_of(source / "Photos" / "alpha.png")
    service, cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))
    state = {"armed": False, "checks": 0}
    messages: list[str] = []

    def on_progress(_files: int, _folders: int, message: str) -> None:
        messages.append(message)
        if message.startswith("Creating image preview") and not state["armed"]:
            state["armed"] = True

    def should_cancel() -> bool:
        if not state["armed"]:
            return False
        state["checks"] += 1
        # 1: the service's pre-check, 2: before decode, 3: after decode,
        # 4: after the temporary JPEG has been written -> cancel there.
        return state["checks"] >= 4

    result = make_scanner(
        catalogue,
        service,
        progress_callback=on_progress,
        cancel_callback=should_cancel,
    ).scan(volume_id)

    assert result.status == "cancelled"
    assert result.message == "Scan cancelled."
    assert state["checks"] == 4
    assert [path.name for path in service.image_generator.calls] == ["alpha.png"]
    assert any(message == "Creating image preview · Photos/alpha.png" for message in messages)
    assert not any("beta.png" in message and "Creating" in message for message in messages)
    assert temporaries_under(cache.root) == []
    assert not cache.preview_path("image", alpha_hash).exists()
    assert preview_files_under(cache.root) == []
    assert count_rows(catalogue, "files") == 0
    assert count_rows(catalogue, "file_preview_status") == 0
    assert result.preview["image_generated"] == 0
    assert result.preview["image_failed"] == 0
    assert result.preview["failures"] == []
    assert catalogue.list_scan_history(volume_id)[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
def test_progress_messages_name_the_preview_phase_and_relay_encode_progress(
    catalogue, source, tmp_path
):
    make_media_tree(source)
    service, _cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))
    events: list[tuple[int, int, str]] = []

    result = make_scanner(
        catalogue,
        service,
        progress_callback=lambda files, folders, message: events.append((files, folders, message)),
    ).scan(volume_id)

    assert result.status == "completed"
    messages = [message for _files, _folders, message in events]
    image_messages = [m for m in messages if m.startswith("Creating image preview · ")]
    assert "Creating image preview · Photos/alpha.png" in image_messages
    assert "Creating image preview · Photos/beta.jpg" in image_messages
    assert "Creating video preview · Videos/clip.mp4" in messages
    assert "Creating video preview · Videos/clip.mp4 · 50% of preview encode" in messages
    assert not any("Creating" in m and "notes.txt" in m for m in messages)
    # Media details are read before the preview is created (spec section 9 order).
    assert messages.index("Reading media details · Photos/alpha.png") < messages.index(
        "Creating image preview · Photos/alpha.png"
    )
    assert messages.index("Creating video preview · Videos/clip.mp4") < messages.index(
        "Creating video preview · Videos/clip.mp4 · 50% of preview encode"
    )
    # Counters accompany every preview message: the file is not yet counted as seen.
    video_events = [event for event in events if event[2].startswith("Creating video preview")]
    assert all(files == 3 for files, _folders, _message in video_events)


# ---------------------------------------------------------------------------
# Discarded rescans and unsupported media
# ---------------------------------------------------------------------------
def test_discarded_rescan_keeps_previous_preview_status_rows(catalogue, source, tmp_path):
    make_media_tree(source)
    volume_id = catalogue.create_volume("Drive", str(source))
    first_service, cache = make_service(tmp_path)
    assert make_scanner(catalogue, first_service).scan(volume_id).status == "completed"
    before = preview_rows(catalogue)
    old_hash = before["Photos/alpha.png"]["source_hash"]
    assert before["Photos/alpha.png"]["status"] == "available"

    write_test_image(source / "Photos" / "alpha.png", 900, 300, "png")
    new_hash = sha256_of(source / "Photos" / "alpha.png")
    assert new_hash != old_hash
    second_service, _ = make_service(tmp_path)
    reviews = []
    result = make_scanner(
        catalogue,
        second_service,
        preview_callback=lambda changes: reviews.append(changes) or False,
    ).scan(volume_id)

    assert result.status == "discarded"
    assert len(reviews) == 1
    assert reviews[0].files_changed == 1
    assert result.preview["image_generated"] == 1
    assert result.preview["image_reused"] == 1
    assert result.preview["video_reused"] == 1
    after = preview_rows(catalogue)
    assert set(after) == set(before)
    for relative_path, row in before.items():
        assert after[relative_path]["status"] == row["status"]
        assert after[relative_path]["source_hash"] == row["source_hash"]
        assert after[relative_path]["updated_at"] == row["updated_at"]
        assert after[relative_path]["generated_at"] == row["generated_at"]
    assert after["Photos/alpha.png"]["source_hash"] == old_hash
    assert file_rows(catalogue)["Photos/alpha.png"]["content_hash"] == old_hash
    # The preview for the new content was published before the review; it may
    # legitimately stay on disk, and the old preview is untouched.
    assert cache.preview_path("image", new_hash).is_file()
    assert cache.preview_path("image", old_hash).is_file()
    assert temporaries_under(cache.root) == []
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == "discarded"
    assert history["preview_mode"] == "enabled"


def test_audio_files_get_no_preview_status_row(catalogue, source, tmp_path):
    write_wave(source / "Audio" / "tone.wav")
    (source / "Audio" / "song.mp3").write_bytes(b"ID3 not really audio")
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    service, cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == "completed"
    assert result.files_seen == 3
    assert result.media_files == 3
    assert result.preview["image_generated"] == 1
    assert result.preview["video_generated"] == 0
    assert result.preview["image_failed"] == 0
    assert result.preview["video_failed"] == 0
    assert count_rows(catalogue, "file_preview_status") == 1
    files = file_rows(catalogue)
    assert catalogue.get_file_preview_status(files["Audio/tone.wav"]["id"]) is None
    assert catalogue.get_file_preview_status(files["Audio/song.mp3"]["id"]) is None
    assert catalogue.get_file_preview_status(files["Photos/alpha.png"]["id"])["status"] == "available"
    assert [path.name for path in service.image_generator.calls] == ["alpha.png"]
    assert service.video_generator.calls == []
    assert len(preview_files_under(cache.root)) == 1


# ---------------------------------------------------------------------------
# Optional: the real FFmpeg
# ---------------------------------------------------------------------------
def test_real_ffmpeg_generates_a_video_preview_during_a_scan(catalogue, source, tmp_path):
    ffmpeg = real_ffmpeg_path()
    if ffmpeg is None:
        pytest.skip("Set JVVV_TEST_FFMPEG to a real ffmpeg executable to run this test.")
    write_source_mp4(source / "Videos" / "holiday.mp4")
    digest = sha256_of(source / "Videos" / "holiday.mp4")
    settings = preview_settings(tmp_path)
    service = PreviewService(settings, ffmpeg_path=ffmpeg)
    cache = service.cache
    volume_id = catalogue.create_volume("Drive", str(source))
    messages: list[str] = []

    result = VolumeScanner(
        catalogue,
        media_extractor=MediaMetadataExtractor(discover_ffprobe=False),
        preview_service=service,
        progress_callback=lambda _files, _folders, message: messages.append(message),
    ).scan(volume_id)

    assert result.status == "completed"
    assert result.preview["video_generated"] == 1
    assert result.preview["video_failed"] == 0
    assert result.preview["failures"] == []
    assert result.preview["bytes_written"] > 0
    preview_path = cache.preview_path("video", digest)
    assert preview_path.is_file()
    validation = validate_video_preview(preview_path)
    assert validation.valid, validation.message
    assert validation.video_codec == "h264"
    assert validation.height is not None and validation.height <= settings.video.max_height
    assert validation.duration_ms is not None and 1500 <= validation.duration_ms <= 2500
    row = preview_rows(catalogue)["Videos/holiday.mp4"]
    assert row["status"] == "available"
    assert row["source_hash"] == digest
    assert row["preview_size"] == preview_path.stat().st_size
    assert row["preview_duration_ms"] == validation.duration_ms
    assert row["preview_width"] == validation.width
    assert row["preview_height"] == validation.height
    assert temporaries_under(cache.root) == []
    assert "Creating video preview · Videos/holiday.mp4" in messages
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["video_previews_generated"] == 1
    assert history["preview_bytes_written"] == preview_path.stat().st_size


def test_real_ffmpeg_reports_an_undecodable_video_as_a_preview_failure(
    catalogue, source, tmp_path
):
    """Spec 40: a recognized video FFmpeg cannot decode is a visible failure in the report."""

    ffmpeg = real_ffmpeg_path()
    if ffmpeg is None:
        pytest.skip("Set JVVV_TEST_FFMPEG to a real ffmpeg executable to run this test.")
    write_source_mp4(source / "Videos" / "holiday.mp4")
    garbage = source / "Videos" / "camera001.mov"
    garbage.write_bytes(b"this is not a QuickTime movie at all " * 64)
    garbage_hash = sha256_of(garbage)
    holiday_hash = sha256_of(source / "Videos" / "holiday.mp4")
    settings = preview_settings(tmp_path)
    service = PreviewService(settings, ffmpeg_path=ffmpeg)
    cache = service.cache
    volume_id = catalogue.create_volume("Drive", str(source))

    result = VolumeScanner(
        catalogue,
        media_extractor=MediaMetadataExtractor(discover_ffprobe=False),
        preview_service=service,
    ).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    assert result.errors_count == 0
    assert result.preview["video_generated"] == 1
    assert result.preview["video_failed"] == 1
    failures = result.preview["failures"]
    assert [item["relative_path"] for item in failures] == ["Videos/camera001.mov"]
    failure = failures[0]
    assert failure["media_kind"] == "video"
    assert failure["stage"] == STAGE_FFMPEG_EXIT
    assert failure["message"].startswith("FFmpeg exited with code ")
    assert failure["detail"]
    assert failure["sha256"] == garbage_hash.hex()
    assert failure["preview_path"] == str(cache.preview_path("video", garbage_hash))

    files = file_rows(catalogue)
    assert files["Videos/camera001.mov"]["content_hash"] == garbage_hash
    rows = preview_rows(catalogue)
    assert rows["Videos/camera001.mov"]["status"] == "failed"
    assert rows["Videos/camera001.mov"]["error_stage"] == STAGE_FFMPEG_EXIT
    assert rows["Videos/camera001.mov"]["error_message"]
    assert rows["Videos/camera001.mov"]["preview_duration_ms"] is None
    assert rows["Videos/holiday.mp4"]["status"] == "available"
    assert not cache.preview_path("video", garbage_hash).exists()
    assert cache.preview_path("video", holiday_hash).is_file()
    assert temporaries_under(cache.root) == []

    history = catalogue.list_scan_history(volume_id)[0]
    assert history["video_previews_generated"] == 1
    assert history["video_previews_failed"] == 1
    assert "1 offline preview was not created" in history["message"]
    assert history["errors_count"] == 0


# ---------------------------------------------------------------------------
# Audit follow-ups (spec §9/§32 hash window, §10 shared roots, §11 corrupt log, §14 counts)
# ---------------------------------------------------------------------------


class MutatingMediaExtractor(StubMediaExtractor):
    """Changes PNG files during the media probe: the source differs from what was hashed."""

    def inspect(self, path, *, extension=None, cancel_callback=None):
        import os
        import time

        file_path = Path(path)
        if file_path.suffix.lower() == ".png":
            with open(file_path, "ab") as handle:
                handle.write(b"\x00" * 64)
            later = time.time() + 10
            os.utime(file_path, (later, later))
        return super().inspect(path, extension=extension, cancel_callback=cancel_callback)


def test_source_changed_between_hashing_and_preview_is_not_previewed_under_the_old_hash(
    catalogue, source, tmp_path
):
    from jvvv.preview_cache import STAGE_SOURCE_CHANGED

    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    hashed = sha256_of(source / "Photos" / "alpha.png")
    service, cache = make_service(tmp_path)
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service, media_extractor=MutatingMediaExtractor()).scan(volume_id)

    assert result.status == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS
    rows = preview_rows(catalogue)
    assert rows["Photos/alpha.png"]["status"] == "failed"
    assert rows["Photos/alpha.png"]["error_stage"] == STAGE_SOURCE_CHANGED
    assert "between hashing and preview generation" in rows["Photos/alpha.png"]["error_message"]
    assert not cache.preview_path("image", hashed).exists(), "no preview may be published under the old hash"
    assert preview_files_under(cache.root) == []
    assert temporaries_under(cache.root) == []
    assert [failure.stage for failure in service.statistics.failures] == [STAGE_SOURCE_CHANGED]


def test_a_second_catalogue_sharing_the_root_reuses_previews_without_generating(source, tmp_path):
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    write_test_image(source / "Photos" / "beta.png", 300, 200, "png")

    first_service, cache = make_service(tmp_path)
    first_db = Database(tmp_path / "first.jvvv")
    try:
        first = make_scanner(first_db, first_service).scan(first_db.create_volume("Drive", str(source)))
    finally:
        first_db.close()
    assert first.status == "completed"
    assert first_service.statistics.image_generated == 2

    second_service, _ = make_service(tmp_path)  # same root, a different catalogue file
    second_db = Database(tmp_path / "second.jvvv")
    try:
        second = make_scanner(second_db, second_service).scan(second_db.create_volume("Drive", str(source)))
        assert second.status == "completed"
        assert second_service.statistics.image_reused == 2
        assert second_service.statistics.image_generated == 0
        assert second_service.image_generator.calls == []
        assert {row["status"] for row in preview_rows(second_db).values()} == {"available"}
    finally:
        second_db.close()
    assert len(preview_files_under(cache.root)) == 2


def test_regenerated_corrupt_previews_are_recorded_in_the_scan_history(catalogue, source, tmp_path):
    from jvvv.image_preview import validate_image_preview

    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    service, cache = make_service(tmp_path)
    corrupt = cache.preview_path("image", sha256_of(source / "Photos" / "alpha.png"))
    cache.ensure_parent(corrupt)
    corrupt.write_bytes(b"not a jpeg at all")
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == "completed"
    assert service.statistics.corrupt_replaced == 1
    assert validate_image_preview(corrupt).valid
    history = catalogue.list_scan_history(volume_id)[0]
    assert history["status"] == "completed"
    assert "1 existing preview failed validation and was regenerated." in history["message"]


def test_media_files_without_a_hash_are_counted_in_the_preview_report(catalogue, source, tmp_path, monkeypatch):
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    write_tiny_mp4(source / "Videos" / "clip.mp4")

    def fail_hash(*_args, **_kwargs):
        raise PermissionError("content read denied")

    monkeypatch.setattr(VolumeScanner, "_hash_file", fail_hash)
    service, _cache = make_service(
        tmp_path,
        image_generator=FailingImageGenerator(PreviewError(STAGE_IMAGE_DECODE, "must not run")),
        video_generator=NeverCalledVideoGenerator(),
    )
    volume_id = catalogue.create_volume("Drive", str(source))

    result = make_scanner(catalogue, service).scan(volume_id)

    assert result.status == "completed"
    assert result.preview["hash_unavailable"] == 2
    assert service.statistics.hash_unavailable == 2
    summary = service.statistics.summary_text(service.statistics.root)
    assert "no SHA-256 could be recorded" in summary and "\n  2" in summary
    history = catalogue.list_scan_history(volume_id)[0]
    assert "2 image/video files had no SHA-256 recorded, so no preview was attempted." in history["message"]


def test_a_preview_root_inside_the_scanned_volume_is_not_catalogued(catalogue, source, tmp_path):
    write_test_image(source / "Photos" / "alpha.png", 320, 240, "png")
    settings = PreviewSettings(enabled=True, root_directory=str(source / "JVVV Previews"))
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    volume_id = catalogue.create_volume("Drive", str(source))

    def scan_once() -> tuple[PreviewService, object]:
        service = PreviewService(
            settings,
            ffmpeg_path=FAKE_FFMPEG,
            cache=cache,
            image_generator=RecordingImageGenerator(cache),
            video_generator=NeverCalledVideoGenerator(),
        )
        return service, make_scanner(catalogue, service).scan(volume_id)

    first_service, first = scan_once()
    assert first.status == "completed"
    assert first_service.statistics.image_generated == 1
    assert preview_files_under(cache.root), "the preview was written inside the scanned volume"

    second_service, second = scan_once()

    assert second.status == "completed"
    assert second_service.statistics.image_reused == 1
    assert second_service.statistics.image_generated == 0
    assert set(file_rows(catalogue)) == {"Photos/alpha.png"}, "the preview store must not be indexed"
    assert count_rows(catalogue, "files") == 1
    assert len(preview_files_under(cache.root)) == 1, "no preview of a preview"
