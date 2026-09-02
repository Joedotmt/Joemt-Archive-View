"""Tests for ``jvvv.preview_service``: validation, the scan-time service, and reporting.

Backends are injected (fake finder/prober/testers, fake generators) so every
failure path is deterministic.  The real Qt image backend is exercised for the
reuse path and the real-backend validation test; real FFmpeg cases skip when
``JVVV_TEST_FFMPEG`` / PATH has no ffmpeg.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from preview_fixtures import (  # noqa: E402
    real_ffmpeg_path,
    tiny_mp4_bytes,
    write_test_image,
)

from jvvv import preview_service as service_module  # noqa: E402
from jvvv.database import PREVIEW_SCAN_MODES, PREVIEW_STATUS_VALUES  # noqa: E402
from jvvv.image_preview import IMAGE_BACKEND_NAME  # noqa: E402
from jvvv.preview_cache import (  # noqa: E402
    PREVIEW_FAILED,
    PREVIEW_GENERATED,
    PREVIEW_REUSED,
    PREVIEW_SKIPPED_DISABLED,
    PREVIEW_SKIPPED_STORAGE,
    PREVIEW_SKIPPED_UNSUPPORTED,
    STAGE_CONFIGURATION,
    STAGE_DISK_FULL,
    STAGE_FFMPEG_ENCODER,
    STAGE_FFMPEG_EXIT,
    STAGE_FFMPEG_START,
    STAGE_HASH_UNAVAILABLE,
    STAGE_IMAGE_DECODE,
    STAGE_IMAGE_ENCODE,
    STAGE_PERMISSION,
    STAGE_PREVIEW_ROOT,
    PreviewCache,
    PreviewCancelled,
    PreviewError,
    PreviewFailure,
    PreviewResult,
)
from jvvv.preview_config import (  # noqa: E402
    ImagePreviewProfile,
    PreviewConfigError,
    PreviewSettings,
    VideoPreviewProfile,
)
from jvvv.preview_service import (  # noqa: E402
    DB_STATUS_AVAILABLE,
    DB_STATUS_FAILED,
    DB_STATUS_MISSING,
    DB_STATUS_UNSUPPORTED,
    MODE_DISABLED,
    MODE_ENABLED,
    MODE_SKIPPED_PREFLIGHT,
    SCAN_OUTCOME_COMPLETED_WITH_WARNINGS,
    STAGE_STORAGE_UNAVAILABLE,
    PreviewService,
    PreviewStatistics,
    PreviewValidationReport,
    ValidationStep,
    disabled_statistics,
    hash_unavailable_status_record,
    inspect_preview_file,
    preflight_preview_configuration,
    preview_cache_for,
    preview_warning_message,
    scan_outcome,
    skipped_preflight_statistics,
    status_record_for,
    validate_preview_configuration,
)
from jvvv.utils import format_size  # noqa: E402
from jvvv.video_preview import FFMPEG_ENCODER, FfmpegCapabilities  # noqa: E402


FAKE_FFMPEG = "C:/Tools/ffmpeg/bin/ffmpeg.exe" if os.name == "nt" else "/opt/ffmpeg/bin/ffmpeg"
FAKE_VERSION = "ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers"
CAPABILITIES = FfmpegCapabilities(
    path=FAKE_FFMPEG, version=FAKE_VERSION, encoders=frozenset({"libx264", "aac", "mpeg4"})
)
CAPABILITIES_WITHOUT_X264 = FfmpegCapabilities(
    path=FAKE_FFMPEG, version=FAKE_VERSION, encoders=frozenset({"aac", "mpeg4"})
)
IMAGE_PROFILE_ID = "jpeg-max1600-q82"
VIDEO_PROFILE_ID = "h264-1fps-240p-crf35-veryfast"
FULL_STEP_KEYS = [
    "configuration",
    "preview-root",
    "image-backend",
    "image-test",
    "ffmpeg-found",
    "ffmpeg-version",
    "ffmpeg-encoder",
    "video-test",
]
PREFLIGHT_STEP_KEYS = [key for key in FULL_STEP_KEYS if not key.endswith("-test")]
VOLUME_ID = 7
VOLUME_LABEL = "AID-007 - Archive"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def files_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def temporaries_under(root: Path) -> list[Path]:
    return [path for path in files_under(root) if PreviewCache.is_temporary_name(path.name)]


def digest_of(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def step_keys(report: PreviewValidationReport) -> list[str]:
    return [step.key for step in report.steps]


def fake_validation_tools(
    calls: list[tuple[object, ...]],
    *,
    ffmpeg_path: str | None = FAKE_FFMPEG,
    capabilities: FfmpegCapabilities | Exception = CAPABILITIES,
    backend: tuple[bool, str] = (True, "Fake image backend can read and write JPEG."),
    image_error: Exception | None = None,
    video_error: Exception | None = None,
) -> dict[str, object]:
    """Injected replacements for every external tool used by the validator."""

    def finder(explicit: str | None) -> str | None:
        calls.append(("find", explicit))
        return ffmpeg_path

    def prober(path: str) -> FfmpegCapabilities:
        calls.append(("probe", path))
        if isinstance(capabilities, Exception):
            raise capabilities
        return capabilities

    def backend_check() -> tuple[bool, str]:
        calls.append(("backend",))
        return backend

    def image_tester(cache: PreviewCache) -> str:
        calls.append(("image-test", cache.root))
        if image_error is not None:
            raise image_error
        return f"Encoded a 64x48 test image to JPEG quality 82 (1.3 KB) in {cache.root}"

    def video_tester(path: str, cache: PreviewCache) -> str:
        calls.append(("video-test", path, cache.root))
        if video_error is not None:
            raise video_error
        return (
            f"Encoded a 64x48 test video (2 frames) with {FFMPEG_ENCODER} preset veryfast "
            f"CRF 35 (2.0 KB) in {cache.root}"
        )

    return {
        "ffmpeg_finder": finder,
        "ffmpeg_prober": prober,
        "image_backend_check": backend_check,
        "image_tester": image_tester,
        "video_tester": video_tester,
    }


def call_names(calls: list[tuple[object, ...]]) -> list[object]:
    return [call[0] for call in calls]


class FakeGenerator:
    """Stand-in for ``ImagePreviewGenerator``/``VideoPreviewGenerator``.

    ``generate`` records every call, then either raises ``error`` or writes
    ``payload`` to the destination and returns a ``generated`` result.  It
    accepts the video keyword arguments too so one class serves both kinds.
    """

    def __init__(
        self,
        media_kind: str,
        *,
        payload: bytes = b"",
        error: BaseException | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        progress: tuple[tuple[float | None, int | None], ...] = (),
    ) -> None:
        self.media_kind = media_kind
        self.profile_id = IMAGE_PROFILE_ID if media_kind == "image" else VIDEO_PROFILE_ID
        self.payload = payload
        self.error = error
        self.width = width
        self.height = height
        self.duration_ms = duration_ms
        self.progress = list(progress)
        self.calls: list[SimpleNamespace] = []

    def generate(
        self,
        source: Path,
        destination: Path,
        *,
        cancel_callback=None,
        progress_callback=None,
        expected_duration_ms: int | None = None,
        source_stat=None,
    ) -> PreviewResult:
        self.calls.append(
            SimpleNamespace(
                source=source,
                destination=destination,
                cancel_callback=cancel_callback,
                progress_callback=progress_callback,
                expected_duration_ms=expected_duration_ms,
                source_stat=source_stat,
            )
        )
        if self.error is not None:
            raise self.error
        for fraction, out_time_ms in self.progress:
            if progress_callback is not None:
                progress_callback(fraction, out_time_ms)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        size = len(self.payload)
        return PreviewResult(
            status=PREVIEW_GENERATED,
            media_kind=self.media_kind,
            profile_id=self.profile_id,
            path=destination,
            bytes_written=size,
            size_bytes=size,
            width=self.width,
            height=self.height,
            duration_ms=self.duration_ms,
        )


def make_service(
    settings: PreviewSettings,
    *,
    image: FakeGenerator | None = None,
    video: FakeGenerator | None = None,
    ffmpeg_path: str | None = FAKE_FFMPEG,
    log: list[str] | None = None,
) -> PreviewService:
    return PreviewService(
        settings,
        ffmpeg_path=ffmpeg_path,
        image_generator=image,
        video_generator=video,
        log_callback=None if log is None else log.append,
    )


def ensure(
    service: PreviewService,
    media_kind: str,
    content_hash: bytes | str,
    *,
    source: Path,
    relative_path: str = "Photos/one.jpg",
    source_name: str | None = None,
    **kwargs,
) -> PreviewResult:
    return service.ensure_preview(
        media_kind=media_kind,
        source=source,
        content_hash=content_hash,
        relative_path=relative_path,
        source_name=source_name or relative_path.rsplit("/", 1)[-1],
        volume_id=VOLUME_ID,
        volume_label=VOLUME_LABEL,
        **kwargs,
    )


def make_failure(**overrides) -> PreviewFailure:
    values = {
        "source_name": "camera001.mov",
        "relative_path": "Videos/camera001.mov",
        "volume_id": VOLUME_ID,
        "volume_label": VOLUME_LABEL,
        "media_kind": "video",
        "sha256": "ab" * 32,
        "preview_path": "E:/JVVV Previews/videos/x/ab/abab.mp4",
        "profile_id": VIDEO_PROFILE_ID,
        "stage": STAGE_FFMPEG_EXIT,
        "message": "FFmpeg exited with code 1.",
        "detail": "Invalid data found when processing input",
    }
    values.update(overrides)
    return PreviewFailure(**values)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "previews"


@pytest.fixture
def settings(root: Path) -> PreviewSettings:
    return PreviewSettings(enabled=True, root_directory=str(root))


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "source" / "one.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not read by fake generators")
    return path


@pytest.fixture
def jpeg_payload(tmp_path: Path) -> bytes:
    return write_test_image(tmp_path / "fixtures" / "payload.jpg", 32, 24, "jpeg").read_bytes()


@pytest.fixture
def real_ffmpeg() -> str:
    path = real_ffmpeg_path()
    if path is None:
        pytest.skip("real FFmpeg not available (set JVVV_TEST_FFMPEG)")
    return path


# ---------------------------------------------------------------------------
# validate_preview_configuration
# ---------------------------------------------------------------------------
def test_validation_all_pass_reports_every_required_item(settings, root):
    calls: list = []

    report = validate_preview_configuration(settings, **fake_validation_tools(calls))

    assert report.passed is True
    assert report.first_failure is None
    assert report.failure_summary() == ""
    assert step_keys(report) == FULL_STEP_KEYS
    assert all(step.passed and not step.skipped for step in report.steps)
    assert all(step.status_text == "PASS" for step in report.steps)
    assert report.settings is settings
    assert report.root == str(root)
    assert isinstance(report.free_bytes, int) and report.free_bytes > 0
    assert isinstance(report.total_bytes, int) and report.total_bytes >= report.free_bytes
    assert report.ffmpeg_path == FAKE_FFMPEG
    assert report.ffmpeg_version == FAKE_VERSION
    assert report.encoder_available is True
    assert report.image_backend == IMAGE_BACKEND_NAME
    assert report.image_profile_id == IMAGE_PROFILE_ID
    assert report.video_profile_id == VIDEO_PROFILE_ID
    assert report.include_encode_tests is True

    text = report.report_text()
    assert text.splitlines()[0] == "Preview Configuration Test: PASS"
    assert text.splitlines()[-1] == "Overall: PASS"
    assert f"Preview storage directory: {root}" in text
    assert f"Available free space: {format_size(report.free_bytes)}" in text
    assert f"FFmpeg path: {FAKE_FFMPEG}" in text
    assert f"FFmpeg version: {FAKE_VERSION}" in text
    assert f"H.264 encoder ({FFMPEG_ENCODER}): available" in text
    assert f"Image backend: {IMAGE_BACKEND_NAME}" in text
    assert f"Image profile: {IMAGE_PROFILE_ID}" in text
    assert f"Video profile: {VIDEO_PROFILE_ID}" in text
    assert "[PASS] Image preview test encode — Encoded a 64x48 test image" in text
    assert "[PASS] Video preview test encode — Encoded a 64x48 test video" in text
    assert "[PASS] Preview storage directory — " in text
    assert "FAIL" not in text and "Not run" not in text

    # Both encode tests ran exactly once against the configured root and FFmpeg,
    # and the root was created but left without any files (spec §2A-C).
    assert calls.count(("image-test", root)) == 1
    assert calls.count(("video-test", FAKE_FFMPEG, root)) == 1
    assert ("find", None) in calls and ("probe", FAKE_FFMPEG) in calls
    assert root.is_dir()
    assert files_under(root) == []


@pytest.mark.parametrize(
    ("overrides", "expected_detail"),
    [
        ({"image": ImagePreviewProfile(max_dimension=100)}, "Image maximum dimension"),
        ({"video": VideoPreviewProfile(crf=99)}, "Video CRF"),
        ({"video": VideoPreviewProfile(preset="turbo")}, "preset"),
        ({"root_directory": "   "}, "preview storage directory has not been selected"),
    ],
)
def test_validation_invalid_configuration_still_reports_the_tools_but_never_touches_the_root(
    root, overrides, expected_detail
):
    values = {"enabled": True, "root_directory": str(root)}
    values.update(overrides)
    settings = PreviewSettings(**values)
    calls: list = []

    report = validate_preview_configuration(settings, **fake_validation_tools(calls))

    assert report.passed is False
    assert step_keys(report) == FULL_STEP_KEYS
    step = report.steps[0]
    assert step.passed is False and step.skipped is False
    assert step.stage == STAGE_CONFIGURATION
    assert step.status_text == "FAIL"
    assert expected_detail.casefold() in step.detail.casefold()
    # Spec §3: the report still names the FFmpeg path/version and the image
    # backend instead of claiming "Not found"; only the root and the encode
    # tests are skipped, and the filesystem is never touched.
    assert report.step("preview-root").skipped
    assert report.step("image-test").skipped and report.step("video-test").skipped
    assert report.step("image-backend").passed
    assert report.step("ffmpeg-found").passed and report.ffmpeg_path == FAKE_FFMPEG
    assert report.step("ffmpeg-encoder").passed
    assert call_names(calls) == ["backend", "find", "probe"]
    assert not root.exists(), "an invalid configuration must not touch the filesystem"
    text = report.report_text()
    assert f"FFmpeg path: {FAKE_FFMPEG}" in text
    assert "Not found" not in text
    assert "[Not run] Preview storage directory" in text
    assert report.failure_summary().count("failed.") == 1
    summary = report.failure_summary()
    assert summary.startswith("Offline previews could not be enabled.\n\n")
    assert "Configuration values failed." in summary
    assert expected_detail.casefold() in summary.casefold()
    text = report.report_text()
    assert text.startswith("Preview Configuration Test: FAIL")
    assert "[FAIL] Configuration values — " in text
    assert text.endswith("Overall: FAIL")


def test_validation_root_that_cannot_be_created_fails_and_skips_encode_tests(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file where a parent directory should be")
    root = blocker / "previews"
    settings = PreviewSettings(enabled=True, root_directory=str(root))
    calls: list = []

    report = validate_preview_configuration(settings, **fake_validation_tools(calls))

    assert report.passed is False
    assert step_keys(report) == FULL_STEP_KEYS
    root_step = report.step("preview-root")
    assert root_step is not None
    assert root_step.passed is False and root_step.skipped is False
    assert root_step.stage == STAGE_PREVIEW_ROOT
    assert f"JVVV cannot write to: {root}" in root_step.detail
    assert "WinError" in root_step.detail or "Errno" in root_step.detail, root_step.detail
    assert report.free_bytes is None and report.total_bytes is None

    for key in ("image-test", "video-test"):
        step = report.step(key)
        assert step is not None
        assert step.skipped is True and step.passed is False
        assert step.status_text == "Not run"
        assert "earlier check failed" in step.detail
    assert "image-test" not in call_names(calls)
    assert "video-test" not in call_names(calls)
    # The FFmpeg checks are independent of the root and still run.
    assert ("probe", FAKE_FFMPEG) in calls
    assert report.step("ffmpeg-version").passed is True
    assert report.step("ffmpeg-encoder").passed is True

    assert report.first_failure is root_step
    summary = report.failure_summary()
    assert summary.startswith("Offline previews could not be enabled.")
    assert "Preview storage directory failed." in summary
    assert str(root) in summary
    text = report.report_text()
    assert "[FAIL] Preview storage directory — JVVV cannot write to:" in text
    assert "[Not run] Image preview test encode" in text
    assert "[Not run] Video preview test encode" in text
    assert "Available free space: Unknown" in text
    assert text.endswith("Overall: FAIL")
    assert blocker.read_bytes() == b"a file where a parent directory should be"


def test_validation_file_at_root_path_fails_with_detail(tmp_path):
    root = tmp_path / "previews"
    root.write_bytes(b"I am a file, not a directory")
    settings = PreviewSettings(enabled=True, root_directory=str(root))
    calls: list = []

    report = validate_preview_configuration(settings, **fake_validation_tools(calls))

    assert report.passed is False
    step = report.step("preview-root")
    assert step.passed is False
    assert step.stage == STAGE_PREVIEW_ROOT
    assert f"JVVV cannot write to: {root}" in step.detail
    assert "not a directory" in step.detail
    assert report.step("image-test").skipped is True
    assert report.step("video-test").skipped is True
    assert "Preview storage directory failed." in report.failure_summary()
    assert root.read_bytes() == b"I am a file, not a directory", "the file must be untouched"


def test_validation_ffmpeg_missing_explicit_path_names_it_and_points_to_settings(settings, tmp_path):
    explicit = str(tmp_path / "tools" / "ffmpeg.exe")
    settings = PreviewSettings(enabled=True, root_directory=settings.root_directory, ffmpeg_path=f"  {explicit}  ")
    calls: list = []

    report = validate_preview_configuration(settings, **fake_validation_tools(calls, ffmpeg_path=None))

    assert report.passed is False
    assert ("find", explicit) in calls, "the finder receives the stripped explicit path"
    assert "probe" not in call_names(calls)
    assert "video-test" not in call_names(calls)
    assert "image-test" in call_names(calls), "the image test does not depend on FFmpeg"
    found = report.step("ffmpeg-found")
    assert found.passed is False and found.skipped is False
    assert found.stage == STAGE_FFMPEG_START
    assert "does not exist" in found.detail
    assert explicit in found.detail
    assert "Settings" in found.detail
    for key in ("ffmpeg-version", "ffmpeg-encoder", "video-test"):
        assert report.step(key).skipped is True
    assert report.ffmpeg_path is None
    assert report.ffmpeg_version is None
    assert report.encoder_available is None
    text = report.report_text()
    assert "FFmpeg path: Not found" in text
    assert "FFmpeg version: Unavailable" in text
    assert f"H.264 encoder ({FFMPEG_ENCODER}): Unknown" in text
    assert "FFmpeg executable failed." in report.failure_summary()
    assert explicit in report.failure_summary()


def test_validation_ffmpeg_missing_on_path_uses_different_wording(settings):
    calls: list = []

    report = validate_preview_configuration(settings, **fake_validation_tools(calls, ffmpeg_path=None))

    found = report.step("ffmpeg-found")
    assert report.passed is False
    assert found.passed is False
    assert found.stage == STAGE_FFMPEG_START
    assert "could not be found on PATH" in found.detail
    assert "does not exist" not in found.detail
    assert "Settings" in found.detail
    assert "libx264" in found.detail
    assert ("find", None) in calls


def test_validation_probe_failure_is_reported_on_the_version_step(settings):
    calls: list = []
    error = PreviewError(STAGE_FFMPEG_EXIT, "FFmpeg exited with code 1.", detail="Unrecognized option 'version'.")

    report = validate_preview_configuration(settings, **fake_validation_tools(calls, capabilities=error))

    assert report.passed is False
    assert report.step("ffmpeg-found").passed is True
    version = report.step("ffmpeg-version")
    assert version.passed is False and version.skipped is False
    assert version.stage == STAGE_FFMPEG_EXIT
    assert "FFmpeg exited with code 1." in version.detail
    assert "Unrecognized option" in version.detail
    assert report.step("ffmpeg-encoder").skipped is True
    assert report.step("video-test").skipped is True
    assert "video-test" not in call_names(calls)
    assert report.ffmpeg_path == FAKE_FFMPEG
    assert report.ffmpeg_version is None
    assert report.encoder_available is None
    assert "FFmpeg version failed." in report.failure_summary()
    assert "[FAIL] FFmpeg version — FFmpeg exited with code 1." in report.report_text()


def test_validation_missing_libx264_fails_encoder_step_with_exact_wording(settings):
    calls: list = []

    report = validate_preview_configuration(
        settings, **fake_validation_tools(calls, capabilities=CAPABILITIES_WITHOUT_X264)
    )

    assert report.passed is False
    assert report.step("ffmpeg-version").passed is True
    encoder = report.step("ffmpeg-encoder")
    assert encoder.passed is False and encoder.skipped is False
    assert encoder.stage == STAGE_FFMPEG_ENCODER
    assert report.encoder_available is False
    assert report.step("video-test").skipped is True
    assert "video-test" not in call_names(calls)
    assert report.step("image-test").passed is True
    summary = report.failure_summary()
    assert summary.startswith("Offline previews could not be enabled.")
    assert f"H.264 encoder ({FFMPEG_ENCODER}) failed." in summary
    assert f"FFmpeg was found at:\n{FAKE_FFMPEG}" in summary
    assert "libx264 encoder is not available" in summary
    assert f"H.264 encoder ({FFMPEG_ENCODER}): NOT available" in report.report_text()


def test_preflight_skips_encode_tests_but_still_probes_ffmpeg(settings, root):
    calls: list = []

    report = validate_preview_configuration(
        settings, include_encode_tests=False, **fake_validation_tools(calls)
    )

    assert report.passed is True
    assert report.include_encode_tests is False
    assert step_keys(report) == PREFLIGHT_STEP_KEYS
    assert "image-test" not in call_names(calls)
    assert "video-test" not in call_names(calls)
    assert ("probe", FAKE_FFMPEG) in calls
    assert ("backend",) in calls
    assert report.ffmpeg_version == FAKE_VERSION
    assert report.encoder_available is True
    assert "test encode" not in report.report_text()
    assert root.is_dir() and files_under(root) == []


def test_preflight_helper_runs_validation_without_encode_tests(settings, monkeypatch):
    seen: dict = {}

    def fake_validate(given, **kwargs):
        seen["settings"] = given
        seen["kwargs"] = kwargs
        return "report"

    monkeypatch.setattr(service_module, "validate_preview_configuration", fake_validate)

    assert preflight_preview_configuration(settings) == "report"
    assert seen["settings"] is settings
    assert seen["kwargs"] == {"include_encode_tests": False}


def test_validation_image_backend_unavailable_skips_image_test(settings):
    calls: list = []

    report = validate_preview_configuration(
        settings,
        **fake_validation_tools(calls, backend=(False, "Qt image reader/writer is missing JPEG writing.")),
    )

    assert report.passed is False
    backend = report.step("image-backend")
    assert backend.passed is False
    assert "JPEG writing" in backend.detail
    assert report.step("image-test").skipped is True
    assert "image-test" not in call_names(calls)
    assert report.step("video-test").passed is True, "the video test does not depend on the image backend"
    assert "Image preview backend failed." in report.failure_summary()


def test_validation_image_tester_failure_is_image_test_fail(settings):
    calls: list = []
    error = PreviewError(STAGE_IMAGE_ENCODE, "Could not write preview.", detail="The JPEG encoder reported an error.")

    report = validate_preview_configuration(settings, **fake_validation_tools(calls, image_error=error))

    assert report.passed is False
    step = report.step("image-test")
    assert step.passed is False and step.skipped is False
    assert step.stage == STAGE_IMAGE_ENCODE
    assert "Could not write preview." in step.detail
    assert "JPEG encoder reported an error" in step.detail
    assert report.step("video-test").passed is True
    assert "video-test" in call_names(calls)
    assert "Image preview test encode failed." in report.failure_summary()
    assert "[FAIL] Image preview test encode — Could not write preview." in report.report_text()


def test_validation_video_tester_failure_is_video_test_fail(settings):
    calls: list = []
    error = PreviewError(STAGE_FFMPEG_EXIT, "FFmpeg exited with code 1.", detail="Unknown encoder 'libx264'")

    report = validate_preview_configuration(settings, **fake_validation_tools(calls, video_error=error))

    assert report.passed is False
    step = report.step("video-test")
    assert step.passed is False and step.skipped is False
    assert step.stage == STAGE_FFMPEG_EXIT
    assert "FFmpeg exited with code 1." in step.detail
    assert "Unknown encoder" in step.detail
    assert [key for key in step_keys(report) if not report.step(key).passed] == ["video-test"]
    assert report.first_failure is step
    assert "Video preview test encode failed." in report.failure_summary()
    assert report.failure_summary(heading="Validation failed.").startswith("Validation failed.\n\n")


def test_validation_report_step_lookup_and_status_text():
    passed = ValidationStep("a", "A", True, "")
    failed = ValidationStep("b", "B", False, "why", stage=STAGE_PREVIEW_ROOT)
    skipped = ValidationStep("c", "C", False, "Not run because an earlier check failed.", skipped=True)
    report = PreviewValidationReport(
        passed=False,
        steps=(passed, failed, skipped),
        settings=PreviewSettings(),
        root="",
        free_bytes=None,
        total_bytes=None,
        ffmpeg_path=None,
        ffmpeg_version=None,
        encoder_available=None,
        image_backend=IMAGE_BACKEND_NAME,
        image_profile_id=None,
        video_profile_id=None,
        include_encode_tests=True,
    )

    assert (passed.status_text, failed.status_text, skipped.status_text) == ("PASS", "FAIL", "Not run")
    assert report.step("b") is failed
    assert report.step("missing") is None
    assert report.first_failure is failed, "skipped steps are never the first failure"
    assert "Preview storage directory: Not selected" in report.report_text()
    assert "Image profile: Invalid" in report.report_text()
    assert "[Not run] C — Not run because an earlier check failed." in report.report_text()


def test_validation_real_image_backend_passes_and_leaves_root_empty(settings, root):
    calls: list = []
    tools = fake_validation_tools(calls, ffmpeg_path=None)
    tools.pop("image_backend_check")
    tools.pop("image_tester")

    report = validate_preview_configuration(settings, **tools)

    assert report.step("image-backend").passed is True
    assert IMAGE_BACKEND_NAME in report.step("image-backend").detail
    image_test = report.step("image-test")
    assert image_test.passed is True, image_test.detail
    assert "64x48" in image_test.detail and "quality 82" in image_test.detail
    assert str(root) in image_test.detail
    assert report.passed is False, "FFmpeg is missing in this scenario"
    assert root.is_dir()
    assert files_under(root) == []


def test_validation_real_backends_pass_and_leave_root_empty(real_ffmpeg, root):
    settings = PreviewSettings(enabled=True, root_directory=str(root), ffmpeg_path=real_ffmpeg)

    report = validate_preview_configuration(settings)

    assert report.passed is True, report.report_text()
    assert step_keys(report) == FULL_STEP_KEYS
    assert report.ffmpeg_path == real_ffmpeg
    assert report.ffmpeg_version is not None and report.ffmpeg_version.startswith("ffmpeg version")
    assert report.encoder_available is True
    assert "64x48" in report.step("image-test").detail
    assert FFMPEG_ENCODER in report.step("video-test").detail
    assert "preset veryfast" in report.step("video-test").detail
    assert report.free_bytes is not None and report.free_bytes > 0
    text = report.report_text()
    assert text.startswith("Preview Configuration Test: PASS")
    assert f"FFmpeg path: {real_ffmpeg}" in text
    assert root.is_dir()
    assert files_under(root) == [], "test media must be deleted afterwards"


# ---------------------------------------------------------------------------
# PreviewService
# ---------------------------------------------------------------------------
def test_service_requires_a_valid_configuration():
    with pytest.raises(PreviewConfigError):
        PreviewService(PreviewSettings())


def test_service_unsupported_kind_is_skipped_without_counters(settings, source_file):
    image = FakeGenerator("image")
    video = FakeGenerator("video")
    service = make_service(settings, image=image, video=video)

    result = ensure(service, "audio", digest_of("song"), source=source_file, relative_path="Music/song.mp3")

    assert result.status == PREVIEW_SKIPPED_UNSUPPORTED
    assert result.ok is False
    assert result.media_kind == "audio"
    assert result.profile_id == ""
    assert result.path is None
    assert "images and videos" in result.message
    assert image.calls == [] and video.calls == []
    assert service.statistics == PreviewStatistics(mode=MODE_ENABLED, root=str(settings.root_path))
    assert service.statistics.has_problems is False
    assert status_record_for(result, digest_of("song")) is None


def test_service_generates_image_and_counts_bytes(settings, root, source_file, jpeg_payload):
    image = FakeGenerator("image", payload=jpeg_payload, width=32, height=24)
    cancel = lambda: False  # noqa: E731
    service = make_service(settings, image=image, video=FakeGenerator("video"))
    digest = digest_of("photo")

    result = ensure(service, "image", digest, source=source_file, cancel_callback=cancel)

    expected = root / "images" / IMAGE_PROFILE_ID / digest.hex()[:2] / f"{digest.hex()}.jpg"
    assert result.status == PREVIEW_GENERATED and result.ok
    assert result.path == expected == service.cache.preview_path("image", digest)
    assert result.bytes_written == result.size_bytes == len(jpeg_payload)
    assert (result.width, result.height) == (32, 24)
    assert result.replaced_corrupt is False
    assert expected.read_bytes() == jpeg_payload
    assert len(image.calls) == 1
    call = image.calls[0]
    assert call.source == source_file and call.destination == expected
    assert call.cancel_callback is cancel
    stats = service.statistics
    assert (stats.image_generated, stats.image_reused, stats.image_failed) == (1, 0, 0)
    assert stats.bytes_written == len(jpeg_payload)
    assert stats.failures == [] and stats.has_problems is False
    assert stats.corrupt_replaced == 0 and stats.storage_skipped == 0
    assert service.root == root


def test_service_reuses_existing_valid_preview_with_real_image_generator(settings, root, tmp_path):
    source = write_test_image(tmp_path / "source" / "photo.png", 400, 300, "png")
    digest = hashlib.sha256(source.read_bytes()).digest()
    log: list[str] = []
    service = make_service(settings, video=FakeGenerator("video"), log=log)

    first = ensure(service, "image", digest, source=source, relative_path="Photos/photo.png")
    second = ensure(service, "image", digest, source=source, relative_path="Copies/photo (copy).png")

    assert first.status == PREVIEW_GENERATED
    assert (first.width, first.height) == (400, 300)
    assert first.bytes_written == first.size_bytes > 0
    assert second.status == PREVIEW_REUSED and second.ok
    assert second.path == first.path
    assert second.bytes_written == 0
    assert second.size_bytes == first.size_bytes == first.path.stat().st_size
    assert (second.width, second.height) == (400, 300)
    assert second.duration_ms is None
    assert second.replaced_corrupt is False
    stats = service.statistics
    assert (stats.image_generated, stats.image_reused, stats.image_failed) == (1, 1, 0)
    assert stats.bytes_written == first.bytes_written
    assert stats.corrupt_replaced == 0
    assert files_under(root) == [first.path]
    assert temporaries_under(root) == []
    assert log == [], "reuse is not a problem worth logging"


def test_service_duplicate_hash_across_paths_generates_once_and_reuses(settings, source_file, jpeg_payload):
    image = FakeGenerator("image", payload=jpeg_payload, width=32, height=24)
    service = make_service(settings, image=image, video=FakeGenerator("video"))
    digest = digest_of("duplicate")

    first = ensure(service, "image", digest, source=source_file, relative_path="A/one.jpg")
    second = ensure(service, "image", digest, source=source_file, relative_path="B/two.jpg")

    assert first.status == PREVIEW_GENERATED
    assert second.status == PREVIEW_REUSED
    assert first.path == second.path
    assert len(image.calls) == 1, "one preview per hash and profile"
    assert (service.statistics.image_generated, service.statistics.image_reused) == (1, 1)
    assert service.statistics.bytes_written == len(jpeg_payload)


def test_service_regenerates_corrupt_existing_image_preview(settings, root, source_file, jpeg_payload):
    image = FakeGenerator("image", payload=jpeg_payload, width=32, height=24)
    log: list[str] = []
    service = make_service(settings, image=image, video=FakeGenerator("video"), log=log)
    digest = digest_of("corrupt-image")
    destination = service.cache.preview_path("image", digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"this is not a JPEG preview")

    result = ensure(service, "image", digest, source=source_file)

    assert result.status == PREVIEW_GENERATED
    assert result.replaced_corrupt is True
    assert result.path == destination
    assert destination.read_bytes() == jpeg_payload
    assert len(image.calls) == 1
    assert service.statistics.corrupt_replaced == 1
    assert service.statistics.image_generated == 1
    assert service.statistics.image_reused == 0
    assert len(log) == 1
    assert "failed validation" in log[0] and str(destination) in log[0]
    assert "not a JPEG" in log[0], "the validation reason is recorded in the scan log"


def test_service_regenerates_corrupt_existing_video_preview(settings, source_file):
    video = FakeGenerator("video", payload=tiny_mp4_bytes(), width=64, height=48, duration_ms=3000)
    log: list[str] = []
    service = make_service(settings, image=FakeGenerator("image"), video=video, log=log)
    digest = digest_of("corrupt-video")
    destination = service.cache.preview_path("video", digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(tiny_mp4_bytes()[:200])

    result = ensure(service, "video", digest, source=source_file, relative_path="Videos/clip.mov")

    assert result.status == PREVIEW_GENERATED and result.replaced_corrupt is True
    assert destination.read_bytes() == tiny_mp4_bytes()
    assert (result.width, result.height, result.duration_ms) == (64, 48, 3000)
    assert service.statistics.corrupt_replaced == 1
    assert service.statistics.video_generated == 1
    assert len(log) == 1 and "failed validation" in log[0]


def test_service_corrupt_preview_whose_regeneration_fails_is_not_counted_as_replaced(settings, source_file):
    error = PreviewError(STAGE_IMAGE_DECODE, "Image decoder could not read the file.")
    image = FakeGenerator("image", error=error)
    log: list[str] = []
    service = make_service(settings, image=image, video=FakeGenerator("video"), log=log)
    digest = digest_of("corrupt-then-fail")
    destination = service.cache.preview_path("image", digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"garbage")

    result = ensure(service, "image", digest, source=source_file)

    assert result.status == PREVIEW_FAILED
    assert result.replaced_corrupt is False
    assert len(image.calls) == 1
    assert service.statistics.corrupt_replaced == 0, "nothing was regenerated"
    assert service.statistics.image_failed == 1
    assert len(service.statistics.failures) == 1
    assert any("failed validation" in line for line in log)
    assert any("Offline preview failed" in line for line in log)


def test_service_generator_preview_error_is_recorded_completely(settings, source_file):
    error = PreviewError(
        STAGE_IMAGE_DECODE, "Image decoder could not read the file.", detail="unsupported format: heic"
    )
    image = FakeGenerator("image", error=error)
    log: list[str] = []
    service = make_service(settings, image=image, video=FakeGenerator("video"), log=log)
    digest = digest_of("broken")
    destination = service.cache.preview_path("image", digest)

    result = ensure(service, "image", digest, source=source_file, relative_path="Photos/broken.heic")

    assert result.status == PREVIEW_FAILED and result.ok is False
    assert result.media_kind == "image" and result.profile_id == IMAGE_PROFILE_ID
    assert result.path == destination
    assert result.stage == STAGE_IMAGE_DECODE
    assert result.message == "Image decoder could not read the file."
    assert result.detail == "unsupported format: heic"
    assert result.bytes_written == 0

    stats = service.statistics
    assert (stats.image_generated, stats.image_reused, stats.image_failed) == (0, 0, 1)
    assert stats.total_failed == 1 and stats.has_problems is True
    assert stats.storage_unavailable_reason is None, "a decode failure does not stop the scan"
    assert len(stats.failures) == 1
    failure = stats.failures[0]
    assert failure == PreviewFailure(
        source_name="broken.heic",
        relative_path="Photos/broken.heic",
        volume_id=VOLUME_ID,
        volume_label=VOLUME_LABEL,
        media_kind="image",
        sha256=digest.hex(),
        preview_path=str(destination),
        profile_id=IMAGE_PROFILE_ID,
        stage=STAGE_IMAGE_DECODE,
        message="Image decoder could not read the file.",
        detail="unsupported format: heic",
    )
    assert failure.display_lines() == [
        "Type: Image",
        f"Profile: {IMAGE_PROFILE_ID}",
        "Error: Image decoder could not read the file.",
        "Detail: unsupported format: heic",
    ]
    assert len(log) == 1
    assert f"Offline preview failed ({STAGE_IMAGE_DECODE}) for Photos/broken.heic" in log[0]
    assert not destination.exists()

    # Generation continues for the next file.
    ensure(service, "image", digest_of("another"), source=source_file, relative_path="Photos/other.jpg")
    assert len(image.calls) == 2


@pytest.mark.parametrize(
    ("stage", "message", "detail"),
    [
        (STAGE_DISK_FULL, "Could not write preview.", "No space left on device"),
        (STAGE_PREVIEW_ROOT, "Could not create the preview directory.", "[WinError 3] The system cannot find the path specified"),
    ],
)
def test_service_storage_failure_stops_further_generation(settings, source_file, jpeg_payload, stage, message, detail):
    image = FakeGenerator("image", error=PreviewError(stage, message, detail=detail))
    video = FakeGenerator("video", payload=tiny_mp4_bytes())
    log: list[str] = []
    service = make_service(settings, image=image, video=video, log=log)

    first = ensure(service, "image", digest_of("one"), source=source_file, relative_path="Photos/one.jpg")
    second = ensure(service, "image", digest_of("two"), source=source_file, relative_path="Photos/two.jpg")
    third = ensure(service, "video", digest_of("three"), source=source_file, relative_path="Videos/three.mov")

    assert first.status == PREVIEW_FAILED and first.stage == stage
    reason = f"{message} — {detail}"
    assert service.statistics.storage_unavailable_reason == reason

    for result, kind in ((second, "image"), (third, "video")):
        assert result.status == PREVIEW_SKIPPED_STORAGE
        assert result.ok is False
        assert result.media_kind == kind
        assert result.stage == STAGE_STORAGE_UNAVAILABLE
        assert "storage became unavailable" in result.message
        assert result.detail == reason
        assert result.path == service.cache.preview_path(kind, digest_of({"image": "two", "video": "three"}[kind]))
    assert len(image.calls) == 1, "no further generator calls after storage failed"
    assert video.calls == []

    stats = service.statistics
    assert stats.storage_skipped == 2
    assert stats.image_failed == 1 and stats.video_failed == 0
    assert len(stats.failures) == 1, "skipped candidates must not flood the failure list"
    assert stats.failures[0].stage == stage
    assert stats.has_problems is True
    assert stats.total_attempted == 3
    assert any("no further previews will be" in line for line in log)
    assert not (service.root / "images").exists() or files_under(service.root) == []


def test_service_oserror_enospc_from_generator_is_disk_full(settings, source_file):
    image = FakeGenerator("image", error=OSError(errno.ENOSPC, "No space left on device"))
    service = make_service(settings, image=image, video=FakeGenerator("video"))

    result = ensure(service, "image", digest_of("full"), source=source_file)
    following = ensure(service, "image", digest_of("after"), source=source_file, relative_path="Photos/after.jpg")

    assert result.status == PREVIEW_FAILED
    assert result.stage == STAGE_DISK_FULL
    assert result.message == "Could not write preview."
    assert "No space left on device" in result.detail
    assert service.statistics.storage_unavailable_reason is not None
    assert "No space left on device" in service.statistics.storage_unavailable_reason
    assert following.status == PREVIEW_SKIPPED_STORAGE
    assert len(image.calls) == 1
    assert service.statistics.failures[0].stage == STAGE_DISK_FULL


@pytest.mark.parametrize("media_kind", ["image", "video"])
def test_service_reuses_existing_valid_preview_after_storage_failure(
    settings, root, source_file, media_kind
):
    """Reuse only reads the store, so a full disk must not turn present previews into skips.

    Spec §10 (reuse whenever a valid preview exists, count it) and §17 (only
    *generation* stops; the report distinguishes real skips).  The persisted
    status must describe the file's real state (spec §46).
    """

    image = FakeGenerator(
        "image",
        error=PreviewError(STAGE_DISK_FULL, "Could not write preview.", detail="No space left on device"),
    )
    video = FakeGenerator("video", payload=tiny_mp4_bytes())
    log: list[str] = []
    service = make_service(settings, image=image, video=video, log=log)

    existing_digest = digest_of(f"already-there-{media_kind}")
    existing = service.cache.preview_path(media_kind, existing_digest)
    existing.parent.mkdir(parents=True)
    if media_kind == "image":
        write_test_image(existing, 40, 30, "jpeg")
        expected_geometry = (40, 30, None)
    else:
        existing.write_bytes(tiny_mp4_bytes())
        expected_geometry = (64, 48, 3000)
    before = existing.read_bytes()

    failed = ensure(service, "image", digest_of("fills-the-disk"), source=source_file, relative_path="Photos/big.jpg")
    assert failed.status == PREVIEW_FAILED and failed.stage == STAGE_DISK_FULL
    assert service.statistics.storage_unavailable_reason is not None
    assert len(log) == 2, "the failure and the storage-unavailable notice"

    reused = ensure(
        service,
        media_kind,
        existing_digest,
        source=source_file,
        relative_path="Media/existing.jpg" if media_kind == "image" else "Media/existing.mov",
    )

    assert reused.status == PREVIEW_REUSED and reused.ok
    assert reused.media_kind == media_kind
    assert reused.path == existing
    assert reused.stage is None and reused.message == "" and reused.detail == ""
    assert reused.bytes_written == 0
    assert reused.size_bytes == existing.stat().st_size == len(before)
    assert (reused.width, reused.height, reused.duration_ms) == expected_geometry
    assert reused.replaced_corrupt is False
    assert existing.read_bytes() == before, "reuse never touches the stored file"
    assert len(log) == 2 and not any(str(existing) in line for line in log), "reuse is not logged as a problem"

    record = status_record_for(reused, existing_digest)
    assert record["status"] == DB_STATUS_AVAILABLE, "persisted status matches the file on disk"
    assert record["error_stage"] is None and record["error_message"] == ""
    assert record["preview_size"] == len(before)

    # A candidate with nothing on disk is still an explicit skip, never a generation attempt.
    missing_digest = digest_of("nothing-on-disk")
    missing = ensure(service, media_kind, missing_digest, source=source_file, relative_path="Media/new.bin")
    assert missing.status == PREVIEW_SKIPPED_STORAGE
    assert missing.stage == STAGE_STORAGE_UNAVAILABLE
    assert missing.path == service.cache.preview_path(media_kind, missing_digest)
    assert status_record_for(missing, missing_digest)["status"] == DB_STATUS_MISSING

    stats = service.statistics
    reused_counter = stats.image_reused if media_kind == "image" else stats.video_reused
    other_reused = stats.video_reused if media_kind == "image" else stats.image_reused
    assert reused_counter == 1 and other_reused == 0
    assert stats.storage_skipped == 1, "only the candidate without a preview counts as skipped"
    assert (stats.image_failed, stats.video_failed) == (1, 0)
    assert stats.total_reused == 1 and stats.total_generated == 0 and stats.total_attempted == 3
    assert len(stats.failures) == 1 and stats.failures[0].stage == STAGE_DISK_FULL
    assert stats.has_problems is True
    assert len(image.calls) == 1 and video.calls == [], "no generator runs after storage failed"
    assert "Previews not attempted afterwards: 1" in stats.summary_text(str(root))
    assert files_under(root) == [existing]
    assert temporaries_under(root) == []


def test_service_corrupt_existing_preview_after_storage_failure_is_skipped_not_regenerated(
    settings, root, source_file
):
    """A corrupt preview found after storage failed is logged (spec §11) but not regenerated (spec §17)."""

    image = FakeGenerator(
        "image",
        error=PreviewError(STAGE_DISK_FULL, "Could not write preview.", detail="No space left on device"),
    )
    log: list[str] = []
    service = make_service(settings, image=image, video=FakeGenerator("video"), log=log)
    digest = digest_of("corrupt-after-full")
    destination = service.cache.preview_path("image", digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"garbage")

    ensure(service, "image", digest_of("fills-the-disk"), source=source_file, relative_path="Photos/big.jpg")
    result = ensure(service, "image", digest, source=source_file, relative_path="Photos/corrupt.jpg")

    assert result.status == PREVIEW_SKIPPED_STORAGE
    assert result.stage == STAGE_STORAGE_UNAVAILABLE
    assert result.path == destination
    assert result.replaced_corrupt is False
    assert len(image.calls) == 1, "the corrupt preview is not regenerated while storage is unavailable"
    assert destination.read_bytes() == b"garbage", "nothing is deleted or rewritten"

    stats = service.statistics
    assert stats.corrupt_replaced == 0
    assert stats.storage_skipped == 1
    assert (stats.image_generated, stats.image_reused, stats.image_failed) == (0, 0, 1)
    assert len(stats.failures) == 1, "a skipped corrupt preview does not join the failure list"
    assert any(
        "failed validation" in line and "cannot be regenerated" in line and str(destination) in line
        for line in log
    ), log
    assert not any("will be regenerated" in line for line in log)
    assert status_record_for(result, digest)["status"] == DB_STATUS_MISSING
    assert temporaries_under(root) == []


def test_service_cancellation_is_raised_even_when_storage_is_unavailable(settings, source_file):
    """Cancellation is always raised, never swallowed into a skipped result."""

    image = FakeGenerator(
        "image",
        error=PreviewError(STAGE_DISK_FULL, "Could not write preview.", detail="No space left on device"),
    )
    service = make_service(settings, image=image, video=FakeGenerator("video"))
    ensure(service, "image", digest_of("fills-the-disk"), source=source_file, relative_path="Photos/big.jpg")

    with pytest.raises(PreviewCancelled):
        ensure(service, "image", digest_of("later"), source=source_file, cancel_callback=lambda: True)

    assert service.statistics.storage_skipped == 0
    assert len(image.calls) == 1


def test_service_permission_error_from_generator_is_permission_stage(settings, source_file):
    image = FakeGenerator("image", error=PermissionError(errno.EACCES, "Permission denied"))
    service = make_service(settings, image=image, video=FakeGenerator("video"))
    settings.root_path.mkdir(parents=True)  # the root itself stays healthy: one file was denied

    result = ensure(service, "image", digest_of("denied"), source=source_file)
    following = ensure(service, "image", digest_of("next"), source=source_file, relative_path="Photos/next.jpg")

    assert result.status == PREVIEW_FAILED
    assert result.stage == STAGE_PERMISSION
    assert result.message == "Could not write preview."
    assert "Permission denied" in result.detail
    assert service.statistics.storage_unavailable_reason is None, "a denied file with a healthy root is not a storage failure"
    assert following.status == PREVIEW_FAILED, "generation is still attempted for the next file"
    assert len(image.calls) == 2
    assert service.statistics.image_failed == 2
    assert [failure.stage for failure in service.statistics.failures] == [STAGE_PERMISSION, STAGE_PERMISSION]


def test_service_cancellation_from_generator_propagates_and_leaves_counters_unchanged(settings, source_file):
    image = FakeGenerator("image", error=PreviewCancelled("cancelled"))
    service = make_service(settings, image=image, video=FakeGenerator("video"))

    with pytest.raises(PreviewCancelled):
        ensure(service, "image", digest_of("cancelled"), source=source_file)

    assert len(image.calls) == 1
    assert service.statistics == PreviewStatistics(mode=MODE_ENABLED, root=str(settings.root_path))
    assert service.statistics.failures == []


def test_service_cancel_callback_before_generation_raises_without_calling_generator(settings, source_file):
    image = FakeGenerator("image", payload=b"unused")
    service = make_service(settings, image=image, video=FakeGenerator("video"))

    with pytest.raises(PreviewCancelled):
        ensure(service, "image", digest_of("early"), source=source_file, cancel_callback=lambda: True)

    assert image.calls == []
    assert service.statistics == PreviewStatistics(mode=MODE_ENABLED, root=str(settings.root_path))


@pytest.mark.parametrize("explicit_path", ["", "C:/missing/ffmpeg.exe"])
def test_service_video_without_ffmpeg_fails_with_ffmpeg_start(settings, root, source_file, monkeypatch, explicit_path):
    monkeypatch.setattr(service_module, "find_ffmpeg", lambda explicit: None)
    settings = PreviewSettings(enabled=True, root_directory=str(root), ffmpeg_path=explicit_path)
    image = FakeGenerator("image")
    log: list[str] = []
    service = make_service(settings, image=image, ffmpeg_path=None, log=log)
    digest = digest_of("clip")

    assert service.ffmpeg_path is None
    assert service.video_generator is None

    result = ensure(service, "video", digest, source=source_file, relative_path="Videos/clip.mov")

    assert result.status == PREVIEW_FAILED
    assert result.stage == STAGE_FFMPEG_START
    assert "FFmpeg" in result.message
    assert result.path == service.cache.preview_path("video", digest)
    if explicit_path:
        assert explicit_path in result.detail
    else:
        assert "PATH" in result.detail
    stats = service.statistics
    assert stats.video_failed == 1 and stats.video_generated == 0
    assert len(stats.failures) == 1
    failure = stats.failures[0]
    assert failure.media_kind == "video" and failure.stage == STAGE_FFMPEG_START
    assert failure.profile_id == VIDEO_PROFILE_ID
    assert failure.relative_path == "Videos/clip.mov"
    assert failure.sha256 == digest.hex()
    assert stats.storage_unavailable_reason is None
    assert image.calls == []
    assert any(STAGE_FFMPEG_START in line for line in log)


def test_service_video_without_ffmpeg_still_reuses_an_existing_valid_preview(settings, source_file, monkeypatch):
    monkeypatch.setattr(service_module, "find_ffmpeg", lambda explicit: None)
    service = make_service(settings, image=FakeGenerator("image"), ffmpeg_path=None)
    digest = digest_of("existing-video")
    destination = service.cache.preview_path("video", digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(tiny_mp4_bytes())

    result = ensure(service, "video", digest, source=source_file, relative_path="Videos/clip.mov")

    assert result.status == PREVIEW_REUSED
    assert (result.width, result.height, result.duration_ms) == (64, 48, 3000)
    assert result.size_bytes == len(tiny_mp4_bytes())
    assert service.statistics.video_reused == 1 and service.statistics.video_failed == 0


def test_service_video_progress_adapter_formats_percentages(settings, source_file):
    video = FakeGenerator(
        "video",
        payload=tiny_mp4_bytes(),
        width=64,
        height=48,
        duration_ms=3000,
        progress=((0.0, 0), (0.42, 1260), (0.5, 1500), (1.0, 3000), (None, 5000), (None, None)),
    )
    service = make_service(settings, image=FakeGenerator("image"), video=video)
    texts: list[str] = []
    cancel = lambda: False  # noqa: E731

    result = ensure(
        service,
        "video",
        digest_of("holiday"),
        source=source_file,
        relative_path="Videos/Holiday-2008.mov",
        cancel_callback=cancel,
        progress_callback=texts.append,
        expected_duration_ms=3000,
    )

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height, result.duration_ms) == (64, 48, 3000)
    assert texts == [
        "0% of preview encode",
        "42% of preview encode",
        "50% of preview encode",
        "100% of preview encode",
        "5 s of video encoded",
        "encoding preview",
    ]
    call = video.calls[0]
    assert call.expected_duration_ms == 3000
    assert call.cancel_callback is cancel
    assert callable(call.progress_callback)
    stats = service.statistics
    assert stats.video_generated == 1
    assert stats.bytes_written == len(tiny_mp4_bytes())


def test_service_video_progress_adapter_clamps_out_of_range_fractions(settings, source_file):
    video = FakeGenerator("video", payload=tiny_mp4_bytes(), progress=((-0.5, 0), (1.7, 9000)))
    service = make_service(settings, image=FakeGenerator("image"), video=video)
    texts: list[str] = []

    ensure(service, "video", digest_of("clamped"), source=source_file, progress_callback=texts.append, expected_duration_ms=3000)

    assert texts == ["0% of preview encode", "100% of preview encode"]


def test_service_without_progress_callback_passes_none_to_the_generator(settings, source_file):
    video = FakeGenerator("video", payload=tiny_mp4_bytes())
    service = make_service(settings, image=FakeGenerator("image"), video=video)

    ensure(service, "video", digest_of("quiet"), source=source_file)

    assert video.calls[0].progress_callback is None
    assert video.calls[0].expected_duration_ms is None


def test_service_invalid_hash_is_a_configuration_failure_without_a_path(settings, source_file):
    image = FakeGenerator("image", payload=b"unused")
    service = make_service(settings, image=image, video=FakeGenerator("video"))

    result = ensure(service, "image", b"too short", source=source_file, relative_path="Photos/odd.jpg")

    assert result.status == PREVIEW_FAILED
    assert result.stage == STAGE_CONFIGURATION
    assert result.path is None
    assert image.calls == []
    assert service.statistics.image_failed == 1
    failure = service.statistics.failures[0]
    assert failure.sha256 is None and failure.preview_path is None
    assert failure.profile_id == IMAGE_PROFILE_ID
    assert failure.relative_path == "Photos/odd.jpg"


def test_service_accepts_hex_hash_text(settings, source_file, jpeg_payload):
    image = FakeGenerator("image", payload=jpeg_payload)
    service = make_service(settings, image=image, video=FakeGenerator("video"))
    digest = digest_of("hex")

    result = ensure(service, "image", digest.hex().upper(), source=source_file)

    assert result.status == PREVIEW_GENERATED
    assert result.path == service.cache.preview_path("image", digest)


# ---------------------------------------------------------------------------
# status_record_for / hash_unavailable_status_record
# ---------------------------------------------------------------------------
def test_status_record_for_generated_and_reused_are_available():
    digest = digest_of("record")
    generated = PreviewResult(
        PREVIEW_GENERATED, "image", IMAGE_PROFILE_ID,
        path=Path("E:/previews/images/x/ab/abab.jpg"), bytes_written=1234, size_bytes=1234, width=1600, height=1067,
    )
    reused = PreviewResult(
        PREVIEW_REUSED, "video", VIDEO_PROFILE_ID,
        path=Path("E:/previews/videos/x/ab/abab.mp4"), size_bytes=98765, width=426, height=240, duration_ms=3000,
    )

    generated_record = status_record_for(generated, digest)
    reused_record = status_record_for(reused, bytearray(digest))

    assert generated_record["status"] == DB_STATUS_AVAILABLE
    assert generated_record["media_kind"] == "image"
    assert generated_record["profile_id"] == IMAGE_PROFILE_ID
    assert generated_record["source_hash"] == digest and isinstance(generated_record["source_hash"], bytes)
    assert generated_record["preview_size"] == 1234
    assert (generated_record["preview_width"], generated_record["preview_height"]) == (1600, 1067)
    assert generated_record["preview_duration_ms"] is None
    assert isinstance(generated_record["generated_at"], str) and generated_record["generated_at"]
    assert generated_record["error_stage"] is None
    assert generated_record["error_message"] == ""
    assert "path" not in generated_record, "absolute preview paths are never persisted (spec §33)"

    assert reused_record["status"] == DB_STATUS_AVAILABLE
    assert reused_record["media_kind"] == "video"
    assert reused_record["profile_id"] == VIDEO_PROFILE_ID
    assert reused_record["source_hash"] == digest and isinstance(reused_record["source_hash"], bytes)
    assert reused_record["preview_size"] == 98765
    assert (reused_record["preview_width"], reused_record["preview_height"]) == (426, 240)
    assert reused_record["preview_duration_ms"] == 3000
    for record in (generated_record, reused_record):
        assert record["status"] in PREVIEW_STATUS_VALUES


def test_status_record_for_failed_includes_stage_and_detail():
    digest = digest_of("failed")
    failed = PreviewResult(
        PREVIEW_FAILED, "video", VIDEO_PROFILE_ID,
        stage=STAGE_FFMPEG_EXIT, message="FFmpeg exited with code 1.", detail="Invalid data found when processing input",
    )
    without_detail = PreviewResult(
        PREVIEW_FAILED, "image", IMAGE_PROFILE_ID, stage=STAGE_IMAGE_DECODE, message="Image decoder could not read the file."
    )

    record = status_record_for(failed, digest)
    plain = status_record_for(without_detail, None)

    assert record["status"] == DB_STATUS_FAILED and record["status"] in PREVIEW_STATUS_VALUES
    assert record["media_kind"] == "video" and record["profile_id"] == VIDEO_PROFILE_ID
    assert record["source_hash"] == digest
    assert record["error_stage"] == STAGE_FFMPEG_EXIT
    assert record["error_message"] == "FFmpeg exited with code 1. — Invalid data found when processing input"
    assert "generated_at" not in record
    assert plain["error_stage"] == STAGE_IMAGE_DECODE
    assert plain["error_message"] == "Image decoder could not read the file."
    assert plain["source_hash"] is None


def test_status_record_for_storage_skipped_is_missing_with_storage_stage():
    digest = digest_of("skipped")
    skipped = PreviewResult(
        PREVIEW_SKIPPED_STORAGE, "image", IMAGE_PROFILE_ID,
        stage=STAGE_STORAGE_UNAVAILABLE,
        message="Not attempted because preview storage became unavailable earlier in this scan.",
        detail="Could not write preview. — No space left on device",
    )

    record = status_record_for(skipped, digest)

    assert record["status"] == DB_STATUS_MISSING and record["status"] in PREVIEW_STATUS_VALUES
    assert record["error_stage"] == STAGE_STORAGE_UNAVAILABLE == "storage-unavailable"
    assert "storage became unavailable" in record["error_message"]
    assert "No space left on device" in record["error_message"]
    assert record["source_hash"] == digest
    assert record["media_kind"] == "image" and record["profile_id"] == IMAGE_PROFILE_ID


def test_status_record_for_unsupported_and_disabled():
    audio = PreviewResult(PREVIEW_SKIPPED_UNSUPPORTED, "audio", "", message="Offline previews are generated for images and videos only.")
    image = PreviewResult(PREVIEW_SKIPPED_UNSUPPORTED, "image", "", stage=STAGE_IMAGE_DECODE, message="RAW is not supported.")
    disabled = PreviewResult(PREVIEW_SKIPPED_DISABLED, "image", IMAGE_PROFILE_ID)

    assert status_record_for(audio, digest_of("a")) is None
    record = status_record_for(image, digest_of("i"))
    assert record["status"] == DB_STATUS_UNSUPPORTED and record["status"] in PREVIEW_STATUS_VALUES
    assert record["profile_id"] == "-", "an empty profile falls back to a placeholder"
    assert record["error_stage"] == STAGE_IMAGE_DECODE
    assert record["error_message"] == "RAW is not supported."
    assert status_record_for(disabled, digest_of("d")) is None


def test_hash_unavailable_status_record():
    record = hash_unavailable_status_record("video", VIDEO_PROFILE_ID)

    assert record["media_kind"] == "video"
    assert record["profile_id"] == VIDEO_PROFILE_ID
    assert record["status"] == DB_STATUS_MISSING and record["status"] in PREVIEW_STATUS_VALUES
    assert record["source_hash"] is None
    assert record["error_stage"] == STAGE_HASH_UNAVAILABLE
    assert "SHA-256" in record["error_message"]


# ---------------------------------------------------------------------------
# scan_outcome / preview_warning_message
# ---------------------------------------------------------------------------
def test_scan_outcome_downgrades_completed_only_when_previews_had_problems():
    with_failures = PreviewStatistics(image_failed=1, failures=[make_failure()])
    storage_only = PreviewStatistics(storage_skipped=5)
    reason_only = PreviewStatistics(storage_unavailable_reason="No space left on device")
    clean = PreviewStatistics(image_generated=10, video_reused=3)

    assert scan_outcome("completed", with_failures) == SCAN_OUTCOME_COMPLETED_WITH_WARNINGS == "completed_with_warnings"
    assert scan_outcome("completed", storage_only) == "completed_with_warnings"
    assert scan_outcome("completed", reason_only) == "completed_with_warnings"
    assert scan_outcome("completed", clean) == "completed"
    assert scan_outcome("completed", None) == "completed"
    assert scan_outcome("cancelled", with_failures) == "cancelled"
    assert scan_outcome("failed", with_failures) == "failed"
    assert scan_outcome("completed", disabled_statistics()) == "completed"


def test_scan_outcome_accepts_persisted_mappings():
    with_failures = PreviewStatistics(video_failed=2, failures=[make_failure(), make_failure()]).as_dict()
    storage_only = PreviewStatistics(storage_skipped=1).as_dict()
    clean = PreviewStatistics(image_generated=4).as_dict()

    assert scan_outcome("completed", with_failures) == "completed_with_warnings"
    assert scan_outcome("completed", storage_only) == "completed_with_warnings"
    assert scan_outcome("completed", {"storage_unavailable_reason": "disk full"}) == "completed_with_warnings"
    assert scan_outcome("completed", clean) == "completed"
    assert scan_outcome("completed", {}) == "completed"
    assert scan_outcome("cancelled", with_failures) == "cancelled"


def test_preview_warning_message_wording():
    assert preview_warning_message(PreviewStatistics()) == ""
    assert preview_warning_message(PreviewStatistics(image_generated=5, video_reused=2)) == ""
    assert preview_warning_message(PreviewStatistics(image_failed=1)) == (
        "Catalogue indexing succeeded, but 1 offline preview was not created."
    )
    assert preview_warning_message(PreviewStatistics(image_failed=2, video_failed=1)) == (
        "Catalogue indexing succeeded, but 3 offline previews were not created."
    )
    assert preview_warning_message(PreviewStatistics(storage_skipped=1)) == (
        "Catalogue indexing succeeded, but 1 preview was not attempted because preview storage became unavailable."
    )
    assert preview_warning_message(PreviewStatistics(video_failed=2, storage_skipped=1234)) == (
        "Catalogue indexing succeeded, but 2 offline previews were not created and "
        "1,234 previews were not attempted because preview storage became unavailable."
    )


# ---------------------------------------------------------------------------
# PreviewStatistics
# ---------------------------------------------------------------------------
def test_statistics_totals_and_problem_detection():
    stats = PreviewStatistics(
        image_generated=3, image_reused=4, image_failed=1,
        video_generated=5, video_reused=6, video_failed=2,
        storage_skipped=7,
    )

    assert stats.total_generated == 8
    assert stats.total_reused == 10
    assert stats.total_failed == 3
    assert stats.total_attempted == 28
    assert stats.has_problems is True
    assert PreviewStatistics().has_problems is False
    assert PreviewStatistics(storage_skipped=1).has_problems is True
    assert PreviewStatistics(storage_unavailable_reason="gone").has_problems is True
    assert PreviewStatistics(corrupt_replaced=3, image_generated=3).has_problems is False


def test_statistics_round_trip_through_dict_including_failures():
    stats = PreviewStatistics(
        mode=MODE_ENABLED,
        image_generated=1284, image_reused=8921, image_failed=3,
        video_generated=312, video_reused=106, video_failed=2,
        bytes_written=18_700_000_000, storage_skipped=40, corrupt_replaced=2,
        storage_unavailable_reason="Could not write preview. — No space left on device",
        message="note",
        failures=[make_failure(), make_failure(media_kind="image", detail="", stage=STAGE_IMAGE_DECODE, sha256=None, preview_path=None)],
    )

    values = stats.as_dict()
    restored = PreviewStatistics.from_dict(values)

    assert json.loads(json.dumps(values)) == values, "as_dict must be JSON-safe"
    assert values["failures"][0] == make_failure().as_dict()
    assert restored == stats
    assert restored.failures[1].sha256 is None and restored.failures[1].preview_path is None
    assert restored.bytes_written == 18_700_000_000
    assert restored.mode == MODE_ENABLED


def test_statistics_from_dict_tolerates_missing_and_malformed_values():
    empty = PreviewStatistics.from_dict(None)
    partial = PreviewStatistics.from_dict({"image_failed": "2", "failures": ["junk", None, make_failure().as_dict()], "storage_unavailable_reason": ""})

    assert empty.mode == MODE_DISABLED
    assert empty.total_attempted == 0 and empty.failures == []
    assert partial.image_failed == 2
    assert partial.failures == [make_failure()]
    assert partial.storage_unavailable_reason is None


def test_statistics_factories_use_known_modes():
    disabled = disabled_statistics("previews off")
    skipped = skipped_preflight_statistics("FFmpeg could not be found.")

    assert disabled.mode == MODE_DISABLED and disabled.message == "previews off"
    assert skipped.mode == MODE_SKIPPED_PREFLIGHT
    assert "preflight" in skipped.message and "FFmpeg could not be found." in skipped.message
    assert "user chose to continue" in skipped.message
    assert {MODE_DISABLED, MODE_ENABLED, MODE_SKIPPED_PREFLIGHT} <= PREVIEW_SCAN_MODES
    assert PreviewStatistics().mode == MODE_ENABLED


def test_summary_text_follows_the_spec_layout():
    stats = PreviewStatistics(
        image_generated=1284, image_reused=8921, image_failed=3,
        video_generated=312, video_reused=106, video_failed=2,
        bytes_written=18_700_000_000,
    )

    text = stats.summary_text("E:\\JVVV Previews")
    lines = text.splitlines()

    assert lines[0] == "Offline Preview Summary"
    assert "Images" in lines and "Videos" in lines
    images = lines.index("Images")
    videos = lines.index("Videos")
    storage = lines.index("Preview storage:")
    space = lines.index("Space used by previews created this scan:")
    assert images < videos < storage < space
    assert re.fullmatch(r"  Generated:\s+1,284", lines[images + 1])
    assert re.fullmatch(r"  Reused:\s+8,921", lines[images + 2])
    assert re.fullmatch(r"  Failed:\s+3", lines[images + 3])
    assert re.fullmatch(r"  Generated:\s+312", lines[videos + 1])
    assert re.fullmatch(r"  Reused:\s+106", lines[videos + 2])
    assert re.fullmatch(r"  Failed:\s+2", lines[videos + 3])
    assert lines[storage + 1] == "  E:\\JVVV Previews"
    assert lines[space + 1] == "  18.7 GB"
    assert "storage became unavailable" not in text
    assert "regenerated" not in text
    assert "Not configured" in PreviewStatistics().summary_text(None)


def test_summary_text_adds_storage_and_corrupt_lines_when_applicable():
    stats = PreviewStatistics(
        image_generated=10, image_failed=1, storage_skipped=40, corrupt_replaced=2,
        storage_unavailable_reason="Could not write preview. — No space left on device",
    )

    text = stats.summary_text("E:\\JVVV Previews")

    assert "Preview generation stopped because preview storage became unavailable." in text
    assert "Previews not attempted afterwards: 40" in text
    assert "Reason: Could not write preview. — No space left on device" in text
    assert "Existing previews that failed validation and were regenerated:\n  2" in text
    assert text.index("Space used by previews created this scan:") < text.index("Preview generation stopped")

    skipped_only = PreviewStatistics(storage_skipped=3).summary_text(None)
    assert "Previews not attempted afterwards: 3" in skipped_only
    assert "Reason:" not in skipped_only


# ---------------------------------------------------------------------------
# preview_cache_for / inspect_preview_file
# ---------------------------------------------------------------------------
def test_preview_cache_for_returns_none_without_root_or_with_invalid_profiles(root):
    assert preview_cache_for(PreviewSettings()) is None
    assert preview_cache_for(PreviewSettings(root_directory="   ")) is None
    assert preview_cache_for(PreviewSettings(root_directory=str(root), image=ImagePreviewProfile(max_dimension=10))) is None
    assert preview_cache_for(PreviewSettings(root_directory=str(root), video=VideoPreviewProfile(fps=0))) is None

    cache = preview_cache_for(PreviewSettings(root_directory=str(root)))

    assert isinstance(cache, PreviewCache)
    assert cache.root == root
    assert cache.profile_id("image") == IMAGE_PROFILE_ID
    assert cache.profile_id("video") == VIDEO_PROFILE_ID
    assert not root.exists(), "looking up the cache must not create directories"


def test_inspect_preview_file_returns_none_when_no_path_can_be_derived(settings):
    digest = digest_of("x")

    assert inspect_preview_file(PreviewSettings(), "image", digest) is None
    assert inspect_preview_file(settings, "image", None) is None
    assert inspect_preview_file(settings, "audio", digest) is None
    assert inspect_preview_file(settings, "image", "not-a-hash") is None
    assert inspect_preview_file(
        PreviewSettings(root_directory=settings.root_directory, image=ImagePreviewProfile(jpeg_quality=1)), "image", digest
    ) is None


def test_inspect_preview_file_reports_missing_preview(settings, root):
    digest = digest_of("missing")

    info = inspect_preview_file(settings, "image", digest)

    assert info is not None
    assert info.exists is False and info.valid is False
    assert info.path == root / "images" / IMAGE_PROFILE_ID / digest.hex()[:2] / f"{digest.hex()}.jpg"
    assert info.media_kind == "image" and info.profile_id == IMAGE_PROFILE_ID
    assert info.size_bytes == 0
    assert (info.width, info.height, info.duration_ms) == (None, None, None)
    assert "does not exist" in info.message
    assert not root.exists()


def test_inspect_preview_file_validates_a_real_image_preview(settings, root):
    digest = digest_of("valid-image")
    cache = PreviewCache(root, settings.image, settings.video)
    destination = cache.preview_path("image", digest)
    write_test_image(destination, 120, 80, "jpeg")

    by_bytes = inspect_preview_file(settings, "image", digest)
    by_hex = inspect_preview_file(settings, "image", digest.hex().upper())

    assert by_bytes is not None and by_bytes.exists is True and by_bytes.valid is True
    assert by_bytes.path == destination
    assert by_bytes.size_bytes == destination.stat().st_size > 0
    assert (by_bytes.width, by_bytes.height) == (120, 80)
    assert by_bytes.duration_ms is None
    assert by_bytes.message == ""
    assert by_hex == by_bytes


def test_inspect_preview_file_flags_garbage_image_as_invalid(settings, root):
    digest = digest_of("garbage-image")
    destination = PreviewCache(root, settings.image, settings.video).preview_path("image", digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"garbage garbage garbage")

    info = inspect_preview_file(settings, "image", digest)

    assert info.exists is True and info.valid is False
    assert info.size_bytes == len(b"garbage garbage garbage")
    assert (info.width, info.height) == (None, None)
    assert info.message and "JPEG" in info.message


def test_inspect_preview_file_validates_video_previews(settings, root):
    cache = PreviewCache(root, settings.image, settings.video)
    valid_digest = digest_of("valid-video")
    valid_path = cache.preview_path("video", valid_digest)
    valid_path.parent.mkdir(parents=True)
    valid_path.write_bytes(tiny_mp4_bytes())
    broken_digest = digest_of("broken-video")
    broken_path = cache.preview_path("video", broken_digest)
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_bytes(b"\x00" * 300)

    valid = inspect_preview_file(settings, "video", valid_digest)
    broken = inspect_preview_file(settings, "video", broken_digest)

    assert valid.exists and valid.valid
    assert valid.profile_id == VIDEO_PROFILE_ID and valid.path == valid_path
    assert (valid.width, valid.height, valid.duration_ms) == (64, 48, 3000)
    assert valid.size_bytes == len(tiny_mp4_bytes())
    assert valid.message == ""
    assert broken.exists is True and broken.valid is False
    assert broken.size_bytes == 300
    assert broken.duration_ms is None
    assert "MP4" in broken.message


class _ExplodingGenerator:
    """A backend with a programming error: raises something that is not a PreviewError."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, source, destination, **_kwargs):
        self.calls += 1
        raise RuntimeError("Qt binding exploded")


