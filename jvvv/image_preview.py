"""Offline image previews on one image stack: Pillow, pillow-heif, and rawpy.

Every image format goes through the same pipeline (spec sections 7, 12, 32):

* **Pillow** decodes the common formats (JPEG, PNG, TIFF, WebP, GIF, BMP, ICO,
  and more), applies EXIF orientation, resizes, flattens, and writes JPEG.
* **pillow-heif** registers HEIC/HEIF decoding with Pillow (libheif).
* **rawpy** (LibRaw) opens camera RAW files.  As the specification asks, a
  large embedded JPEG preview is preferred; otherwise the RAW data is
  demosaiced and the result continues through the same Pillow pipeline.

The module never displays anything - JVVV opens previews with the operating
system's default application.  Every failure raises ``PreviewError`` with a
stage, cancellation raises ``PreviewCancelled``, and every exception path
removes the temporary file so a partial preview is never published.
"""

from __future__ import annotations

import io
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import PIL
from PIL import Image, ImageOps, UnidentifiedImageError

from jvvv.media_metadata import RAW_EXTENSIONS
from jvvv.preview_cache import (
    PREVIEW_GENERATED,
    STAGE_CONFIGURATION,
    STAGE_DISK_FULL,
    STAGE_IMAGE_DECODE,
    STAGE_IMAGE_ENCODE,
    STAGE_IMAGE_TRANSFORM,
    STAGE_IMAGE_VALIDATE,
    STAGE_RENAME,
    STAGE_SOURCE_CHANGED,
    STAGE_TEMP_FILE,
    PreviewCache,
    PreviewCancelled,
    PreviewError,
    PreviewResult,
    classify_os_error,
    ensure_source_snapshot,
    os_error_detail,
)
from jvvv.preview_config import ImagePreviewProfile, PreviewConfigError
from jvvv.utils import format_size

try:  # HEIC/HEIF support is a hard requirement for enabling previews (spec §2C).
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the installation
    pillow_heif = None  # type: ignore[assignment]
    HEIF_ERROR = f"{type(exc).__name__}: {exc}"

try:  # Camera RAW support is a hard requirement for enabling previews (spec §39).
    import rawpy

    RAW_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the installation
    rawpy = None  # type: ignore[assignment]
    RAW_ERROR = f"{type(exc).__name__}: {exc}"

# Pillow's own "decompression bomb" guard is replaced by an explicit, larger,
# clearly reported limit: archived photos legitimately exceed 89 megapixels.
Image.MAX_IMAGE_PIXELS = None
MAX_SOURCE_PIXELS = 600_000_000

IMAGE_BACKEND_NAME = "Pillow + pillow-heif (libheif) + rawpy (LibRaw)"
TRANSPARENCY_BACKGROUND = "#808080"  # neutral grey used to flatten alpha
IMAGE_TEST_SIZE = (64, 48)
IMAGE_MEDIA_KIND = "image"

SOURCE_PILLOW = "pillow"
SOURCE_RAW_EMBEDDED = "raw-embedded-jpeg"
SOURCE_RAW_DEMOSAIC = "raw-demosaic"

# LibRaw ``sizes.flip`` values -> Pillow transposes (3 = 180°, 5 = 90° CCW, 6 = 90° CW).
_LIBRAW_FLIP_TRANSPOSE = {
    3: Image.Transpose.ROTATE_180,
    5: Image.Transpose.ROTATE_90,
    6: Image.Transpose.ROTATE_270,
}
_ROTATED_EXIF_ORIENTATIONS = {5, 6, 7, 8}
_DISK_FULL_TEXTS = ("no space", "not enough space", "disk full", "errno 28")

CancelCallback = Callable[[], bool]


class ImageOpenError(Exception):
    """A source image could not be opened; ``unsupported`` separates format from corruption."""

    def __init__(
        self,
        message: str,
        *,
        unsupported: bool = False,
        image_format: str | None = None,
    ) -> None:
        super().__init__(message)
        self.unsupported = unsupported
        self.image_format = image_format


