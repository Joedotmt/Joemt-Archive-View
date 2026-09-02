from __future__ import annotations

import hashlib
import pathlib
import random
import struct
import sys
import threading
import zlib

import pytest
from PySide6.QtGui import QBrush, QColor, QImage, QImageReader, QImageWriter, QLinearGradient, QPainter

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from preview_fixtures import (  # noqa: E402
    make_test_image,
    write_jpeg_with_exif_orientation,
    write_test_image,
)

from jvvv import image_preview  # noqa: E402
from jvvv.image_preview import (  # noqa: E402
    ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE,
    IMAGE_ALLOCATION_LIMIT_MB,
    IMAGE_BACKEND_NAME,
    IMAGE_TEST_SIZE,
    QT_DEFAULT_IMAGE_ALLOCATION_LIMIT_MB,
    TRANSPARENCY_BACKGROUND,
    ImagePreviewGenerator,
    ImagePreviewValidation,
    compute_target_size,
    image_backend_available,
    validate_image_preview,
)
from jvvv.image_preview import test_image_backend as run_image_backend_test  # noqa: E402
from jvvv.preview_cache import (  # noqa: E402
    PREVIEW_GENERATED,
    STAGE_CONFIGURATION,
    STAGE_DISK_FULL,
    STAGE_IMAGE_DECODE,
    STAGE_IMAGE_ENCODE,
    STAGE_IMAGE_TRANSFORM,
    STAGE_IMAGE_VALIDATE,
    STAGE_RENAME,
    STAGE_SOURCE_CHANGED,
    PreviewCache,
    PreviewCancelled,
    PreviewError,
)
from jvvv.preview_config import ImagePreviewProfile, VideoPreviewProfile  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cache(tmp_path: pathlib.Path, **profile_values: int) -> PreviewCache:
    root = tmp_path / "previews"
    return PreviewCache(root, ImagePreviewProfile(**profile_values), VideoPreviewProfile())


def destination_for(cache: PreviewCache, source: pathlib.Path) -> pathlib.Path:
    digest = hashlib.sha256(source.read_bytes()).digest()
    return cache.preview_path("image", digest)


def temporary_files(root: pathlib.Path) -> list[pathlib.Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and ".tmp-" in path.name]