def test_service_unexpected_generator_exception_is_a_recorded_failure(settings, source_file):
    from jvvv.preview_cache import PREVIEW_FAILED, STAGE_UNEXPECTED_ERROR

    generator = _ExplodingGenerator()
    service = PreviewService(settings, image_generator=generator, video_generator=object())

    result = service.ensure_preview(
        media_kind="image",
        source=source_file,
        content_hash=digest_of("boom"),
        relative_path="Photos/boom.png",
        source_name="boom.png",
        volume_id=1,
        volume_label="AID-001 - Test",
    )

    # Spec §16 / §51-F: a preview failure never aborts the scan, so even a
    # programming error inside a backend is an explicit per-file failure.
    assert result.status == PREVIEW_FAILED
    assert result.stage == STAGE_UNEXPECTED_ERROR
    assert "RuntimeError" in result.detail and "Qt binding exploded" in result.detail
    assert service.statistics.image_failed == 1
    assert service.statistics.failures[0].stage == STAGE_UNEXPECTED_ERROR
    assert service.statistics.failures[0].relative_path == "Photos/boom.png"
    assert generator.calls == 1

    # Storage is still available, so later candidates are still attempted.
    second = service.ensure_preview(
        media_kind="image",
        source=source_file,
        content_hash=digest_of("boom-2"),
        relative_path="Photos/boom-2.png",
        source_name="boom-2.png",
        volume_id=1,
    )
    assert second.status == PREVIEW_FAILED
    assert generator.calls == 2
    assert service.statistics.storage_skipped == 0
    assert service.statistics.image_failed == 2


