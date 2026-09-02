from __future__ import annotations

import errno
import hashlib
import itertools
import json
import os
import time
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from preview_fixtures import tiny_mp4_bytes  # noqa: E402

from jvvv import preview_cache as cache_module  # noqa: E402
from jvvv.preview_cache import (  # noqa: E402
    PREVIEW_FAILED,
    PREVIEW_GENERATED,
    PREVIEW_REUSED,
    PREVIEW_SKIPPED_DISABLED,
    PREVIEW_SKIPPED_STORAGE,
    PREVIEW_SKIPPED_UNSUPPORTED,
    PREVIEW_STATUSES,
    PROGRESS_INTERVAL,
    STAGE_CONFIGURATION,
    STAGE_DISK_FULL,
    STAGE_FFMPEG_EXIT,
    STAGE_IMAGE_DECODE,
    STAGE_PERMISSION,
    STAGE_PREVIEW_ROOT,
    STAGE_RENAME,
    STORAGE_STAGES,
    PreviewCache,
    PreviewCancelled,
    PreviewEntry,
    PreviewError,
    PreviewFailure,
    PreviewResult,
    PreviewStoreStatistics,
    ProfileStatistics,
    RootValidation,
    classify_os_error,
    is_disk_full_error,
    is_permission_error,
    os_error_detail,
    sha256_hex,
)
from jvvv.preview_config import (  # noqa: E402
    ImagePreviewProfile,
    PreviewConfigError,
    VideoPreviewProfile,
)
from jvvv.utils import format_size  # noqa: E402


IMAGE_PROFILE_ID = "jpeg-max1600-q82"
VIDEO_PROFILE_ID = "h264-1fps-240p-crf35-veryfast"
OTHER_IMAGE_PROFILE_ID = "jpeg-max1024-q75"
FIVE_TIB = 5 * 1024**4


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_cache(root: Path) -> PreviewCache:
    return PreviewCache(root, ImagePreviewProfile(), VideoPreviewProfile())


def hex_for(seed: object) -> str:
    return hashlib.sha256(str(seed).encode("utf-8")).hexdigest()


def digest_for(seed: object) -> bytes:
    return hashlib.sha256(str(seed).encode("utf-8")).digest()


def temporary_files_under(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and PreviewCache.is_temporary_name(path.name)
    ]


def populate(
    root: Path,
    kind_dir: str,
    profile_id: str,
    hashes: list[str],
    extension: str,
    *,
    size: int = 0,
) -> list[Path]:
    written: list[Path] = []
    for digest in hashes:
        path = root / kind_dir / profile_id / digest[:2] / f"{digest}.{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        written.append(path)
    return written


class WinError(OSError):
    """OSError carrying a Windows error code on any platform."""

    def __init__(self, winerror: int, message: str = "windows failure") -> None:
        super().__init__(0, message)
        self._winerror = winerror

    @property
    def winerror(self) -> int:  # type: ignore[override]
        return self._winerror


# ---------------------------------------------------------------------------
# constants, exceptions, classification
# ---------------------------------------------------------------------------


def test_status_and_stage_constants():
    assert PREVIEW_STATUSES == frozenset(
        {
            PREVIEW_GENERATED,
            PREVIEW_REUSED,
            PREVIEW_FAILED,
            PREVIEW_SKIPPED_DISABLED,
            PREVIEW_SKIPPED_UNSUPPORTED,
            PREVIEW_SKIPPED_STORAGE,
        }
    )
    assert PREVIEW_SKIPPED_STORAGE == "skipped-storage-unavailable"
    assert STORAGE_STAGES == frozenset({STAGE_PREVIEW_ROOT, STAGE_DISK_FULL})
    assert STAGE_PREVIEW_ROOT == "preview-root"
    assert STAGE_DISK_FULL == "disk-full"


def test_preview_error_exposes_stage_message_and_detail():
    error = PreviewError(STAGE_FFMPEG_EXIT, "FFmpeg exited with code 1.")
    assert error.stage == STAGE_FFMPEG_EXIT
    assert error.message == "FFmpeg exited with code 1."
    assert error.detail == ""
    assert str(error) == "FFmpeg exited with code 1."
    assert isinstance(error, Exception)

    with_detail = PreviewError(
        STAGE_FFMPEG_EXIT,
        "FFmpeg exited with code 1.",
        detail="Invalid data found\n   when processing input.",
    )
    assert with_detail.detail == "Invalid data found\n   when processing input."
    assert str(with_detail) == (
        "FFmpeg exited with code 1. — Invalid data found when processing input."
    )


def test_preview_error_str_truncates_long_detail_to_500_characters():
    long_detail = "x" * 2000
    error = PreviewError(STAGE_IMAGE_DECODE, "msg", detail=long_detail)
    text = str(error)
    assert text.startswith("msg — ")
    rendered_detail = text[len("msg — ") :]
    assert len(rendered_detail) == 500
    assert rendered_detail.endswith("…")
    # The raw detail is preserved for callers that want the full text.
    assert error.detail == long_detail


def test_preview_cancelled_is_a_plain_exception():
    assert issubclass(PreviewCancelled, Exception)
    assert not issubclass(PreviewCancelled, PreviewError)


def test_sha256_hex_accepts_raw_digest_and_hex_text_in_any_case():
    digest = digest_for("holiday.mov")
    expected = digest.hex()
    assert sha256_hex(digest) == expected
    assert sha256_hex(bytearray(digest)) == expected
    assert sha256_hex(memoryview(digest)) == expected
    assert sha256_hex(expected) == expected
    assert sha256_hex(expected.upper()) == expected
    assert sha256_hex(expected.title()) == expected
    assert len(expected) == 64 and expected == expected.lower()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "f" * 63,
        "f" * 65,
        "g" + "f" * 63,
        " " + "f" * 64,
        "f" * 64 + "\n",
        b"",
        b"\x00" * 31,
        b"\x00" * 33,
        None,
        123,
        1.5,
        ["f" * 64],
    ],
    ids=repr,
)
def test_sha256_hex_rejects_bad_hashes(bad):
    with pytest.raises(PreviewError) as info:
        sha256_hex(bad)  # type: ignore[arg-type]
    assert info.value.stage == STAGE_CONFIGURATION
    assert info.value.message


def test_is_disk_full_error_matches_errno_winerror_and_wording():
    assert is_disk_full_error(OSError(errno.ENOSPC, "No space left on device"))
    edquot = getattr(errno, "EDQUOT", None)
    if edquot is not None:
        assert is_disk_full_error(OSError(edquot, "Disk quota exceeded"))
    assert is_disk_full_error(WinError(112, "There is not enough space on the disk"))
    assert is_disk_full_error(WinError(39, "The disk is full"))
    assert is_disk_full_error(WinError(39, "unrelated wording"))
    assert is_disk_full_error(RuntimeError("Encoder failed: No Space Left on device"))
    assert is_disk_full_error(ValueError("DISK FULL while writing"))
    assert is_disk_full_error(OSError(0, "There is not enough space on the disk"))
    assert is_disk_full_error(PreviewError(STAGE_DISK_FULL, "Could not write preview."))

    assert not is_disk_full_error(PermissionError(errno.EACCES, "Permission denied"))
    assert not is_disk_full_error(OSError(errno.EIO, "Input/output error"))
    assert not is_disk_full_error(ValueError("something else"))
    assert not is_disk_full_error(FileNotFoundError(errno.ENOENT, "gone"))


