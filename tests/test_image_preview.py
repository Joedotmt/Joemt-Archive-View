"""Image previews on the Pillow + pillow-heif + rawpy backend (spec §7, §12, §32, §39)."""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import struct
import sys
import threading
import time
import zlib

import numpy as np
import pytest
from PIL import Image, features

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from preview_fixtures import (  # noqa: E402
    build_dng,
    jpeg_bytes,
    make_test_image,
    write_dng,
    write_heic,
    write_jpeg_with_exif_orientation,
    write_test_image,
)

from jvvv import image_preview  # noqa: E402
from jvvv.image_preview import (  # noqa: E402
    IMAGE_BACKEND_NAME,
    IMAGE_TEST_SIZE,
    MAX_SOURCE_PIXELS,
    SOURCE_PILLOW,
    SOURCE_RAW_DEMOSAIC,
    SOURCE_RAW_EMBEDDED,
    TRANSPARENCY_BACKGROUND,
    ImageOpenError,
    ImagePreviewGenerator,
    ImagePreviewValidation,
    compute_target_size,
    image_backend_available,
    open_image,
    read_image_dimensions,
    validate_image_preview,
)
from jvvv.image_preview import test_image_backend as run_image_backend_test  # noqa: E402
from jvvv.media_metadata import HEIF_EXTENSIONS, IMAGE_EXTENSIONS, RAW_EXTENSIONS  # noqa: E402
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


def read_jpeg(path: pathlib.Path) -> Image.Image:
    with Image.open(path) as image:
        assert image.format == "JPEG"
        return image.convert("RGB")