def files_under(root: pathlib.Path) -> list[pathlib.Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def read_jpeg(path: pathlib.Path) -> QImage:
    reader = QImageReader(str(path))
    assert bytes(reader.format()) == b"jpeg"
    image = reader.read()
    assert not image.isNull(), reader.errorString()
    return image


def write_transparent_png(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    assert image.save(str(path), "PNG")
    return path


def write_noisy_png(path: pathlib.Path, width: int, height: int, seed: int = 7) -> pathlib.Path:
    """A gradient with random blocks: JPEG size depends strongly on quality."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0.0, 0.0, float(width), float(height))
    gradient.setColorAt(0.0, QColor("#ee3046"))
    gradient.setColorAt(0.5, QColor("#3366cc"))
    gradient.setColorAt(1.0, QColor("#ffffff"))
    painter.fillRect(0, 0, width, height, QBrush(gradient))
    generator = random.Random(seed)
    for _ in range(4000):
        painter.fillRect(
            generator.randrange(width),
            generator.randrange(height),
            generator.randrange(1, 12),
            generator.randrange(1, 12),
            QColor(generator.randrange(256), generator.randrange(256), generator.randrange(256)),
        )
    painter.end()
    assert image.save(str(path), "PNG")
    return path


def _lzw_pack(codes: list[int], width: int = 3) -> bytes:
    bits = 0
    pending = 0
    out = bytearray()
    for code in codes:
        bits |= code << pending
        pending += width
        while pending >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            pending -= 8
    if pending:
        out.append(bits & 0xFF)
    return bytes(out)


def _gif_sub_blocks(data: bytes) -> bytes:
    out = bytearray()
    for offset in range(0, len(data), 255):
        chunk = data[offset : offset + 255]
        out.append(len(chunk))
        out += chunk
    out.append(0)
    return bytes(out)


def _gif_frame(width: int, height: int, colour_index: int) -> bytes:
    # LZW with a 2-bit minimum code size: CLEAR (4) before every pixel keeps all
    # codes 3 bits wide; END (5) finishes the stream.
    codes: list[int] = []
    for _ in range(width * height):
        codes += [4, colour_index]
    codes.append(5)
    graphic_control = b"\x21\xf9\x04\x00" + struct.pack("<H", 10) + b"\x00\x00"
    descriptor = b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
    return graphic_control + descriptor + b"\x02" + _gif_sub_blocks(_lzw_pack(codes))


def write_animated_gif(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    """Two-frame GIF: frame 1 is solid red, frame 2 is solid blue."""

    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"GIF89a" + struct.pack("<HH", width, height) + b"\x91\x00\x00"
    palette = bytes([0, 0, 0, 220, 30, 30, 30, 30, 220, 255, 255, 255])
    loop = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    path.write_bytes(
        header + palette + loop + _gif_frame(width, height, 1) + _gif_frame(width, height, 2) + b"\x3b"
    )
    return path


def colour_distance(colour: QColor, expected: QColor) -> int:
    return max(
        abs(colour.red() - expected.red()),
        abs(colour.green() - expected.green()),
        abs(colour.blue() - expected.blue()),
    )


MIB = 1024 * 1024


def write_deep_tiff(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    """A 16-bit-per-channel RGBA TIFF (like a scanner produces), LZW compressed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(width, height, QImage.Format.Format_RGBA64)
    image.fill(QColor(30, 60, 200, 255))
    writer = QImageWriter(str(path), b"tiff")
    writer.setCompression(1)  # LZW keeps the fixture small; the decode is still full size
    assert writer.write(image), writer.errorString()
    return path


def write_png_claiming_size(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    """A tiny PNG whose IHDR header claims ``width`` x ``height`` pixels."""

    path.parent.mkdir(parents=True, exist_ok=True)
    small = QImage(8, 8, QImage.Format.Format_ARGB32)
    small.fill(QColor(1, 2, 3, 255))
    assert small.save(str(path), "PNG")
    data = bytearray(path.read_bytes())
    assert data[12:16] == b"IHDR"
    struct.pack_into(">II", data, 16, width, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    path.write_bytes(bytes(data))
    return path


@pytest.fixture
def qt_default_allocation_limit():
    """Start from Qt's stock 256 MB limit so the module must raise it itself."""

    original = QImageReader.allocationLimit()
    QImageReader.setAllocationLimit(QT_DEFAULT_IMAGE_ALLOCATION_LIMIT_MB)
    assert QImageReader.allocationLimit() == QT_DEFAULT_IMAGE_ALLOCATION_LIMIT_MB
    yield QT_DEFAULT_IMAGE_ALLOCATION_LIMIT_MB
    QImageReader.setAllocationLimit(original)


@pytest.fixture
def cache(tmp_path: pathlib.Path) -> PreviewCache:
    return make_cache(tmp_path)


@pytest.fixture
def generator(cache: PreviewCache) -> ImagePreviewGenerator:
    return ImagePreviewGenerator(cache)


# ---------------------------------------------------------------------------
# Backend availability
# ---------------------------------------------------------------------------


def test_image_backend_is_available_and_describes_formats():
    available, message = image_backend_available()

    assert available is True
    assert IMAGE_BACKEND_NAME in message
    assert "jpeg" in message.casefold()
    assert "png" in message.casefold()


def test_backend_unavailable_when_qt_cannot_write_jpeg(monkeypatch):
    monkeypatch.setattr(image_preview.QImageWriter, "supportedImageFormats", staticmethod(lambda: [b"png"]))

    available, message = image_backend_available()

    assert available is False
    assert "JPEG writing" in message
    assert IMAGE_BACKEND_NAME in message


def test_generator_exposes_backend_and_profile(cache):
    generator = ImagePreviewGenerator(cache)

    assert generator.backend_name == IMAGE_BACKEND_NAME
    assert generator.profile is cache.image_profile
    assert generator.profile_id == "jpeg-max1600-q82"
    assert generator.media_kind == "image"


# ---------------------------------------------------------------------------
# compute_target_size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "maximum", "expected"),
    [
        (6000, 4000, 1600, (1600, 1067)),
        (4000, 6000, 1600, (1067, 1600)),
        (1200, 800, 1600, (1200, 800)),
        (1600, 1600, 1600, (1600, 1600)),
        (3000, 2000, 800, (800, 533)),
        (2000, 3000, 800, (533, 800)),
        (200, 100, 1600, (200, 100)),
        (1, 1, 320, (1, 1)),
        (100000, 1, 1600, (1600, 1)),
        (1, 100000, 1600, (1, 1600)),
    ],
)
def test_compute_target_size_examples(width, height, maximum, expected):
    assert compute_target_size(width, height, maximum) == expected


def test_compute_target_size_never_exceeds_maximum_or_upscales():
    for width in (1, 17, 319, 320, 321, 1600, 1601, 4096, 8192, 12000):
        for height in (1, 9, 320, 1067, 1600, 9000):
            result_width, result_height = compute_target_size(width, height, 1600)
            assert max(result_width, result_height) <= 1600
            assert result_width >= 1 and result_height >= 1
            assert result_width <= width and result_height <= height
            if max(width, height) <= 1600:
                assert (result_width, result_height) == (width, height)


@pytest.mark.parametrize(("width", "height", "maximum"), [(0, 100, 1600), (100, -1, 1600), (100, 100, 0)])
def test_compute_target_size_rejects_non_positive_values(width, height, maximum):
    with pytest.raises(ValueError):
        compute_target_size(width, height, maximum)


# ---------------------------------------------------------------------------
# Successful generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jpeg", "png", "tiff", "webp", "bmp"])
def test_source_formats_produce_a_valid_jpeg_preview(tmp_path, cache, generator, fmt):
    source = write_test_image(tmp_path / "src" / f"photo.{fmt}", 320, 200, fmt)
    destination = destination_for(cache, source)
    assert destination.parent.parent.parent == cache.root / "images"
    assert destination.parent.parent.name == "jpeg-max1600-q82"
    assert destination.suffix == ".jpg"

    result = generator.generate(source, destination)

    assert result.status == PREVIEW_GENERATED
    assert result.ok is True
    assert result.media_kind == "image"
    assert result.profile_id == "jpeg-max1600-q82"
    assert result.path == destination
    assert (result.width, result.height) == (320, 200)
    assert destination.is_file()
    assert result.size_bytes == destination.stat().st_size > 0
    assert result.bytes_written == result.size_bytes
    assert destination.read_bytes()[:2] == b"\xff\xd8"
    validation = validate_image_preview(destination)
    assert validation.valid is True
    assert (validation.width, validation.height) == (320, 200)
    assert temporary_files(cache.root) == []
    assert files_under(cache.root) == [destination]


def test_animated_gif_uses_its_first_frame(tmp_path, cache, generator):
    source = write_animated_gif(tmp_path / "src" / "anim.gif", 32, 32)
    reader = QImageReader(str(source))
    assert reader.imageCount() == 2, "fixture GIF should be animated"
    destination = destination_for(cache, source)

    result = generator.generate(source, destination)

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (32, 32)
    preview = read_jpeg(destination)
    sampled = preview.pixelColor(16, 16)
    assert colour_distance(sampled, QColor(220, 30, 30)) < 24, sampled.name()
    assert colour_distance(sampled, QColor(30, 30, 220)) > 100


@pytest.mark.parametrize("orientation", [6, 8])
def test_exif_orientation_rotates_landscape_source_to_portrait(tmp_path, cache, generator, orientation):
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / "rotated.jpg", 200, 100, orientation)
    destination = destination_for(cache, source)

    result = generator.generate(source, destination)

    assert (result.width, result.height) == (100, 200)
    assert read_jpeg(destination).size().toTuple() == (100, 200)
    assert temporary_files(cache.root) == []


@pytest.mark.parametrize("orientation", [1, 3])
def test_exif_orientation_without_rotation_keeps_landscape(tmp_path, cache, generator, orientation):
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / "flat.jpg", 200, 100, orientation)

    result = generator.generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (200, 100)


def test_exif_orientation_is_applied_after_downscaling(tmp_path):
    cache = make_cache(tmp_path, max_dimension=800)
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / "big-rotated.jpg", 3000, 2000, 6)

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (533, 800)
    assert result.profile_id == "jpeg-max800-q82"