@pytest.mark.skipif(os.name != "nt", reason="OSError.winerror is only populated on Windows")
def test_is_disk_full_error_recognises_real_windows_error_codes():
    disk_full = OSError(0, "There is not enough space on the disk", None, 112)
    handle_disk_full = OSError(0, "The disk is full", None, 39)
    assert disk_full.winerror == 112
    assert handle_disk_full.winerror == 39
    # WinError 39 maps to EINVAL, so only the winerror check can catch it.
    assert handle_disk_full.errno != errno.ENOSPC
    assert is_disk_full_error(disk_full)
    assert is_disk_full_error(handle_disk_full)
    assert classify_os_error(handle_disk_full, STAGE_RENAME) == STAGE_DISK_FULL

    access_denied = OSError(0, "Access is denied", None, 5)
    sharing = OSError(0, "The process cannot access the file", None, 32)
    assert isinstance(access_denied, PermissionError)
    assert is_permission_error(access_denied)
    assert is_permission_error(sharing)
    assert not is_disk_full_error(access_denied)


def test_is_permission_error_matches_type_errno_and_winerror():
    assert is_permission_error(PermissionError())
    assert is_permission_error(PermissionError(errno.EACCES, "Permission denied", "E:\\x"))
    assert is_permission_error(OSError(errno.EACCES, "Permission denied"))
    assert is_permission_error(OSError(errno.EPERM, "Operation not permitted"))
    assert is_permission_error(WinError(5, "Access is denied"))
    assert is_permission_error(WinError(32, "Sharing violation"))
    assert is_permission_error(PreviewError(STAGE_PERMISSION, "denied"))

    assert not is_permission_error(OSError(errno.ENOSPC, "No space left on device"))
    assert not is_permission_error(FileNotFoundError(errno.ENOENT, "missing"))
    assert not is_permission_error(ValueError("Permission denied"))


def test_classify_os_error_prefers_disk_full_then_permission_then_default():
    assert classify_os_error(OSError(errno.ENOSPC, "No space left on device"), STAGE_RENAME) == STAGE_DISK_FULL
    assert classify_os_error(WinError(112, "x"), STAGE_RENAME) == STAGE_DISK_FULL
    assert classify_os_error(PermissionError(errno.EACCES, "denied"), STAGE_RENAME) == STAGE_PERMISSION
    assert classify_os_error(WinError(5, "denied"), STAGE_PREVIEW_ROOT) == STAGE_PERMISSION
    assert classify_os_error(FileNotFoundError(errno.ENOENT, "gone"), STAGE_RENAME) == STAGE_RENAME
    assert classify_os_error(OSError(errno.EIO, "io"), STAGE_PREVIEW_ROOT) == STAGE_PREVIEW_ROOT
    # Disk-full wording wins even on a PermissionError.
    assert classify_os_error(PermissionError(errno.EACCES, "disk full"), STAGE_RENAME) == STAGE_DISK_FULL


def test_os_error_detail_collapses_to_one_line_and_never_returns_empty():
    error = OSError(errno.EACCES, "Access\n   is\tdenied", "E:\\JVVV Previews\\x")
    detail = os_error_detail(error)
    assert "\n" not in detail and "\t" not in detail
    assert "Access is denied" in detail
    assert "JVVV Previews" in detail
    assert os_error_detail(OSError()) == "OSError"
    assert os_error_detail(PermissionError()) == "PermissionError"
    long_error = OSError(errno.EIO, "y" * 2000)
    assert len(os_error_detail(long_error)) == 500
    assert os_error_detail(long_error).endswith("…")


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def test_preview_result_ok_and_json_safe_dict(tmp_path):
    path = tmp_path / "previews" / "images" / IMAGE_PROFILE_ID / "8f" / f"{hex_for(1)}.jpg"
    generated = PreviewResult(
        PREVIEW_GENERATED,
        "image",
        IMAGE_PROFILE_ID,
        path=path,
        bytes_written=1234,
        size_bytes=1234,
        width=1600,
        height=1067,
    )
    reused = PreviewResult(PREVIEW_REUSED, "video", VIDEO_PROFILE_ID, path=path, size_bytes=5)
    failed = PreviewResult(
        PREVIEW_FAILED,
        "video",
        VIDEO_PROFILE_ID,
        path=path,
        stage=STAGE_FFMPEG_EXIT,
        message="FFmpeg exited with code 1.",
        detail="Invalid data found when processing input.",
    )
    skipped = PreviewResult(PREVIEW_SKIPPED_STORAGE, "image", IMAGE_PROFILE_ID)

    assert generated.ok and reused.ok
    assert not failed.ok and not skipped.ok
    assert generated.replaced_corrupt is False

    values = generated.as_dict()
    assert values["path"] == str(path)
    assert values["status"] == PREVIEW_GENERATED
    assert values["width"] == 1600 and values["height"] == 1067
    assert skipped.as_dict()["path"] is None
    for result in (generated, reused, failed, skipped):
        json.dumps(result.as_dict())  # JSON-safe
    with pytest.raises(ValueError):
        PreviewResult("mystery", "image", IMAGE_PROFILE_ID)


def test_preview_failure_round_trips_through_dict_and_json():
    failure = PreviewFailure(
        source_name="camera001.mov",
        relative_path="Videos/camera001.mov",
        volume_id=7,
        volume_label="AID-007 - Archive",
        media_kind="video",
        sha256=hex_for("camera001"),
        preview_path="E:\\JVVV Previews\\videos\\x.mp4",
        profile_id=VIDEO_PROFILE_ID,
        stage=STAGE_FFMPEG_EXIT,
        message="FFmpeg exited with code 1.",
        detail="Invalid data found when processing input.",
    )
    values = failure.as_dict()
    restored = PreviewFailure.from_dict(json.loads(json.dumps(values)))
    assert restored == failure
    assert set(values) == {
        "source_name",
        "relative_path",
        "volume_id",
        "volume_label",
        "media_kind",
        "sha256",
        "preview_path",
        "profile_id",
        "stage",
        "message",
        "detail",
    }


def test_preview_failure_from_dict_tolerates_missing_and_null_values():
    restored = PreviewFailure.from_dict(
        {"source_name": "broken.tif", "media_kind": "image", "volume_id": "12", "sha256": None}
    )
    assert restored.source_name == "broken.tif"
    assert restored.volume_id == 12
    assert restored.sha256 is None
    assert restored.preview_path is None
    assert restored.volume_label == ""
    assert restored.stage == ""
    assert restored.detail == ""
    assert PreviewFailure.from_dict({"volume_id": "not-a-number"}).volume_id == 0


def test_preview_failure_display_lines_match_the_scan_report_layout():
    with_detail = PreviewFailure(
        source_name="camera001.mov",
        relative_path="Videos/camera001.mov",
        volume_id=1,
        volume_label="",
        media_kind="video",
        sha256=None,
        preview_path=None,
        profile_id=VIDEO_PROFILE_ID,
        stage=STAGE_FFMPEG_EXIT,
        message="FFmpeg exited with code 1.",
        detail="Invalid data found when processing input.",
    )
    assert with_detail.display_lines() == [
        "Type: Video",
        f"Profile: {VIDEO_PROFILE_ID}",
        "Error: FFmpeg exited with code 1.",
        "Detail: Invalid data found when processing input.",
    ]
    without_detail = PreviewFailure(
        source_name="broken.tif",
        relative_path="Photos/broken.tif",
        volume_id=1,
        volume_label="",
        media_kind="image",
        sha256=None,
        preview_path=None,
        profile_id=IMAGE_PROFILE_ID,
        stage=STAGE_IMAGE_DECODE,
        message="Image decoder could not read the file.",
    )
    assert without_detail.display_lines() == [
        "Type: Image",
        f"Profile: {IMAGE_PROFILE_ID}",
        "Error: Image decoder could not read the file.",
    ]
    no_profile = PreviewFailure(
        source_name="huge-photo.jpg",
        relative_path="Photos/huge-photo.jpg",
        volume_id=1,
        volume_label="",
        media_kind="image",
        sha256=None,
        preview_path=None,
        profile_id="",
        stage=STAGE_DISK_FULL,
        message="Could not write preview.",
        detail="No space left on device.",
    )
    assert no_profile.display_lines() == [
        "Type: Image",
        "Error: Could not write preview.",
        "Detail: No space left on device.",
    ]


