"""Offline-preview settings, validation ranges, and deterministic profile IDs.

This module is deliberately Qt-free so it can be unit tested without a
``QApplication`` and shared by the scanner, the preview cache, and the UI.

Profile IDs are the on-disk directory names that keep previews generated with
different quality settings apart (``images/jpeg-max1600-q82/`` and
``videos/h264-1fps-240p-crf35-veryfast/``).  They must be deterministic,
locale-independent, and contain only filesystem-safe characters.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Any, Mapping


# QSettings keys (also the keys of ``PreviewSettings.as_mapping``).
PREVIEWS_ENABLED_SETTING = "previews/enabled"
PREVIEWS_ROOT_SETTING = "previews/root_directory"
PREVIEWS_FFMPEG_PATH_SETTING = "previews/ffmpeg_path"
PREVIEWS_IMAGE_MAX_DIMENSION_SETTING = "previews/image/max_dimension"
PREVIEWS_IMAGE_JPEG_QUALITY_SETTING = "previews/image/jpeg_quality"
PREVIEWS_VIDEO_FPS_SETTING = "previews/video/fps"
PREVIEWS_VIDEO_MAX_HEIGHT_SETTING = "previews/video/max_height"
PREVIEWS_VIDEO_CRF_SETTING = "previews/video/crf"
PREVIEWS_VIDEO_PRESET_SETTING = "previews/video/preset"

PREVIEW_SETTING_KEYS: tuple[str, ...] = (
    PREVIEWS_ENABLED_SETTING,
    PREVIEWS_ROOT_SETTING,
    PREVIEWS_FFMPEG_PATH_SETTING,
    PREVIEWS_IMAGE_MAX_DIMENSION_SETTING,
    PREVIEWS_IMAGE_JPEG_QUALITY_SETTING,
    PREVIEWS_VIDEO_FPS_SETTING,
    PREVIEWS_VIDEO_MAX_HEIGHT_SETTING,
    PREVIEWS_VIDEO_CRF_SETTING,
    PREVIEWS_VIDEO_PRESET_SETTING,
)

# Allowed configuration ranges (inclusive).
IMAGE_MAX_DIMENSION_RANGE = (320, 8192)
IMAGE_JPEG_QUALITY_RANGE = (40, 100)
VIDEO_FPS_RANGE = (0.1, 10.0)
VIDEO_MAX_HEIGHT_RANGE = (120, 2160)
VIDEO_CRF_RANGE = (18, 45)
VIDEO_PRESETS: tuple[str, ...] = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
)
VIDEO_PRESET_DESCRIPTIONS: dict[str, str] = {
    "ultrafast": "Fastest encode, largest files",
    "superfast": "Very fast encode, large files",
    "veryfast": "Fast encode, good size (recommended)",
    "faster": "Slightly slower, slightly smaller",
    "fast": "Balanced speed and size",
    "medium": "Slower encode, smaller files",
    "slow": "Slowest encode, smallest files",
}

# Recommended defaults.
DEFAULT_PREVIEWS_ENABLED = False
DEFAULT_IMAGE_MAX_DIMENSION = 1600
DEFAULT_IMAGE_JPEG_QUALITY = 82
DEFAULT_VIDEO_FPS = 1.0
DEFAULT_VIDEO_MAX_HEIGHT = 240
DEFAULT_VIDEO_CRF = 35
DEFAULT_VIDEO_PRESET = "veryfast"

IMAGE_PREVIEW_EXTENSION = "jpg"
VIDEO_PREVIEW_EXTENSION = "mp4"
PREVIEW_MEDIA_KINDS: tuple[str, ...] = ("image", "video")
PREVIEW_EXTENSIONS: dict[str, str] = {
    "image": IMAGE_PREVIEW_EXTENSION,
    "video": VIDEO_PREVIEW_EXTENSION,
}

# Only these characters may ever appear in a profile ID.
_SAFE_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
_FPS_MAX_DECIMALS = 3


class PreviewConfigError(ValueError):
    """A preview setting is outside its supported range or otherwise invalid."""


def _check_int_range(name: str, value: Any, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreviewConfigError(f"{name} must be a whole number, not {value!r}.")
    low, high = bounds
    if value < low or value > high:
        raise PreviewConfigError(
            f"{name} must be between {low} and {high}, not {value}."
        )
    return value


def _check_fps(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreviewConfigError(f"Video FPS must be a number, not {value!r}.")
    fps = float(value)
    if not math.isfinite(fps):
        raise PreviewConfigError("Video FPS must be a finite number.")
    low, high = VIDEO_FPS_RANGE
    # Reject wildly out-of-range values on the raw float first so the decimal
    # canonicalisation below never has to represent absurd magnitudes.
    rounding_allowance = 0.5 * 10 ** (-_FPS_MAX_DECIMALS)
    if fps < low - rounding_allowance or fps > high + rounding_allowance:
        raise PreviewConfigError(
            f"Video FPS must be between {low:g} and {high:g}, not {fps:g}."
        )
    # Compare on the canonical (rounded) value so 0.0999999 is treated as 0.1.
    canonical = float(_canonical_fps_decimal(fps))
    if canonical < low or canonical > high:
        raise PreviewConfigError(
            f"Video FPS must be between {low:g} and {high:g}, not {fps:g}."
        )
    if canonical <= 0:
        raise PreviewConfigError("Video FPS must be greater than zero.")
    return fps


def _canonical_fps_decimal(fps: float) -> Decimal:
    try:
        value = Decimal(repr(float(fps)))
        quantized = value.quantize(Decimal(1).scaleb(-_FPS_MAX_DECIMALS))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise PreviewConfigError(f"Video FPS is not a valid number: {fps!r}") from exc
    normalized = quantized.normalize()
    # normalize() can produce exponent notation such as 1E+1 for 10.0.
    if normalized == normalized.to_integral():
        normalized = normalized.quantize(Decimal(1))
    return normalized


def format_fps(fps: float) -> str:
    """Return the canonical, locale-independent FPS text used in profile IDs.

    ``1.0 -> "1"``, ``0.5 -> "0.5"``, ``2.0 -> "2"``, ``0.25 -> "0.25"``.
    Values are rounded to at most three decimals; commas never appear.
    """

    _check_fps(fps)
    text = str(_canonical_fps_decimal(fps))
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or text == "0":
        raise PreviewConfigError(f"Video FPS {fps!r} is too small to represent.")
    return text


def _ensure_safe_profile_id(profile_id: str) -> str:
    if not _SAFE_PROFILE_ID_RE.fullmatch(profile_id):
        raise PreviewConfigError(
            f"Profile ID contains unsafe filename characters: {profile_id!r}"
        )
    return profile_id


@dataclass(frozen=True)
class ImagePreviewProfile:
    """Every setting that changes the bytes of a generated image preview."""

    max_dimension: int = DEFAULT_IMAGE_MAX_DIMENSION
    jpeg_quality: int = DEFAULT_IMAGE_JPEG_QUALITY

    def validate(self) -> None:
        _check_int_range("Image maximum dimension", self.max_dimension, IMAGE_MAX_DIMENSION_RANGE)
        _check_int_range("Image JPEG quality", self.jpeg_quality, IMAGE_JPEG_QUALITY_RANGE)

    @property
    def profile_id(self) -> str:
        self.validate()
        return _ensure_safe_profile_id(
            f"jpeg-max{int(self.max_dimension)}-q{int(self.jpeg_quality)}"
        )

    @property
    def extension(self) -> str:
        return IMAGE_PREVIEW_EXTENSION

    def describe(self) -> tuple[str, ...]:
        return (
            "JPEG",
            f"Max dimension: {int(self.max_dimension)} px",
            f"Quality: {int(self.jpeg_quality)}",
        )


@dataclass(frozen=True)
class VideoPreviewProfile:
    """Every setting that changes the bytes of a generated video preview."""

    fps: float = DEFAULT_VIDEO_FPS
    max_height: int = DEFAULT_VIDEO_MAX_HEIGHT
    crf: int = DEFAULT_VIDEO_CRF
    preset: str = DEFAULT_VIDEO_PRESET

    def validate(self) -> None:
        _check_fps(self.fps)
        _check_int_range("Video maximum height", self.max_height, VIDEO_MAX_HEIGHT_RANGE)
        _check_int_range("Video CRF", self.crf, VIDEO_CRF_RANGE)
        if not isinstance(self.preset, str) or self.preset not in VIDEO_PRESETS:
            raise PreviewConfigError(
                f"Video encoder preset {self.preset!r} is not supported. "
                f"Choose one of: {', '.join(VIDEO_PRESETS)}."
            )

    @property
    def profile_id(self) -> str:
        self.validate()
        return _ensure_safe_profile_id(
            f"h264-{format_fps(self.fps)}fps-{int(self.max_height)}p-"
            f"crf{int(self.crf)}-{self.preset}"
        )

    @property
    def extension(self) -> str:
        return VIDEO_PREVIEW_EXTENSION

    @property
    def fps_text(self) -> str:
        return format_fps(self.fps)

    def describe(self) -> tuple[str, ...]:
        return (
            "H.264 MP4",
            f"{format_fps(self.fps)} fps",
            f"Max height {int(self.max_height)} px",
            f"CRF {int(self.crf)}",
            f"Preset {self.preset}",
            "No audio",
        )


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    return default


def _coerce_int(value: Any, default: int, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            integer = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default
    low, high = bounds
    if integer < low or integer > high:
        return default
    return integer


def _coerce_fps(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
            if not value:
                return default
        fps = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    try:
        _check_fps(fps)
    except PreviewConfigError:
        return default
    return float(_canonical_fps_decimal(fps))


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class PreviewSettings:
    """The complete persisted offline-preview configuration."""

    enabled: bool = DEFAULT_PREVIEWS_ENABLED
    root_directory: str = ""
    ffmpeg_path: str = ""
    image: ImagePreviewProfile = field(default_factory=ImagePreviewProfile)
    video: VideoPreviewProfile = field(default_factory=VideoPreviewProfile)

    def __post_init__(self) -> None:
        # Tolerate ``None`` for the text fields so a hand-built instance behaves
        # like one produced by ``from_mapping``.
        if self.root_directory is None:
            object.__setattr__(self, "root_directory", "")
        if self.ffmpeg_path is None:
            object.__setattr__(self, "ffmpeg_path", "")
        object.__setattr__(self, "enabled", bool(self.enabled))

    # -- validation -------------------------------------------------------
    def validate(self, *, require_root: bool = True) -> None:
        """Raise ``PreviewConfigError`` for any out-of-range value.

        ``require_root`` also insists that a preview storage directory has been
        chosen; it does not touch the filesystem.
        """

        self.image.validate()
        self.video.validate()
        if require_root and not self.root_directory.strip():
            raise PreviewConfigError("A preview storage directory has not been selected.")

    @property
    def root_path(self) -> Path | None:
        text = self.root_directory.strip()
        if not text:
            return None
        return Path(text).expanduser()

    @property
    def ffmpeg_path_or_none(self) -> str | None:
        text = self.ffmpeg_path.strip()
        return text or None

    def profile_id(self, media_kind: str) -> str:
        if media_kind == "image":
            return self.image.profile_id
        if media_kind == "video":
            return self.video.profile_id
        raise PreviewConfigError(f"Previews are not supported for media kind {media_kind!r}.")

    def profile_for(self, media_kind: str) -> ImagePreviewProfile | VideoPreviewProfile:
        if media_kind == "image":
            return self.image
        if media_kind == "video":
            return self.video
        raise PreviewConfigError(f"Previews are not supported for media kind {media_kind!r}.")

    def output_signature(self) -> tuple[Any, ...]:
        """Everything except ``enabled`` – used to detect configuration changes."""

        return (
            self.root_directory.strip(),
            self.ffmpeg_path.strip(),
            self.image,
            self.video,
        )

    def with_enabled(self, enabled: bool) -> PreviewSettings:
        return replace(self, enabled=bool(enabled))

    # -- persistence -------------------------------------------------------
    def as_mapping(self) -> dict[str, Any]:
        """Flat ``{setting key: value}`` mapping with stable scalar types."""

        return {
            PREVIEWS_ENABLED_SETTING: bool(self.enabled),
            PREVIEWS_ROOT_SETTING: self.root_directory.strip(),
            PREVIEWS_FFMPEG_PATH_SETTING: self.ffmpeg_path.strip(),
            PREVIEWS_IMAGE_MAX_DIMENSION_SETTING: int(self.image.max_dimension),
            PREVIEWS_IMAGE_JPEG_QUALITY_SETTING: int(self.image.jpeg_quality),
            PREVIEWS_VIDEO_FPS_SETTING: float(_canonical_fps_decimal(self.video.fps)),
            PREVIEWS_VIDEO_MAX_HEIGHT_SETTING: int(self.video.max_height),
            PREVIEWS_VIDEO_CRF_SETTING: int(self.video.crf),
            PREVIEWS_VIDEO_PRESET_SETTING: str(self.video.preset),
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> PreviewSettings:
        """Build settings from stored values, falling back to defaults.

        Never raises: missing, malformed, or out-of-range stored values revert
        to the recommended default for that single setting.  ``enabled`` is
        only honoured when it is a recognizable true value, so previews stay
        disabled by default.
        """

        values: Mapping[str, Any] = mapping or {}
        preset = _coerce_text(values.get(PREVIEWS_VIDEO_PRESET_SETTING)).casefold()
        if preset not in VIDEO_PRESETS:
            preset = DEFAULT_VIDEO_PRESET
        return cls(
            enabled=_coerce_bool(values.get(PREVIEWS_ENABLED_SETTING), DEFAULT_PREVIEWS_ENABLED),
            root_directory=_coerce_text(values.get(PREVIEWS_ROOT_SETTING)),
            ffmpeg_path=_coerce_text(values.get(PREVIEWS_FFMPEG_PATH_SETTING)),
            image=ImagePreviewProfile(
                max_dimension=_coerce_int(
                    values.get(PREVIEWS_IMAGE_MAX_DIMENSION_SETTING),
                    DEFAULT_IMAGE_MAX_DIMENSION,
                    IMAGE_MAX_DIMENSION_RANGE,
                ),
                jpeg_quality=_coerce_int(
                    values.get(PREVIEWS_IMAGE_JPEG_QUALITY_SETTING),
                    DEFAULT_IMAGE_JPEG_QUALITY,
                    IMAGE_JPEG_QUALITY_RANGE,
                ),
            ),
            video=VideoPreviewProfile(
                fps=_coerce_fps(values.get(PREVIEWS_VIDEO_FPS_SETTING), DEFAULT_VIDEO_FPS),
                max_height=_coerce_int(
                    values.get(PREVIEWS_VIDEO_MAX_HEIGHT_SETTING),
                    DEFAULT_VIDEO_MAX_HEIGHT,
                    VIDEO_MAX_HEIGHT_RANGE,
                ),
                crf=_coerce_int(
                    values.get(PREVIEWS_VIDEO_CRF_SETTING),
                    DEFAULT_VIDEO_CRF,
                    VIDEO_CRF_RANGE,
                ),
                preset=preset,
            ),
        )


def default_preview_settings() -> PreviewSettings:
    return PreviewSettings()


def preview_extension_for(media_kind: str) -> str:
    try:
        return PREVIEW_EXTENSIONS[media_kind]
    except KeyError as exc:
        raise PreviewConfigError(
            f"Previews are not supported for media kind {media_kind!r}."
        ) from exc


STORAGE_TRADEOFF_TEXT = (
    "Higher image dimensions, higher JPEG quality, higher video FPS, higher video "
    "resolution, and lower CRF values use more storage. Previews are auxiliary "
    "files: the original archived files and the .jvvv catalogue remain authoritative."
)

ROOT_CHANGE_WARNING_TEXT = (
    "Changing the preview directory does not move existing previews.\n\n"
    "Existing previews in the previous directory will not be used unless copied "
    "manually to the new directory."
)

BACKUP_POLICY_TEXT = (
    "JVVV catalogue backups do not include offline preview files.\n\n"
    "To back up previews, copy the configured preview directory separately using "
    "your normal file-copy or backup software."
)