def test_fully_transparent_png_is_flattened_onto_neutral_grey(tmp_path, cache, generator):
    source = write_transparent_png(tmp_path / "src" / "clear.png", 64, 64)
    destination = destination_for(cache, source)

    result = generator.generate(source, destination)

    assert (result.width, result.height) == (64, 64)
    preview = read_jpeg(destination)
    assert not preview.hasAlphaChannel()
    expected = QColor(TRANSPARENCY_BACKGROUND)
    for x, y in ((0, 0), (32, 32), (63, 63), (10, 50)):
        assert preview.pixelColor(x, y).name() == expected.name()


def test_partially_transparent_png_keeps_colour_and_greys_transparent_area(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "half.png", 200, 200, "png", alpha=True)
    assert QImageReader(str(source)).read().hasAlphaChannel()
    destination = destination_for(cache, source)

    generator.generate(source, destination)

    preview = read_jpeg(destination)
    grey = QColor(TRANSPARENCY_BACKGROUND)
    transparent_sample = preview.pixelColor(150, 50)  # top-right quadrant was transparent
    painted_sample = preview.pixelColor(50, 50)  # top-left quadrant is #3366cc
    assert colour_distance(transparent_sample, grey) <= 3
    assert colour_distance(painted_sample, QColor("#3366cc")) <= 12
    assert colour_distance(painted_sample, grey) > 40


def test_large_landscape_image_is_downscaled_preserving_aspect(tmp_path):
    cache = make_cache(tmp_path, max_dimension=800)
    source = write_test_image(tmp_path / "src" / "large.png", 3000, 2000, "png")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (800, 533)
    assert read_jpeg(result.path).size().toTuple() == (800, 533)


def test_large_portrait_image_is_downscaled_preserving_aspect(tmp_path):
    cache = make_cache(tmp_path, max_dimension=800)
    source = write_test_image(tmp_path / "src" / "tall.jpg", 2000, 3000, "jpeg")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (533, 800)


def test_small_image_is_not_upscaled(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "small.png", 200, 100, "png")

    result = generator.generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (200, 100)


def test_configured_maximum_dimension_is_honoured(tmp_path):
    cache = make_cache(tmp_path, max_dimension=320)
    source = write_test_image(tmp_path / "src" / "medium.png", 640, 480, "png")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (320, 240)
    assert "jpeg-max320-q82" in str(result.path)


