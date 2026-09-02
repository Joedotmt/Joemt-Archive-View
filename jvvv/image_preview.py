"""Offline image previews: decode, orient, downscale, flatten, and encode as JPEG.

This is the Qt-only image backend of the offline preview system.  It builds on
``QImageReader``/``QImageWriter`` (which work without a ``QApplication`` and
inside worker threads) and never displays anything - JVVV opens previews in the
operating system's default viewer.

Pipeline for one source image (spec sections 7, 12, and 32):

1. remember the source ``lstat`` so a file that changes mid-way is detected;
2. decode with ``QImageReader`` (EXIF orientation applied, oversized images are
   downscaled by the decoder itself so huge photos never sit in memory at full
   size);
3. flatten onto a neutral grey canvas (this also drops every text/metadata key
   the decoder may have picked up) and downscale again if the decoder ignored
   the requested size - never upscale;
4. encode to a ``.tmp-`` file beside the final path with the configured JPEG
   quality;
5. re-check the source, validate the temporary JPEG by decoding it, and only
   then atomically publish it under the deterministic final name.

Qt refuses to decode an image whose pixel buffer exceeds
``QImageReader.allocationLimit`` and its default of 256 MB rejects realistic
archive material (a 600 dpi A4 scan stored as 16-bit TIFF needs 280 MB, a
9000x8000 PNG 288 MB) as well as the intermediate image the 8192 px profile
needs.  Every decode here first raises that limit to
``IMAGE_ALLOCATION_LIMIT_MB`` - bounded rather than unlimited, so a corrupt or
hostile header claiming 60000x60000 pixels is refused instantly instead of
attempting a 14 GB allocation - and a source that is still too large fails with
an honest "too large" error instead of looking corrupt.  An explicit
``QT_IMAGEIO_MAXALLOC`` in the environment is respected as an operator override.

Every failure raises ``PreviewError`` with a failure stage, cancellation raises
``PreviewCancelled``, and every exception path removes the temporary file.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QImageReader,
    QImageWriter,
    QLinearGradient,
    QPainter,
)

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
    os_error_detail,
)
from jvvv.preview_config import ImagePreviewProfile, PreviewConfigError
from jvvv.utils import format_size


IMAGE_BACKEND_NAME = "Qt image reader/writer (QImageReader, QImageWriter)"
TRANSPARENCY_BACKGROUND = "#808080"  # neutral grey used to flatten alpha
IMAGE_TEST_SIZE = (64, 48)
IMAGE_MEDIA_KIND = "image"
JPEG_FORMAT_NAMES = frozenset({"jpeg", "jpg"})

# Text fragments in a Qt writer error that mean the preview disk is full.
_DISK_FULL_TEXTS = ("space", "disk full")

# Largest pixel buffer (MiB) Qt may allocate while decoding one image.  Qt's own
# default (256) is far too small for archive scans and for the 8192 px profile;
# 2048 covers 150-megapixel 16-bit TIFFs and A2 600 dpi scans while still
# refusing absurd headers without touching memory.  0 would mean unlimited.
IMAGE_ALLOCATION_LIMIT_MB = 2048
QT_DEFAULT_IMAGE_ALLOCATION_LIMIT_MB = 256
# Qt reads this variable at start-up; when an operator sets it, it wins.
ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE = "QT_IMAGEIO_MAXALLOC"

CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class ImagePreviewValidation:
    """Outcome of checking that a file is a decodable, non-empty JPEG preview."""

    valid: bool
    width: int | None
    height: int | None
    size_bytes: int
    message: str


# ---------------------------------------------------------------------------
# Decoder allocation limit
# ---------------------------------------------------------------------------


def _environment_allocation_limit() -> int | None:
    """Return the operator's ``QT_IMAGEIO_MAXALLOC`` (MiB, 0 = unlimited) or ``None``."""

    text = os.environ.get(ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE, "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def _apply_allocation_limit() -> int:
    """Make sure Qt will decode large images; return the effective limit in MiB.

    ``QImageReader.setAllocationLimit`` is process-wide, so this is idempotent
    and only writes when the value differs.  ``media_metadata`` only reads
    image headers and is unaffected.  An explicit environment override is left
    exactly as Qt applied it.  ``0`` means no limit.
    """

    if _environment_allocation_limit() is not None:
        return int(QImageReader.allocationLimit())
    if int(QImageReader.allocationLimit()) != IMAGE_ALLOCATION_LIMIT_MB:
        QImageReader.setAllocationLimit(IMAGE_ALLOCATION_LIMIT_MB)
    return IMAGE_ALLOCATION_LIMIT_MB


def _allocation_limit_text(limit_mb: int) -> str:
    if limit_mb <= 0:
        return f"no image allocation limit ({ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE}=0)"
    return (
        f"image allocation limit of {format_size(limit_mb * 1024 * 1024)} "
        f"({ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE}={limit_mb})"
    )


def _estimated_decoded_bytes(reader: QImageReader, size: QSize) -> int | None:
    """Bytes the decoded image needs, from the header alone (``None`` if unknown).

    Mirrors Qt's own arithmetic: scanlines padded to 4 bytes and the depth of
    the format the handler will produce (32-bit assumed when it does not say).
    """

    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        return None
    bits = 0
    try:
        image_format = reader.imageFormat()
        if image_format != QImage.Format.Format_Invalid:
            bits = int(QImage.toPixelFormat(image_format).bitsPerPixel())
    except Exception:  # pragma: no cover - defensive, Qt does not normally raise
        bits = 0
    if bits <= 0:
        bits = 32
    bytes_per_line = ((int(size.width()) * bits + 31) // 32) * 4
    return bytes_per_line * int(size.height())


def _decode_failure(reader: QImageReader, size: QSize, limit_mb: int) -> PreviewError:
    """Classify a null ``read()``.

    Qt reports the same ``InvalidDataError`` for a corrupt file and for one it
    refused because it exceeds the allocation limit, so the header size decides:
    an over-limit image is reported as too large (spec section 14 asks for an
    accurate technical message), everything else as undecodable, and the size
    arithmetic is included either way.
    """

    detail = _reader_detail(reader)
    estimate = _estimated_decoded_bytes(reader, size)
    if estimate is None:
        return PreviewError(STAGE_IMAGE_DECODE, "Image decoder could not read the file.", detail=detail)
    size_text = f"{size.width()}x{size.height()} needs about {format_size(estimate)} to decode"
    if limit_mb > 0 and estimate > limit_mb * 1024 * 1024:
        too_large = f"{size_text}, more than the {_allocation_limit_text(limit_mb)}."
        return PreviewError(
            STAGE_IMAGE_DECODE,
            "The image is too large to decode.",
            detail=f"{too_large} {detail}".strip(),
        )
    return PreviewError(
        STAGE_IMAGE_DECODE,
        "Image decoder could not read the file.",
        detail=f"{detail} ({size_text})" if detail else size_text,
    )


# ---------------------------------------------------------------------------
# Backend availability
# ---------------------------------------------------------------------------


def _format_names(formats: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for entry in formats:
        text = bytes(entry).decode("ascii", "replace").strip().casefold()  # type: ignore[call-overload]
        if text:
            names.add(text)
    return names


def image_backend_available() -> tuple[bool, str]:
    """Return ``(available, message)`` for the Qt image backend.

    Available means Qt can write JPEG and read both JPEG and PNG.  The message
    always names the backend and lists the readable formats so a validation
    report can show what this installation supports.
    """

    try:
        write_formats = _format_names(QImageWriter.supportedImageFormats())
        read_formats = _format_names(QImageReader.supportedImageFormats())
    except Exception as exc:  # pragma: no cover - Qt plugin loading failure
        return False, f"{IMAGE_BACKEND_NAME} could not be initialised: {exc}"

    missing: list[str] = []
    if not (write_formats & JPEG_FORMAT_NAMES):
        missing.append("JPEG writing")
    if not (read_formats & JPEG_FORMAT_NAMES):
        missing.append("JPEG reading")
    if "png" not in read_formats:
        missing.append("PNG reading")
    readable = ", ".join(sorted(read_formats)) or "none"
    if missing:
        return (
            False,
            f"{IMAGE_BACKEND_NAME} is missing {', '.join(missing)}. "
            f"Readable formats: {readable}.",
        )
    limit_text = _allocation_limit_text(_apply_allocation_limit())
    return (
        True,
        f"{IMAGE_BACKEND_NAME} can read and write JPEG. Readable formats: {readable}. "
        f"{limit_text[0].upper()}{limit_text[1:]}.",
    )


# ---------------------------------------------------------------------------
# Validation of an existing preview
# ---------------------------------------------------------------------------


def _reader_format(reader: QImageReader) -> str:
    return bytes(reader.format()).decode("ascii", "replace").strip().casefold()


def _reader_detail(reader: QImageReader) -> str:
    text = " ".join(str(reader.errorString() or "").split())
    image_format = _reader_format(reader)
    if image_format:
        suffix = f"detected format: {image_format}"
        text = f"{text} ({suffix})" if text else suffix
    return text


def validate_image_preview(path: Path) -> ImagePreviewValidation:
    """Check that ``path`` is a regular, non-empty, decodable JPEG.

    Never raises; the message explains the first failing check so a corrupt
    preview can be reported and regenerated (spec section 11).
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
        _apply_allocation_limit()
        reader = QImageReader(str(file_path))
        image_format = _reader_format(reader)
        if image_format not in JPEG_FORMAT_NAMES:
            detected = image_format or "unrecognised"
            return ImagePreviewValidation(
                False,
                None,
                None,
                size_bytes,
                f"The preview is not a JPEG file (detected format: {detected}).",
            )
        image = reader.read()
        if image.isNull():
            return ImagePreviewValidation(
                False,
                None,
                None,
                size_bytes,
                f"The JPEG decoder could not read the preview: {_reader_detail(reader)}",
            )
        width, height = int(image.width()), int(image.height())
    except Exception as exc:  # pragma: no cover - Qt does not normally raise
        return ImagePreviewValidation(
            False, None, None, size_bytes, f"The preview could not be decoded: {exc}"
        )
    if width <= 0 or height <= 0:
        return ImagePreviewValidation(
            False, None, None, size_bytes, f"The preview has invalid dimensions ({width}x{height})."
        )
    return ImagePreviewValidation(
        True, width, height, size_bytes, f"Valid JPEG preview, {width}x{height}."
    )


# ---------------------------------------------------------------------------
# Geometry
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


def _needs_downscale(width: int, height: int, max_dimension: int) -> bool:
    return width > 0 and height > 0 and max(width, height) > int(max_dimension)


# ---------------------------------------------------------------------------
# Shared pipeline steps (used by the generator and the backend self-test)
# ---------------------------------------------------------------------------


def _prepare_image(image: QImage, max_dimension: int) -> QImage:
    """Downscale if still needed, then flatten onto a fresh opaque canvas.

    The canvas is a brand-new ``QImage`` so no text keys, DPI hints, or other
    metadata from the source survive (spec section 7, "strip metadata").
    Transparent areas end up ``TRANSPARENCY_BACKGROUND`` grey.
    """

    try:
        width, height = int(image.width()), int(image.height())
        if image.isNull() or width <= 0 or height <= 0:
            raise PreviewError(STAGE_IMAGE_TRANSFORM, "The decoded image has no pixels.")
        if _needs_downscale(width, height, max_dimension):
            # compute_target_size already preserves the aspect ratio, so the
            # exact computed size is used (KeepAspectRatio could round a side
            # down by one pixel and break the deterministic dimensions).
            target = compute_target_size(width, height, max_dimension)
            image = image.scaled(
                QSize(*target),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if image.isNull():
                raise PreviewError(STAGE_IMAGE_TRANSFORM, "The image could not be resized.")
        canvas = QImage(image.size(), QImage.Format.Format_RGB32)
        if canvas.isNull():
            raise PreviewError(
                STAGE_IMAGE_TRANSFORM,
                "Not enough memory to prepare the image for encoding.",
                detail=f"{image.width()}x{image.height()} canvas allocation failed",
            )
        canvas.fill(QColor(TRANSPARENCY_BACKGROUND))
        painter = QPainter(canvas)
        if not painter.isActive():
            raise PreviewError(
                STAGE_IMAGE_TRANSFORM, "Could not paint the image onto the preview canvas."
            )
        try:
            painter.drawImage(0, 0, image)
        finally:
            painter.end()
        return canvas
    except PreviewError:
        raise
    except Exception as exc:  # Qt wrapper failures must never escape unclassified
        raise PreviewError(
            STAGE_IMAGE_TRANSFORM,
            "The image could not be prepared for encoding.",
            detail=str(exc),
        ) from exc


def _mentions_disk_full(text: str) -> bool:
    folded = " ".join(str(text or "").split()).casefold()
    return any(fragment in folded for fragment in _DISK_FULL_TEXTS)


def _encode_jpeg(image: QImage, temp_path: Path, jpeg_quality: int) -> None:
    """Write ``image`` as a JPEG to ``temp_path`` or raise a staged ``PreviewError``."""

    try:
        writer = QImageWriter(str(temp_path), b"jpeg")
        writer.setQuality(int(jpeg_quality))
        written = writer.write(image)
    except OSError as exc:
        raise PreviewError(
            classify_os_error(exc, STAGE_TEMP_FILE),
            "Could not write preview.",
            detail=os_error_detail(exc),
        ) from exc
    if not written:
        error_text = " ".join(str(writer.errorString() or "").split())
        stage = STAGE_DISK_FULL if _mentions_disk_full(error_text) else STAGE_IMAGE_ENCODE
        raise PreviewError(
            stage,
            "Could not write preview.",
            detail=error_text or "The JPEG encoder reported an error.",
        )


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
    ) -> PreviewResult:
        """Generate the preview for ``source`` at ``destination`` atomically.

        Raises ``PreviewCancelled`` when ``cancel_callback`` returns true (it is
        checked before decoding, after decoding, and after encoding) and
        ``PreviewError`` for every other failure.  A temporary file never
        survives an exception, and an existing valid preview at
        ``destination`` is only replaced by a validated new one.
        """

        source_path = Path(source)
        final_path = Path(destination)
        before = _lstat_source(source_path)
        self.cache.ensure_parent(final_path)
        temp_path = self.cache.temporary_path(final_path)
        try:
            _check_cancelled(cancel_callback)
            image = self._decode(source_path)
            _check_cancelled(cancel_callback)
            image = _prepare_image(image, self.profile.max_dimension)
            _encode_jpeg(image, temp_path, self.profile.jpeg_quality)
            del image
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
        )

    # -- pipeline steps ----------------------------------------------------
    def _decode(self, source: Path) -> QImage:
        limit_mb = _apply_allocation_limit()
        reader = QImageReader(str(source))
        reader.setAutoTransform(True)  # honour EXIF orientation
        if not reader.canRead():
            raise PreviewError(
                STAGE_IMAGE_DECODE,
                "Image decoder could not read the file.",
                detail=_reader_detail(reader),
            )
        size = reader.size()
        max_dimension = self.profile.max_dimension
        if size.isValid() and _needs_downscale(size.width(), size.height(), max_dimension):
            # reader.size() is the stored (pre-EXIF) size and Qt scales before
            # it applies the EXIF transform, so the larger side still ends up
            # at most max_dimension after rotation.
            target = compute_target_size(size.width(), size.height(), max_dimension)
            reader.setScaledSize(QSize(*target))
        image = reader.read()  # animated formats: first frame
        if image.isNull():
            raise _decode_failure(reader, size, limit_mb)
        return image

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
# Backend self-test (spec section 2C)
# ---------------------------------------------------------------------------


def _make_gradient_test_image(width: int, height: int) -> QImage:
    """A small gradient with varying alpha so the self-test exercises flattening."""

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    gradient = QLinearGradient(0.0, 0.0, float(width), float(height))
    gradient.setColorAt(0.0, QColor(238, 48, 70, 255))
    gradient.setColorAt(0.5, QColor(51, 102, 204, 160))
    gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter = QPainter(image)
    try:
        painter.fillRect(0, 0, width, height, QBrush(gradient))
    finally:
        painter.end()
    return image


def test_image_backend(cache: PreviewCache) -> str:
    """Prove the image backend can encode into ``cache.root`` and clean up.

    Encodes a generated 64x48 gradient (with alpha) through the real
    flatten + JPEG pipeline into a temporary file under the preview root,
    validates it by decoding, deletes it, and returns a one-line description.
    Raises ``PreviewError`` on any failure; never leaves files behind.
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
        image = _prepare_image(_make_gradient_test_image(width, height), profile.max_dimension)
        _encode_jpeg(image, temp_path, profile.jpeg_quality)
        del image
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
        f"({format_size(validation.size_bytes)}) in {cache.root}"
    )