def test_status_record_for_ignores_a_non_sha256_source_hash():
    from jvvv.preview_cache import PREVIEW_FAILED, PreviewResult

    result = PreviewResult(
        status=PREVIEW_FAILED,
        media_kind="image",
        profile_id="jpeg-max1600-q82",
        stage="configuration",
        message="bad hash",
    )

    record = status_record_for(result, b"short")

    assert record is not None
    assert record["status"] == "failed"
    assert record["source_hash"] is None
    assert status_record_for(result, digest_of("real"))["source_hash"] == digest_of("real")


# ---------------------------------------------------------------------------
# Audit follow-ups (spec §2, §3, §9, §15, §17)
# ---------------------------------------------------------------------------


def test_failure_summary_names_every_failed_check(settings):
    calls: list = []
    tools = fake_validation_tools(
        calls,
        backend=(False, "rawpy (LibRaw camera RAW) is not installed"),
        capabilities=RuntimeError("ffmpeg crashed while reporting its version"),
    )

    report = validate_preview_configuration(settings, **tools)
    summary = report.failure_summary()

    assert [step.key for step in report.failures] == ["image-backend", "ffmpeg-version"]
    assert summary.startswith("Offline previews could not be enabled.")
    assert "Image preview backend failed." in summary
    assert "rawpy (LibRaw camera RAW) is not installed" in summary
    assert "FFmpeg version failed." in summary
    assert "ffmpeg crashed while reporting its version" in summary


