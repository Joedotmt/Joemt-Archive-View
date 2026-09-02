"""Shared fixtures and helpers for offline-preview tests.

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

import os
import shutil
import struct
from pathlib import Path

from PySide6.QtGui import QColor, QImage, QImageWriter, QPainter


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


def make_test_image(
    width: int,
    height: int,
    *,
    alpha: bool = False,
    color: str = "#3366cc",
) -> QImage:
    """Return a two-tone test image; ``alpha=True`` leaves half transparent."""

    image_format = (
        QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32
    )
    image = QImage(width, height, image_format)
    image.fill(QColor(0, 0, 0, 0) if alpha else QColor("#ffffff"))
    painter = QPainter(image)
    painter.fillRect(0, 0, max(1, width // 2), max(1, height // 2), QColor(color))
    painter.fillRect(
        max(1, width // 2),
        max(1, height // 2),
        max(1, width - width // 2),
        max(1, height - height // 2),
        QColor("#cc3333"),
    )
    painter.end()
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
    writer = QImageWriter(str(path), fmt.encode("ascii"))
    if fmt.lower() in {"jpg", "jpeg"}:
        writer.setQuality(95)
    if not writer.write(image):
        raise RuntimeError(f"Could not write {fmt} fixture: {writer.errorString()}")
    return path


def exif_app1_segment(orientation: int) -> bytes:
    """Return a minimal JPEG APP1 (Exif) segment with one Orientation tag."""

    # Big-endian TIFF header followed by IFD0 holding a single SHORT entry.
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8)
    ifd = struct.pack(">H", 1)
    ifd += struct.pack(">HHI", 0x0112, 3, 1) + struct.pack(">H", orientation) + b"\x00\x00"
    ifd += struct.pack(">I", 0)  # no next IFD
    payload = b"Exif\x00\x00" + tiff + ifd
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def write_jpeg_with_exif_orientation(
    path: Path,
    width: int,
    height: int,
    orientation: int,
) -> Path:
    """Write a JPEG and inject an EXIF Orientation tag right after SOI."""

    write_test_image(path, width, height, "jpeg")
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8"
    path.write_bytes(data[:2] + exif_app1_segment(orientation) + data[2:])
    return path