@dataclass(frozen=True)
class ImagePreviewValidation:
    """Outcome of checking that a file is a decodable, non-empty JPEG preview."""

    valid: bool
    width: int | None
    height: int | None
    size_bytes: int
    message: str


@dataclass(frozen=True)
class DecodedImage:
    image: Image.Image
    source: str  # SOURCE_PILLOW, SOURCE_RAW_EMBEDDED, or SOURCE_RAW_DEMOSAIC
    image_format: str  # lower-case container/format name, e.g. "jpeg", "heif", "dng"


# ---------------------------------------------------------------------------
# Backend availability (spec §2C, §26)
# ---------------------------------------------------------------------------


def backend_versions() -> dict[str, str]:
    versions = {"pillow": PIL.__version__}
    if pillow_heif is not None:
        try:
            info = pillow_heif.libheif_info()
        except Exception:  # pragma: no cover - broken installation
            info = {}
        versions["pillow-heif"] = f"{pillow_heif.__version__} (libheif {info.get('libheif', '?')})"
    if rawpy is not None:
        versions["rawpy"] = (
            f"{rawpy.__version__} (LibRaw {'.'.join(str(part) for part in rawpy.libraw_version)})"
        )
    return versions


def image_backend_available() -> tuple[bool, str]:
    """Return ``(available, message)``; every image dependency must be present.

    Enabling previews is an all-dependencies-valid decision: JPEG encoding via
    Pillow, HEIC/HEIF decoding via pillow-heif, and camera RAW via rawpy must
    all be importable and functional, otherwise the feature stays disabled.
    """

    problems: list[str] = []
    try:
        from PIL import features

        if not features.check("jpg"):
            problems.append("Pillow was built without JPEG support")
    except Exception as exc:  # pragma: no cover - broken installation
        problems.append(f"Pillow could not be initialised: {exc}")
    if pillow_heif is None:
        problems.append(
            f"pillow-heif (HEIC/HEIF) is not available: {HEIF_ERROR}"
            if HEIF_ERROR
            else "pillow-heif (HEIC/HEIF) is not installed"
        )
    else:
        try:
            decoders = pillow_heif.libheif_info().get("decoders") or {}
        except Exception as exc:  # pragma: no cover - broken installation
            decoders = {}
            problems.append(f"libheif could not be queried: {exc}")
        if not decoders:
            problems.append("libheif has no HEVC decoder, so HEIC/HEIF files cannot be read")
    if rawpy is None:
        problems.append(
            f"rawpy (LibRaw camera RAW) is not available: {RAW_ERROR}"
            if RAW_ERROR
            else "rawpy (LibRaw camera RAW) is not installed"
        )
    versions = ", ".join(f"{name} {version}" for name, version in backend_versions().items())
    if problems:
        return (
            False,
            f"{IMAGE_BACKEND_NAME} is incomplete: " + "; ".join(problems) + ". "
            "Install the missing package into the JVVV environment "
            "(pip install -r requirements.txt)." + (f" Present: {versions}." if versions else ""),
        )
    return (
        True,
        f"{IMAGE_BACKEND_NAME}: {versions}. Formats: JPEG, PNG, TIFF, WebP, GIF, BMP, ICO, "
        f"HEIC/HEIF, and camera RAW ({', '.join(sorted(RAW_EXTENSIONS))}).",
    )


# ---------------------------------------------------------------------------
# Opening images (shared by preview generation and media metadata)
# ---------------------------------------------------------------------------