def test_statistics_round_trip_hash_unavailable_count_and_root():
    stats = PreviewStatistics(mode=MODE_ENABLED, hash_unavailable=2, root=r"E:\Previews", image_generated=1)

    assert PreviewStatistics.from_dict(stats.as_dict()) == stats
    text = stats.summary_text(stats.root)
    assert "Images/videos not attempted because no SHA-256 could be recorded:" in text
    assert "\n  2" in text
    assert PreviewStatistics.from_dict({}).hash_unavailable == 0
    assert PreviewStatistics.from_dict({}).root == ""


def test_preview_report_message_covers_failures_regenerations_and_unhashed_files():
    from jvvv.preview_service import preview_report_message

    stats = PreviewStatistics(mode=MODE_ENABLED, image_failed=1, corrupt_replaced=2, hash_unavailable=1)

    message = preview_report_message(stats)

    assert message.startswith("Catalogue indexing succeeded, but 1 offline preview was not created.")
    assert "2 existing previews failed validation and were regenerated." in message
    assert "1 image/video file had no SHA-256 recorded, so no preview was attempted." in message
    assert preview_report_message(PreviewStatistics(mode=MODE_ENABLED)) == ""
    single = preview_report_message(PreviewStatistics(mode=MODE_ENABLED, corrupt_replaced=1))
    assert single == "1 existing preview failed validation and was regenerated."