def test_statistics_dataclasses_have_expected_defaults():
    assert ProfileStatistics() == ProfileStatistics(count=0, bytes=0)
    statistics = PreviewStoreStatistics(
        image_count=1,
        video_count=2,
        image_bytes=3,
        video_bytes=4,
        total_bytes=7,
        temporary_files=0,
        profiles={("image", IMAGE_PROFILE_ID): ProfileStatistics(1, 3)},
    )
    assert statistics.cancelled is False
    validation = RootValidation(Path("E:/x"), False, 10, 5, "E:\\x is writable (5 B free).")
    assert validation.free_bytes == 5


# ---------------------------------------------------------------------------
# deterministic paths
# ---------------------------------------------------------------------------


def test_preview_paths_are_deterministic_for_images_and_videos(tmp_path):
    root = tmp_path / "JVVV Previews"
    cache = make_cache(root)
    digest = digest_for("Holiday.mov")
    hex_digest = digest.hex()

    image_path = cache.preview_path("image", digest)
    video_path = cache.preview_path("video", digest)

    assert image_path == root / "images" / IMAGE_PROFILE_ID / hex_digest[:2] / f"{hex_digest}.jpg"
    assert video_path == root / "videos" / VIDEO_PROFILE_ID / hex_digest[:2] / f"{hex_digest}.mp4"
    assert image_path.suffix == ".jpg" and video_path.suffix == ".mp4"
    assert image_path.parent.name == hex_digest[:2]
    assert len(image_path.parent.name) == 2
    # Raw digest, lowercase hex and uppercase hex all address the same file.
    assert cache.preview_path("image", hex_digest) == image_path
    assert cache.preview_path("image", hex_digest.upper()) == image_path
    assert cache.preview_path("video", bytearray(digest)) == video_path
    # Nothing was created on disk by deriving paths.
    assert not root.exists()
    # The derived name parses back to the same hash.
    assert PreviewCache.parse_preview_name(image_path.name) == (hex_digest, "jpg")
    assert PreviewCache.parse_preview_name(video_path.name) == (hex_digest, "mp4")


def test_preview_path_rejects_bad_hashes_and_unknown_kinds(tmp_path):
    cache = make_cache(tmp_path / "previews")
    with pytest.raises(PreviewError) as info:
        cache.preview_path("image", "not-a-hash")
    assert info.value.stage == STAGE_CONFIGURATION
    with pytest.raises(PreviewError) as info:
        cache.preview_path("image", b"\x00" * 16)
    assert info.value.stage == STAGE_CONFIGURATION
    with pytest.raises(PreviewError) as info:
        cache.preview_path("audio", hex_for(1))
    assert info.value.stage == STAGE_CONFIGURATION
    with pytest.raises(PreviewError) as info:
        cache.kind_directory("document")
    assert info.value.stage == STAGE_CONFIGURATION
    with pytest.raises(PreviewError):
        cache.profile("audio")
    with pytest.raises(PreviewError):
        cache.profile_id("audio")


def test_explicit_profile_ids_are_honoured_and_unsafe_ones_rejected(tmp_path):
    root = tmp_path / "previews"
    cache = make_cache(root)
    digest = hex_for("x")
    path = cache.preview_path("image", digest, OTHER_IMAGE_PROFILE_ID)
    assert path == root / "images" / OTHER_IMAGE_PROFILE_ID / digest[:2] / f"{digest}.jpg"
    assert cache.profile_directory("video") == root / "videos" / VIDEO_PROFILE_ID
    assert cache.profile_directory("video", "h264-2fps-720p-crf28-fast") == (
        root / "videos" / "h264-2fps-720p-crf28-fast"
    )
    for unsafe in ("../escape", "Bad Name", "UPPER", "", "a/b", "a\\b", ".hidden"):
        with pytest.raises(PreviewError) as info:
            cache.profile_directory("image", unsafe)
        assert info.value.stage == STAGE_CONFIGURATION


def test_profiles_and_ids_come_from_the_configured_profiles(tmp_path):
    image = ImagePreviewProfile(max_dimension=4096, jpeg_quality=92)
    video = VideoPreviewProfile(fps=2.0, max_height=720, crf=28, preset="fast")
    cache = PreviewCache(tmp_path / "previews", image, video)
    assert cache.image_profile is image and cache.video_profile is video
    assert cache.profile("image") is image and cache.profile("video") is video
    assert cache.profile_id("image") == "jpeg-max4096-q92"
    assert cache.profile_id("video") == "h264-2fps-720p-crf28-fast"
    assert cache.kind_directory("image") == tmp_path / "previews" / "images"
    assert cache.kind_directory("video") == tmp_path / "previews" / "videos"


def test_invalid_profile_settings_surface_as_preview_errors(tmp_path):
    cache = PreviewCache(tmp_path / "previews", ImagePreviewProfile(max_dimension=10), VideoPreviewProfile())
    with pytest.raises(PreviewConfigError):
        ImagePreviewProfile(max_dimension=10).profile_id
    with pytest.raises(PreviewError) as info:
        cache.profile_id("image")
    assert info.value.stage == STAGE_CONFIGURATION
    with pytest.raises(PreviewError):
        cache.preview_path("image", hex_for(1))
    # The valid video profile still works.
    assert cache.profile_id("video") == VIDEO_PROFILE_ID


def test_two_caches_with_the_same_root_and_profiles_share_paths(tmp_path):
    """Spec §4/§25: one preview root shared across catalogues → identical paths."""

    root = tmp_path / "Shared Previews"
    work_catalogue = PreviewCache(root, ImagePreviewProfile(), VideoPreviewProfile())
    personal_catalogue = PreviewCache(
        str(root),
        ImagePreviewProfile(1600, 82),
        VideoPreviewProfile(1.0, 240, 35, "veryfast"),
    )
    for seed in range(25):
        digest = digest_for(seed)
        assert work_catalogue.preview_path("image", digest) == personal_catalogue.preview_path(
            "image", digest.hex()
        )
        assert work_catalogue.preview_path("video", digest) == personal_catalogue.preview_path(
            "video", digest.hex().upper()
        )
    # Different content, profile, or root each give a different file.
    digest = digest_for("a")
    assert work_catalogue.preview_path("image", digest) != work_catalogue.preview_path("image", digest_for("b"))
    other_profile = PreviewCache(root, ImagePreviewProfile(1024, 75), VideoPreviewProfile())
    assert other_profile.preview_path("image", digest) != work_catalogue.preview_path("image", digest)
    assert other_profile.preview_path("video", digest) == work_catalogue.preview_path("video", digest)
    other_root = PreviewCache(tmp_path / "Elsewhere", ImagePreviewProfile(), VideoPreviewProfile())
    assert other_root.preview_path("image", digest) != work_catalogue.preview_path("image", digest)


def test_root_is_expanded_but_never_resolved(tmp_path):
    relative = PreviewCache("previews-relative", ImagePreviewProfile(), VideoPreviewProfile())
    assert relative.root == Path("previews-relative")
    assert not relative.root.is_absolute()
    assert relative.root_text == "previews-relative"

    home = PreviewCache("~/jvvv-previews", ImagePreviewProfile(), VideoPreviewProfile())
    assert home.root == Path.home() / "jvvv-previews"

    given = tmp_path / "Given Root"
    from_path = PreviewCache(given, ImagePreviewProfile(), VideoPreviewProfile())
    assert from_path.root == given
    if os.name == "nt":
        # UNC roots stay as given (never resolved to a mapped drive or vice versa).
        unc = PreviewCache(r"\\NAS01\JVVV-Previews\Store", ImagePreviewProfile(), VideoPreviewProfile())
        assert str(unc.root) == r"\\NAS01\JVVV-Previews\Store"
        assert unc.root.anchor == "\\\\NAS01\\JVVV-Previews\\"
        share_root = PreviewCache(r"\\NAS01\JVVV-Previews", ImagePreviewProfile(), VideoPreviewProfile())
        assert share_root.root == Path(r"\\NAS01\JVVV-Previews")
        assert share_root.root_text == r"\\NAS01\JVVV-Previews"