def _extension(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _check_pixel_budget(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageOpenError(f"The image reports invalid dimensions ({width}x{height}).")
    if width * height > MAX_SOURCE_PIXELS:
        raise ImageOpenError(
            f"The image is {width}x{height} ({width * height / 1e6:,.0f} megapixels), "
            f"above the {MAX_SOURCE_PIXELS / 1e6:,.0f} megapixel decode limit."
        )


def _open_with_pillow(path: Path, max_dimension: int | None) -> DecodedImage:
    try:
        image = Image.open(path)
    except UnidentifiedImageError as exc:
        raise ImageOpenError(
            f"Image decoder could not identify the file format: {exc}",
            unsupported=True,
        ) from exc
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageOpenError(f"Image decoder could not read the file: {exc}") from exc
    try:
        image_format = str(image.format or "").lower()
        width, height = image.size
        _check_pixel_budget(width, height)
        if max_dimension is not None and image.format == "JPEG" and image.mode in {"RGB", "L"}:
            # Let libjpeg decode at a reduced DCT scale (never below the target).
            image.draft(image.mode, compute_target_size(width, height, max_dimension))
        oriented = ImageOps.exif_transpose(image)
        if oriented is None:  # pragma: no cover - older Pillow returned None
            oriented = image
        oriented.load()
    except ImageOpenError:
        image.close()
        raise
    except (OSError, ValueError, SyntaxError, EOFError, MemoryError) as exc:
        image.close()
        raise ImageOpenError(f"Image decoder could not read the file: {exc}") from exc
    if oriented is not image:
        image.close()
    return DecodedImage(oriented, SOURCE_PILLOW, image_format or _extension(path))


def _thumbnail_to_image(thumb: object) -> Image.Image | None:
    """Convert a LibRaw thumbnail (JPEG bytes or RGB array) to a loaded Pillow image."""

    thumb_format = getattr(thumb, "format", None)
    data = getattr(thumb, "data", None)
    try:
        if thumb_format == rawpy.ThumbFormat.JPEG and data:
            image = Image.open(io.BytesIO(bytes(data)))
            image.load()
            return image
        if thumb_format == rawpy.ThumbFormat.BITMAP and data is not None:
            return Image.fromarray(np.asarray(data))
    except (UnidentifiedImageError, OSError, ValueError, TypeError):
        return None
    return None


def _orient_embedded(image: Image.Image, flip: int) -> Image.Image:
    """Orient an embedded RAW preview: its own EXIF wins, else LibRaw's flip."""

    try:
        orientation = int(image.getexif().get(0x0112, 1) or 1)
    except Exception:
        orientation = 1
    if orientation != 1:
        oriented = ImageOps.exif_transpose(image)
        return oriented if oriented is not None else image
    transpose = _LIBRAW_FLIP_TRANSPOSE.get(int(flip or 0))
    return image.transpose(transpose) if transpose is not None else image


def _libraw_text(exc: BaseException) -> str:
    text = str(exc)
    if text.startswith("b'") and text.endswith("'"):
        text = text[2:-1]
    return text or type(exc).__name__


def _open_raw(path: Path, max_dimension: int | None) -> DecodedImage:
    if rawpy is None:
        raise ImageOpenError(
            "Camera RAW files need the rawpy (LibRaw) package, which is not available.",
            unsupported=True,
        )
    try:
        raw = rawpy.imread(str(path))
    except rawpy.LibRawFileUnsupportedError as exc:
        raise ImageOpenError(
            f"LibRaw does not support this RAW file: {_libraw_text(exc)}",
            unsupported=True,
            image_format=_extension(path),
        ) from exc
    except (rawpy.LibRawError, OSError, ValueError) as exc:
        raise ImageOpenError(
            f"Camera RAW decoder could not read the file: {_libraw_text(exc)}",
            image_format=_extension(path),
        ) from exc
    with raw:
        try:
            sizes = raw.sizes
            full_side = max(int(sizes.width), int(sizes.height))
            _check_pixel_budget(int(sizes.width), int(sizes.height))
            required_side = (
                full_side if max_dimension is None else min(int(max_dimension), full_side)
            )

            # Spec §39: prefer an embedded full/large JPEG preview when available.
            thumb = None
            try:
                thumb = raw.extract_thumb()
            except rawpy.LibRawError:
                thumb = None
            if thumb is not None:
                embedded = _thumbnail_to_image(thumb)
                if embedded is not None and max(embedded.size) >= required_side:
                    return DecodedImage(
                        _orient_embedded(embedded, int(sizes.flip)),
                        SOURCE_RAW_EMBEDDED,
                        _extension(path),
                    )
                if embedded is not None:
                    embedded.close()

            # Otherwise demosaic.  Half-size output is four times cheaper and is
            # used whenever it still satisfies the requested maximum dimension.
            half_size = max_dimension is not None and full_side // 2 >= int(max_dimension)
            rgb = raw.postprocess(use_camera_wb=True, half_size=half_size, output_bps=8)
        except ImageOpenError:
            raise
        except (rawpy.LibRawError, OSError, ValueError, MemoryError) as exc:
            raise ImageOpenError(
                f"Camera RAW decoder could not process the file: {_libraw_text(exc)}",
                image_format=_extension(path),
            ) from exc
    return DecodedImage(Image.fromarray(rgb), SOURCE_RAW_DEMOSAIC, _extension(path))


def open_image(
    path: str | os.PathLike[str],
    *,
    max_dimension: int | None = None,
) -> DecodedImage:
    """Open any recognised image with orientation applied and pixels loaded.

    ``max_dimension`` lets decoders work at a reduced size when the preview is
    going to be smaller anyway (JPEG DCT scaling, RAW half-size demosaic); the
    returned image is never smaller than the eventual preview needs.
    Raises ``ImageOpenError``.
    """

    file_path = Path(path)
    if _extension(file_path) in RAW_EXTENSIONS:
        return _open_raw(file_path, max_dimension)
    return _open_with_pillow(file_path, max_dimension)


def read_image_dimensions(path: str | os.PathLike[str]) -> tuple[int, int, str]:
    """Return ``(width, height, format)`` after orientation, decoding as little as possible."""

    file_path = Path(path)
    if _extension(file_path) in RAW_EXTENSIONS:
        if rawpy is None:
            raise ImageOpenError(
                "Camera RAW files need the rawpy (LibRaw) package, which is not available.",
                unsupported=True,
            )
        try:
            with rawpy.imread(str(file_path)) as raw:
                sizes = raw.sizes
                width, height, flip = int(sizes.width), int(sizes.height), int(sizes.flip)
        except rawpy.LibRawFileUnsupportedError as exc:
            raise ImageOpenError(
                f"LibRaw does not support this RAW file: {_libraw_text(exc)}",
                unsupported=True,
            ) from exc
        except (rawpy.LibRawError, OSError, ValueError) as exc:
            raise ImageOpenError(
                f"Camera RAW decoder could not read the file: {_libraw_text(exc)}"
            ) from exc
        if flip in (5, 6):
            width, height = height, width
        _check_pixel_budget(width, height)
        return width, height, _extension(file_path)
    try:
        with Image.open(file_path) as image:
            width, height = image.size
            image_format = str(image.format or "").lower() or _extension(file_path)
            try:
                orientation = int(image.getexif().get(0x0112, 1) or 1)
            except Exception:
                orientation = 1
    except UnidentifiedImageError as exc:
        raise ImageOpenError(
            f"Image decoder could not identify the file format: {exc}", unsupported=True
        ) from exc
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageOpenError(f"Image decoder could not read the file: {exc}") from exc
    if orientation in _ROTATED_EXIF_ORIENTATIONS:
        width, height = height, width
    _check_pixel_budget(width, height)
    return width, height, image_format


# ---------------------------------------------------------------------------
# Validation of an existing preview (spec §7, §11)
# ---------------------------------------------------------------------------


def validate_image_preview(path: Path) -> ImagePreviewValidation:
    """Check that ``path`` is a regular, non-empty, fully decodable JPEG.

    Never raises; the message explains the first failing check so a corrupt
    preview can be reported and regenerated (spec §11).
    """

    file_path = Path(path)
    try:
        info = os.lstat(file_path)
    except OSError as exc:
        return ImagePreviewValidation(
            False, None, None, 0, f"The preview file could not be read: {os_error_detail(exc)}"
        )
    if stat.S_ISLNK(info.st_mode):
        return ImagePreviewValidation(
            False, None, None, 0, "The preview path is a symbolic link, not a regular file."
        )
    if not stat.S_ISREG(info.st_mode):
        return ImagePreviewValidation(False, None, None, 0, "The preview path is not a regular file.")
    size_bytes = int(info.st_size)
    if size_bytes <= 0:
        return ImagePreviewValidation(False, None, None, 0, "The preview file is empty.")
    try:
        with Image.open(file_path) as image:
            if image.format != "JPEG":
                detected = str(image.format or "unrecognised").lower()
                return ImagePreviewValidation(
                    False,
                    None,
                    None,
                    size_bytes,
                    f"The preview is not a JPEG file (detected format: {detected}).",
                )
            width, height = int(image.width), int(image.height)
            if width <= 0 or height <= 0:
                return ImagePreviewValidation(
                    False, None, None, size_bytes, f"The preview has invalid dimensions ({width}x{height})."
                )
            if width * height > MAX_SOURCE_PIXELS:
                # JVVV never writes previews this large; a header claiming it is
                # corrupt, and decoding it on the UI thread must not be attempted.
                return ImagePreviewValidation(
                    False,
                    None,
                    None,
                    size_bytes,
                    f"The preview header claims {width}x{height} pixels, above the "
                    f"{MAX_SOURCE_PIXELS / 1e6:,.0f} megapixel limit.",
                )
            image.load()  # a truncated or corrupt JPEG fails here
    except UnidentifiedImageError:
        return ImagePreviewValidation(
            False,
            None,
            None,
            size_bytes,
            "The preview is not a JPEG file (detected format: unrecognised).",
        )
    except (OSError, ValueError, SyntaxError, EOFError, MemoryError) as exc:
        return ImagePreviewValidation(
            False, None, None, size_bytes, f"The JPEG decoder could not read the preview: {exc}"
        )
    if width <= 0 or height <= 0:
        return ImagePreviewValidation(
            False, None, None, size_bytes, f"The preview has invalid dimensions ({width}x{height})."
        )
    return ImagePreviewValidation(
        True, width, height, size_bytes, f"Valid JPEG preview, {width}x{height}."
    )


# ---------------------------------------------------------------------------
# Geometry and pixel preparation
# ---------------------------------------------------------------------------


def compute_target_size(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    """Return the preview size for a ``width`` x ``height`` source.

    The larger side never exceeds ``max_dimension``, the aspect ratio is kept
    (``round(other * max / larger)``), images are never upscaled, and each side
    is at least 1 pixel.  ``6000x4000 -> 1600x1067``, ``4000x6000 -> 1067x1600``,
    ``1200x800 -> 1200x800`` for a maximum of 1600.
    """

    width, height, max_dimension = int(width), int(height), int(max_dimension)
    if width <= 0 or height <= 0:
        raise ValueError(f"Image dimensions must be positive, not {width}x{height}.")
    if max_dimension <= 0:
        raise ValueError(f"Maximum dimension must be positive, not {max_dimension}.")
    larger = max(width, height)
    if larger <= max_dimension:
        return width, height
    if width >= height:
        return max_dimension, max(1, round(height * max_dimension / width))
    return max(1, round(width * max_dimension / height)), max_dimension


def _to_rgb(image: Image.Image) -> Image.Image:
    """Flatten transparency onto neutral grey and normalise every mode to 8-bit RGB."""

    mode = image.mode
    # PNG tRNS chunks make P, RGB and L images transparent without an alpha band.
    has_alpha = mode in {"RGBA", "LA", "PA", "RGBa", "La"} or (
        mode in {"P", "RGB", "L"} and "transparency" in image.info
    )
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, TRANSPARENCY_BACKGROUND)
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if mode in {"I;16", "I;16B", "I;16L", "I;16N", "I", "F"}:
        # 16/32-bit greyscale: scale deterministically to 8 bits.
        values = np.asarray(image).astype(np.float64)
        top = 65535.0 if mode.startswith("I;16") else max(float(values.max(initial=0.0)), 1.0)
        eight_bit = np.clip(values / top * 255.0 + 0.5, 0, 255).astype(np.uint8)
        return Image.fromarray(eight_bit, "L").convert("RGB")
    if mode != "RGB":
        return image.convert("RGB")
    return image


def prepare_preview_image(image: Image.Image, max_dimension: int) -> Image.Image:
    """Orientation is already applied; downscale (never upscale) and flatten to RGB."""

    try:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise PreviewError(STAGE_IMAGE_TRANSFORM, "The decoded image has no pixels.")
        target = compute_target_size(width, height, max_dimension)
        rgb = _to_rgb(image)
        if target != (width, height):
            rgb = rgb.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)
        return rgb
    except PreviewError:
        raise
    except (MemoryError, OSError, ValueError) as exc:
        raise PreviewError(
            STAGE_IMAGE_TRANSFORM,
            "The image could not be prepared for encoding.",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


def _mentions_disk_full(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(fragment in text for fragment in _DISK_FULL_TEXTS)


def encode_jpeg(
    image: Image.Image,
    temp_path: Path,
    jpeg_quality: int,
    *,
    icc_profile: bytes | None = None,
) -> None:
    """Write ``image`` as a JPEG to ``temp_path`` or raise a staged ``PreviewError``.

    Only the ICC profile (needed to display colours correctly) is carried over;
    EXIF, XMP, comments and other metadata are deliberately dropped (spec §7).
    """

    options: dict[str, object] = {"quality": int(jpeg_quality)}
    if icc_profile:
        options["icc_profile"] = icc_profile
    # Pillow copies comments/XMP from ``image.info`` into the JPEG; drop them all.
    image.info = {}
    try:
        image.save(temp_path, format="JPEG", **options)
    except OSError as exc:
        if _mentions_disk_full(exc):
            stage = STAGE_DISK_FULL
        else:
            stage = classify_os_error(exc, STAGE_IMAGE_ENCODE)
        raise PreviewError(stage, "Could not write preview.", detail=os_error_detail(exc)) from exc
    except (ValueError, MemoryError) as exc:
        raise PreviewError(
            STAGE_IMAGE_ENCODE, "Could not write preview.", detail=f"{type(exc).__name__}: {exc}"
        ) from exc


_RGB_MODEL_MODES = frozenset({"RGB", "RGBA", "RGBa", "RGBX", "P", "PA"})


def _transferable_icc_profile(image: Image.Image) -> bytes | None:
    """The source ICC profile, but only when the preview keeps its colour model.

    A CMYK or greyscale profile applied to the RGB pixels of the preview would
    make viewers render wrong colours, so those are dropped (spec §7).
    """

    profile = image.info.get("icc_profile")
    if not isinstance(profile, bytes) or not profile:
        return None
    return profile if image.mode in _RGB_MODEL_MODES else None


def _lstat_source(source: Path) -> os.stat_result:
    try:
        return os.lstat(source)
    except OSError as exc:
        raise PreviewError(
            STAGE_IMAGE_DECODE,
            "The source image could not be read.",
            detail=os_error_detail(exc),
        ) from exc


def _check_cancelled(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback is not None and cancel_callback():
        raise PreviewCancelled("Image preview generation was cancelled.")


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class ImagePreviewGenerator:
    """Create JPEG previews for one ``PreviewCache`` and its image profile."""

    backend_name: str = IMAGE_BACKEND_NAME
    media_kind: str = IMAGE_MEDIA_KIND

    def __init__(self, cache: PreviewCache) -> None:
        self.cache = cache
        self.profile: ImagePreviewProfile = cache.image_profile
        try:
            self.profile_id: str = self.profile.profile_id
        except PreviewConfigError as exc:
            raise PreviewError(
                STAGE_CONFIGURATION,
                "The image preview settings are invalid.",
                detail=str(exc),
            ) from exc

    def generate(
        self,
        source: Path,
        destination: Path,
        *,
        cancel_callback: CancelCallback | None = None,
        source_stat: os.stat_result | None = None,
    ) -> PreviewResult:
        """Generate the preview for ``source`` at ``destination`` atomically.

        ``source_stat`` is the snapshot taken when the file's SHA-256 was
        computed; the source must still match it (spec §32).  Raises
        ``PreviewCancelled`` when ``cancel_callback`` returns true (it is
        checked before decoding, after decoding, and after encoding) and
        ``PreviewError`` for every other failure.  A temporary file never
        survives an exception, and an existing valid preview at
        ``destination`` is only replaced by a validated new one.
        """

        source_path = Path(source)
        final_path = Path(destination)
        before = _lstat_source(source_path)
        ensure_source_snapshot(source_path, before, source_stat)
        self.cache.ensure_parent(final_path)
        temp_path = self.cache.temporary_path(final_path)
        try:
            _check_cancelled(cancel_callback)
            decoded = self._decode(source_path)
            try:
                _check_cancelled(cancel_callback)
                prepared = prepare_preview_image(decoded.image, self.profile.max_dimension)
                encode_jpeg(
                    prepared,
                    temp_path,
                    self.profile.jpeg_quality,
                    icc_profile=_transferable_icc_profile(decoded.image),
                )
            finally:
                decoded.image.close()
            _check_cancelled(cancel_callback)
            self._ensure_source_unchanged(source_path, before)
            validation = validate_image_preview(temp_path)
            if not validation.valid:
                raise PreviewError(STAGE_IMAGE_VALIDATE, validation.message)
            self.cache.publish(temp_path, final_path)
        except BaseException:
            self.cache.discard_temporary(temp_path)
            raise

        size_bytes = self._published_size(final_path)
        return PreviewResult(
            status=PREVIEW_GENERATED,
            media_kind=IMAGE_MEDIA_KIND,
            profile_id=self.profile_id,
            path=final_path,
            bytes_written=size_bytes,
            size_bytes=size_bytes,
            width=validation.width,
            height=validation.height,
            detail=decoded.source,
        )

    # -- pipeline steps ----------------------------------------------------
    def _decode(self, source: Path) -> DecodedImage:
        try:
            return open_image(source, max_dimension=self.profile.max_dimension)
        except ImageOpenError as exc:
            raise PreviewError(
                STAGE_IMAGE_DECODE,
                "Image decoder could not read the file.",
                detail=str(exc),
            ) from exc

    @staticmethod
    def _ensure_source_unchanged(source: Path, before: os.stat_result) -> None:
        try:
            after = os.lstat(source)
        except OSError as exc:
            raise PreviewError(
                STAGE_SOURCE_CHANGED,
                "The source file changed while its preview was being created.",
                detail=f"The source disappeared: {os_error_detail(exc)}",
            ) from exc
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise PreviewError(
                STAGE_SOURCE_CHANGED,
                "The source file changed while its preview was being created.",
                detail=(
                    f"size {before.st_size} -> {after.st_size} bytes, "
                    f"mtime_ns {before.st_mtime_ns} -> {after.st_mtime_ns}"
                ),
            )

    @staticmethod
    def _published_size(final_path: Path) -> int:
        try:
            return int(final_path.stat().st_size)
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_RENAME),
                "The preview was moved into place but could not be read back.",
                detail=os_error_detail(exc),
            ) from exc


# ---------------------------------------------------------------------------
# Backend self-test (spec §2C)
# ---------------------------------------------------------------------------


def make_gradient_test_image(width: int, height: int) -> Image.Image:
    """A small gradient with varying alpha so the self-test exercises flattening."""

    xs = np.linspace(0.0, 1.0, width, dtype=np.float64)[None, :]
    ys = np.linspace(0.0, 1.0, height, dtype=np.float64)[:, None]
    red = np.broadcast_to(238 * (1 - xs) + 51 * xs, (height, width))
    green = np.broadcast_to(48 * (1 - ys) + 102 * ys, (height, width))
    blue = np.broadcast_to(70 * (1 - xs) + 204 * xs, (height, width))
    alpha = np.broadcast_to(255 * (1 - xs * ys), (height, width))
    pixels = np.stack([red, green, blue, alpha], axis=-1).astype(np.uint8)
    return Image.fromarray(pixels, "RGBA")


def test_image_backend(cache: PreviewCache) -> str:
    """Prove the image backend can encode into ``cache.root`` and clean up.

    Runs a generated 64x48 gradient (with alpha) through the real flatten and
    JPEG pipeline into a temporary file under the preview root, validates it
    by decoding, deletes it, and returns a one-line description.  Raises
    ``PreviewError`` on any failure; never leaves files behind.
    """

    available, message = image_backend_available()
    if not available:
        raise PreviewError(STAGE_IMAGE_ENCODE, message)

    profile = cache.image_profile
    try:
        profile.validate()
    except PreviewConfigError as exc:
        raise PreviewError(
            STAGE_CONFIGURATION, "The image preview settings are invalid.", detail=str(exc)
        ) from exc

    width, height = IMAGE_TEST_SIZE
    final_path = Path(cache.root) / "jvvv-image-test.jpg"
    cache.ensure_parent(final_path)
    temp_path = cache.temporary_path(final_path)
    try:
        image = prepare_preview_image(make_gradient_test_image(width, height), profile.max_dimension)
        encode_jpeg(image, temp_path, profile.jpeg_quality)
        validation = validate_image_preview(temp_path)
        if not validation.valid:
            raise PreviewError(STAGE_IMAGE_VALIDATE, validation.message)
        if (validation.width, validation.height) != (width, height):
            raise PreviewError(
                STAGE_IMAGE_VALIDATE,
                "The test image came back with unexpected dimensions.",
                detail=f"expected {width}x{height}, got {validation.width}x{validation.height}",
            )
        try:
            os.remove(temp_path)
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_TEMP_FILE),
                "Could not delete the temporary test image.",
                detail=os_error_detail(exc),
            ) from exc
    except BaseException:
        cache.discard_temporary(temp_path)
        raise

    return (
        f"Encoded a {width}x{height} test image to JPEG quality {int(profile.jpeg_quality)} "
        f"({format_size(validation.size_bytes)}) in {cache.root} using {IMAGE_BACKEND_NAME}"
    )


__all__ = [
    "DecodedImage",
    "IMAGE_BACKEND_NAME",
    "IMAGE_TEST_SIZE",
    "ImageOpenError",
    "ImagePreviewGenerator",
    "ImagePreviewValidation",
    "MAX_SOURCE_PIXELS",
    "SOURCE_PILLOW",
    "SOURCE_RAW_DEMOSAIC",
    "SOURCE_RAW_EMBEDDED",
    "TRANSPARENCY_BACKGROUND",
    "backend_versions",
    "compute_target_size",
    "encode_jpeg",
    "image_backend_available",
    "make_gradient_test_image",
    "open_image",
    "prepare_preview_image",
    "read_image_dimensions",
    "test_image_backend",
    "validate_image_preview",
]