def test_fallback_scaling_when_reader_ignores_scaled_size(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, max_dimension=800)
    source = write_test_image(tmp_path / "src" / "large.png", 3000, 2000, "png")
    monkeypatch.setattr(image_preview.QImageReader, "setScaledSize", lambda self, size: None)

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (800, 533)


def test_configured_jpeg_quality_changes_output_size(tmp_path):
    source = write_noisy_png(tmp_path / "src" / "noisy.png", 1200, 800)
    low_cache = make_cache(tmp_path / "low", jpeg_quality=40)
    high_cache = make_cache(tmp_path / "high", jpeg_quality=100)

    low = ImagePreviewGenerator(low_cache).generate(source, destination_for(low_cache, source))
    high = ImagePreviewGenerator(high_cache).generate(source, destination_for(high_cache, source))

    assert low.profile_id == "jpeg-max1600-q40"
    assert high.profile_id == "jpeg-max1600-q100"
    assert (low.width, low.height) == (high.width, high.height) == (1200, 800)
    assert 0 < low.size_bytes < high.size_bytes
    assert high.size_bytes > low.size_bytes * 2


def test_source_metadata_is_not_copied_into_the_preview(tmp_path, cache, generator):
    source = tmp_path / "src" / "tagged.png"
    source.parent.mkdir(parents=True)
    image = make_test_image(120, 90)
    image.setText("Description", "JVVV-SECRET-CAMERA-NOTE")
    writer = QImageWriter(str(source), b"png")
    assert writer.write(image)
    assert b"JVVV-SECRET-CAMERA-NOTE" in source.read_bytes(), "PNG fixture should carry the text chunk"
    destination = destination_for(cache, source)

    generator.generate(source, destination)

    assert b"JVVV-SECRET-CAMERA-NOTE" not in destination.read_bytes()
    assert QImageReader(str(destination)).read().textKeys() == []