def pixel(image: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    value = image.getpixel((x, y))
    return tuple(int(channel) for channel in value[:3])  # type: ignore[index]


def colour_distance(colour: tuple[int, int, int], expected: tuple[int, int, int]) -> int:
    return max(abs(a - b) for a, b in zip(colour, expected))


GREY = (0x80, 0x80, 0x80)
assert TRANSPARENCY_BACKGROUND == "#808080"


def write_transparent_png(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(path, "PNG")
    return path


def write_noisy_png(path: pathlib.Path, width: int, height: int, seed: int = 7) -> pathlib.Path:
    """Random noise: JPEG size depends strongly on quality."""

    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    Image.fromarray(pixels, "RGB").save(path, "PNG")
    return path


def write_animated_gif(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    """Two-frame GIF: frame 1 is solid red, frame 2 is solid blue."""

    path.parent.mkdir(parents=True, exist_ok=True)
    red = Image.new("RGB", (width, height), (220, 30, 30))
    blue = Image.new("RGB", (width, height), (30, 30, 220))
    red.save(path, "GIF", save_all=True, append_images=[blue], duration=100, loop=0)
    return path


def write_png_claiming_size(path: pathlib.Path, width: int, height: int) -> pathlib.Path:
    """A tiny PNG whose IHDR header claims ``width`` x ``height`` pixels."""

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(path, "PNG")
    data = bytearray(path.read_bytes())
    assert data[12:16] == b"IHDR"
    struct.pack_into(">II", data, 16, width, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    path.write_bytes(bytes(data))
    return path


@pytest.fixture
def cache(tmp_path: pathlib.Path) -> PreviewCache:
    return make_cache(tmp_path)


@pytest.fixture
def generator(cache: PreviewCache) -> ImagePreviewGenerator:
    return ImagePreviewGenerator(cache)


# ---------------------------------------------------------------------------
# Backend availability (spec §2C: all dependencies must be present)
# ---------------------------------------------------------------------------


def test_image_backend_is_available_and_describes_formats():
    available, message = image_backend_available()

    assert available is True
    assert message.startswith(IMAGE_BACKEND_NAME)
    assert "Pillow" in message and "pillow-heif" in message and "rawpy" in message
    assert "HEIC" in message
    for extension in ("dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2"):
        assert extension in message


def test_backend_unavailable_without_rawpy(monkeypatch):
    monkeypatch.setattr(image_preview, "rawpy", None)
    monkeypatch.setattr(image_preview, "RAW_ERROR", "ModuleNotFoundError: No module named 'rawpy'")

    available, message = image_backend_available()

    assert available is False
    assert "rawpy" in message and "No module named 'rawpy'" in message
    assert "pip install -r requirements.txt" in message


def test_backend_unavailable_without_pillow_heif(monkeypatch):
    monkeypatch.setattr(image_preview, "pillow_heif", None)
    monkeypatch.setattr(image_preview, "HEIF_ERROR", "")

    available, message = image_backend_available()

    assert available is False
    assert "pillow-heif (HEIC/HEIF) is not installed" in message


def test_backend_unavailable_without_a_hevc_decoder(monkeypatch):
    class NoDecoders:
        __version__ = "1.0"

        @staticmethod
        def libheif_info():
            return {"libheif": "1.23.2", "decoders": {}, "encoders": {}}

    monkeypatch.setattr(image_preview, "pillow_heif", NoDecoders)

    available, message = image_backend_available()

    assert available is False
    assert "no HEVC decoder" in message


def test_backend_unavailable_without_jpeg_support(monkeypatch):
    monkeypatch.setattr(features, "check", lambda feature: feature != "jpg")

    available, message = image_backend_available()

    assert available is False
    assert "JPEG" in message


def test_extension_sets_cover_raw_and_heif_and_exclude_generic_raw():
    for extension in ("dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw", "x3f", "3fr", "iiq"):
        assert extension in RAW_EXTENSIONS
    assert "raw" not in RAW_EXTENSIONS  # ambiguous: many unrelated tools use .raw
    assert HEIF_EXTENSIONS == {"heic", "heif", "hif"}
    assert RAW_EXTENSIONS <= IMAGE_EXTENSIONS and HEIF_EXTENSIONS <= IMAGE_EXTENSIONS
    assert {"jpg", "png", "tif", "webp"} <= IMAGE_EXTENSIONS


def test_generator_exposes_backend_and_profile(cache):
    generator = ImagePreviewGenerator(cache)

    assert generator.backend_name == IMAGE_BACKEND_NAME
    assert generator.media_kind == "image"
    assert generator.profile_id == "jpeg-max1600-q82"
    assert generator.profile == cache.image_profile


# ---------------------------------------------------------------------------
# Target size arithmetic (spec §7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "maximum", "expected"),
    [
        (6000, 4000, 1600, (1600, 1067)),
        (4000, 6000, 1600, (1067, 1600)),
        (1200, 800, 1600, (1200, 800)),
        (1600, 1600, 1600, (1600, 1600)),
        (1601, 1, 1600, (1600, 1)),
        (1, 1601, 1600, (1, 1600)),
        (10000, 10, 1600, (1600, 2)),
        (3200, 2000, 1600, (1600, 1000)),
    ],
)
def test_compute_target_size_examples(width, height, maximum, expected):
    assert compute_target_size(width, height, maximum) == expected


def test_compute_target_size_never_exceeds_maximum_or_upscales():
    for width in (1, 7, 640, 1599, 1600, 1601, 9000):
        for height in (1, 9, 480, 1600, 2400):
            target_w, target_h = compute_target_size(width, height, 1600)
            assert max(target_w, target_h) <= 1600
            assert target_w <= width and target_h <= height
            assert target_w >= 1 and target_h >= 1


@pytest.mark.parametrize(("width", "height", "maximum"), [(0, 10, 100), (10, 0, 100), (10, 10, 0), (-1, 5, 100)])
def test_compute_target_size_rejects_non_positive_values(width, height, maximum):
    with pytest.raises(ValueError):
        compute_target_size(width, height, maximum)


# ---------------------------------------------------------------------------
# Standard formats through Pillow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fmt",
    [
        "png",
        "jpeg",
        "bmp",
        "gif",
        "tiff",
        pytest.param(
            "webp",
            marks=pytest.mark.skipif(not features.check("webp"), reason="Pillow built without WebP"),
        ),
    ],
)
def test_source_formats_produce_a_valid_jpeg_preview(tmp_path, cache, generator, fmt):
    source = write_test_image(tmp_path / "src" / f"photo.{fmt}", 320, 200, fmt)
    destination = destination_for(cache, source)

    result = generator.generate(source, destination)

    assert result.status == PREVIEW_GENERATED
    assert result.media_kind == "image"
    assert result.profile_id == "jpeg-max1600-q82"
    assert result.path == destination
    assert (result.width, result.height) == (320, 200)
    assert result.detail == SOURCE_PILLOW
    assert result.bytes_written == result.size_bytes == destination.stat().st_size > 0
    validation = validate_image_preview(destination)
    assert validation.valid and (validation.width, validation.height) == (320, 200)
    preview = read_jpeg(destination)
    assert colour_distance(pixel(preview, 40, 40), (0x33, 0x66, 0xCC)) <= 8
    assert colour_distance(pixel(preview, 280, 180), (0xCC, 0x33, 0x33)) <= 8
    assert temporary_files(cache.root) == []


def test_ico_source_is_supported(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "icon.ico", 64, 64, "ico")

    result = generator.generate(source, destination_for(cache, source))

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (64, 64)


def test_16bit_greyscale_tiff_is_scaled_to_8_bits(tmp_path, cache, generator):
    source = tmp_path / "src" / "scan16.tif"
    source.parent.mkdir(parents=True)
    Image.fromarray(np.full((40, 60), 65535 // 2, dtype=np.uint16)).save(source, "TIFF")

    result = generator.generate(source, destination_for(cache, source))

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (60, 40)
    assert colour_distance(pixel(read_jpeg(result.path), 30, 20), (127, 127, 127)) <= 6


def test_animated_gif_uses_its_first_frame(tmp_path, cache, generator):
    source = write_animated_gif(tmp_path / "src" / "loop.gif", 40, 30)

    result = generator.generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (40, 30)
    assert colour_distance(pixel(read_jpeg(result.path), 20, 15), (220, 30, 30)) <= 12


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_exif_orientation_rotates_landscape_source_to_portrait(tmp_path, cache, generator, orientation):
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / f"o{orientation}.jpg", 200, 100, orientation)

    result = generator.generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (100, 200)
    with Image.open(result.path) as preview:
        assert preview.size == (100, 200)
        assert preview.getexif().get(0x0112) is None, "the preview must not carry an orientation tag"


@pytest.mark.parametrize("orientation", [1, 2, 3, 4])
def test_exif_orientation_without_rotation_keeps_landscape(tmp_path, cache, generator, orientation):
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / f"o{orientation}.jpg", 200, 100, orientation)

    result = generator.generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (200, 100)


def test_exif_orientation_is_applied_before_the_maximum_dimension(tmp_path):
    cache = make_cache(tmp_path, max_dimension=400)
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / "tall.jpg", 1600, 800, 6)

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (200, 400)


def test_exif_orientation_6_puts_the_top_left_corner_top_right(tmp_path, cache, generator):
    # The source's top-left quarter is blue; rotating 90° clockwise moves it to the top-right.
    source = write_jpeg_with_exif_orientation(tmp_path / "src" / "rot.jpg", 200, 100, 6)

    result = generator.generate(source, destination_for(cache, source))

    preview = read_jpeg(result.path)
    assert preview.size == (100, 200)
    assert colour_distance(pixel(preview, 75, 25), (0x33, 0x66, 0xCC)) <= 10
    assert colour_distance(pixel(preview, 25, 175), (0xCC, 0x33, 0x33)) <= 10


def test_fully_transparent_png_is_flattened_onto_neutral_grey(tmp_path, cache, generator):
    source = write_transparent_png(tmp_path / "src" / "clear.png", 64, 48)

    result = generator.generate(source, destination_for(cache, source))

    preview = read_jpeg(result.path)
    for point in ((0, 0), (32, 24), (63, 47)):
        assert colour_distance(pixel(preview, *point), GREY) <= 3


def test_partially_transparent_png_keeps_colour_and_greys_transparent_area(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "half.png", 120, 80, "png", alpha=True)
    with Image.open(source) as check:
        assert check.mode == "RGBA"

    result = generator.generate(source, destination_for(cache, source))

    preview = read_jpeg(result.path)
    assert colour_distance(pixel(preview, 20, 20), (0x33, 0x66, 0xCC)) <= 8  # opaque quarter kept
    assert colour_distance(pixel(preview, 100, 20), GREY) <= 4  # transparent quarter -> grey


def test_palette_png_with_transparency_is_flattened(tmp_path, cache, generator):
    source = tmp_path / "src" / "palette.png"
    source.parent.mkdir(parents=True)
    make_test_image(60, 40, alpha=True).quantize(colors=8).save(source, "PNG")
    with Image.open(source) as check:
        assert check.mode == "P" and "transparency" in check.info

    result = generator.generate(source, destination_for(cache, source))

    preview = read_jpeg(result.path)
    assert colour_distance(pixel(preview, 50, 10), GREY) <= 6


def test_large_landscape_image_is_downscaled_preserving_aspect(tmp_path):
    cache = make_cache(tmp_path, max_dimension=800)
    source = write_test_image(tmp_path / "src" / "wide.png", 3200, 2000, "png")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (800, 500)


def test_large_portrait_image_is_downscaled_preserving_aspect(tmp_path):
    cache = make_cache(tmp_path, max_dimension=800)
    source = write_test_image(tmp_path / "src" / "tall.png", 2000, 3200, "png")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (500, 800)


def test_large_jpeg_uses_dct_scaling_but_is_never_smaller_than_the_target(tmp_path):
    cache = make_cache(tmp_path, max_dimension=1600)
    source = write_test_image(tmp_path / "src" / "big.jpg", 6000, 4000, "jpeg")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (1600, 1067)
    preview = read_jpeg(result.path)
    assert colour_distance(pixel(preview, 200, 200), (0x33, 0x66, 0xCC)) <= 10


def test_small_image_is_not_upscaled(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "small.png", 120, 90, "png")

    result = generator.generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (120, 90)


def test_configured_maximum_dimension_is_honoured(tmp_path):
    cache = make_cache(tmp_path, max_dimension=320)
    source = write_test_image(tmp_path / "src" / "medium.png", 640, 480, "png")

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (320, 240)
    assert result.profile_id == "jpeg-max320-q82"


def test_configured_jpeg_quality_changes_output_size(tmp_path):
    source = write_noisy_png(tmp_path / "src" / "noise.png", 400, 300)
    sizes = {}
    for quality in (45, 95):
        cache = make_cache(tmp_path / str(quality), jpeg_quality=quality)
        result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))
        sizes[quality] = result.size_bytes
        assert result.profile_id == f"jpeg-max1600-q{quality}"

    assert sizes[95] > sizes[45] * 1.5