# ---------------------------------------------------------------------------
# temporary names
# ---------------------------------------------------------------------------


def test_temporary_path_lives_beside_the_final_file_and_is_unique(tmp_path):
    cache = make_cache(tmp_path / "previews")
    final = cache.preview_path("video", digest_for("v"))
    first = cache.temporary_path(final)
    second = cache.temporary_path(final)

    assert first.parent == final.parent
    assert first.name.startswith(f".{final.name}.tmp-")
    token = first.name[len(f".{final.name}.tmp-") :]
    assert len(token) == 16 and all(c in "0123456789abcdef" for c in token)
    assert first != second
    assert PreviewCache.is_temporary_name(first.name)
    assert PreviewCache.parse_preview_name(first.name) is None
    assert not first.exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f".{hex_for(1)}.mp4.tmp-0123456789abcdef", True),
        (".jvvv-preview-root-check.tmp-00ff", True),
        (".x.tmp-", True),
        (f"{hex_for(1)}.mp4.tmp-0123456789abcdef", False),
        (f".{hex_for(1)}.mp4", False),
        (f"{hex_for(1)}.jpg", False),
        (".DS_Store", False),
        ("", False),
    ],
)
def test_is_temporary_name(name, expected):
    assert PreviewCache.is_temporary_name(name) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f"{hex_for(1)}.jpg", (hex_for(1), "jpg")),
        (f"{hex_for(2)}.mp4", (hex_for(2), "mp4")),
        (f"{hex_for(1).upper()}.jpg", None),
        (f"{hex_for(1)}.jpeg", None),
        (f"{hex_for(1)}.JPG", None),
        (f"{hex_for(1)}.png", None),
        (f"{hex_for(1)[:-1]}.jpg", None),
        (f"{hex_for(1)}0.jpg", None),
        (f"{hex_for(1)}", None),
        (f".{hex_for(1)}.jpg.tmp-abcd", None),
        (f"{hex_for(1)}.jpg.tmp-abcd", None),
        ("readme.txt", None),
        ("", None),
    ],
)
def test_parse_preview_name(name, expected):
    assert PreviewCache.parse_preview_name(name) == expected


# ---------------------------------------------------------------------------
# ensure_parent / publish / discard
# ---------------------------------------------------------------------------


def test_ensure_parent_creates_nested_directories_idempotently(tmp_path):
    cache = make_cache(tmp_path / "previews")
    final = cache.preview_path("image", digest_for("img"))
    assert not final.parent.exists()
    cache.ensure_parent(final)
    assert final.parent.is_dir()
    cache.ensure_parent(final)  # exist_ok
    assert final.parent.is_dir()
    assert not final.exists()


@pytest.mark.parametrize(
    ("error", "expected_stage"),
    [
        (PermissionError(errno.EACCES, "Access is denied"), STAGE_PERMISSION),
        (OSError(errno.ENOSPC, "No space left on device"), STAGE_DISK_FULL),
        (OSError(errno.EIO, "Input/output error"), STAGE_PREVIEW_ROOT),
    ],
)
def test_ensure_parent_failures_are_classified(tmp_path, monkeypatch, error, expected_stage):
    cache = make_cache(tmp_path / "previews")
    final = cache.preview_path("image", digest_for("img"))

    def failing_mkdir(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    with pytest.raises(PreviewError) as info:
        cache.ensure_parent(final)
    assert info.value.stage == expected_stage
    assert info.value.message == "Could not create the preview directory."
    assert str(error.strerror) in info.value.detail
    assert info.value.__cause__ is error


def test_publish_uses_os_replace_and_leaves_no_temporaries(tmp_path, monkeypatch):
    root = tmp_path / "previews"
    cache = make_cache(root)
    final = cache.preview_path("video", digest_for("v"))
    cache.ensure_parent(final)
    temp = cache.temporary_path(final)
    payload = tiny_mp4_bytes()
    temp.write_bytes(payload)
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst, *args, **kwargs):
        calls.append((os.fspath(src), os.fspath(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cache_module.os, "replace", recording_replace)

    cache.publish(temp, final)

    assert calls == [(os.fspath(temp), os.fspath(final))]
    assert final.read_bytes() == payload
    assert not temp.exists()
    assert temporary_files_under(root) == []
    assert [entry.path for entry in cache.iter_previews("video")] == [final]


def test_publish_replaces_an_existing_final_file(tmp_path):
    root = tmp_path / "previews"
    cache = make_cache(root)
    final = cache.preview_path("image", digest_for("i"))
    cache.ensure_parent(final)
    final.write_bytes(b"old corrupt preview")
    temp = cache.temporary_path(final)
    temp.write_bytes(b"new valid preview")

    cache.publish(temp, final)

    assert final.read_bytes() == b"new valid preview"
    assert not temp.exists()
    assert temporary_files_under(root) == []


@pytest.mark.parametrize(
    ("error", "expected_stage"),
    [
        (PermissionError(errno.EACCES, "Access is denied", "E:\\x"), STAGE_PERMISSION),
        (OSError(errno.ENOSPC, "No space left on device"), STAGE_DISK_FULL),
        (WinError(112, "There is not enough space on the disk"), STAGE_DISK_FULL),
        (OSError(errno.EIO, "Input/output error"), STAGE_RENAME),
    ],
)
def test_publish_failures_discard_the_temporary_and_keep_the_old_preview(
    tmp_path, monkeypatch, error, expected_stage
):
    root = tmp_path / "previews"
    cache = make_cache(root)
    final = cache.preview_path("image", digest_for("i"))
    cache.ensure_parent(final)
    final.write_bytes(b"previously valid preview")
    temp = cache.temporary_path(final)
    temp.write_bytes(b"half written")

    def failing_replace(src, dst, *args, **kwargs):
        raise error

    monkeypatch.setattr(cache_module.os, "replace", failing_replace)
    with pytest.raises(PreviewError) as info:
        cache.publish(temp, final)

    assert info.value.stage == expected_stage
    assert info.value.message == "Could not move the finished preview into place."
    assert error.strerror in info.value.detail
    assert not temp.exists(), "the temporary must be discarded on failure"
    assert final.read_bytes() == b"previously valid preview"
    assert temporary_files_under(root) == []


def test_discard_temporary_is_best_effort_and_only_touches_temporaries(tmp_path, monkeypatch):
    root = tmp_path / "previews"
    cache = make_cache(root)
    final = cache.preview_path("image", digest_for("i"))
    cache.ensure_parent(final)
    final.write_bytes(b"final")
    temp = cache.temporary_path(final)
    temp.write_bytes(b"temp")

    cache.discard_temporary(None)
    cache.discard_temporary(temp.parent / ".missing.jpg.tmp-0000")  # missing: no error
    cache.discard_temporary(final)  # refuses to delete a final preview
    assert final.exists()
    cache.discard_temporary(temp)
    assert not temp.exists()

    temp.write_bytes(b"temp again")

    def failing_remove(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(path))

    monkeypatch.setattr(cache_module.os, "remove", failing_remove)
    cache.discard_temporary(temp)  # error swallowed
    monkeypatch.undo()
    assert temp.exists()
    temp.unlink()


# ---------------------------------------------------------------------------
# validate_root
# ---------------------------------------------------------------------------


def test_validate_root_on_a_local_writable_directory(tmp_path):
    root = tmp_path / "JVVV Previews"
    root.mkdir()
    cache = make_cache(root)

    validation = cache.validate_root()

    assert isinstance(validation, RootValidation)
    assert validation.root == root
    assert validation.created is False
    assert isinstance(validation.total_bytes, int) and validation.total_bytes > 0
    assert isinstance(validation.free_bytes, int)
    assert 0 <= validation.free_bytes <= validation.total_bytes
    assert validation.message.startswith(f"{root} is writable")
    assert "free" in validation.message
    assert list(root.iterdir()) == [], "the validation file must be removed"


def test_validate_root_creates_a_missing_root_with_nested_parents(tmp_path):
    root = tmp_path / "level1" / "level2" / "JVVV Previews"
    cache = make_cache(root)
    assert not root.parent.exists()

    validation = cache.validate_root()

    assert validation.created is True
    assert root.is_dir()
    assert list(root.iterdir()) == []
    assert "created" in validation.message and "writable" in validation.message
    # A second validation of the now-existing root reports created=False.
    assert cache.validate_root().created is False


def test_validate_root_missing_root_without_create_fails(tmp_path):
    root = tmp_path / "missing"
    cache = make_cache(root)
    with pytest.raises(PreviewError) as info:
        cache.validate_root(create=False)
    assert info.value.stage == STAGE_PREVIEW_ROOT
    assert info.value.message == f"JVVV cannot write to: {root}"
    assert "does not exist" in info.value.detail
    assert not root.exists()
    assert cache.free_space() is None


def test_validate_root_rejects_a_file_where_the_directory_should_be(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"I am a file")
    cache = make_cache(root)
    for create in (True, False):
        with pytest.raises(PreviewError) as info:
            cache.validate_root(create=create)
        assert info.value.stage == STAGE_PREVIEW_ROOT
        assert info.value.message == f"JVVV cannot write to: {root}"
        assert "not a directory" in info.value.detail
    assert root.read_bytes() == b"I am a file"
    # A root under a file cannot be created either: the OS error is reported.
    nested = make_cache(root / "sub")
    with pytest.raises(PreviewError) as info:
        nested.validate_root()
    assert info.value.stage in {STAGE_PREVIEW_ROOT, STAGE_PERMISSION}
    assert info.value.message == f"JVVV cannot write to: {root / 'sub'}"
    assert info.value.detail


def test_validate_root_unwritable_root_reports_the_permission_error(tmp_path, monkeypatch):
    root = tmp_path / "read-only"
    root.mkdir()
    cache = make_cache(root)
    attempted: list[Path] = []

    def denied_write(self, data):
        attempted.append(self)
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(self))

    monkeypatch.setattr(Path, "write_bytes", denied_write)
    with pytest.raises(PreviewError) as info:
        cache.validate_root()

    assert info.value.stage == STAGE_PERMISSION
    assert info.value.message == f"JVVV cannot write to: {root}"
    assert "Access is denied" in info.value.detail
    assert len(attempted) == 1
    assert attempted[0].parent == root
    assert PreviewCache.is_temporary_name(attempted[0].name)
    assert attempted[0].name.startswith(".jvvv-preview-root-check.tmp-")
    assert list(root.iterdir()) == []


def test_validate_root_disk_full_during_validation_is_classified(tmp_path, monkeypatch):
    root = tmp_path / "full"
    root.mkdir()
    cache = make_cache(root)

    def full_write(self, data):
        raise OSError(errno.ENOSPC, "No space left on device", os.fspath(self))

    monkeypatch.setattr(Path, "write_bytes", full_write)
    with pytest.raises(PreviewError) as info:
        cache.validate_root()
    assert info.value.stage == STAGE_DISK_FULL
    assert info.value.stage in STORAGE_STAGES
    assert list(root.iterdir()) == []


def test_validate_root_detects_a_readback_mismatch_and_cleans_up(tmp_path, monkeypatch):
    root = tmp_path / "flaky"
    root.mkdir()
    cache = make_cache(root)
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"garbage")
    with pytest.raises(PreviewError) as info:
        cache.validate_root()
    assert info.value.stage == STAGE_PREVIEW_ROOT
    assert "read back" in info.value.detail
    assert list(root.iterdir()) == []


