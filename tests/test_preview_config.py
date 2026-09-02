"""Tests for ``jvvv.preview_config``.

Covers spec section 49 "Profile-ID tests" and the non-widget half of
"Settings tests": recommended defaults, allowed ranges, deterministic and
filesystem-safe profile IDs, locale-independent FPS canonicalisation, and the
``as_mapping`` / ``from_mapping`` persistence round trip.  The module under
test is Qt-free, so nothing here needs a ``QApplication`` or FFmpeg.
"""

from __future__ import annotations

import contextlib
import dataclasses
import decimal
import locale
import math
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Sequence

import pytest

from jvvv.preview_config import (
    BACKUP_POLICY_TEXT,
    DEFAULT_IMAGE_JPEG_QUALITY,
    DEFAULT_IMAGE_MAX_DIMENSION,
    DEFAULT_PREVIEWS_ENABLED,
    DEFAULT_VIDEO_CRF,
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_MAX_HEIGHT,
    DEFAULT_VIDEO_PRESET,
    IMAGE_JPEG_QUALITY_RANGE,
    IMAGE_MAX_DIMENSION_RANGE,
    IMAGE_PREVIEW_EXTENSION,
    PREVIEW_EXTENSIONS,
    PREVIEW_MEDIA_KINDS,
    PREVIEW_SETTING_KEYS,
    PREVIEWS_ENABLED_SETTING,
    PREVIEWS_FFMPEG_PATH_SETTING,
    PREVIEWS_IMAGE_JPEG_QUALITY_SETTING,
    PREVIEWS_IMAGE_MAX_DIMENSION_SETTING,
    PREVIEWS_ROOT_SETTING,
    PREVIEWS_VIDEO_CRF_SETTING,
    PREVIEWS_VIDEO_FPS_SETTING,
    PREVIEWS_VIDEO_MAX_HEIGHT_SETTING,
    PREVIEWS_VIDEO_PRESET_SETTING,
    ROOT_CHANGE_WARNING_TEXT,
    STORAGE_TRADEOFF_TEXT,
    VIDEO_CRF_RANGE,
    VIDEO_FPS_RANGE,
    VIDEO_MAX_HEIGHT_RANGE,
    VIDEO_PRESET_DESCRIPTIONS,
    VIDEO_PRESETS,
    VIDEO_PREVIEW_EXTENSION,
    ImagePreviewProfile,
    PreviewConfigError,
    PreviewSettings,
    VideoPreviewProfile,
    default_preview_settings,
    format_fps,
    preview_extension_for,
)


# Spec section 5: "Only safe filename characters should be used."
SAFE_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

# Spec section 1: the persisted values every implementation must provide.
SPEC_REQUIRED_SETTING_KEYS = frozenset(
    {
        "previews/enabled",
        "previews/root_directory",
        "previews/image/max_dimension",
        "previews/image/jpeg_quality",
        "previews/video/fps",
        "previews/video/max_height",
        "previews/video/crf",
        "previews/video/preset",
    }
)

# Spec section 1: the recommended presets, in the order the UI exposes them.
SPEC_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")

FRENCH_LOCALES = ("fr_FR.UTF-8", "fr_FR", "French_France.1252", "fr-FR")
GERMAN_LOCALES = ("de_DE.UTF-8", "de_DE", "German_Germany.1252", "de-DE")

# Spec section 5 canonicalisation examples plus rounding/normalisation cases.
CANONICAL_FPS_TEXT: dict[float, str] = {
    1.0: "1",
    0.5: "0.5",
    2.0: "2",
    0.25: "0.25",
    10.0: "10",
    0.1: "0.1",
    0.333333: "0.333",
    1.0000001: "1",
}


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _settings_with_root(**overrides: Any) -> PreviewSettings:
    """A valid settings object with a storage root selected."""

    values: dict[str, Any] = {"root_directory": "E:/JVVV Previews"}
    values.update(overrides)
    return PreviewSettings(**values)


@contextlib.contextmanager
def _comma_decimal_locale(candidates: Sequence[str]) -> Iterator[str]:
    """Activate the first available locale whose decimal separator is a comma.

    Skips the test when none of ``candidates`` can be set on this machine and
    always restores the previous locale, even when the body raises.
    """

    original = locale.setlocale(locale.LC_ALL)
    try:
        chosen: str | None = None
        for name in candidates:
            try:
                locale.setlocale(locale.LC_ALL, name)
            except locale.Error:
                continue
            if locale.localeconv()["decimal_point"] == ",":
                chosen = name
                break
            locale.setlocale(locale.LC_ALL, original)
        if chosen is None:
            pytest.skip(f"No comma-decimal locale available among {candidates!r}")
        yield chosen
    finally:
        locale.setlocale(locale.LC_ALL, original)


# ---------------------------------------------------------------------------
# Constants and recommended defaults
# ---------------------------------------------------------------------------


def test_recommended_defaults_match_spec() -> None:
    settings = PreviewSettings()

    assert settings.enabled is False
    assert DEFAULT_PREVIEWS_ENABLED is False
    assert settings.root_directory == ""
    assert settings.ffmpeg_path == ""
    assert settings.image == ImagePreviewProfile(max_dimension=1600, jpeg_quality=82)
    assert settings.video == VideoPreviewProfile(
        fps=1.0, max_height=240, crf=35, preset="veryfast"
    )
    assert (DEFAULT_IMAGE_MAX_DIMENSION, DEFAULT_IMAGE_JPEG_QUALITY) == (1600, 82)
    assert (DEFAULT_VIDEO_FPS, DEFAULT_VIDEO_MAX_HEIGHT, DEFAULT_VIDEO_CRF) == (1.0, 240, 35)
    assert DEFAULT_VIDEO_PRESET == "veryfast"
    assert type(settings.video.fps) is float


def test_default_settings_are_valid_apart_from_the_missing_root() -> None:
    settings = default_preview_settings()

    assert settings == PreviewSettings()
    settings.validate(require_root=False)
    assert settings.profile_id("image") == "jpeg-max1600-q82"
    assert settings.profile_id("video") == "h264-1fps-240p-crf35-veryfast"