def test_generate_works_in_a_worker_thread_without_qapplication(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "threaded.jpg", 300, 200, "jpeg")
    destination = destination_for(cache, source)
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            outcome["result"] = generator.generate(source, destination)
        except BaseException as exc:  # pragma: no cover - reported via assertion
            outcome["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    result = outcome["result"]
    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (300, 200)


# ---------------------------------------------------------------------------
# Large sources and Qt's decoder allocation limit
# ---------------------------------------------------------------------------


def test_module_limit_is_bounded_and_large_enough_for_the_biggest_profile():
    # A 9000x9000 JPEG scaled to the 8192 px profile is decoded at full size by
    # libjpeg (it only downscales by 2/4/8), so that intermediate must fit.
    assert IMAGE_ALLOCATION_LIMIT_MB > QT_DEFAULT_IMAGE_ALLOCATION_LIMIT_MB
    assert IMAGE_ALLOCATION_LIMIT_MB * MIB > 9000 * 9000 * 4
    # Bounded: an absurd header must still be refused without touching memory.
    assert IMAGE_ALLOCATION_LIMIT_MB * MIB < 60000 * 60000 * 4


def test_png_above_qt_default_allocation_limit_is_decoded(tmp_path, cache, generator, qt_default_allocation_limit):
    source = write_test_image(tmp_path / "src" / "huge.png", 9000, 8000, "png")
    assert 9000 * 8000 * 4 > qt_default_allocation_limit * MIB, "fixture must exceed Qt's default"
    destination = destination_for(cache, source)

    result = generator.generate(source, destination)

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (1600, 1422)
    assert validate_image_preview(destination).valid
    assert QImageReader.allocationLimit() == IMAGE_ALLOCATION_LIMIT_MB
    assert temporary_files(cache.root) == []
    assert files_under(cache.root) == [destination]


def test_16bit_tiff_scan_above_qt_default_allocation_limit_is_decoded(tmp_path, cache, generator, qt_default_allocation_limit):
    source = write_deep_tiff(tmp_path / "src" / "scan.tiff", 6000, 6000)
    reader = QImageReader(str(source))
    assert reader.imageFormat() == QImage.Format.Format_RGBA64
    assert 6000 * 6000 * 8 > qt_default_allocation_limit * MIB
    destination = destination_for(cache, source)

    result = generator.generate(source, destination)

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (1600, 1600)
    assert colour_distance(read_jpeg(destination).pixelColor(800, 800), QColor(30, 60, 200)) <= 6
    assert temporary_files(cache.root) == []


def test_maximum_profile_dimension_decodes_a_larger_jpeg_source(tmp_path, qt_default_allocation_limit):
    cache = make_cache(tmp_path, max_dimension=8192)
    source = write_test_image(tmp_path / "src" / "9k.jpg", 9000, 9000, "jpeg")
    destination = destination_for(cache, source)

    result = ImagePreviewGenerator(cache).generate(source, destination)

    assert result.status == PREVIEW_GENERATED
    assert result.profile_id == "jpeg-max8192-q82"
    assert (result.width, result.height) == (8192, 8192)
    validation = validate_image_preview(destination)
    assert validation.valid and (validation.width, validation.height) == (8192, 8192)
    assert temporary_files(cache.root) == []


def test_validate_reads_a_preview_larger_than_qt_default_allocation_limit(tmp_path, qt_default_allocation_limit):
    path = write_test_image(tmp_path / "big-preview.jpg", 8200, 8200, "jpeg")
    assert 8200 * 8200 * 4 > qt_default_allocation_limit * MIB

    validation = validate_image_preview(path)

    assert validation.valid is True
    assert (validation.width, validation.height) == (8200, 8200)


def test_header_claiming_absurd_size_fails_honestly_as_too_large(tmp_path, cache, generator, qt_default_allocation_limit):
    source = write_png_claiming_size(tmp_path / "src" / "gigantic.png", 60000, 60000)
    assert QImageReader(str(source)).size().toTuple() == (60000, 60000)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    error = info.value
    assert error.stage == STAGE_IMAGE_DECODE
    assert error.message == "The image is too large to decode."
    assert "60000x60000" in error.detail
    assert "14.4 GB" in error.detail
    assert f"{ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE}={IMAGE_ALLOCATION_LIMIT_MB}" in error.detail
    assert "Unable to read image data" in error.detail  # Qt's own text is kept too
    assert QImageReader.allocationLimit() == IMAGE_ALLOCATION_LIMIT_MB, "the limit must stay bounded"
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_corrupt_file_with_readable_header_is_still_reported_as_undecodable(tmp_path, cache, generator):
    # Header well under the limit but no pixel data: not a size problem.
    source = write_png_claiming_size(tmp_path / "src" / "truncated.png", 640, 480)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert info.value.message == "Image decoder could not read the file."
    assert "640x480" in info.value.detail
    assert "too large" not in str(info.value).casefold()
    assert temporary_files(cache.root) == []


def test_explicit_qt_environment_override_is_respected(tmp_path, cache, generator, monkeypatch, qt_default_allocation_limit):
    monkeypatch.setenv(ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE, "64")
    QImageReader.setAllocationLimit(64)  # what Qt does at start-up when the variable is set
    small = write_test_image(tmp_path / "src" / "fits.png", 320, 200, "png")
    over = write_png_claiming_size(tmp_path / "src" / "over.png", 6000, 6000)  # 144 MB > 64 MB

    result = generator.generate(small, destination_for(cache, small))
    with pytest.raises(PreviewError) as info:
        generator.generate(over, destination_for(cache, over))

    assert result.status == PREVIEW_GENERATED
    assert QImageReader.allocationLimit() == 64, "an operator override must not be replaced"
    assert info.value.message == "The image is too large to decode."
    assert f"{ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE}=64" in info.value.detail
    assert temporary_files(cache.root) == []


def test_backend_message_reports_the_effective_allocation_limit(qt_default_allocation_limit):
    available, message = image_backend_available()

    assert available is True
    assert f"{ALLOCATION_LIMIT_ENVIRONMENT_VARIABLE}={IMAGE_ALLOCATION_LIMIT_MB}" in message
    assert QImageReader.allocationLimit() == IMAGE_ALLOCATION_LIMIT_MB


# ---------------------------------------------------------------------------
# Atomic output
# ---------------------------------------------------------------------------


def test_preview_is_written_to_a_temporary_name_and_published_atomically(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "atomic.png", 300, 200, "png")
    destination = destination_for(cache, source)
    observed: dict[str, object] = {}
    original_publish = cache.publish

    def recording_publish(temp_path, final_path):
        observed["temp"] = pathlib.Path(temp_path)
        observed["final"] = pathlib.Path(final_path)
        observed["temp_exists"] = pathlib.Path(temp_path).is_file()
        observed["final_exists_before"] = pathlib.Path(final_path).exists()
        observed["temp_valid"] = validate_image_preview(pathlib.Path(temp_path)).valid
        return original_publish(temp_path, final_path)

    monkeypatch.setattr(cache, "publish", recording_publish)

    result = generator.generate(source, destination)

    temp = observed["temp"]
    assert observed["final"] == destination
    assert temp.parent == destination.parent
    assert temp.name.startswith(".")
    assert ".tmp-" in temp.name
    assert destination.name in temp.name
    assert PreviewCache.is_temporary_name(temp.name)
    assert observed["temp_exists"] is True
    assert observed["temp_valid"] is True
    assert observed["final_exists_before"] is False
    assert not temp.exists()
    assert destination.is_file()
    assert result.path == destination
    assert temporary_files(cache.root) == []
    assert [path.name for path in destination.parent.iterdir()] == [destination.name]


def test_generating_twice_to_the_same_destination_replaces_it_atomically(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "twice.png", 300, 200, "png")
    destination = destination_for(cache, source)

    first = generator.generate(source, destination)
    first_bytes = destination.read_bytes()
    second = generator.generate(source, destination)

    assert first.status == second.status == PREVIEW_GENERATED
    assert (first.width, first.height) == (second.width, second.height) == (300, 200)
    assert second.path == first.path == destination
    assert destination.read_bytes() == first_bytes  # deterministic output
    assert validate_image_preview(destination).valid
    assert temporary_files(cache.root) == []
    assert files_under(cache.root) == [destination]


def test_publish_failure_propagates_and_leaves_no_temporary(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "publish.png", 120, 80, "png")
    destination = destination_for(cache, source)

    def failing_publish(temp_path, final_path):
        cache.discard_temporary(temp_path)
        raise PreviewError(STAGE_RENAME, "Could not move the finished preview into place.", detail="[WinError 5]")

    monkeypatch.setattr(cache, "publish", failing_publish)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_RENAME
    assert not destination.exists()
    assert temporary_files(cache.root) == []


# ---------------------------------------------------------------------------
# validate_image_preview
# ---------------------------------------------------------------------------


def test_validate_accepts_a_real_jpeg(tmp_path):
    path = write_test_image(tmp_path / "ok.jpg", 40, 30, "jpeg")

    validation = validate_image_preview(path)

    assert validation == ImagePreviewValidation(True, 40, 30, path.stat().st_size, validation.message)
    assert "40x30" in validation.message


def test_validate_rejects_zero_byte_file(tmp_path):
    path = tmp_path / "empty.jpg"
    path.write_bytes(b"")

    validation = validate_image_preview(path)

    assert validation.valid is False
    assert validation.size_bytes == 0
    assert (validation.width, validation.height) == (None, None)
    assert "empty" in validation.message.casefold()


def test_validate_rejects_garbage(tmp_path):
    path = tmp_path / "garbage.jpg"
    path.write_bytes(b"this is definitely not a jpeg file" * 20)

    validation = validate_image_preview(path)

    assert validation.valid is False
    assert validation.size_bytes == path.stat().st_size
    assert "not a jpeg" in validation.message.casefold()


def test_validate_rejects_truncated_jpeg(tmp_path):
    path = write_test_image(tmp_path / "full.jpg", 400, 300, "jpeg")
    data = path.read_bytes()
    start_of_scan = data.find(b"\xff\xda")
    assert start_of_scan > 0
    truncated = tmp_path / "truncated.jpg"
    truncated.write_bytes(data[:start_of_scan])
    assert bytes(QImageReader(str(truncated)).format()) == b"jpeg"

    validation = validate_image_preview(truncated)

    assert validation.valid is False
    assert validation.size_bytes == start_of_scan
    assert "decoder could not read" in validation.message.casefold()


def test_validate_rejects_png_renamed_as_jpg(tmp_path):
    path = write_test_image(tmp_path / "really-a-png.jpg", 40, 30, "png")

    validation = validate_image_preview(path)

    assert validation.valid is False
    assert "png" in validation.message.casefold()


def test_validate_rejects_missing_file_and_directory(tmp_path):
    missing = validate_image_preview(tmp_path / "missing.jpg")
    directory = validate_image_preview(tmp_path)

    assert missing.valid is False
    assert "could not be read" in missing.message.casefold()
    assert directory.valid is False
    assert "regular file" in directory.message.casefold()


def test_validate_never_raises_for_odd_paths(tmp_path):
    assert validate_image_preview(tmp_path / "nested" / "deeper" / "x.jpg").valid is False
    assert validate_image_preview(pathlib.Path("")).valid is False


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_corrupt_source_fails_at_image_decode_without_temporaries(tmp_path, cache, generator):
    source = tmp_path / "src" / "broken.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xff\xd8 not really a jpeg" + b"\x00" * 200)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    error = info.value
    assert error.stage == STAGE_IMAGE_DECODE
    assert error.message == "Image decoder could not read the file."
    assert error.detail
    assert "Image decoder could not read the file." in str(error)
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_unsupported_heic_fails_visibly_at_image_decode(tmp_path, cache, generator):
    source = tmp_path / "src" / "IMG_0001.heic"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 256)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert info.value.message == "Image decoder could not read the file."
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_missing_source_fails_at_image_decode(tmp_path, cache, generator):
    source = tmp_path / "src" / "gone.png"
    destination = cache.preview_path("image", hashlib.sha256(b"gone").digest())

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert info.value.message == "The source image could not be read."
    assert info.value.detail
    assert files_under(cache.root) == []