def test_service_records_the_root_it_writes_to(settings):
    service = make_service(settings, image=FakeGenerator("image"), video=FakeGenerator("video"))

    assert service.statistics.root == str(settings.root_path)
    assert PreviewStatistics.from_dict(service.statistics.as_dict()).root == str(settings.root_path)


def test_service_forwards_the_hash_time_snapshot_to_the_generators(settings, source_file, jpeg_payload):
    image = FakeGenerator("image", payload=jpeg_payload)
    service = make_service(settings, image=image)
    snapshot = os.lstat(source_file)

    ensure(service, "image", digest_of("snap"), source=source_file, source_stat=snapshot)

    assert image.calls[0].source_stat is snapshot


def test_service_write_failure_with_a_vanished_root_stops_further_generation(settings, source_file):
    """Spec §17: a root that disappeared mid-scan must not flood the report with identical failures."""

    image = FakeGenerator(
        "image",
        error=PreviewError(STAGE_PERMISSION, "Could not write preview.", detail="[WinError 5] Access is denied"),
    )
    log: list[str] = []
    service = make_service(settings, image=image, log=log)
    assert not settings.root_path.exists()

    first = ensure(service, "image", digest_of("one"), source=source_file, relative_path="Photos/one.jpg")
    second = ensure(service, "image", digest_of("two"), source=source_file, relative_path="Photos/two.jpg")

    assert first.status == PREVIEW_FAILED and first.stage == STAGE_PERMISSION
    assert second.status == PREVIEW_SKIPPED_STORAGE
    assert len(image.calls) == 1
    assert service.statistics.image_failed == 1 and service.statistics.storage_skipped == 1
    assert "does not exist" in (service.statistics.storage_unavailable_reason or "")
    assert any("no longer writable" in line for line in log)