def test_allowed_ranges_match_spec() -> None:
    assert IMAGE_MAX_DIMENSION_RANGE == (320, 8192)
    assert IMAGE_JPEG_QUALITY_RANGE == (40, 100)
    assert VIDEO_FPS_RANGE == (0.1, 10.0)
    assert VIDEO_MAX_HEIGHT_RANGE == (120, 2160)
    assert VIDEO_CRF_RANGE == (18, 45)


def test_presets_match_spec_and_each_has_a_description() -> None:
    assert VIDEO_PRESETS == SPEC_PRESETS
    assert set(VIDEO_PRESET_DESCRIPTIONS) == set(VIDEO_PRESETS)
    assert all(description.strip() for description in VIDEO_PRESET_DESCRIPTIONS.values())
    assert DEFAULT_VIDEO_PRESET in VIDEO_PRESETS


def test_setting_keys_cover_every_spec_key_exactly_once() -> None:
    assert len(set(PREVIEW_SETTING_KEYS)) == len(PREVIEW_SETTING_KEYS)
    assert SPEC_REQUIRED_SETTING_KEYS <= set(PREVIEW_SETTING_KEYS)
    # The only addition beyond the spec list is the explicit FFmpeg path.
    assert set(PREVIEW_SETTING_KEYS) - SPEC_REQUIRED_SETTING_KEYS == {
        PREVIEWS_FFMPEG_PATH_SETTING
    }
    assert all(key.startswith("previews/") for key in PREVIEW_SETTING_KEYS)
    assert PREVIEWS_ENABLED_SETTING == "previews/enabled"
    assert PREVIEWS_ROOT_SETTING == "previews/root_directory"
    assert PREVIEWS_IMAGE_MAX_DIMENSION_SETTING == "previews/image/max_dimension"
    assert PREVIEWS_IMAGE_JPEG_QUALITY_SETTING == "previews/image/jpeg_quality"
    assert PREVIEWS_VIDEO_FPS_SETTING == "previews/video/fps"
    assert PREVIEWS_VIDEO_MAX_HEIGHT_SETTING == "previews/video/max_height"
    assert PREVIEWS_VIDEO_CRF_SETTING == "previews/video/crf"
    assert PREVIEWS_VIDEO_PRESET_SETTING == "previews/video/preset"


def test_media_kinds_and_extensions() -> None:
    assert PREVIEW_MEDIA_KINDS == ("image", "video")
    assert tuple(PREVIEW_EXTENSIONS) == PREVIEW_MEDIA_KINDS
    assert (IMAGE_PREVIEW_EXTENSION, VIDEO_PREVIEW_EXTENSION) == ("jpg", "mp4")
    assert preview_extension_for("image") == "jpg"
    assert preview_extension_for("video") == "mp4"
    assert ImagePreviewProfile().extension == preview_extension_for("image")
    assert VideoPreviewProfile().extension == preview_extension_for("video")


@pytest.mark.parametrize("media_kind", ["audio", "document", "", "Image", "IMAGE", None])
def test_preview_extension_for_rejects_non_preview_kinds(media_kind: Any) -> None:
    with pytest.raises(PreviewConfigError, match=re.escape(repr(media_kind))):
        preview_extension_for(media_kind)


def test_preview_config_error_is_a_value_error() -> None:
    assert issubclass(PreviewConfigError, ValueError)
    with pytest.raises(ValueError):
        ImagePreviewProfile(max_dimension=319).validate()


# ---------------------------------------------------------------------------
# Profile IDs (spec sections 5, 21, 49)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "max_dimension, jpeg_quality, expected",
    [
        (1600, 82, "jpeg-max1600-q82"),
        (1024, 75, "jpeg-max1024-q75"),
        (4096, 92, "jpeg-max4096-q92"),
        (320, 40, "jpeg-max320-q40"),
        (8192, 100, "jpeg-max8192-q100"),
    ],
)
def test_image_profile_ids_are_deterministic(
    max_dimension: int, jpeg_quality: int, expected: str
) -> None:
    profile = ImagePreviewProfile(max_dimension=max_dimension, jpeg_quality=jpeg_quality)

    assert profile.profile_id == expected
    # Repeated evaluation and independent instances agree.
    assert profile.profile_id == profile.profile_id
    assert ImagePreviewProfile(max_dimension, jpeg_quality).profile_id == expected
    assert _settings_with_root(image=profile).profile_id("image") == expected


@pytest.mark.parametrize(
    "fps, max_height, crf, preset, expected",
    [
        (1.0, 240, 35, "veryfast", "h264-1fps-240p-crf35-veryfast"),
        (0.5, 180, 38, "veryfast", "h264-0.5fps-180p-crf38-veryfast"),
        (2.0, 720, 28, "fast", "h264-2fps-720p-crf28-fast"),
        (0.1, 120, 18, "ultrafast", "h264-0.1fps-120p-crf18-ultrafast"),
        (10.0, 2160, 45, "slow", "h264-10fps-2160p-crf45-slow"),
        (0.25, 1080, 23, "medium", "h264-0.25fps-1080p-crf23-medium"),
    ],
)
def test_video_profile_ids_are_deterministic(
    fps: float, max_height: int, crf: int, preset: str, expected: str
) -> None:
    profile = VideoPreviewProfile(fps=fps, max_height=max_height, crf=crf, preset=preset)

    assert profile.profile_id == expected
    assert profile.profile_id == profile.profile_id
    assert VideoPreviewProfile(fps, max_height, crf, preset).profile_id == expected
    assert _settings_with_root(video=profile).profile_id("video") == expected


@pytest.mark.parametrize("fps", [1, 1.0, 1.0000001, 0.9999999])
def test_equivalent_fps_values_produce_the_same_video_profile_id(fps: float) -> None:
    profile = VideoPreviewProfile(fps=fps)

    assert profile.profile_id == "h264-1fps-240p-crf35-veryfast"
    assert profile.fps_text == "1"