def test_validate_root_writes_and_removes_a_temporary_validation_file(tmp_path, monkeypatch):
    root = tmp_path / "previews"
    root.mkdir()
    cache = make_cache(root)
    seen: dict[str, object] = {}
    real_write = Path.write_bytes

    def observing_write(self, data):
        seen["path"] = self
        seen["data"] = data
        result = real_write(self, data)
        seen["existed_after_write"] = self.exists()
        seen["size"] = self.stat().st_size
        return result

    monkeypatch.setattr(Path, "write_bytes", observing_write)
    cache.validate_root()
    monkeypatch.undo()

    path = seen["path"]
    assert isinstance(path, Path)
    assert path.parent == root
    assert PreviewCache.is_temporary_name(path.name)
    assert seen["existed_after_write"] is True
    assert seen["size"] == len(seen["data"]) > 0
    assert not path.exists()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("error", "expected_stage"),
    [
        (PermissionError(errno.EACCES, "Access is denied"), STAGE_PERMISSION),
        (WinError(32, "The process cannot access the file because it is being used"), STAGE_PERMISSION),
        (OSError(errno.EIO, "Input/output error"), STAGE_PREVIEW_ROOT),
    ],
    ids=["access-denied", "sharing-violation", "io-error"],
)
def test_validate_root_fails_when_the_validation_file_cannot_be_removed(
    tmp_path, monkeypatch, error, expected_stage
):
    """Spec §2A: the root must let JVVV create *and remove* the validation file.

    A failed delete used to be swallowed by the best-effort cleanup, so the
    root was reported writable and the check file was left behind.
    """

    root = tmp_path / "sticky"
    root.mkdir()
    cache = make_cache(root)
    attempted: list[str] = []

    def stuck_remove(path, *args, **kwargs):
        attempted.append(os.fspath(path))
        raise error

    monkeypatch.setattr(cache_module.os, "remove", stuck_remove)
    with pytest.raises(PreviewError) as info:
        cache.validate_root()
    monkeypatch.undo()

    assert info.value.stage == expected_stage
    assert info.value.message == f"JVVV cannot write to: {root}"
    assert "could not be removed" in info.value.detail
    assert error.strerror in info.value.detail
    assert info.value.__cause__ is error
    # The file JVVV could not delete is still there, and the report names it.
    leftover = list(root.iterdir())
    assert len(leftover) == 1
    assert leftover[0].is_file()
    assert PreviewCache.is_temporary_name(leftover[0].name)
    assert leftover[0].name.startswith(".jvvv-preview-root-check.tmp-")
    assert leftover[0].name in info.value.detail
    assert len(info.value.detail.splitlines()) == 1
    # Exactly one delete was attempted, on the validation file itself; the
    # best-effort cleanup did not quietly retry and mask the failure.
    assert attempted == [os.fspath(leftover[0])]
    leftover[0].unlink()
    assert list(root.iterdir()) == []


def test_validate_root_failed_removal_with_a_filename_is_not_reported_twice(tmp_path, monkeypatch):
    root = tmp_path / "sticky"
    root.mkdir()
    cache = make_cache(root)

    def stuck_remove(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(path))

    monkeypatch.setattr(cache_module.os, "remove", stuck_remove)
    with pytest.raises(PreviewError) as info:
        cache.validate_root()
    monkeypatch.undo()

    leftover = list(root.iterdir())
    assert len(leftover) == 1
    assert info.value.stage == STAGE_PERMISSION
    assert info.value.detail.count(leftover[0].name) == 1
    assert info.value.detail.startswith("The validation file could not be removed: ")
    assert "Access is denied" in info.value.detail
    leftover[0].unlink()