def test_source_metadata_is_not_copied_but_icc_profile_is(tmp_path, cache, generator):
    source = tmp_path / "src" / "tagged.jpg"
    source.parent.mkdir(parents=True)
    exif = Image.Exif()
    exif[0x010F] = "JVVV Camera Co."  # Make
    exif[0x9003] = "2024:01:02 03:04:05"  # DateTimeOriginal (in the EXIF IFD)
    fake_icc = b"ICC_PROFILE_TEST" * 8
    make_test_image(200, 150).save(
        source,
        "JPEG",
        quality=90,
        exif=exif.tobytes(),
        icc_profile=fake_icc,
        comment=b"do not copy this comment",
    )

    result = generator.generate(source, destination_for(cache, source))

    with Image.open(result.path) as preview:
        assert dict(preview.getexif()) == {}
        assert "comment" not in preview.info
        assert "xmp" not in preview.info
        assert preview.info.get("icc_profile") == fake_icc
    assert b"JVVV Camera Co." not in result.path.read_bytes()


def test_generating_the_same_source_twice_is_deterministic(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "twice.png", 300, 200, "png")
    destination = destination_for(cache, source)

    first = generator.generate(source, destination)
    first_bytes = destination.read_bytes()
    second = generator.generate(source, destination)

    assert first.status == second.status == PREVIEW_GENERATED
    assert (first.width, first.height) == (second.width, second.height) == (300, 200)
    assert destination.read_bytes() == first_bytes
    assert files_under(cache.root) == [destination]