def test_service_write_failure_with_a_healthy_root_does_not_stop_generation(settings, source_file, jpeg_payload):
    settings.root_path.mkdir(parents=True)
    failing = FakeGenerator(
        "image",
        error=PreviewError(STAGE_IMAGE_ENCODE, "Could not write preview.", detail="encoder error -2"),
    )
    service = make_service(settings, image=failing)

    first = ensure(service, "image", digest_of("one"), source=source_file, relative_path="Photos/one.jpg")
    second = ensure(service, "image", digest_of("two"), source=source_file, relative_path="Photos/two.jpg")

    assert first.status == second.status == PREVIEW_FAILED
    assert len(failing.calls) == 2
    assert service.statistics.storage_unavailable_reason is None
    assert service.statistics.storage_skipped == 0
    assert temporaries_under(settings.root_path) == []


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows concept")
def test_service_with_an_unreachable_drive_stops_after_the_first_real_os_error(tmp_path):
    used = {drive[0].upper() for drive in os.listdrives()}
    free = next((letter for letter in "QRSTUVWXYZ" if letter not in used), None)
    if free is None:
        pytest.skip("every candidate drive letter is in use")
    settings = PreviewSettings(enabled=True, root_directory=f"{free}:\\jvvv-previews")
    service = PreviewService(settings, ffmpeg_path=FAKE_FFMPEG)  # real image generator
    first_source = write_test_image(tmp_path / "one.png", 32, 24, "png")
    second_source = write_test_image(tmp_path / "two.png", 32, 24, "png")

    first = ensure(service, "image", digest_of("one"), source=first_source, relative_path="one.png")
    second = ensure(service, "image", digest_of("two"), source=second_source, relative_path="two.png")

    assert first.status == PREVIEW_FAILED and first.stage == STAGE_PREVIEW_ROOT
    assert "[WinError" in (first.detail or "")
    assert second.status == PREVIEW_SKIPPED_STORAGE
    assert service.statistics.image_failed == 1 and service.statistics.storage_skipped == 1
    assert "[WinError" in (service.statistics.storage_unavailable_reason or "")