IMAGE_FIELD_CHANGES = [
    ("max_dimension", 1601),
    ("max_dimension", 1024),
    ("jpeg_quality", 83),
    ("jpeg_quality", 75),
]

VIDEO_FIELD_CHANGES = [
    ("fps", 2.0),
    ("fps", 0.5),
    ("fps", 1.001),  # the smallest fractional change the canonical form keeps
    ("max_height", 241),
    ("max_height", 720),
    ("crf", 36),
    ("crf", 28),
    ("preset", "fast"),
    ("preset", "ultrafast"),
]


@pytest.mark.parametrize("field_name, new_value", IMAGE_FIELD_CHANGES)
def test_changing_any_image_setting_changes_the_profile_id(
    field_name: str, new_value: Any
) -> None:
    base = ImagePreviewProfile()
    changed = dataclasses.replace(base, **{field_name: new_value})

    assert getattr(changed, field_name) != getattr(base, field_name)
    assert changed.profile_id != base.profile_id
    assert changed.profile_id.startswith("jpeg-")


@pytest.mark.parametrize("field_name, new_value", VIDEO_FIELD_CHANGES)
def test_changing_any_video_setting_changes_the_profile_id(
    field_name: str, new_value: Any
) -> None:
    base = VideoPreviewProfile()
    changed = dataclasses.replace(base, **{field_name: new_value})

    assert getattr(changed, field_name) != getattr(base, field_name)
    assert changed.profile_id != base.profile_id
    assert changed.profile_id.startswith("h264-")


def test_every_profile_field_is_exercised_by_the_change_tests() -> None:
    """Adding an output-affecting field must force the change tests to grow."""

    image_fields = {field.name for field in dataclasses.fields(ImagePreviewProfile)}
    video_fields = {field.name for field in dataclasses.fields(VideoPreviewProfile)}

    assert image_fields == {name for name, _ in IMAGE_FIELD_CHANGES} == {
        "max_dimension",
        "jpeg_quality",
    }
    assert video_fields == {name for name, _ in VIDEO_FIELD_CHANGES} == {
        "fps",
        "max_height",
        "crf",
        "preset",
    }


def test_image_profile_ids_use_only_safe_filename_characters() -> None:
    seen: set[str] = set()
    for max_dimension in (320, 1024, 1600, 4096, 8192):
        for jpeg_quality in (40, 75, 82, 92, 100):
            profile_id = ImagePreviewProfile(max_dimension, jpeg_quality).profile_id
            assert SAFE_PROFILE_ID_RE.fullmatch(profile_id), profile_id
            assert profile_id not in seen, f"duplicate ID {profile_id}"
            seen.add(profile_id)


def test_video_profile_ids_use_only_safe_filename_characters() -> None:
    seen: set[str] = set()
    for fps in (0.1, 0.25, 0.333, 0.5, 1.0, 1.5, 2.0, 2.997, 10.0):
        for max_height in (120, 240, 720, 1080, 2160):
            for crf in (18, 35, 45):
                for preset in VIDEO_PRESETS:
                    profile_id = VideoPreviewProfile(fps, max_height, crf, preset).profile_id
                    assert SAFE_PROFILE_ID_RE.fullmatch(profile_id), profile_id
                    assert "," not in profile_id
                    assert profile_id not in seen, f"duplicate ID {profile_id}"
                    seen.add(profile_id)


def test_profile_for_and_profile_id_dispatch_by_media_kind() -> None:
    image = ImagePreviewProfile(1024, 75)
    video = VideoPreviewProfile(2.0, 720, 28, "fast")
    settings = _settings_with_root(image=image, video=video)

    assert settings.profile_for("image") is image
    assert settings.profile_for("video") is video
    assert settings.profile_id("image") == "jpeg-max1024-q75"
    assert settings.profile_id("video") == "h264-2fps-720p-crf28-fast"


@pytest.mark.parametrize("media_kind", ["audio", "document", "", "Video", None])
def test_profile_id_and_profile_for_reject_non_preview_kinds(media_kind: Any) -> None:
    settings = _settings_with_root()

    with pytest.raises(PreviewConfigError, match=re.escape(repr(media_kind))):
        settings.profile_id(media_kind)
    with pytest.raises(PreviewConfigError, match=re.escape(repr(media_kind))):
        settings.profile_for(media_kind)


def test_describe_lists_the_properties_shown_in_the_properties_panel() -> None:
    """Spec section 20: profile description lines for images and videos."""

    assert ImagePreviewProfile().describe() == (
        "JPEG",
        "Max dimension: 1600 px",
        "Quality: 82",
    )
    assert VideoPreviewProfile().describe() == (
        "H.264 MP4",
        "1 fps",
        "Max height 240 px",
        "CRF 35",
        "Preset veryfast",
        "No audio",
    )
    fractional = VideoPreviewProfile(fps=0.5, max_height=1080, crf=23, preset="medium")
    assert fractional.describe()[1] == "0.5 fps"
    assert fractional.describe()[2] == "Max height 1080 px"


# ---------------------------------------------------------------------------
# FPS canonicalisation (spec section 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps, expected", sorted(CANONICAL_FPS_TEXT.items()))
def test_format_fps_canonicalises_equivalent_values(fps: float, expected: str) -> None:
    text = format_fps(fps)

    assert text == expected
    assert "," not in text
    assert SAFE_PROFILE_ID_RE.fullmatch(text)
    assert VideoPreviewProfile(fps=fps).fps_text == expected
    assert VideoPreviewProfile(fps=fps).profile_id.startswith(f"h264-{expected}fps-")


def test_format_fps_accepts_integers_and_rounds_to_three_decimals() -> None:
    assert format_fps(1) == "1"
    assert format_fps(2) == "2"
    assert format_fps(0.12345) == "0.123"
    assert format_fps(2.9999) == "3"
    assert format_fps(0.0999999) == "0.1"  # rounds up into range
    assert format_fps(10.0004) == "10"  # rounds down into range