def test_validate_root_removes_the_validation_file_explicitly_not_by_best_effort(tmp_path, monkeypatch):
    """The delete is a validation step: it must not depend on ``discard_temporary``."""

    root = tmp_path / "previews"
    root.mkdir()
    cache = make_cache(root)
    removed: list[str] = []
    discarded: list[object] = []
    real_remove = os.remove

    def recording_remove(path, *args, **kwargs):
        removed.append(os.fspath(path))
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(cache_module.os, "remove", recording_remove)
    # Neuter the best-effort cleanup: if validate_root relied on it, the check
    # file would now survive and the assertions below would fail.
    monkeypatch.setattr(
        PreviewCache, "discard_temporary", lambda self, temp_path: discarded.append(temp_path)
    )

    validation = cache.validate_root()

    assert validation.created is False
    assert len(removed) == 1
    assert Path(removed[0]).parent == root
    assert Path(removed[0]).name.startswith(".jvvv-preview-root-check.tmp-")
    assert all(item is None for item in discarded), "nothing was left for the best-effort cleanup"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("root_text", ["", "   "])
def test_validate_root_requires_a_selected_directory(root_text):
    cache = PreviewCache(root_text, ImagePreviewProfile(), VideoPreviewProfile())
    with pytest.raises(PreviewError) as info:
        cache.validate_root()
    assert info.value.stage == STAGE_PREVIEW_ROOT
    assert "not been selected" in info.value.message


def test_free_space_reports_usage_or_none(tmp_path):
    root = tmp_path / "previews"
    assert make_cache(root).free_space() is None
    root.mkdir()
    usage = make_cache(root).free_space()
    assert usage is not None
    total, free = usage
    assert isinstance(total, int) and isinstance(free, int)
    assert total >= free >= 0


# ---------------------------------------------------------------------------
# contains
# ---------------------------------------------------------------------------


def test_contains_only_accepts_paths_strictly_inside_the_root(tmp_path):
    root = tmp_path / "previews"
    cache = make_cache(root)
    inside = root / "images" / IMAGE_PROFILE_ID / "8f" / f"{hex_for(1)}.jpg"
    assert cache.contains(inside)
    assert cache.contains(root / "anything")
    assert not cache.contains(root)
    assert not cache.contains(tmp_path)
    assert not cache.contains(tmp_path / "previews-sibling" / "x.jpg")
    assert not cache.contains(tmp_path / "previewsX")
    assert not cache.contains(root / ".." / "escape.jpg")
    assert cache.contains(root / "images" / ".." / "videos" / "x.mp4")
    assert cache.contains(str(inside))
    if os.name == "nt":
        swapped_case = Path(str(root).upper()) / "images" / "x.jpg"
        assert cache.contains(swapped_case)
        assert cache.contains(Path(str(inside).replace("\\", "/")))


# ---------------------------------------------------------------------------
# iteration over a large synthetic store
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_store(tmp_path):
    root = tmp_path / "Big Store"
    image_hashes = [hex_for(f"image-{i}") for i in range(300)]
    other_image_hashes = [hex_for(f"other-{i}") for i in range(40)]
    video_hashes = [hex_for(f"video-{i}") for i in range(120)]
    image_paths = populate(root, "images", IMAGE_PROFILE_ID, image_hashes, "jpg")
    other_paths = populate(root, "images", OTHER_IMAGE_PROFILE_ID, other_image_hashes, "jpg", size=3)
    video_paths = populate(root, "videos", VIDEO_PROFILE_ID, video_hashes, "mp4", size=7)

    image_dir = root / "images" / IMAGE_PROFILE_ID
    video_dir = root / "videos" / VIDEO_PROFILE_ID
    junk: list[Path] = []
    temporaries: list[Path] = []
    # Temporaries that a crashed encoder might have left behind.
    for i in range(6):
        digest = hex_for(f"temp-{i}")
        temp = image_dir / digest[:2] / f".{digest}.jpg.tmp-{i:016x}"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(b"partial")
        temporaries.append(temp)
    digest = hex_for("video-temp")
    temp = video_dir / digest[:2] / f".{digest}.mp4.tmp-00000000deadbeef"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b"partial video")
    temporaries.append(temp)
    # Junk names inside a prefix directory.
    first_prefix = image_dir / image_hashes[0][:2]
    for name in (
        "readme.txt",
        "Thumbs.db",
        f"{hex_for('upper').upper()}.jpg",
        f"{hex_for('wrongext')}.png",
        f"{hex_for('video-in-images')}.mp4",
        f"{hex_for('short')[:-1]}.jpg",
    ):
        path = first_prefix / name
        path.write_bytes(b"junk")
        junk.append(path)
    # A hash filed under the wrong prefix directory is not a valid preview.
    misplaced_digest = hex_for("misplaced")
    wrong_prefix = "00" if misplaced_digest[:2] != "00" else "01"
    misplaced = image_dir / wrong_prefix / f"{misplaced_digest}.jpg"
    misplaced.parent.mkdir(parents=True, exist_ok=True)
    misplaced.write_bytes(b"misplaced")
    junk.append(misplaced)
    # A directory named like a preview, and junk directories at every level.
    (first_prefix / f"{hex_for('dir')}.jpg").mkdir()
    (image_dir / "not-a-prefix").mkdir()
    (image_dir / "zz").mkdir()  # not hex
    (image_dir / "abc").mkdir()  # wrong length
    (root / "images" / "Not A Profile").mkdir()
    (root / "images" / "stray.jpg").write_bytes(b"stray")
    (root / "videos" / VIDEO_PROFILE_ID / "readme.txt").write_bytes(b"stray")
    return {
        "root": root,
        "image_hashes": image_hashes,
        "other_image_hashes": other_image_hashes,
        "video_hashes": video_hashes,
        "image_paths": image_paths,
        "other_paths": other_paths,
        "video_paths": video_paths,
        "junk": junk,
        "temporaries": temporaries,
    }


def test_iter_previews_streams_valid_entries_and_skips_junk(populated_store):
    store = populated_store
    cache = make_cache(store["root"])

    entries = list(cache.iter_previews("image", IMAGE_PROFILE_ID))

    assert len(entries) == 300
    assert {entry.sha256 for entry in entries} == set(store["image_hashes"])
    assert {entry.path for entry in entries} == set(store["image_paths"])
    assert all(isinstance(entry, PreviewEntry) for entry in entries)
    assert all(entry.media_kind == "image" for entry in entries)
    assert all(entry.profile_id == IMAGE_PROFILE_ID for entry in entries)
    assert all(entry.size_bytes == 0 for entry in entries)
    assert all(entry.path.name == f"{entry.sha256}.jpg" for entry in entries)
    assert all(
        cache.preview_path("image", entry.sha256, entry.profile_id) == entry.path for entry in entries
    )
    # Prefix directories are visited in sorted order so output is deterministic.
    prefixes = [entry.sha256[:2] for entry in entries]
    assert prefixes == sorted(prefixes)
    assert len(set(prefixes)) > 100, "300 hashes should spread across many prefix directories"
    yielded_paths = {entry.path for entry in entries}
    assert not yielded_paths.intersection(store["junk"])
    assert not yielded_paths.intersection(store["temporaries"])
    # The other image profile and the videos are separate.
    other = list(cache.iter_previews("image", OTHER_IMAGE_PROFILE_ID))
    assert {entry.sha256 for entry in other} == set(store["other_image_hashes"])
    assert all(entry.size_bytes == 3 for entry in other)
    videos = list(cache.iter_previews("video"))
    assert {entry.sha256 for entry in videos} == set(store["video_hashes"])
    assert all(entry.size_bytes == 7 and entry.path.suffix == ".mp4" for entry in videos)


def test_iter_previews_without_profile_covers_every_profile_directory(populated_store):
    store = populated_store
    cache = make_cache(store["root"])
    assert cache.iter_profile_ids("image") == [OTHER_IMAGE_PROFILE_ID, IMAGE_PROFILE_ID]
    assert cache.iter_profile_ids("video") == [VIDEO_PROFILE_ID]
    entries = list(cache.iter_previews("image"))
    assert len(entries) == 340
    by_profile: dict[str, set[str]] = {}
    for entry in entries:
        by_profile.setdefault(entry.profile_id, set()).add(entry.sha256)
    assert by_profile == {
        IMAGE_PROFILE_ID: set(store["image_hashes"]),
        OTHER_IMAGE_PROFILE_ID: set(store["other_image_hashes"]),
    }