def test_generate_works_in_a_worker_thread(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "threaded.png", 300, 200, "png")
    destination = destination_for(cache, source)
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            outcome["result"] = generator.generate(source, destination)
        except BaseException as exc:  # pragma: no cover - reported through the assertion
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"].status == PREVIEW_GENERATED  # type: ignore[union-attr]
    assert validate_image_preview(destination).valid


# ---------------------------------------------------------------------------
# Decode limits and hostile headers (spec §32)
# ---------------------------------------------------------------------------


def test_decode_limit_is_explicit_and_large_enough_for_real_photos():
    assert Image.MAX_IMAGE_PIXELS is None, "Pillow's own bomb guard is replaced by the explicit budget"
    assert 200_000_000 <= MAX_SOURCE_PIXELS <= 2_000_000_000


def test_header_claiming_absurd_size_fails_honestly(tmp_path, cache, generator):
    source = write_png_claiming_size(tmp_path / "src" / "huge.png", 60000, 60000)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert "60000x60000" in info.value.detail and "megapixel" in info.value.detail
    assert temporary_files(cache.root) == []
    assert files_under(cache.root) == []


def test_corrupt_file_with_readable_header_is_still_reported_as_undecodable(tmp_path, cache, generator):
    source = write_png_claiming_size(tmp_path / "src" / "lying.png", 4000, 3000)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert info.value.message == "Image decoder could not read the file."
    assert files_under(cache.root) == []


# ---------------------------------------------------------------------------
# Atomic output (spec §10)
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
# validate_image_preview (spec §11)
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
    assert validation.message == "The preview file is empty."


def test_validate_rejects_garbage(tmp_path):
    path = tmp_path / "garbage.jpg"
    path.write_bytes(b"definitely not a jpeg" * 10)

    validation = validate_image_preview(path)

    assert validation.valid is False
    assert "not a JPEG" in validation.message


def test_validate_rejects_truncated_jpeg(tmp_path):
    complete = write_test_image(tmp_path / "complete.jpg", 400, 300, "jpeg")
    truncated = tmp_path / "truncated.jpg"
    data = complete.read_bytes()
    truncated.write_bytes(data[: len(data) // 3])

    validation = validate_image_preview(truncated)

    assert validation.valid is False
    assert "decoder could not read" in validation.message
    assert validation.size_bytes == truncated.stat().st_size


def test_validate_rejects_png_renamed_as_jpg(tmp_path):
    path = write_test_image(tmp_path / "actually.png", 40, 30, "png")
    renamed = tmp_path / "renamed.jpg"
    path.rename(renamed)

    validation = validate_image_preview(renamed)

    assert validation.valid is False
    assert validation.message == "The preview is not a JPEG file (detected format: png)."


def test_validate_rejects_missing_file_and_directory(tmp_path):
    missing = validate_image_preview(tmp_path / "missing.jpg")
    assert missing.valid is False and "could not be read" in missing.message

    directory = tmp_path / "dir.jpg"
    directory.mkdir()
    assert validate_image_preview(directory).valid is False


def test_validate_never_raises_for_odd_paths(tmp_path):
    for odd in (tmp_path / "with space.jpg", tmp_path / "ünïcödé.jpg", tmp_path / ("x" * 200 + ".jpg")):
        validation = validate_image_preview(odd)
        assert validation.valid is False
        assert validation.message


# ---------------------------------------------------------------------------
# Failures (spec §12, §30)
# ---------------------------------------------------------------------------


def test_corrupt_source_fails_at_image_decode_without_temporaries(tmp_path, cache, generator):
    source = tmp_path / "src" / "broken.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xff\xd8\xff" + b"\x00" * 500)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert info.value.message == "Image decoder could not read the file."
    assert info.value.detail
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_unknown_format_fails_visibly_at_image_decode(tmp_path, cache, generator):
    source = tmp_path / "src" / "mystery.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not an image at all" * 20)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert "identify" in info.value.detail
    assert files_under(cache.root) == []


def test_missing_source_fails_at_image_decode(tmp_path, cache, generator):
    source = tmp_path / "src" / "missing.png"
    destination = cache.preview_path("image", b"\x11" * 32)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert info.value.message == "The source image could not be read."
    assert not destination.exists()
    assert files_under(cache.root) == []