@pytest.mark.parametrize(
    "candidates", [FRENCH_LOCALES, GERMAN_LOCALES], ids=["french", "german"]
)
def test_format_fps_never_uses_a_comma_under_comma_decimal_locales(
    candidates: Sequence[str],
) -> None:
    before = locale.setlocale(locale.LC_ALL)

    with _comma_decimal_locale(candidates):
        # Prove the locale is really in effect for locale-aware formatting.
        assert locale.format_string("%g", 0.5) == "0,5"
        assert format(0.5, "n") == "0,5"

        for fps, expected in CANONICAL_FPS_TEXT.items():
            text = format_fps(fps)
            assert "," not in text
            assert text == expected
        profile_id = VideoPreviewProfile(fps=0.5, max_height=180, crf=38).profile_id
        assert profile_id == "h264-0.5fps-180p-crf38-veryfast"
        assert "," not in profile_id
        assert SAFE_PROFILE_ID_RE.fullmatch(profile_id)
        # The persisted FPS value is a plain float, never locale text.
        stored = PreviewSettings(video=VideoPreviewProfile(fps=0.5)).as_mapping()
        assert stored[PREVIEWS_VIDEO_FPS_SETTING] == 0.5
        assert type(stored[PREVIEWS_VIDEO_FPS_SETTING]) is float

    assert locale.setlocale(locale.LC_ALL) == before
    assert locale.localeconv()["decimal_point"] == "."


@pytest.mark.parametrize(
    "fps",
    [0.05, 10.5, 0.0994, 0.0, -1.0, math.nan, math.inf, -math.inf, True, "1.0", None],
    ids=repr,
)
def test_format_fps_rejects_invalid_values(fps: Any) -> None:
    with pytest.raises(PreviewConfigError, match="Video FPS"):
        format_fps(fps)


# ---------------------------------------------------------------------------
# Range validation (spec sections 1, 2D, 49)
# ---------------------------------------------------------------------------

INVALID_IMAGE_VALUES: list[tuple[str, Any, str]] = [
    ("max_dimension", 319, "Image maximum dimension"),
    ("max_dimension", 8193, "Image maximum dimension"),
    ("max_dimension", 0, "Image maximum dimension"),
    ("max_dimension", -1600, "Image maximum dimension"),
    ("max_dimension", True, "Image maximum dimension"),
    ("max_dimension", False, "Image maximum dimension"),
    ("max_dimension", 1600.0, "Image maximum dimension"),
    ("max_dimension", "1600", "Image maximum dimension"),
    ("max_dimension", None, "Image maximum dimension"),
    ("jpeg_quality", 39, "Image JPEG quality"),
    ("jpeg_quality", 101, "Image JPEG quality"),
    ("jpeg_quality", True, "Image JPEG quality"),
    ("jpeg_quality", False, "Image JPEG quality"),
    ("jpeg_quality", 82.0, "Image JPEG quality"),
    ("jpeg_quality", "82", "Image JPEG quality"),
    ("jpeg_quality", None, "Image JPEG quality"),
]

INVALID_VIDEO_VALUES: list[tuple[str, Any, str]] = [
    ("fps", 0.05, "Video FPS"),
    ("fps", 10.5, "Video FPS"),
    ("fps", 0.0, "Video FPS"),
    ("fps", -1.0, "Video FPS"),
    ("fps", math.nan, "Video FPS"),
    ("fps", math.inf, "Video FPS"),
    ("fps", True, "Video FPS"),
    ("fps", "1.0", "Video FPS"),
    ("fps", None, "Video FPS"),
    ("max_height", 119, "Video maximum height"),
    ("max_height", 2161, "Video maximum height"),
    ("max_height", True, "Video maximum height"),
    ("max_height", 240.0, "Video maximum height"),
    ("max_height", "240", "Video maximum height"),
    ("max_height", None, "Video maximum height"),
    ("crf", 17, "Video CRF"),
    ("crf", 46, "Video CRF"),
    ("crf", True, "Video CRF"),
    ("crf", 35.0, "Video CRF"),
    ("crf", "35", "Video CRF"),
    ("crf", None, "Video CRF"),
    ("preset", "placebo", "Video encoder preset"),
    ("preset", "VERYFAST", "Video encoder preset"),
    ("preset", " veryfast", "Video encoder preset"),
    ("preset", "", "Video encoder preset"),
    ("preset", None, "Video encoder preset"),
    ("preset", 3, "Video encoder preset"),
]


def _case_id(case: tuple[str, Any, str]) -> str:
    field_name, value, _ = case
    return f"{field_name}={value!r}"


@pytest.mark.parametrize("case", INVALID_IMAGE_VALUES, ids=_case_id)
def test_invalid_image_values_are_rejected_with_the_field_name(
    case: tuple[str, Any, str],
) -> None:
    field_name, value, field_label = case
    profile = dataclasses.replace(ImagePreviewProfile(), **{field_name: value})

    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        profile.validate()
    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        _ = profile.profile_id
    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        _settings_with_root(image=profile).validate()
    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        PreviewSettings(image=profile).validate(require_root=False)


@pytest.mark.parametrize("case", INVALID_VIDEO_VALUES, ids=_case_id)
def test_invalid_video_values_are_rejected_with_the_field_name(
    case: tuple[str, Any, str],
) -> None:
    field_name, value, field_label = case
    profile = dataclasses.replace(VideoPreviewProfile(), **{field_name: value})

    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        profile.validate()
    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        _ = profile.profile_id
    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        _settings_with_root(video=profile).validate()
    with pytest.raises(PreviewConfigError, match=re.escape(field_label)):
        PreviewSettings(video=profile).validate(require_root=False)