def test_inspect_preview_file_validates_once_per_file_identity(settings, monkeypatch):
    cache = PreviewCache(settings.root_path, settings.image, settings.video)
    digest = digest_of("cached")
    path = cache.preview_path("image", digest)
    cache.ensure_parent(path)
    write_test_image(path, 40, 30, "jpeg")
    calls: list[Path] = []
    real_validate = service_module.validate_image_preview

    def counting_validate(target):
        calls.append(Path(target))
        return real_validate(target)

    monkeypatch.setattr(service_module, "validate_image_preview", counting_validate)
    service_module._validated_preview.cache_clear()

    first = inspect_preview_file(settings, "image", digest)
    second = inspect_preview_file(settings, "image", digest)
    assert first is not None and first.valid and second is not None and second.valid
    assert len(calls) == 1, "the same unchanged file is validated once"

    write_test_image(path, 60, 40, "jpeg")  # replaced on disk: size differs -> re-validated
    third = inspect_preview_file(settings, "image", digest)
    assert third is not None and (third.width, third.height) == (60, 40)
    assert len(calls) == 2

    path.write_bytes(b"corrupt" * 100)
    fourth = inspect_preview_file(settings, "image", digest)
    assert fourth is not None and fourth.exists and fourth.valid is False
    assert len(calls) == 3