def test_failed_regeneration_preserves_the_existing_valid_preview(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "keep.png", 200, 150, "png")
    destination = destination_for(cache, source)
    generator.generate(source, destination)
    good_bytes = destination.read_bytes()
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 300)  # now corrupt

    with pytest.raises(PreviewError):
        generator.generate(source, destination)

    assert destination.read_bytes() == good_bytes
    assert temporary_files(cache.root) == []


# ---------------------------------------------------------------------------
# Cancellation (spec §13)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Source changes and write failures
# ---------------------------------------------------------------------------


def test_source_modified_during_generation_is_reported(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "changing.png", 200, 150, "png")
    destination = destination_for(cache, source)
    original_encode = image_preview.encode_jpeg

    def mutating_encode(image, temp_path, jpeg_quality, **kwargs):
        with open(source, "ab") as handle:
            handle.write(b"\x00" * 64)  # size changes even if mtime granularity hides the edit
        original_encode(image, temp_path, jpeg_quality, **kwargs)

    monkeypatch.setattr(image_preview, "encode_jpeg", mutating_encode)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert info.value.message == "The source file changed while its preview was being created."
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_source_removed_during_generation_is_reported(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "vanishing.png", 200, 150, "png")
    destination = destination_for(cache, source)
    original_encode = image_preview.encode_jpeg

    def deleting_encode(image, temp_path, jpeg_quality, **kwargs):
        original_encode(image, temp_path, jpeg_quality, **kwargs)
        source.unlink()

    monkeypatch.setattr(image_preview, "encode_jpeg", deleting_encode)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert "disappeared" in info.value.detail
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_encode_failure_mentioning_space_is_classified_as_disk_full(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "full-disk.png", 200, 150, "png")
    destination = destination_for(cache, source)
    partial: list[pathlib.Path] = []

    def full_disk_save(self, fp, format=None, **params):
        pathlib.Path(fp).write_bytes(b"\xff\xd8partial")  # half-written file
        partial.append(pathlib.Path(fp))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Image.Image, "save", full_disk_save)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_DISK_FULL
    assert info.value.message == "Could not write preview."
    assert "No space left" in info.value.detail
    assert partial and not partial[0].exists()
    assert not destination.exists()
    assert temporary_files(cache.root) == []


def test_other_encode_failure_is_classified_as_image_encode(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "encode-fail.png", 200, 150, "png")
    destination = destination_for(cache, source)

    def failing_save(self, fp, format=None, **params):
        raise OSError("encoder error -2 when writing image file")

    monkeypatch.setattr(Image.Image, "save", failing_save)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination)

    assert info.value.stage == STAGE_IMAGE_ENCODE
    assert info.value.message == "Could not write preview."
    assert "encoder error" in info.value.detail
    assert files_under(cache.root) == []


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
    source = write_test_image(tmp_path / "src" / "unexpected.png", 200, 150, "png")
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

    def failing_resize(self, *args, **kwargs):
        raise ValueError("simulated resampling failure")

    monkeypatch.setattr(Image.Image, "resize", failing_resize)

    with pytest.raises(PreviewError) as info:
        ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_TRANSFORM
    assert info.value.message == "The image could not be prepared for encoding."
    assert "simulated resampling failure" in info.value.detail
    assert files_under(cache.root) == []


def test_invalid_image_profile_is_reported_as_configuration_error(tmp_path, cache, monkeypatch):
    bad_profile = ImagePreviewProfile(max_dimension=10)
    monkeypatch.setattr(cache, "image_profile", bad_profile, raising=False)

    with pytest.raises(PreviewError) as info:
        ImagePreviewGenerator(cache)

    assert info.value.stage == STAGE_CONFIGURATION
    assert "Image maximum dimension" in info.value.detail


# ---------------------------------------------------------------------------
# Camera RAW via rawpy / LibRaw (spec §39)
# ---------------------------------------------------------------------------


def test_raw_with_a_full_size_embedded_jpeg_uses_the_embedded_preview(tmp_path, cache, generator):
    source = write_dng(tmp_path / "src" / "shot.dng", 96, 64, embedded_jpeg=jpeg_bytes(96, 64, (200, 40, 40)))

    result = generator.generate(source, destination_for(cache, source))

    assert result.status == PREVIEW_GENERATED
    assert result.detail == SOURCE_RAW_EMBEDDED
    assert (result.width, result.height) == (96, 64)
    preview = read_jpeg(result.path)
    assert colour_distance(pixel(preview, 48, 32), (200, 40, 40)) <= 12
    assert validate_image_preview(result.path).valid


def test_raw_embedded_preview_only_needs_to_cover_the_configured_maximum(tmp_path):
    cache = make_cache(tmp_path, max_dimension=320)
    source = write_dng(tmp_path / "src" / "shot.dng", 1600, 1200, embedded_jpeg=jpeg_bytes(800, 600, (40, 200, 40)))

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert result.detail == SOURCE_RAW_EMBEDDED
    assert (result.width, result.height) == (320, 240)
    assert colour_distance(pixel(read_jpeg(result.path), 160, 120), (40, 200, 40)) <= 12