@pytest.mark.parametrize(
    "profile, bounds",
    [
        (ImagePreviewProfile(max_dimension=319), ("320", "8192")),
        (ImagePreviewProfile(max_dimension=8193), ("320", "8192")),
        (ImagePreviewProfile(jpeg_quality=39), ("40", "100")),
        (ImagePreviewProfile(jpeg_quality=101), ("40", "100")),
        (VideoPreviewProfile(fps=0.05), ("0.1", "10")),
        (VideoPreviewProfile(fps=10.5), ("0.1", "10")),
        (VideoPreviewProfile(max_height=119), ("120", "2160")),
        (VideoPreviewProfile(max_height=2161), ("120", "2160")),
        (VideoPreviewProfile(crf=17), ("18", "45")),
        (VideoPreviewProfile(crf=46), ("18", "45")),
    ],
)
def test_out_of_range_messages_explain_the_allowed_range(
    profile: ImagePreviewProfile | VideoPreviewProfile, bounds: tuple[str, str]
) -> None:
    with pytest.raises(PreviewConfigError) as excinfo:
        profile.validate()

    message = str(excinfo.value)
    for bound in bounds:
        assert bound in message
    # The offending value at the end of the message is rendered locale-free.
    assert "," not in message.split("not ")[-1]


def test_unsupported_preset_message_lists_the_supported_presets() -> None:
    with pytest.raises(PreviewConfigError) as excinfo:
        VideoPreviewProfile(preset="placebo").validate()

    message = str(excinfo.value)
    assert "'placebo'" in message
    for preset in VIDEO_PRESETS:
        assert preset in message


@pytest.mark.parametrize(
    "profile",
    [
        ImagePreviewProfile(320, 40),
        ImagePreviewProfile(320, 100),
        ImagePreviewProfile(8192, 40),
        ImagePreviewProfile(8192, 100),
        VideoPreviewProfile(0.1, 120, 18, "ultrafast"),
        VideoPreviewProfile(10.0, 2160, 45, "slow"),
        VideoPreviewProfile(0.0999999, 240, 35, "veryfast"),  # canonicalises to 0.1
        VideoPreviewProfile(10.0004, 240, 35, "veryfast"),  # canonicalises to 10
        VideoPreviewProfile(1, 240, 35, "veryfast"),  # integer FPS
    ],
)
def test_boundary_values_are_accepted(
    profile: ImagePreviewProfile | VideoPreviewProfile,
) -> None:
    profile.validate()

    assert SAFE_PROFILE_ID_RE.fullmatch(profile.profile_id)
    if isinstance(profile, ImagePreviewProfile):
        _settings_with_root(image=profile).validate()
    else:
        _settings_with_root(video=profile).validate()


@pytest.mark.parametrize("preset", VIDEO_PRESETS)
def test_every_spec_preset_is_accepted_and_appears_verbatim_in_the_id(preset: str) -> None:
    profile = VideoPreviewProfile(preset=preset)

    profile.validate()
    assert profile.profile_id == f"h264-1fps-240p-crf35-{preset}"


def test_large_profiles_are_allowed_within_the_ranges() -> None:
    """Spec section 36: high-quality previews must not be artificially blocked."""

    image = ImagePreviewProfile(max_dimension=4096, jpeg_quality=95)
    video = VideoPreviewProfile(fps=2.0, max_height=1080, crf=23, preset="medium")

    settings = _settings_with_root(image=image, video=video)
    settings.validate()
    assert settings.profile_id("image") == "jpeg-max4096-q95"
    assert settings.profile_id("video") == "h264-2fps-1080p-crf23-medium"


# ---------------------------------------------------------------------------
# PreviewSettings.validate and derived paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root_directory", ["", "   ", "\t\n"])
def test_validate_requires_a_root_only_when_asked(root_directory: str) -> None:
    settings = PreviewSettings(root_directory=root_directory)

    with pytest.raises(PreviewConfigError, match="preview storage directory"):
        settings.validate()
    with pytest.raises(PreviewConfigError, match="preview storage directory"):
        settings.validate(require_root=True)
    settings.validate(require_root=False)
    assert settings.root_path is None


@pytest.mark.parametrize(
    "root_directory",
    [
        "D:\\JVVV Previews",
        "E:\\Archive Proxy Media",
        "\\\\NAS01\\JVVV-Previews",
        "/mnt/archive-previews",
        "  E:/JVVV Previews  ",
    ],
)
def test_validate_accepts_any_selected_root_without_touching_it(
    root_directory: str,
) -> None:
    settings = PreviewSettings(root_directory=root_directory)

    settings.validate()
    settings.validate(require_root=True)
    assert settings.root_path == Path(root_directory.strip())


def test_validate_checks_profiles_before_the_root() -> None:
    settings = PreviewSettings(image=ImagePreviewProfile(max_dimension=319))

    with pytest.raises(PreviewConfigError, match="Image maximum dimension"):
        settings.validate()
    with pytest.raises(PreviewConfigError, match="Image maximum dimension"):
        settings.validate(require_root=False)


def test_validate_and_profile_ids_never_touch_the_filesystem(tmp_path: Path) -> None:
    root = tmp_path / "previews-root-that-must-not-be-created"
    settings = PreviewSettings(root_directory=str(root))

    settings.validate()
    assert settings.profile_id("image") == "jpeg-max1600-q82"
    assert settings.profile_id("video") == "h264-1fps-240p-crf35-veryfast"
    assert settings.root_path == root
    assert settings.as_mapping()[PREVIEWS_ROOT_SETTING] == str(root)

    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_root_path_expands_the_home_directory() -> None:
    settings = PreviewSettings(root_directory="~/jvvv-previews")

    root_path = settings.root_path
    assert root_path is not None
    assert root_path == Path("~/jvvv-previews").expanduser()
    assert not str(root_path).startswith("~")


def test_ffmpeg_path_or_none_strips_and_blanks() -> None:
    assert PreviewSettings().ffmpeg_path_or_none is None
    assert PreviewSettings(ffmpeg_path="   ").ffmpeg_path_or_none is None
    assert (
        PreviewSettings(ffmpeg_path="  C:/ffmpeg/bin/ffmpeg.exe ").ffmpeg_path_or_none
        == "C:/ffmpeg/bin/ffmpeg.exe"
    )


# ---------------------------------------------------------------------------
# with_enabled / output_signature / immutability
# ---------------------------------------------------------------------------