def test_failed_regeneration_preserves_the_existing_valid_preview(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "good.png", 100, 60, "png")
    destination = destination_for(cache, source)
    generator.generate(source, destination)
    good_bytes = destination.read_bytes()
    source.write_bytes(b"corrupted after the first scan" * 10)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert destination.read_bytes() == good_bytes
    assert validate_image_preview(destination).valid
    assert temporary_files(cache.root) == []


def test_cancellation_after_decode_raises_and_leaves_nothing(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "cancel.png", 300, 200, "png")
    destination = destination_for(cache, source)
    calls = {"count": 0}

    def cancel_callback() -> bool:
        calls["count"] += 1
        return calls["count"] >= 2

    with pytest.raises(PreviewCancelled):
        generator.generate(source, destination, cancel_callback=cancel_callback)

    assert calls["count"] == 2
    assert not destination.exists()
    assert temporary_files(cache.root) == []
    assert files_under(cache.root) == []


def test_cancellation_after_encode_removes_the_temporary_file(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "cancel-late.png", 300, 200, "png")
    destination = destination_for(cache, source)
    issued: list[pathlib.Path] = []
    original_temporary_path = cache.temporary_path

    def recording_temporary_path(final_path):
        temp = original_temporary_path(final_path)
        issued.append(temp)
        return temp

    monkeypatch.setattr(cache, "temporary_path", recording_temporary_path)
    calls = {"count": 0}

    def cancel_callback() -> bool:
        calls["count"] += 1
        if calls["count"] == 3:
            assert issued and issued[0].is_file(), "the encoded temporary should exist at the third check"
            return True
        return False

    with pytest.raises(PreviewCancelled):
        generator.generate(source, destination, cancel_callback=cancel_callback)

    assert calls["count"] == 3
    assert issued and not issued[0].exists()
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_cancellation_before_decode_never_reads_the_source(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "cancel-early.png", 30, 20, "png")
    destination = destination_for(cache, source)
    decoded = {"count": 0}
    original_decode = ImagePreviewGenerator._decode

    def counting_decode(self, path):
        decoded["count"] += 1
        return original_decode(self, path)

    monkeypatch.setattr(ImagePreviewGenerator, "_decode", counting_decode)

    with pytest.raises(PreviewCancelled):
        generator.generate(source, destination, cancel_callback=lambda: True)

    assert decoded["count"] == 0
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_completed_generation_without_cancel_checks_every_stage(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "counted.png", 30, 20, "png")
    calls = {"count": 0}

    def cancel_callback() -> bool:
        calls["count"] += 1
        return False

    result = generator.generate(source, destination_for(cache, source), cancel_callback=cancel_callback)

    assert result.status == PREVIEW_GENERATED
    assert calls["count"] >= 3