def test_raw_with_a_small_embedded_jpeg_falls_back_to_demosaicing(tmp_path, cache, generator):
    source = write_dng(tmp_path / "src" / "shot.dng", 96, 64, embedded_jpeg=jpeg_bytes(48, 32, (200, 40, 40)))

    result = generator.generate(source, destination_for(cache, source))

    assert result.detail == SOURCE_RAW_DEMOSAIC
    assert (result.width, result.height) == (96, 64)
    preview = read_jpeg(result.path)
    assert colour_distance(pixel(preview, 48, 32), (200, 40, 40)) > 40, "the thumbnail colour must not appear"


def test_raw_without_an_embedded_preview_is_demosaiced(tmp_path, cache, generator):
    source = write_dng(tmp_path / "src" / "plain.dng", 96, 64)

    result = generator.generate(source, destination_for(cache, source))

    assert result.status == PREVIEW_GENERATED
    assert result.detail == SOURCE_RAW_DEMOSAIC
    assert (result.width, result.height) == (96, 64)
    preview = read_jpeg(result.path)
    left, right = pixel(preview, 4, 32), pixel(preview, 91, 32)
    assert right[0] > left[0] + 60, "the sensor gradient (red rising left to right) must survive"


def test_raw_demosaic_uses_half_size_when_it_still_covers_the_maximum(tmp_path):
    cache = make_cache(tmp_path, max_dimension=320)
    source = write_dng(tmp_path / "src" / "plain.dng", 1600, 1200)

    decoded = open_image(source, max_dimension=320)
    assert decoded.source == SOURCE_RAW_DEMOSAIC
    assert decoded.image.size == (800, 600), "LibRaw half-size output is enough for a 320 px preview"
    decoded.image.close()

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (320, 240)


def test_raw_demosaic_stays_full_size_when_half_size_would_be_too_small(tmp_path):
    source = write_dng(tmp_path / "src" / "plain.dng", 400, 300)

    decoded = open_image(source, max_dimension=320)

    assert decoded.image.size == (400, 300)
    decoded.image.close()


def test_raw_orientation_from_libraw_rotates_the_demosaiced_image(tmp_path, cache, generator):
    source = write_dng(tmp_path / "src" / "portrait.dng", 96, 64, orientation=6)

    result = generator.generate(source, destination_for(cache, source))

    assert result.detail == SOURCE_RAW_DEMOSAIC
    assert (result.width, result.height) == (64, 96)


def test_raw_orientation_from_libraw_rotates_an_embedded_preview_without_exif(tmp_path, cache, generator):
    source = write_dng(
        tmp_path / "src" / "portrait.dng", 96, 64, orientation=6, embedded_jpeg=jpeg_bytes(96, 64, (200, 40, 40))
    )

    result = generator.generate(source, destination_for(cache, source))

    assert result.detail == SOURCE_RAW_EMBEDDED
    assert (result.width, result.height) == (64, 96)


def test_embedded_preview_with_its_own_exif_orientation_is_rotated_exactly_once(tmp_path, cache, generator):
    embedded = jpeg_bytes(96, 64, (200, 40, 40), orientation=6)
    source = write_dng(tmp_path / "src" / "portrait.dng", 96, 64, orientation=6, embedded_jpeg=embedded)

    result = generator.generate(source, destination_for(cache, source))

    assert result.detail == SOURCE_RAW_EMBEDDED
    assert (result.width, result.height) == (64, 96)
    with Image.open(result.path) as preview:
        assert preview.getexif().get(0x0112) is None


def test_raw_dimensions_are_reported_after_orientation(tmp_path):
    landscape = write_dng(tmp_path / "landscape.dng", 96, 64)
    portrait = write_dng(tmp_path / "portrait.dng", 96, 64, orientation=6)

    assert read_image_dimensions(landscape) == (96, 64, "dng")
    assert read_image_dimensions(portrait) == (64, 96, "dng")


def test_truncated_raw_fails_at_image_decode_without_temporaries(tmp_path, cache, generator):
    source = tmp_path / "src" / "truncated.dng"
    source.parent.mkdir(parents=True)
    source.write_bytes(build_dng(96, 64)[:200])

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert "RAW" in info.value.detail
    assert files_under(cache.root) == []


def test_garbage_with_a_raw_extension_fails_at_image_decode(tmp_path, cache, generator):
    source = tmp_path / "src" / "garbage.cr2"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not a canon raw file" * 30)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert files_under(cache.root) == []
    with pytest.raises(ImageOpenError):
        read_image_dimensions(source)


def test_raw_files_fail_visibly_when_rawpy_is_missing(tmp_path, cache, generator, monkeypatch):
    source = write_dng(tmp_path / "src" / "shot.dng", 96, 64)
    monkeypatch.setattr(image_preview, "rawpy", None)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert "rawpy" in info.value.detail
    with pytest.raises(ImageOpenError) as open_info:
        read_image_dimensions(source)
    assert open_info.value.unsupported is True
    assert files_under(cache.root) == []


def test_raw_pixel_budget_is_enforced_before_demosaicing(tmp_path, cache, generator, monkeypatch):
    source = write_dng(tmp_path / "src" / "shot.dng", 96, 64)
    monkeypatch.setattr(image_preview, "MAX_SOURCE_PIXELS", 1000)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert "megapixel" in info.value.detail


# ---------------------------------------------------------------------------
# HEIC / HEIF via pillow-heif (spec §2C, §39)
# ---------------------------------------------------------------------------