def test_with_enabled_returns_a_copy_and_leaves_the_original_alone() -> None:
    original = _settings_with_root()

    enabled = original.with_enabled(True)
    assert enabled is not original
    assert enabled.enabled is True
    assert original.enabled is False
    assert dataclasses.replace(enabled, enabled=False) == original
    assert enabled.with_enabled(False) == original


@pytest.mark.parametrize("value, expected", [(1, True), (0, False), ("x", True), ("", False)])
def test_with_enabled_coerces_to_a_real_bool(value: Any, expected: bool) -> None:
    result = PreviewSettings().with_enabled(value)

    assert result.enabled is expected
    assert type(result.enabled) is bool


def test_output_signature_ignores_enabled() -> None:
    settings = _settings_with_root(
        ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
        image=ImagePreviewProfile(1024, 75),
        video=VideoPreviewProfile(0.5, 180, 38, "veryfast"),
    )

    assert settings.output_signature() == settings.with_enabled(True).output_signature()
    assert settings.output_signature() == settings.with_enabled(False).output_signature()
    assert hash(settings.output_signature()) == hash(
        settings.with_enabled(True).output_signature()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"root_directory": "F:/Other Previews"},
        {"ffmpeg_path": "D:/tools/ffmpeg.exe"},
        {"image": ImagePreviewProfile(1024, 82)},
        {"image": ImagePreviewProfile(1600, 75)},
        {"video": VideoPreviewProfile(2.0, 240, 35, "veryfast")},
        {"video": VideoPreviewProfile(1.0, 720, 35, "veryfast")},
        {"video": VideoPreviewProfile(1.0, 240, 28, "veryfast")},
        {"video": VideoPreviewProfile(1.0, 240, 35, "fast")},
    ],
    ids=lambda change: next(iter(change)),
)
def test_output_signature_changes_with_every_configuration_change(
    change: dict[str, Any],
) -> None:
    base = _settings_with_root(ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe")
    changed = dataclasses.replace(base, **change)

    assert changed.output_signature() != base.output_signature()
    # Whether previews are enabled does not mask the change.
    assert changed.with_enabled(True).output_signature() != base.output_signature()


def test_output_signature_ignores_surrounding_whitespace_in_paths() -> None:
    padded = PreviewSettings(root_directory="  E:/x  ", ffmpeg_path=" C:/f.exe ")
    trimmed = PreviewSettings(root_directory="E:/x", ffmpeg_path="C:/f.exe")

    assert padded.output_signature() == trimmed.output_signature()


def test_settings_and_profiles_are_frozen_and_hashable() -> None:
    settings = _settings_with_root()

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.enabled = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.image.max_dimension = 1024  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.video.fps = 2.0  # type: ignore[misc]

    assert hash(settings) == hash(_settings_with_root())
    assert {settings: "a"}[_settings_with_root()] == "a"
    assert hash(ImagePreviewProfile()) == hash(ImagePreviewProfile(1600, 82))
    assert hash(VideoPreviewProfile()) == hash(VideoPreviewProfile(1.0, 240, 35, "veryfast"))


# ---------------------------------------------------------------------------
# as_mapping (persistence, spec section 1)
# ---------------------------------------------------------------------------

EXPECTED_MAPPING_TYPES: dict[str, type] = {
    PREVIEWS_ENABLED_SETTING: bool,
    PREVIEWS_ROOT_SETTING: str,
    PREVIEWS_FFMPEG_PATH_SETTING: str,
    PREVIEWS_IMAGE_MAX_DIMENSION_SETTING: int,
    PREVIEWS_IMAGE_JPEG_QUALITY_SETTING: int,
    PREVIEWS_VIDEO_FPS_SETTING: float,
    PREVIEWS_VIDEO_MAX_HEIGHT_SETTING: int,
    PREVIEWS_VIDEO_CRF_SETTING: int,
    PREVIEWS_VIDEO_PRESET_SETTING: str,
}

SAMPLE_SETTINGS = [
    PreviewSettings(),
    PreviewSettings(
        enabled=True,
        root_directory="E:/JVVV Previews",
        ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
        image=ImagePreviewProfile(4096, 92),
        video=VideoPreviewProfile(2.0, 720, 28, "fast"),
    ),
    PreviewSettings(
        root_directory="\\\\NAS01\\JVVV-Previews",
        image=ImagePreviewProfile(320, 40),
        video=VideoPreviewProfile(0.1, 120, 18, "ultrafast"),
    ),
    PreviewSettings(
        enabled=True,
        root_directory="/mnt/archive-previews",
        image=ImagePreviewProfile(8192, 100),
        video=VideoPreviewProfile(10.0, 2160, 45, "slow"),
    ),
    PreviewSettings(
        root_directory="D:\\JVVV Previews",
        video=VideoPreviewProfile(0.5, 180, 38, "veryfast"),
    ),
]


@pytest.mark.parametrize("settings", SAMPLE_SETTINGS)
def test_as_mapping_keys_equal_the_setting_keys_exactly(settings: PreviewSettings) -> None:
    mapping = settings.as_mapping()

    assert tuple(mapping) == PREVIEW_SETTING_KEYS
    assert set(mapping) == set(PREVIEW_SETTING_KEYS)
    assert set(EXPECTED_MAPPING_TYPES) == set(PREVIEW_SETTING_KEYS)


@pytest.mark.parametrize("settings", SAMPLE_SETTINGS)
def test_as_mapping_values_have_stable_scalar_types(settings: PreviewSettings) -> None:
    mapping = settings.as_mapping()

    for key, expected_type in EXPECTED_MAPPING_TYPES.items():
        assert type(mapping[key]) is expected_type, (key, mapping[key])


def test_as_mapping_reports_the_defaults() -> None:
    assert PreviewSettings().as_mapping() == {
        "previews/enabled": False,
        "previews/root_directory": "",
        "previews/ffmpeg_path": "",
        "previews/image/max_dimension": 1600,
        "previews/image/jpeg_quality": 82,
        "previews/video/fps": 1.0,
        "previews/video/max_height": 240,
        "previews/video/crf": 35,
        "previews/video/preset": "veryfast",
    }


def test_as_mapping_strips_paths_and_canonicalises_fps() -> None:
    settings = PreviewSettings(
        root_directory="  E:/JVVV Previews  ",
        ffmpeg_path="\tC:/ffmpeg/bin/ffmpeg.exe\n",
        video=VideoPreviewProfile(fps=1.0000001),
    )

    mapping = settings.as_mapping()
    assert mapping[PREVIEWS_ROOT_SETTING] == "E:/JVVV Previews"
    assert mapping[PREVIEWS_FFMPEG_PATH_SETTING] == "C:/ffmpeg/bin/ffmpeg.exe"
    assert mapping[PREVIEWS_VIDEO_FPS_SETTING] == 1.0
    rounded = PreviewSettings(video=VideoPreviewProfile(fps=0.3333333)).as_mapping()
    assert rounded[PREVIEWS_VIDEO_FPS_SETTING] == 0.333


# ---------------------------------------------------------------------------
# from_mapping (persistence, spec sections 1 and 49)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("settings", SAMPLE_SETTINGS)
def test_from_mapping_round_trips_as_mapping(settings: PreviewSettings) -> None:
    restored = PreviewSettings.from_mapping(settings.as_mapping())

    assert restored == settings
    assert restored.as_mapping() == settings.as_mapping()
    assert restored.enabled is settings.enabled
    assert restored.profile_id("image") == settings.profile_id("image")
    assert restored.profile_id("video") == settings.profile_id("video")


@pytest.mark.parametrize("settings", SAMPLE_SETTINGS)
def test_from_mapping_round_trips_stringified_values(settings: PreviewSettings) -> None:
    """INI-backed QSettings hands every value back as text."""

    stringified = {key: str(value) for key, value in settings.as_mapping().items()}

    assert PreviewSettings.from_mapping(stringified) == settings


def test_from_mapping_accepts_none_empty_and_read_only_mappings() -> None:
    assert PreviewSettings.from_mapping(None) == PreviewSettings()
    assert PreviewSettings.from_mapping({}) == PreviewSettings()
    read_only = MappingProxyType({PREVIEWS_IMAGE_MAX_DIMENSION_SETTING: 1024})
    assert PreviewSettings.from_mapping(read_only).image.max_dimension == 1024


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        ("true", True),
        ("True", True),
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        (1, True),
        (2, True),
        (1.0, True),
        (False, False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        (0, False),
        (0.0, False),
        (None, False),
        ("banana", False),
        ("enabled", False),
        ("2", False),
        ([True], False),
    ],
    ids=repr,
)
def test_from_mapping_enabled_is_only_true_for_recognised_true_values(
    value: Any, expected: bool
) -> None:
    settings = PreviewSettings.from_mapping({PREVIEWS_ENABLED_SETTING: value})

    assert settings.enabled is expected