def test_source_modified_during_generation_is_reported(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "changing.png", 200, 150, "png")
    destination = destination_for(cache, source)

    class SourceMutatingWriter(QImageWriter):
        def write(self, image):
            with open(source, "ab") as handle:
                handle.write(b"\x00" * 64)  # size changes even if mtime granularity hides the edit
            return super().write(image)

    monkeypatch.setattr(image_preview, "QImageWriter", SourceMutatingWriter)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert info.value.message == "The source file changed while its preview was being created."
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_source_removed_during_generation_is_reported(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "vanishing.png", 200, 150, "png")
    destination = destination_for(cache, source)

    class SourceDeletingWriter(QImageWriter):
        def write(self, image):
            written = super().write(image)
            source.unlink()
            return written

    monkeypatch.setattr(image_preview, "QImageWriter", SourceDeletingWriter)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert "disappeared" in info.value.detail
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_encode_failure_mentioning_space_is_classified_as_disk_full(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "full-disk.png", 200, 150, "png")
    destination = destination_for(cache, source)
    created: list[tuple[str, bytes, int | None]] = []

    class FullDiskWriter:
        def __init__(self, path, image_format):
            self.path = path
            self.image_format = image_format
            self.quality = None

        def setQuality(self, quality):
            self.quality = quality
            created.append((self.path, self.image_format, quality))

        def write(self, image):
            return False

        def errorString(self):
            return "Not enough space on the device"

    monkeypatch.setattr(image_preview, "QImageWriter", FullDiskWriter)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_DISK_FULL
    assert info.value.message == "Could not write preview."
    assert "Not enough space" in info.value.detail
    path_text, image_format, quality = created[0]
    assert image_format == b"jpeg"
    assert quality == 82
    temp = pathlib.Path(path_text)
    assert temp.parent == destination.parent and ".tmp-" in temp.name
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_other_encode_failure_is_classified_as_image_encode(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "unwritable.png", 200, 150, "png")
    destination = destination_for(cache, source)

    class BrokenWriter:
        def __init__(self, path, image_format):
            pass

        def setQuality(self, quality):
            pass

        def write(self, image):
            return False

        def errorString(self):
            return "Device not writable"

    monkeypatch.setattr(image_preview, "QImageWriter", BrokenWriter)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_ENCODE
    assert info.value.detail == "Device not writable"
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_invalid_temporary_output_fails_validation_and_is_removed(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "validate.png", 200, 150, "png")
    destination = destination_for(cache, source)
    checked: list[pathlib.Path] = []

    def failing_validation(path):
        checked.append(pathlib.Path(path))
        return ImagePreviewValidation(False, None, None, 0, "The preview is not a JPEG file (detected format: png).")

    monkeypatch.setattr(image_preview, "validate_image_preview", failing_validation)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_VALIDATE
    assert info.value.message == "The preview is not a JPEG file (detected format: png)."
    assert checked and ".tmp-" in checked[0].name
    assert not checked[0].exists()
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_unexpected_exception_still_removes_the_temporary(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "transform.png", 200, 150, "png")
    destination = destination_for(cache, source)
    issued: list[pathlib.Path] = []
    original_temporary_path = cache.temporary_path

    def recording_temporary_path(final_path):
        temp = original_temporary_path(final_path)
        issued.append(temp)
        return temp

    def exploding_source_check(source_path, before):
        raise RuntimeError("unexpected failure after the temporary was written")

    monkeypatch.setattr(cache, "temporary_path", recording_temporary_path)
    monkeypatch.setattr(ImagePreviewGenerator, "_ensure_source_unchanged", staticmethod(exploding_source_check))

    with pytest.raises(RuntimeError):
        generator.generate(source, destination)

    assert issued and not issued[0].exists()
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_transform_failure_is_classified_as_image_transform(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, max_dimension=320)
    source = write_test_image(tmp_path / "src" / "big.png", 640, 480, "png")
    monkeypatch.setattr(image_preview.QImageReader, "setScaledSize", lambda self, size: None)
    monkeypatch.setattr(image_preview.QImage, "scaled", lambda self, *args, **kwargs: QImage())

    with pytest.raises(PreviewError) as info:
        ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_TRANSFORM
    assert info.value.message == "The image could not be resized."
    assert temporary_files(cache.root) == []
    assert files_under(cache.root) == []