def test_heic_source_produces_a_valid_preview(tmp_path, cache, generator):
    source = write_heic(tmp_path / "src" / "phone.heic", 120, 80, (30, 120, 200))

    result = generator.generate(source, destination_for(cache, source))

    assert result.status == PREVIEW_GENERATED
    assert result.detail == SOURCE_PILLOW
    assert (result.width, result.height) == (120, 80)
    assert colour_distance(pixel(read_jpeg(result.path), 60, 40), (30, 120, 200)) <= 16
    assert read_image_dimensions(source) == (120, 80, "heif")


def test_large_heic_is_downscaled(tmp_path):
    cache = make_cache(tmp_path, max_dimension=320)
    source = write_heic(tmp_path / "src" / "phone.heif", 800, 400)

    result = ImagePreviewGenerator(cache).generate(source, destination_for(cache, source))

    assert (result.width, result.height) == (320, 160)


def test_corrupt_heic_fails_at_image_decode(tmp_path, cache, generator):
    good = write_heic(tmp_path / "src" / "good.heic", 120, 80)
    source = tmp_path / "src" / "bad.heic"
    data = good.read_bytes()
    source.write_bytes(data[: len(data) // 2])

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source))

    assert info.value.stage == STAGE_IMAGE_DECODE
    assert files_under(cache.root) == []


# ---------------------------------------------------------------------------
# read_image_dimensions (used by media metadata)
# ---------------------------------------------------------------------------


def test_read_image_dimensions_applies_exif_orientation(tmp_path):
    plain = write_test_image(tmp_path / "plain.png", 13, 7, "png")
    rotated = write_jpeg_with_exif_orientation(tmp_path / "rotated.jpg", 200, 100, 6)
    flipped = write_jpeg_with_exif_orientation(tmp_path / "flipped.jpg", 200, 100, 2)

    assert read_image_dimensions(plain) == (13, 7, "png")
    assert read_image_dimensions(rotated) == (100, 200, "jpeg")
    assert read_image_dimensions(flipped) == (200, 100, "jpeg")


def test_read_image_dimensions_reports_unsupported_and_corrupt_files(tmp_path):
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"nope" * 50)
    with pytest.raises(ImageOpenError) as info:
        read_image_dimensions(garbage)
    assert info.value.unsupported is True

    absurd = write_png_claiming_size(tmp_path / "absurd.png", 60000, 60000)
    with pytest.raises(ImageOpenError) as too_large:
        read_image_dimensions(absurd)
    assert too_large.value.unsupported is False
    assert "megapixel" in str(too_large.value)


# ---------------------------------------------------------------------------
# Backend self-test (spec §2C)
# ---------------------------------------------------------------------------


def test_backend_self_test_encodes_validates_and_cleans_up(tmp_path):
    cache = make_cache(tmp_path, jpeg_quality=60)
    assert not cache.root.exists()

    message = run_image_backend_test(cache)

    width, height = IMAGE_TEST_SIZE
    assert isinstance(message, str)
    assert message.startswith(f"Encoded a {width}x{height} test image to JPEG quality 60 (")
    assert " KB) in " in message or " B) in " in message
    assert str(cache.root) in message
    assert message.endswith(IMAGE_BACKEND_NAME)
    assert cache.root.is_dir()
    assert files_under(cache.root) == []