def test_from_mapping_enabled_defaults_to_false_when_missing() -> None:
    assert PreviewSettings.from_mapping({PREVIEWS_ROOT_SETTING: "E:/x"}).enabled is False


@pytest.mark.parametrize(
    "key, value, expected",
    [
        (PREVIEWS_IMAGE_MAX_DIMENSION_SETTING, "1024", 1024),
        (PREVIEWS_IMAGE_MAX_DIMENSION_SETTING, " 2048 ", 2048),
        (PREVIEWS_IMAGE_MAX_DIMENSION_SETTING, "1024.0", 1024),
        (PREVIEWS_IMAGE_MAX_DIMENSION_SETTING, 4096.0, 4096),
        (PREVIEWS_IMAGE_JPEG_QUALITY_SETTING, "75", 75),
        (PREVIEWS_IMAGE_JPEG_QUALITY_SETTING, 92.0, 92),
        (PREVIEWS_VIDEO_FPS_SETTING, "0.5", 0.5),
        (PREVIEWS_VIDEO_FPS_SETTING, "0,5", 0.5),
        (PREVIEWS_VIDEO_FPS_SETTING, " 2 ", 2.0),
        (PREVIEWS_VIDEO_FPS_SETTING, "2,0", 2.0),
        (PREVIEWS_VIDEO_FPS_SETTING, 2, 2.0),
        (PREVIEWS_VIDEO_FPS_SETTING, 0.3333333, 0.333),
        (PREVIEWS_VIDEO_MAX_HEIGHT_SETTING, "720", 720),
        (PREVIEWS_VIDEO_MAX_HEIGHT_SETTING, 1080.0, 1080),
        (PREVIEWS_VIDEO_CRF_SETTING, "28", 28),
        (PREVIEWS_VIDEO_CRF_SETTING, 23.0, 23),
        (PREVIEWS_VIDEO_PRESET_SETTING, "fast", "fast"),
        (PREVIEWS_VIDEO_PRESET_SETTING, "FAST", "fast"),
        (PREVIEWS_VIDEO_PRESET_SETTING, " Medium ", "medium"),
        (PREVIEWS_VIDEO_PRESET_SETTING, "VeryFast", "veryfast"),
        (PREVIEWS_ROOT_SETTING, "  E:/JVVV Previews  ", "E:/JVVV Previews"),
        (PREVIEWS_ROOT_SETTING, None, ""),
        (PREVIEWS_FFMPEG_PATH_SETTING, " C:/ffmpeg/bin/ffmpeg.exe ", "C:/ffmpeg/bin/ffmpeg.exe"),
        (PREVIEWS_FFMPEG_PATH_SETTING, None, ""),
    ],
)
def test_from_mapping_tolerates_text_and_numeric_variants(
    key: str, value: Any, expected: Any
) -> None:
    settings = PreviewSettings.from_mapping({key: value})
    stored = settings.as_mapping()[key]

    assert stored == expected
    assert type(stored) is EXPECTED_MAPPING_TYPES[key]
    settings.validate(require_root=False)