def test_iter_previews_is_lazy_and_does_not_list_the_whole_store(populated_store, monkeypatch):
    store = populated_store
    cache = make_cache(store["root"])
    opened: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path=".", *args, **kwargs):
        opened.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(cache_module.os, "scandir", counting_scandir)
    iterator = cache.iter_previews("image", IMAGE_PROFILE_ID)
    assert opened == [], "creating the generator must not touch the disk"

    first_five = list(itertools.islice(iterator, 5))

    assert len(first_five) == 5
    prefix_directories = sorted(
        p for p in (store["root"] / "images" / IMAGE_PROFILE_ID).iterdir() if p.is_dir()
    )
    assert len(prefix_directories) > 100
    # Only the profile directory listing plus the first few prefix directories were opened.
    assert 2 <= len(opened) <= 6
    iterator.close()
    # The store as a whole is only walked as far as the consumer pulls.
    opened.clear()
    stats_iterator = cache.iter_previews("image")
    next(stats_iterator)
    assert len(opened) <= 4
    stats_iterator.close()


def test_iter_previews_missing_directories_yield_nothing(tmp_path):
    cache = make_cache(tmp_path / "never-created")
    assert list(cache.iter_previews("image")) == []
    assert list(cache.iter_previews("video")) == []
    assert list(cache.iter_previews("image", "jpeg-max800-q50")) == []
    assert cache.iter_profile_ids("image") == []
    assert cache.iter_profile_ids("video") == []
    (tmp_path / "never-created" / "images").mkdir(parents=True)
    assert list(cache.iter_previews("image")) == []
    with pytest.raises(PreviewError) as info:
        list(cache.iter_previews("audio"))
    assert info.value.stage == STAGE_CONFIGURATION
    with pytest.raises(PreviewError):
        list(cache.iter_previews("image", "../escape"))


def test_iter_previews_cancellation_raises_between_directories(populated_store):
    cache = make_cache(populated_store["root"])
    calls = {"count": 0}

    def cancel_after_three():
        calls["count"] += 1
        return calls["count"] > 3

    iterator = cache.iter_previews("image", IMAGE_PROFILE_ID, cancel_callback=cancel_after_three)
    with pytest.raises(PreviewCancelled):
        list(iterator)
    assert calls["count"] == 4
    # An immediately-cancelled walk never yields.
    with pytest.raises(PreviewCancelled):
        next(cache.iter_previews("image", cancel_callback=lambda: True))
    # A never-cancelling callback is consulted but does not interfere.
    assert len(list(cache.iter_previews("video", cancel_callback=lambda: False))) == 120


# ---------------------------------------------------------------------------
# store statistics
# ---------------------------------------------------------------------------


def test_store_statistics_counts_kinds_profiles_bytes_and_temporaries(populated_store):
    store = populated_store
    cache = make_cache(store["root"])

    statistics = cache.store_statistics()

    assert isinstance(statistics, PreviewStoreStatistics)
    assert statistics.cancelled is False
    assert statistics.image_count == 340
    assert statistics.video_count == 120
    assert statistics.image_bytes == 40 * 3
    assert statistics.video_bytes == 120 * 7
    assert statistics.total_bytes == 40 * 3 + 120 * 7
    assert statistics.temporary_files == len(store["temporaries"]) == 7
    assert statistics.profiles == {
        ("image", IMAGE_PROFILE_ID): ProfileStatistics(count=300, bytes=0),
        ("image", OTHER_IMAGE_PROFILE_ID): ProfileStatistics(count=40, bytes=120),
        ("video", VIDEO_PROFILE_ID): ProfileStatistics(count=120, bytes=840),
    }


def test_store_statistics_on_an_empty_or_missing_root(tmp_path):
    cache = make_cache(tmp_path / "missing")
    statistics = cache.store_statistics()
    assert statistics == PreviewStoreStatistics(0, 0, 0, 0, 0, 0, {}, False)
    root = tmp_path / "empty"
    (root / "images" / IMAGE_PROFILE_ID).mkdir(parents=True)
    statistics = make_cache(root).store_statistics()
    assert statistics.image_count == 0 and statistics.total_bytes == 0
    assert statistics.profiles == {("image", IMAGE_PROFILE_ID): ProfileStatistics()}


def test_store_statistics_uses_64_bit_byte_counts(tmp_path, monkeypatch):
    """Spec §18: terabyte stores must never overflow a 32-bit counter."""

    root = tmp_path / "huge"
    image_hashes = [hex_for(f"big-image-{i}") for i in range(3)]
    video_hashes = [hex_for(f"big-video-{i}") for i in range(2)]
    populate(root, "images", IMAGE_PROFILE_ID, image_hashes, "jpg")
    populate(root, "videos", VIDEO_PROFILE_ID, video_hashes, "mp4")
    cache = make_cache(root)
    # Pretend every file is 5 TiB (creating real sparse files is not portable).
    monkeypatch.setattr(PreviewCache, "_entry_size", lambda self, entry: FIVE_TIB)

    statistics = cache.store_statistics()

    assert statistics.image_count == 3 and statistics.video_count == 2
    assert statistics.image_bytes == 3 * FIVE_TIB
    assert statistics.video_bytes == 2 * FIVE_TIB
    assert statistics.total_bytes == 5 * FIVE_TIB
    assert statistics.total_bytes > 2**32
    assert statistics.total_bytes > 2**40
    assert statistics.profiles[("video", VIDEO_PROFILE_ID)].bytes == 2 * FIVE_TIB
    entries = list(cache.iter_previews("image"))
    assert all(entry.size_bytes == FIVE_TIB for entry in entries)
    assert format_size(statistics.total_bytes).endswith("TB")


def test_store_statistics_progress_callback_fires_about_every_1000_entries(tmp_path):
    root = tmp_path / "many"
    count = 2 * PROGRESS_INTERVAL + 200
    hashes = [hex_for(f"many-{i}") for i in range(count)]
    populate(root, "images", IMAGE_PROFILE_ID, hashes, "jpg")
    cache = make_cache(root)
    reports: list[tuple[int, str]] = []

    statistics = cache.store_statistics(
        progress_callback=lambda files, where: reports.append((files, where))
    )

    assert statistics.image_count == count
    assert len(reports) == 2
    assert [files for files, _ in reports] == [PROGRESS_INTERVAL, 2 * PROGRESS_INTERVAL]
    for _, where in reports:
        assert isinstance(where, str)
        assert Path(where).parent == root / "images" / IMAGE_PROFILE_ID
    # Fewer entries than the interval → no progress calls at all.
    small_reports: list[tuple[int, str]] = []
    small = make_cache(tmp_path / "small")
    populate(tmp_path / "small", "images", IMAGE_PROFILE_ID, hashes[:5], "jpg")
    small.store_statistics(progress_callback=lambda files, where: small_reports.append((files, where)))
    assert small_reports == []


def test_store_statistics_cancellation_returns_partial_results_with_flag(populated_store):
    cache = make_cache(populated_store["root"])
    full = cache.store_statistics()
    calls = {"count": 0}

    def cancel_after_ten():
        calls["count"] += 1
        return calls["count"] > 10

    partial = cache.store_statistics(cancel_callback=cancel_after_ten)

    assert partial.cancelled is True
    assert 0 <= partial.image_count < full.image_count
    assert partial.video_count == 0
    assert partial.total_bytes <= full.total_bytes
    assert set(partial.profiles) <= set(full.profiles)
    immediate = cache.store_statistics(cancel_callback=lambda: True)
    assert immediate.cancelled is True
    assert immediate.image_count == 0 and immediate.video_count == 0 and immediate.total_bytes == 0
    never = cache.store_statistics(cancel_callback=lambda: False)
    assert never.cancelled is False
    assert never == full


