"""Shared fixtures and helpers for offline-preview tests.

Image fixtures are generated with Pillow (plus pillow-heif for HEIC and a
small hand-built DNG writer for camera RAW) so every test runs from a fresh,
deterministic file.

The MP4 fixtures under ``tests/fixtures`` were produced once with a real FFmpeg
(libx264, yuv420p, ``+faststart``) so the pure-Python MP4 validator and the
fake-FFmpeg tests can run on machines without FFmpeg installed.

* ``tiny_1fps_3s.mp4`` – 64x48, 1 fps, 3.000 s, H.264 High, faststart.
* ``source_320x180_10fps_2s.mp4`` – 320x180, 10 fps, 2 s, MPEG-4 part 2. A
  plausible *source* video for real-FFmpeg proxy tests.
* ``source_portrait_180x320.mp4`` – 180x320, 10 fps, 2 s, H.264. Portrait
  source for aspect-ratio tests.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
TINY_MP4 = FIXTURES_DIR / "tiny_1fps_3s.mp4"
SOURCE_MP4 = FIXTURES_DIR / "source_320x180_10fps_2s.mp4"
PORTRAIT_SOURCE_MP4 = FIXTURES_DIR / "source_portrait_180x320.mp4"


def tiny_mp4_bytes() -> bytes:
    return TINY_MP4.read_bytes()


def source_mp4_bytes() -> bytes:
    return SOURCE_MP4.read_bytes()


def portrait_source_mp4_bytes() -> bytes:
    return PORTRAIT_SOURCE_MP4.read_bytes()


def write_tiny_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tiny_mp4_bytes())
    return path


def write_source_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source_mp4_bytes())
    return path


def write_portrait_source_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(portrait_source_mp4_bytes())
    return path


def real_ffmpeg_path() -> str | None:
    """Return a real FFmpeg for integration tests, or ``None`` to skip them.

    Set ``JVVV_TEST_FFMPEG`` to an explicit executable when FFmpeg is not on
    ``PATH`` (for example a vendor-bundled ``ffmpeg6.exe``).
    """

    explicit = os.environ.get("JVVV_TEST_FFMPEG", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# Standard images (Pillow)
# ---------------------------------------------------------------------------

_PILLOW_FORMATS = {
    "bmp": "BMP",
    "gif": "GIF",
    "ico": "ICO",
    "jpeg": "JPEG",
    "jpg": "JPEG",
    "png": "PNG",
    "tif": "TIFF",
    "tiff": "TIFF",
    "webp": "WEBP",
}


def make_test_image(
    width: int,
    height: int,
    *,
    alpha: bool = False,
    color: str = "#3366cc",
) -> Image.Image:
    """Return a two-tone test image; ``alpha=True`` leaves half transparent.

    The top-left quarter is ``color``, the bottom-right quarter is ``#cc3333``,
    and the rest is white (or fully transparent when ``alpha`` is set).
    """

    image = Image.new("RGBA" if alpha else "RGB", (width, height), (0, 0, 0, 0) if alpha else "#ffffff")
    draw = ImageDraw.Draw(image)
    half_w, half_h = max(1, width // 2), max(1, height // 2)
    draw.rectangle([0, 0, half_w - 1, half_h - 1], fill=color)
    if half_w < width and half_h < height:
        draw.rectangle([half_w, half_h, width - 1, height - 1], fill="#cc3333")
    return image


def write_test_image(
    path: Path,
    width: int,
    height: int,
    fmt: str,
    **kwargs: object,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = make_test_image(width, height, **kwargs)  # type: ignore[arg-type]
    pil_format = _PILLOW_FORMATS[fmt.lower()]
    options: dict[str, object] = {}
    if pil_format == "JPEG":
        image = image.convert("RGB")
        options["quality"] = 95
    elif pil_format == "WEBP":
        options["lossless"] = True
    image.save(path, pil_format, **options)
    return path


def jpeg_bytes(
    width: int,
    height: int,
    color: tuple[int, int, int] = (200, 40, 40),
    *,
    quality: int = 90,
    orientation: int | None = None,
) -> bytes:
    """A solid-colour JPEG, optionally carrying an EXIF Orientation tag."""

    buffer = io.BytesIO()
    options: dict[str, object] = {"quality": quality}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        options["exif"] = exif.tobytes()
    Image.new("RGB", (width, height), color).save(buffer, "JPEG", **options)
    return buffer.getvalue()


def write_jpeg_with_exif_orientation(
    path: Path,
    width: int,
    height: int,
    orientation: int,
) -> Path:
    """Write the two-tone test image as a JPEG with an EXIF Orientation tag."""

    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[0x0112] = orientation
    make_test_image(width, height).save(path, "JPEG", quality=95, exif=exif.tobytes())
    return path


def write_heic(
    path: Path,
    width: int,
    height: int,
    color: tuple[int, int, int] = (30, 120, 200),
) -> Path:
    """Encode a solid-colour HEIC file with pillow-heif (libheif + x265)."""

    import pillow_heif

    pillow_heif.register_heif_opener()
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color).save(path, format="HEIF", quality=80)
    return path


# ---------------------------------------------------------------------------
# Camera RAW: a minimal but valid DNG (TIFF/EP) that LibRaw opens
# ---------------------------------------------------------------------------

_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _SRATIONAL = 1, 2, 3, 4, 5, 10


def _ifd_entry(tag: int, type_: int, count: int, value: bytes) -> bytes:
    return struct.pack("<HHI", tag, type_, count) + value.ljust(4, b"\0")[:4]


def build_dng(
    width: int,
    height: int,
    *,
    orientation: int = 1,
    embedded_jpeg: bytes | None = None,
    pattern: str = "RGGB",
) -> bytes:
    """Return a DNG with a 16-bit CFA main image and an optional JPEG preview SubIFD.

    The sensor data is a red/green gradient over a constant blue level, so a
    demosaiced result is visibly non-uniform.  ``orientation`` is the TIFF
    Orientation tag that LibRaw reports through ``sizes.flip``.
    """

    yy, xx = np.mgrid[0:height, 0:width]
    red = (xx * 4000 // max(1, width - 1)).astype(np.uint16)
    green = (yy * 4000 // max(1, height - 1)).astype(np.uint16)
    blue = np.full((height, width), 1500, np.uint16)
    cfa = np.zeros((height, width), np.uint16)
    cfa[0::2, 0::2] = red[0::2, 0::2]
    cfa[0::2, 1::2] = green[0::2, 1::2]
    cfa[1::2, 0::2] = green[1::2, 0::2]
    cfa[1::2, 1::2] = blue[1::2, 1::2]
    cfa += 64  # black level
    raw_bytes = cfa.astype("<u2").tobytes()

    entries: list[tuple[int, int, int, bytes]] = []

    def add(tag: int, type_: int, values) -> None:
        if type_ == _ASCII:
            data = values.encode("ascii") + b"\0"
            count = len(data)
        elif type_ == _BYTE:
            data = bytes(values)
            count = len(data)
        elif type_ == _SHORT:
            data = b"".join(struct.pack("<H", v) for v in values)
            count = len(values)
        elif type_ == _LONG:
            data = b"".join(struct.pack("<I", v) for v in values)
            count = len(values)
        elif type_ == _RATIONAL:
            data = b"".join(struct.pack("<II", *v) for v in values)
            count = len(values)
        elif type_ == _SRATIONAL:
            data = b"".join(struct.pack("<ii", *v) for v in values)
            count = len(values)
        else:  # pragma: no cover - programming error
            raise ValueError(type_)
        entries.append((tag, type_, count, data))

    add(254, _LONG, [0])  # NewSubFileType: main image
    add(256, _LONG, [width])
    add(257, _LONG, [height])
    add(258, _SHORT, [16])  # BitsPerSample
    add(259, _SHORT, [1])  # Compression: none
    add(262, _SHORT, [32803])  # PhotometricInterpretation: CFA
    add(271, _ASCII, "JVVV")  # Make
    add(272, _ASCII, "Synthetic DNG")  # Model
    add(273, _LONG, [0])  # StripOffsets (patched below)
    add(274, _SHORT, [orientation])
    add(277, _SHORT, [1])  # SamplesPerPixel
    add(278, _LONG, [height])  # RowsPerStrip
    add(279, _LONG, [len(raw_bytes)])  # StripByteCounts
    add(284, _SHORT, [1])  # PlanarConfiguration
    add(33421, _SHORT, [2, 2])  # CFARepeatPatternDim
    add(33422, _BYTE, [{"R": 0, "G": 1, "B": 2}[c] for c in pattern])  # CFAPattern
    add(50706, _BYTE, [1, 4, 0, 0])  # DNGVersion
    add(50707, _BYTE, [1, 1, 0, 0])  # DNGBackwardVersion
    add(50708, _ASCII, "JVVV Synthetic DNG")  # UniqueCameraModel
    add(50714, _SHORT, [64])  # BlackLevel
    add(50717, _SHORT, [4095])  # WhiteLevel
    add(50721, _SRATIONAL, [(1, 1), (0, 1), (0, 1), (0, 1), (1, 1), (0, 1), (0, 1), (0, 1), (1, 1)])
    add(50728, _RATIONAL, [(1, 1), (1, 1), (1, 1)])  # AsShotNeutral
    add(50778, _SHORT, [21])  # CalibrationIlluminant1: D65
    if embedded_jpeg is not None:
        add(330, _LONG, [0])  # SubIFDs (patched below)

    entries.sort(key=lambda entry: entry[0])
    ifd0_size = 2 + 12 * len(entries) + 4
    cursor = 8 + ifd0_size
    packed: list[tuple[int, int, int, bytes | None, int | None]] = []
    blobs: list[bytes] = []
    for tag, type_, count, data in entries:
        if len(data) <= 4:
            packed.append((tag, type_, count, data, None))
        else:
            packed.append((tag, type_, count, None, cursor))
            blobs.append(data)
            cursor += len(data) + (len(data) % 2)
    strip_offset = cursor
    cursor += len(raw_bytes)
    subifd_offset = cursor if embedded_jpeg is not None else None

    out = bytearray(b"II*\0" + struct.pack("<I", 8))
    out += struct.pack("<H", len(packed))
    for tag, type_, count, data, offset in packed:
        if tag == 273:
            out += _ifd_entry(tag, type_, count, struct.pack("<I", strip_offset))
        elif tag == 330:
            out += _ifd_entry(tag, type_, count, struct.pack("<I", subifd_offset or 0))
        elif data is not None:
            out += _ifd_entry(tag, type_, count, data)
        else:
            out += _ifd_entry(tag, type_, count, struct.pack("<I", offset or 0))
    out += struct.pack("<I", 0)  # no next IFD
    for blob in blobs:
        out += blob
        if len(blob) % 2:
            out += b"\0"
    assert len(out) == strip_offset, (len(out), strip_offset)
    out += raw_bytes

    if embedded_jpeg is not None:
        assert len(out) == subifd_offset
        with Image.open(io.BytesIO(embedded_jpeg)) as jpeg_image:
            jpeg_width, jpeg_height = jpeg_image.size
        sub_entries: list[tuple[int, int, int, bytes | None]] = [
            (254, _LONG, 1, struct.pack("<I", 1)),  # NewSubFileType: preview
            (256, _LONG, 1, struct.pack("<I", jpeg_width)),
            (257, _LONG, 1, struct.pack("<I", jpeg_height)),
            (258, _SHORT, 3, None),  # BitsPerSample 8,8,8 (offset)
            (259, _SHORT, 1, struct.pack("<H", 7)),  # Compression: JPEG
            (262, _SHORT, 1, struct.pack("<H", 6)),  # YCbCr
            (273, _LONG, 1, None),  # StripOffsets -> JPEG bytes
            (277, _SHORT, 1, struct.pack("<H", 3)),
            (278, _LONG, 1, struct.pack("<I", jpeg_height)),
            (279, _LONG, 1, struct.pack("<I", len(embedded_jpeg))),
            (284, _SHORT, 1, struct.pack("<H", 1)),
        ]
        sub_size = 2 + 12 * len(sub_entries) + 4
        bps_offset = subifd_offset + sub_size
        jpeg_offset = bps_offset + 6
        out += struct.pack("<H", len(sub_entries))
        for tag, type_, count, value in sub_entries:
            if tag == 258:
                out += _ifd_entry(tag, type_, count, struct.pack("<I", bps_offset))
            elif tag == 273:
                out += _ifd_entry(tag, type_, count, struct.pack("<I", jpeg_offset))
            else:
                out += _ifd_entry(tag, type_, count, value or b"")
        out += struct.pack("<I", 0)
        out += struct.pack("<HHH", 8, 8, 8)
        out += embedded_jpeg
    return bytes(out)


def write_dng(path: Path, width: int, height: int, **kwargs: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_dng(width, height, **kwargs))  # type: ignore[arg-type]
    return path