def test_invalid_image_profile_is_reported_as_configuration_error(tmp_path, cache, monkeypatch):
    bad_profile = ImagePreviewProfile(max_dimension=10)
    monkeypatch.setattr(cache, "image_profile", bad_profile, raising=False)

    with pytest.raises(PreviewError) as info:
        ImagePreviewGenerator(cache)

    assert info.value.stage == STAGE_CONFIGURATION
    assert "Image maximum dimension" in info.value.detail


# ---------------------------------------------------------------------------
# Backend self-test (spec section 2C)
# ---------------------------------------------------------------------------


def test_backend_self_test_encodes_validates_and_cleans_up(tmp_path):
    cache = make_cache(tmp_path, jpeg_quality=60)
    assert not cache.root.exists()

    message = run_image_backend_test(cache)

    width, height = IMAGE_TEST_SIZE
    assert isinstance(message, str)
    assert message.startswith(f"Encoded a {width}x{height} test image to JPEG quality 60 (")
    assert " KB) in " in message or " B) in " in message
    assert message.endswith(str(cache.root))
    assert cache.root.is_dir()
    assert files_under(cache.root) == []


def test_backend_self_test_uses_the_real_pipeline(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    seen: dict[str, object] = {}
    original_encode = image_preview._encode_jpeg

    def recording_encode(image, temp_path, jpeg_quality):
        seen["size"] = image.size().toTuple()
        seen["alpha"] = image.hasAlphaChannel()
        seen["format"] = image.format()
        seen["temp"] = pathlib.Path(temp_path)
        seen["quality"] = jpeg_quality
        original_encode(image, temp_path, jpeg_quality)
        seen["temp_exists"] = pathlib.Path(temp_path).is_file()

    monkeypatch.setattr(image_preview, "_encode_jpeg", recording_encode)

    run_image_backend_test(cache)

    assert seen["size"] == IMAGE_TEST_SIZE
    assert seen["alpha"] is False
    assert seen["format"] == QImage.Format.Format_RGB32
    assert seen["quality"] == 82
    temp = seen["temp"]
    assert temp.parent == cache.root
    assert temp.name.startswith(".") and ".tmp-" in temp.name and "jvvv-image-test.jpg" in temp.name
    assert seen["temp_exists"] is True
    assert not temp.exists()
    assert files_under(cache.root) == []


def test_backend_self_test_failure_raises_and_leaves_no_files(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)

    class FullDiskWriter:
        supportedImageFormats = staticmethod(QImageWriter.supportedImageFormats)

        def __init__(self, path, image_format):
            pathlib.Path(path).write_bytes(b"partial")  # simulate a half-written file

        def setQuality(self, quality):
            pass

        def write(self, image):
            return False

        def errorString(self):
            return "There is not enough space on the disk."

    monkeypatch.setattr(image_preview, "QImageWriter", FullDiskWriter)

    with pytest.raises(PreviewError) as info:
        run_image_backend_test(cache)

    assert info.value.stage == STAGE_DISK_FULL
    assert info.value.message == "Could not write preview."
    assert files_under(cache.root) == []


def test_backend_self_test_reports_unavailable_backend(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(image_preview, "image_backend_available", lambda: (False, "no JPEG plugin"))

    with pytest.raises(PreviewError) as info:
        run_image_backend_test(cache)

    assert info.value.message == "no JPEG plugin"
    assert files_under(cache.root) == []


def test_backend_self_test_rejects_invalid_profile(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(cache, "image_profile", ImagePreviewProfile(jpeg_quality=5), raising=False)

    with pytest.raises(PreviewError) as info:
        run_image_backend_test(cache)

    assert info.value.stage == STAGE_CONFIGURATION
    assert files_under(cache.root) == []