# ---------------------------------------------------------------------------
# remove_preview
# ---------------------------------------------------------------------------


def test_remove_preview_deletes_only_previews_inside_the_root(tmp_path):
    root = tmp_path / "previews"
    cache = make_cache(root)
    preview = cache.preview_path("image", digest_for("i"))
    cache.ensure_parent(preview)
    preview.write_bytes(b"preview")

    outside = tmp_path / f"{hex_for('outside')}.jpg"
    outside.write_bytes(b"outside")
    with pytest.raises(PreviewError) as info:
        cache.remove_preview(outside)
    assert info.value.stage == STAGE_CONFIGURATION
    assert outside.exists()

    escaping = root / ".." / f"{hex_for('outside')}.jpg"
    with pytest.raises(PreviewError):
        cache.remove_preview(escaping)
    assert outside.exists()

    not_a_preview = preview.parent / "notes.txt"
    not_a_preview.write_bytes(b"notes")
    with pytest.raises(PreviewError) as info:
        cache.remove_preview(not_a_preview)
    assert info.value.stage == STAGE_CONFIGURATION
    assert not_a_preview.exists()

    temp = cache.temporary_path(preview)
    temp.write_bytes(b"temp")
    with pytest.raises(PreviewError):
        cache.remove_preview(temp)
    assert temp.exists()
    temp.unlink()

    cache.remove_preview(preview)
    assert not preview.exists()
    assert not_a_preview.exists()
    # Removing an already-removed preview is not an error.
    cache.remove_preview(preview)


def test_remove_preview_reports_os_errors_and_refuses_directories(tmp_path, monkeypatch):
    root = tmp_path / "previews"
    cache = make_cache(root)
    preview = cache.preview_path("video", digest_for("v"))
    cache.ensure_parent(preview)
    preview.write_bytes(b"preview")

    def denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied", os.fspath(path))

    monkeypatch.setattr(cache_module.os, "remove", denied)
    with pytest.raises(PreviewError) as info:
        cache.remove_preview(preview)
    assert info.value.stage == STAGE_PERMISSION
    assert info.value.message == "Could not delete the preview."
    assert "Access is denied" in info.value.detail
    monkeypatch.undo()
    assert preview.exists()

    directory = cache.preview_path("video", digest_for("dir"))
    directory.mkdir(parents=True)
    with pytest.raises(PreviewError):
        cache.remove_preview(directory)
    assert directory.is_dir()


# ---------------------------------------------------------------------------
# no temporaries are ever left behind by success paths
# ---------------------------------------------------------------------------


def test_success_paths_leave_no_temporary_files(tmp_path):
    root = tmp_path / "clean"
    cache = make_cache(root)
    cache.validate_root()
    for seed in range(5):
        final = cache.preview_path("image", digest_for(seed))
        cache.ensure_parent(final)
        temp = cache.temporary_path(final)
        temp.write_bytes(b"jpeg bytes")
        cache.publish(temp, final)
    video = cache.preview_path("video", digest_for("v"))
    cache.ensure_parent(video)
    temp = cache.temporary_path(video)
    temp.write_bytes(tiny_mp4_bytes())
    cache.publish(temp, video)
    cache.validate_root()

    assert temporary_files_under(root) == []
    statistics = cache.store_statistics()
    assert statistics.temporary_files == 0
    assert statistics.image_count == 5 and statistics.video_count == 1
    assert statistics.video_bytes == len(tiny_mp4_bytes())
    # Only preview files and their directories exist under the kind directories.
    for path in root.rglob("*"):
        if path.is_file():
            assert PreviewCache.parse_preview_name(path.name) is not None


# ---------------------------------------------------------------------------
# Audit follow-ups: directory junctions inside the store never lead outside it (spec §22)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows feature")
def test_junction_inside_the_store_is_neither_listed_nor_deletable(tmp_path):
    import _winapi

    from jvvv.preview_config import ImagePreviewProfile, VideoPreviewProfile

    cache = PreviewCache(tmp_path / "previews", ImagePreviewProfile(), VideoPreviewProfile())
    profile_id = cache.profile_id("image")
    victim_dir = tmp_path / "elsewhere"
    victim_dir.mkdir()
    victim = victim_dir / ("ab" + "0" * 62 + ".jpg")
    victim.write_bytes(b"precious bytes outside the preview store")
    prefix_dir = cache.root / "images" / profile_id / "ab"
    prefix_dir.parent.mkdir(parents=True)
    _winapi.CreateJunction(str(victim_dir), str(prefix_dir))
    assert (prefix_dir / victim.name).exists(), "the junction must resolve for the test to mean anything"

    assert list(cache.iter_previews("image", profile_id)) == []
    assert cache.store_statistics().image_count == 0
    assert cache.contains(prefix_dir / victim.name) is False
    with pytest.raises(PreviewError):
        cache.remove_preview(prefix_dir / victim.name)
    assert victim.exists()


# ---------------------------------------------------------------------------
# Leftover temporary files (crash / power loss) can be found and removed safely
# ---------------------------------------------------------------------------


def _temp_cache(tmp_path):
    from jvvv.preview_config import ImagePreviewProfile, VideoPreviewProfile

    return PreviewCache(tmp_path / "previews", ImagePreviewProfile(), VideoPreviewProfile())


def _write_temporary(cache, final_path, *, age_seconds: float) -> Path:
    cache.ensure_parent(final_path)
    temp = cache.temporary_path(final_path)
    temp.write_bytes(b"partial")
    stamp = time.time() - age_seconds
    os.utime(temp, (stamp, stamp))
    return temp


def test_iter_temporary_files_finds_leftovers_in_profiles_and_root_only(tmp_path):
    cache = _temp_cache(tmp_path)
    digest = "ab" + "0" * 62
    old_image_temp = _write_temporary(cache, cache.preview_path("image", digest), age_seconds=3 * 86400)
    video_temp = _write_temporary(cache, cache.preview_path("video", digest), age_seconds=60)
    root_temp = _write_temporary(cache, cache.root / "jvvv-image-test.jpg", age_seconds=3 * 86400)
    final = cache.preview_path("image", "cd" + "0" * 62)
    cache.ensure_parent(final)
    final.write_bytes(b"a finished preview")
    (cache.root / "notes.txt").write_text("not ours", encoding="utf-8")

    found = sorted(cache.iter_temporary_files())

    assert found == sorted([old_image_temp, video_temp, root_temp])


def test_remove_stale_temporary_keeps_recent_files_and_refuses_non_temporaries(tmp_path):
    cache = _temp_cache(tmp_path)
    digest = "ab" + "0" * 62
    old_temp = _write_temporary(cache, cache.preview_path("image", digest), age_seconds=3 * 86400)
    recent_temp = _write_temporary(cache, cache.preview_path("video", digest), age_seconds=60)
    final = cache.preview_path("image", "cd" + "0" * 62)
    cache.ensure_parent(final)
    final.write_bytes(b"a finished preview")
    outside = tmp_path / ".elsewhere.jpg.tmp-deadbeefdeadbeef"
    outside.write_bytes(b"not in the store")

    assert cache.remove_stale_temporary(old_temp) is True
    assert not old_temp.exists()
    assert cache.remove_stale_temporary(recent_temp) is False
    assert recent_temp.exists(), "a file another JVVV window may still be writing is kept"
    assert cache.remove_stale_temporary(recent_temp, now=time.time() + 2 * 86400) is True
    assert cache.remove_stale_temporary(old_temp) is False, "already gone: nothing to do"
    with pytest.raises(PreviewError):
        cache.remove_stale_temporary(final)
    assert final.exists()
    with pytest.raises(PreviewError):
        cache.remove_stale_temporary(outside)
    assert outside.exists()