def test_backend_self_test_uses_the_real_pipeline(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    seen: dict[str, object] = {}
    original_encode = image_preview.encode_jpeg

    def recording_encode(image, temp_path, jpeg_quality, **kwargs):
        seen["size"] = image.size
        seen["mode"] = image.mode
        seen["temp"] = pathlib.Path(temp_path)
        seen["quality"] = jpeg_quality
        original_encode(image, temp_path, jpeg_quality, **kwargs)
        seen["temp_exists"] = pathlib.Path(temp_path).is_file()

    monkeypatch.setattr(image_preview, "encode_jpeg", recording_encode)

    run_image_backend_test(cache)

    assert seen["size"] == IMAGE_TEST_SIZE
    assert seen["mode"] == "RGB", "the gradient's alpha must have been flattened"
    assert seen["quality"] == 82
    temp = seen["temp"]
    assert temp.parent == cache.root
    assert temp.name.startswith(".") and ".tmp-" in temp.name and "jvvv-image-test.jpg" in temp.name
    assert seen["temp_exists"] is True
    assert not temp.exists()
    assert files_under(cache.root) == []


def test_backend_self_test_failure_raises_and_leaves_no_files(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)

    def full_disk_save(self, fp, format=None, **params):
        pathlib.Path(fp).write_bytes(b"partial")
        raise OSError("There is not enough space on the disk.")

    monkeypatch.setattr(Image.Image, "save", full_disk_save)

    with pytest.raises(PreviewError) as info:
        run_image_backend_test(cache)

    assert info.value.stage == STAGE_DISK_FULL
    assert info.value.message == "Could not write preview."
    assert files_under(cache.root) == []


def test_backend_self_test_reports_unavailable_backend(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(image_preview, "image_backend_available", lambda: (False, "rawpy is not installed"))

    with pytest.raises(PreviewError) as info:
        run_image_backend_test(cache)

    assert info.value.message == "rawpy is not installed"
    assert files_under(cache.root) == []


def test_backend_self_test_rejects_invalid_profile(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(cache, "image_profile", ImagePreviewProfile(jpeg_quality=5), raising=False)

    with pytest.raises(PreviewError) as info:
        run_image_backend_test(cache)

    assert info.value.stage == STAGE_CONFIGURATION
    assert files_under(cache.root) == []


def test_gradient_test_image_has_alpha_and_the_requested_size():
    image = image_preview.make_gradient_test_image(*IMAGE_TEST_SIZE)

    assert image.mode == "RGBA"
    assert image.size == IMAGE_TEST_SIZE
    alpha = np.asarray(image)[:, :, 3]
    assert alpha.min() < 32 and alpha.max() == 255


def test_prepare_preview_image_flattens_without_touching_opaque_pixels():
    buffer = io.BytesIO()
    make_test_image(40, 30, alpha=True).save(buffer, "PNG")
    with Image.open(io.BytesIO(buffer.getvalue())) as image:
        prepared = image_preview.prepare_preview_image(image, 1600)

    assert prepared.mode == "RGB" and prepared.size == (40, 30)
    assert pixel(prepared, 5, 5) == (0x33, 0x66, 0xCC)
    assert pixel(prepared, 35, 5) == GREY


# ---------------------------------------------------------------------------
# Audit follow-ups: tRNS transparency, hostile preview headers, hash-time snapshot
# ---------------------------------------------------------------------------


def test_rgb_png_with_single_colour_transparency_is_flattened(tmp_path, cache, generator):
    source = tmp_path / "src" / "trns.png"
    source.parent.mkdir(parents=True)
    image = Image.new("RGB", (60, 40), (255, 255, 255))
    for x in range(30):
        for y in range(40):
            image.putpixel((x, y), (0x33, 0x66, 0xCC))
    image.save(source, "PNG", transparency=(255, 255, 255))  # white is the transparent key colour
    with Image.open(source) as check:
        assert check.mode == "RGB" and "transparency" in check.info

    result = generator.generate(source, destination_for(cache, source))

    preview = read_jpeg(result.path)
    assert colour_distance(pixel(preview, 10, 20), (0x33, 0x66, 0xCC)) <= 8
    assert colour_distance(pixel(preview, 50, 20), GREY) <= 6, "the keyed-transparent area must become grey"


def test_validate_rejects_a_jpeg_header_claiming_absurd_dimensions(tmp_path):
    path = write_test_image(tmp_path / "huge.jpg", 40, 30, "jpeg")
    data = bytearray(path.read_bytes())
    sof = data.find(b"\xff\xc0")
    assert sof > 0
    struct.pack_into(">HH", data, sof + 5, 60000, 60000)  # SOF0: precision byte, then height, width
    path.write_bytes(bytes(data))

    validation = validate_image_preview(path)

    assert validation.valid is False
    assert "60000x60000" in validation.message and "megapixel" in validation.message


def test_generate_refuses_a_source_that_changed_since_it_was_hashed(tmp_path, cache, generator, monkeypatch):
    source = write_test_image(tmp_path / "src" / "hashed.png", 200, 150, "png")
    hashed_stat = os.lstat(source)
    with open(source, "ab") as handle:
        handle.write(b"\x00" * 32)  # the file changed after its SHA-256 was taken
    later = time.time() + 5
    os.utime(source, (later, later))
    decoded = {"count": 0}
    original_decode = ImagePreviewGenerator._decode

    def counting_decode(self, path):
        decoded["count"] += 1
        return original_decode(self, path)

    monkeypatch.setattr(ImagePreviewGenerator, "_decode", counting_decode)

    with pytest.raises(PreviewError) as info:
        generator.generate(source, destination_for(cache, source), source_stat=hashed_stat)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert info.value.message == "The source file changed between hashing and preview generation."
    assert decoded["count"] == 0, "nothing may be decoded from a source that no longer matches its hash"
    assert files_under(cache.root) == []


def test_generate_accepts_a_matching_hash_time_snapshot(tmp_path, cache, generator):
    source = write_test_image(tmp_path / "src" / "stable.png", 200, 150, "png")

    result = generator.generate(source, destination_for(cache, source), source_stat=os.lstat(source))

    assert result.status == PREVIEW_GENERATED
    assert (result.width, result.height) == (200, 150)


def test_icc_profile_is_dropped_when_the_colour_model_changes(tmp_path, cache, generator):
    fake_icc = b"CMYK_PROFILE_BYTES" * 8
    cmyk = tmp_path / "src" / "print.jpg"
    cmyk.parent.mkdir(parents=True)
    Image.new("CMYK", (40, 30), (0, 255, 255, 0)).save(cmyk, "JPEG", icc_profile=fake_icc)
    grey = tmp_path / "src" / "scan.tif"
    Image.new("L", (40, 30), 90).save(grey, "TIFF", icc_profile=b"GRAY_PROFILE" * 8)

    for source in (cmyk, grey):
        result = generator.generate(source, destination_for(cache, source))
        with Image.open(result.path) as preview:
            assert preview.mode == "RGB"
            assert "icc_profile" not in preview.info, f"{source.name}: a non-RGB profile must not be copied"