def _valid_non_default_mapping() -> dict[str, Any]:
    return PreviewSettings(
        enabled=True,
        root_directory="E:/JVVV Previews",
        ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
        image=ImagePreviewProfile(4096, 92),
        video=VideoPreviewProfile(2.0, 720, 28, "fast"),
    ).as_mapping()


FALLBACK_CASES: list[tuple[str, Any, list[Any]]] = [
    (
        PREVIEWS_IMAGE_MAX_DIMENSION_SETTING,
        DEFAULT_IMAGE_MAX_DIMENSION,
        [319, 8193, 0, -1, "garbage", "", None, True, False, math.nan, math.inf, [1600], {"px": 1600}],
    ),
    (
        PREVIEWS_IMAGE_JPEG_QUALITY_SETTING,
        DEFAULT_IMAGE_JPEG_QUALITY,
        [39, 101, "garbage", "", None, True, False, math.nan],
    ),
    (
        PREVIEWS_VIDEO_FPS_SETTING,
        DEFAULT_VIDEO_FPS,
        [0.05, 10.5, 0, -1, "garbage", "", None, True, False, "nan", "inf", math.nan, math.inf, [1.0]],
    ),
    (
        PREVIEWS_VIDEO_MAX_HEIGHT_SETTING,
        DEFAULT_VIDEO_MAX_HEIGHT,
        [119, 2161, "garbage", "", None, True, False, math.inf],
    ),
    (
        PREVIEWS_VIDEO_CRF_SETTING,
        DEFAULT_VIDEO_CRF,
        [17, 46, "garbage", "", None, True, False, math.nan],
    ),
    (
        PREVIEWS_VIDEO_PRESET_SETTING,
        DEFAULT_VIDEO_PRESET,
        ["placebo", "very fast", "", None, 3, True],
    ),
]


@pytest.mark.parametrize(
    "key, default, bad_values",
    FALLBACK_CASES,
    ids=[case[0].split("/", 1)[1] for case in FALLBACK_CASES],
)
def test_from_mapping_falls_back_per_field_without_disturbing_the_others(
    key: str, default: Any, bad_values: list[Any]
) -> None:
    good = _valid_non_default_mapping()
    assert good[key] != default, "the sample must differ from the default to prove the fallback"

    for bad_value in bad_values:
        corrupted = dict(good)
        corrupted[key] = bad_value

        settings = PreviewSettings.from_mapping(corrupted)  # must not raise

        restored = settings.as_mapping()
        assert restored[key] == default, (key, bad_value)
        for other_key, other_value in good.items():
            if other_key != key:
                assert restored[other_key] == other_value, (key, bad_value, other_key)
        settings.validate()

    missing = dict(good)
    del missing[key]
    assert PreviewSettings.from_mapping(missing).as_mapping()[key] == default


def test_from_mapping_with_garbage_everywhere_yields_the_defaults_and_stays_disabled() -> None:
    garbage: dict[str, Any] = {key: object() for key in PREVIEW_SETTING_KEYS}
    garbage[PREVIEWS_ENABLED_SETTING] = "definitely"

    settings = PreviewSettings.from_mapping(garbage)

    assert settings.enabled is False
    assert settings.image == ImagePreviewProfile()
    assert settings.video == VideoPreviewProfile()
    # Free text keeps whatever text was stored; validation, not loading, judges it.
    assert isinstance(settings.root_directory, str)
    assert isinstance(settings.ffmpeg_path, str)
    settings.validate(require_root=False)


def test_from_mapping_ignores_unrelated_keys() -> None:
    mapping = _valid_non_default_mapping()
    mapping.update({"window/geometry": b"...", "previews/unknown": 1, "": None})

    assert PreviewSettings.from_mapping(mapping).as_mapping() == _valid_non_default_mapping()


# ---------------------------------------------------------------------------
# Settings-page texts (spec sections 23, 34, 37, 48)
# ---------------------------------------------------------------------------


def test_storage_tradeoff_text_contains_the_spec_sentence() -> None:
    text = _normalize_whitespace(STORAGE_TRADEOFF_TEXT)

    assert (
        "Higher image dimensions, higher JPEG quality, higher video FPS, higher video "
        "resolution, and lower CRF values use more storage."
    ) in text


def test_root_change_warning_text_contains_the_spec_sentences() -> None:
    text = _normalize_whitespace(ROOT_CHANGE_WARNING_TEXT)

    assert "does not move existing previews" in text
    assert "Changing the preview directory does not move existing previews." in text
    assert (
        "Existing previews in the previous directory will not be used unless copied "
        "manually to the new directory."
    ) in text


def test_backup_policy_text_contains_the_spec_sentences() -> None:
    text = _normalize_whitespace(BACKUP_POLICY_TEXT)

    assert "do not include offline preview files" in text
    assert "JVVV catalogue backups do not include offline preview files." in text
    assert "copy the configured preview directory separately" in text


# ---------------------------------------------------------------------------
# Absurd magnitudes must surface as PreviewConfigError, never as a raw
# decimal.InvalidOperation from the canonicalisation step.
# ---------------------------------------------------------------------------


def test_huge_fps_is_rejected_with_a_preview_config_error() -> None:
    with pytest.raises(PreviewConfigError, match="Video FPS"):
        VideoPreviewProfile(fps=1e30).validate()


def test_format_fps_rejects_a_huge_value_with_a_preview_config_error() -> None:
    with pytest.raises(PreviewConfigError, match="Video FPS"):
        format_fps(1e25)


def test_from_mapping_never_raises_for_a_huge_stored_fps() -> None:
    settings = PreviewSettings.from_mapping({PREVIEWS_VIDEO_FPS_SETTING: "1e30"})

    assert settings.video.fps == DEFAULT_VIDEO_FPS


def test_none_text_fields_are_normalized_to_empty_strings() -> None:
    settings = PreviewSettings(root_directory=None, ffmpeg_path=None)  # type: ignore[arg-type]

    assert settings.root_directory == ""
    assert settings.ffmpeg_path == ""
    assert settings.root_path is None
    with pytest.raises(PreviewConfigError, match="storage directory"):
        settings.validate(require_root=True)
