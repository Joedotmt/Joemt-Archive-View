"""Offline video previews: FFmpeg (libx264) discovery, encoding and validation.

This module owns everything FFmpeg-related for the offline preview system:

* discovering and probing an ``ffmpeg`` executable (version + encoder list);
* building the exact argument list for a proxy encode (H.264, ``yuv420p``,
  no audio, ``+faststart``);
* running that encode with structured progress, cancellation and a *stall*
  watchdog (never a fixed total timeout – large sources legitimately take a
  long time);
* a pure-Python MP4 box parser used to validate every preview before it is
  published or reused.  There is no ``ffprobe`` dependency anywhere.

No media is ever displayed or played from here; previews are opened by the
operating system's default application.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import stat as stat_module
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from jvvv.preview_cache import (
    ensure_source_snapshot,
    PREVIEW_GENERATED,
    STAGE_DISK_FULL,
    STAGE_FFMPEG_ENCODE,
    STAGE_FFMPEG_ENCODER,
    STAGE_FFMPEG_EXIT,
    STAGE_FFMPEG_START,
    STAGE_FFMPEG_TIMEOUT,
    STAGE_FFMPEG_VERSION,
    STAGE_RENAME,
    STAGE_SOURCE_CHANGED,
    STAGE_VIDEO_VALIDATE,
    PreviewCache,
    PreviewCancelled,
    PreviewError,
    PreviewResult,
    classify_os_error,
    os_error_detail,
)
from jvvv.preview_config import VideoPreviewProfile, format_fps
from jvvv.utils import format_size


FFMPEG_ENCODER = "libx264"
FFMPEG_PIXEL_FORMAT = "yuv420p"
DEFAULT_PROBE_TIMEOUT_SECONDS = 20.0
DEFAULT_TEST_ENCODE_TIMEOUT_SECONDS = 120.0
# Watchdog on FFmpeg's *progress output*, NOT a total encode timeout (spec §29).
DEFAULT_STALL_TIMEOUT_SECONDS = 600.0
VIDEO_TEST_SIZE = (64, 48)
VIDEO_TEST_FRAME_COUNT = 2
VIDEO_TEST_INPUT_FPS = 2

MAX_DETAIL_LENGTH = 500
STDERR_TAIL_BYTES = 64 * 1024
_PROCESS_STOP_WAIT_SECONDS = 2.0
_READER_JOIN_SECONDS = 5.0
# Largest leaf box body the MP4 parser will ever load into memory.
_MAX_LEAF_READ = 4096

CancelCallback = Callable[[], bool]
ProgressCallback = Callable[[float | None, int | None], None]

_ENCODER_LINE_RE = re.compile(r"^\s*[VAS][.FSXBD]{5}\s+([A-Za-z0-9_+.\-]+)\s")
_DISK_FULL_PHRASES = (
    "no space left",
    "not enough space",
    "disk full",
    "disk quota exceeded",
    "there is not enough space on the disk",
)
HANDLER_VIDEO = "vide"
HANDLER_AUDIO = "soun"
_CODEC_NAMES = {
    "avc1": "h264",
    "avc3": "h264",
    "hvc1": "hevc",
    "hev1": "hevc",
    "mp4v": "mp4v",
}


# --------------------------------------------------------------------------
# FFmpeg discovery and probing
# --------------------------------------------------------------------------
def find_ffmpeg(explicit_path: str | None = None) -> str | None:
    """Return the FFmpeg executable to use, or ``None`` when it cannot be found.

    An explicit path is honoured *only* if it exists; there is deliberately no
    fallback to ``PATH`` in that case so a stale configured path produces a
    visible error instead of silently using another binary.
    """

    text = (explicit_path or "").strip()
    if text:
        expanded = os.path.expanduser(text)
        return expanded if Path(expanded).is_file() else None
    return shutil.which("ffmpeg")


@dataclass(frozen=True)
class FfmpegCapabilities:
    path: str
    version: str
    encoders: frozenset[str]

    @property
    def has_libx264(self) -> bool:
        return FFMPEG_ENCODER in self.encoders


def _popen_options(**extra: object) -> dict[str, object]:
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    options.update(extra)
    return options


def _run_ffmpeg_query(path: str, arguments: list[str], timeout_seconds: float) -> str:
    """Run a short informational FFmpeg command and return its stdout text."""

    command = [path, "-hide_banner", *arguments]
    try:
        process = subprocess.Popen(command, **_popen_options())
    except (OSError, ValueError) as exc:
        raise PreviewError(
            STAGE_FFMPEG_START,
            "FFmpeg could not be started.",
            detail=os_error_detail(exc),
        ) from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        raise PreviewError(
            STAGE_FFMPEG_TIMEOUT,
            f"FFmpeg did not respond within {timeout_seconds:g} seconds.",
            detail=" ".join(arguments),
        ) from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _stop_process(process)
        raise PreviewError(
            STAGE_FFMPEG_EXIT,
            "FFmpeg communication failed.",
            detail=os_error_detail(exc),
        ) from exc
    finally:
        _close_pipes(process)
    returncode = process.returncode
    stderr_text = _decode_output(stderr)
    if returncode != 0:
        raise PreviewError(
            STAGE_FFMPEG_EXIT,
            f"FFmpeg exited with code {returncode}.",
            detail=_detail(stderr_text or " ".join(arguments)),
        )
    return _decode_output(stdout)


def parse_ffmpeg_version(output: str) -> str | None:
    """Return the ``ffmpeg version ...`` line from ``ffmpeg -version`` output."""

    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        return text if text.startswith("ffmpeg version") else None
    return None


def parse_ffmpeg_encoders(output: str) -> frozenset[str]:
    """Return encoder names from ``ffmpeg -encoders`` output."""

    names: set[str] = set()
    for line in output.splitlines():
        match = _ENCODER_LINE_RE.match(line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def probe_ffmpeg(
    path: str,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> FfmpegCapabilities:
    """Start FFmpeg, confirm it reports a version, and list its encoders.

    A missing libx264 is *not* an error here (``has_libx264`` is False);
    callers decide via :func:`require_libx264`.
    """

    version_output = _run_ffmpeg_query(path, ["-version"], timeout_seconds)
    version = parse_ffmpeg_version(version_output)
    if version is None:
        raise PreviewError(
            STAGE_FFMPEG_VERSION,
            "FFmpeg did not report a valid version.",
            detail=_detail(version_output),
        )
    encoders_output = _run_ffmpeg_query(path, ["-encoders"], timeout_seconds)
    return FfmpegCapabilities(
        path=path,
        version=version,
        encoders=parse_ffmpeg_encoders(encoders_output),
    )


def require_libx264(capabilities: FfmpegCapabilities) -> None:
    if not capabilities.has_libx264:
        raise PreviewError(
            STAGE_FFMPEG_ENCODER,
            f"FFmpeg was found at:\n{capabilities.path}\n\n"
            f"However, the H.264 {FFMPEG_ENCODER} encoder is not available.",
        )


# --------------------------------------------------------------------------
# Argument construction
# --------------------------------------------------------------------------
def video_filter_expression(profile: VideoPreviewProfile) -> str:
    """FFmpeg ``-vf`` chain: configured frame rate, then a non-upscaling resize.

    ``scale=w=-2:h=2*trunc(min(ih\\,MAX)/2)`` keeps the aspect ratio, produces
    even dimensions (required by ``yuv420p``) and never enlarges a source that
    is already smaller than the configured maximum height.  The backslash
    escapes the comma for FFmpeg's filter parser; the value is passed as a
    single argv element so no shell escaping is involved.
    """

    profile.validate()
    return (
        f"fps={format_fps(profile.fps)},"
        f"scale=w=-2:h=2*trunc(min(ih\\,{int(profile.max_height)})/2)"
    )


def build_ffmpeg_arguments(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    profile: VideoPreviewProfile,
    *,
    progress: bool = True,
) -> list[str]:
    """Return the complete FFmpeg argv for one proxy encode.

    FFmpeg auto-rotates according to display-matrix metadata by default, so
    the preview keeps the source's intended orientation (spec §41).
    """

    arguments = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter_expression(profile),
        "-c:v",
        FFMPEG_ENCODER,
        "-preset",
        str(profile.preset),
        "-crf",
        str(int(profile.crf)),
        "-pix_fmt",
        FFMPEG_PIXEL_FORMAT,
        "-movflags",
        "+faststart",
    ]
    if progress:
        arguments.extend(["-progress", "pipe:1", "-nostats"])
    arguments.extend(["-f", "mp4", str(destination)])
    return arguments


# --------------------------------------------------------------------------
# Pure-Python MP4 parsing
# --------------------------------------------------------------------------
class Mp4ParseError(ValueError):
    """The file is not a parseable MP4 container."""


@dataclass(frozen=True)
class Mp4Box:
    type: str
    offset: int
    size: int
    header_size: int

    @property
    def body_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def body_size(self) -> int:
        return self.size - self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class Mp4Track:
    handler_type: str
    codec: str | None
    width: int | None
    height: int | None
    tkhd_width: int | None
    tkhd_height: int | None

    @property
    def is_video(self) -> bool:
        return self.handler_type == HANDLER_VIDEO

    @property
    def is_audio(self) -> bool:
        return self.handler_type == HANDLER_AUDIO


@dataclass(frozen=True)
class Mp4Structure:
    size_bytes: int
    boxes: tuple[Mp4Box, ...]
    timescale: int
    duration: int | None
    tracks: tuple[Mp4Track, ...]

    def offset_of(self, box_type: str) -> int | None:
        for box in self.boxes:
            if box.type == box_type:
                return box.offset
        return None

    @property
    def duration_ms(self) -> int | None:
        if self.duration is None or self.timescale <= 0:
            return None
        # Integer arithmetic: 64-bit safe and equal to round() except at exact halves.
        return (self.duration * 1000 + self.timescale // 2) // self.timescale

    @property
    def video_tracks(self) -> tuple[Mp4Track, ...]:
        return tuple(track for track in self.tracks if track.is_video)

    @property
    def audio_track_count(self) -> int:
        return sum(1 for track in self.tracks if track.is_audio)

    @property
    def faststart(self) -> bool | None:
        moov = self.offset_of("moov")
        mdat = self.offset_of("mdat")
        if moov is None or mdat is None:
            return None
        return moov < mdat


def _box_type_text(raw: bytes) -> str:
    return raw.decode("latin-1")


def _is_plausible_box_type(raw: bytes) -> bool:
    return len(raw) == 4 and all(0x20 <= byte <= 0x7E for byte in raw)


def _read_box_header(handle: BinaryIO, offset: int, limit: int) -> Mp4Box | None:
    """Read one box header at ``offset``; ``None`` when ``limit`` is reached.

    Raises ``Mp4ParseError`` for truncated or malformed headers.
    """

    if offset >= limit:
        return None
    if limit - offset < 8:
        raise Mp4ParseError(f"truncated box header at offset {offset}")
    handle.seek(offset)
    header = handle.read(8)
    if len(header) < 8:
        raise Mp4ParseError(f"truncated box header at offset {offset}")
    size, raw_type = struct.unpack(">I4s", header)
    header_size = 8
    if size == 1:
        large = handle.read(8)
        if len(large) < 8 or limit - offset < 16:
            raise Mp4ParseError(f"truncated 64-bit box header at offset {offset}")
        size = struct.unpack(">Q", large)[0]
        header_size = 16
    elif size == 0:
        size = limit - offset
    if not _is_plausible_box_type(raw_type):
        raise Mp4ParseError(f"malformed box header at offset {offset}")
    if size < header_size:
        raise Mp4ParseError(
            f"box {_box_type_text(raw_type)!r} at offset {offset} has an invalid size {size}"
        )
    if offset + size > limit:
        raise Mp4ParseError(
            f"box {_box_type_text(raw_type)!r} at offset {offset} is truncated"
        )
    return Mp4Box(_box_type_text(raw_type), offset, size, header_size)


def _iter_boxes(handle: BinaryIO, start: int, end: int) -> Iterator[Mp4Box]:
    offset = start
    while True:
        box = _read_box_header(handle, offset, end)
        if box is None:
            return
        yield box
        offset = box.end


def _read_leaf(handle: BinaryIO, box: Mp4Box, limit: int = _MAX_LEAF_READ) -> bytes:
    handle.seek(box.body_offset)
    return handle.read(min(box.body_size, limit))


def _find_child(handle: BinaryIO, parent: Mp4Box, box_type: str) -> Mp4Box | None:
    for child in _iter_boxes(handle, parent.body_offset, parent.end):
        if child.type == box_type:
            return child
    return None


def _parse_mvhd(handle: BinaryIO, box: Mp4Box) -> tuple[int, int | None]:
    body = _read_leaf(handle, box)
    if len(body) < 4:
        raise Mp4ParseError("the 'mvhd' box is truncated")
    version = body[0]
    if version == 0:
        if len(body) < 20:
            raise Mp4ParseError("the 'mvhd' box is truncated")
        timescale, duration = struct.unpack(">II", body[12:20])
        unknown = duration == 0xFFFFFFFF
    elif version == 1:
        if len(body) < 32:
            raise Mp4ParseError("the 'mvhd' box is truncated")
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
        unknown = duration == 0xFFFFFFFFFFFFFFFF
    else:
        raise Mp4ParseError(f"unsupported 'mvhd' version {version}")
    return timescale, (None if unknown else duration)


def _parse_tkhd(handle: BinaryIO, box: Mp4Box) -> tuple[int | None, int | None]:
    body = _read_leaf(handle, box)
    if not body:
        return None, None
    version = body[0]
    offset = 88 if version == 1 else 76
    if len(body) < offset + 8:
        return None, None
    fixed_width, fixed_height = struct.unpack(">II", body[offset : offset + 8])
    return _fixed_16_16(fixed_width), _fixed_16_16(fixed_height)


def _fixed_16_16(value: int) -> int | None:
    pixels = int(round(value / 65536))
    return pixels if pixels > 0 else None


def _parse_hdlr(handle: BinaryIO, box: Mp4Box) -> str:
    body = _read_leaf(handle, box, 12)
    if len(body) < 12:
        return ""
    return _box_type_text(body[8:12]).strip("\x00")


def _parse_stsd(handle: BinaryIO, box: Mp4Box) -> tuple[str | None, int | None, int | None]:
    body = _read_leaf(handle, box, 64)
    if len(body) < 16:
        return None, None, None
    entry_count = struct.unpack(">I", body[4:8])[0]
    if entry_count == 0:
        return None, None, None
    entry_size, raw_type = struct.unpack(">I4s", body[8:16])
    if not _is_plausible_box_type(raw_type) or entry_size < 16:
        return None, None, None
    codec = _box_type_text(raw_type)
    entry = body[8:]
    if entry_size < 36 or len(entry) < 36:
        return codec, None, None
    # Visual sample entry: 8 header + 6 reserved + 2 data_ref_index + 16
    # pre_defined/reserved, then uint16 width and height.
    width, height = struct.unpack(">HH", entry[32:36])
    return codec, (width or None), (height or None)


def _parse_trak(handle: BinaryIO, trak: Mp4Box) -> Mp4Track:
    handler = ""
    codec: str | None = None
    width = height = tkhd_width = tkhd_height = None
    for child in _iter_boxes(handle, trak.body_offset, trak.end):
        if child.type == "tkhd":
            tkhd_width, tkhd_height = _parse_tkhd(handle, child)
        elif child.type == "mdia":
            hdlr = _find_child(handle, child, "hdlr")
            if hdlr is not None:
                handler = _parse_hdlr(handle, hdlr)
            minf = _find_child(handle, child, "minf")
            stbl = _find_child(handle, minf, "stbl") if minf is not None else None
            stsd = _find_child(handle, stbl, "stsd") if stbl is not None else None
            if stsd is not None:
                codec, width, height = _parse_stsd(handle, stsd)
    return Mp4Track(
        handler_type=handler,
        codec=codec,
        width=width,
        height=height,
        tkhd_width=tkhd_width,
        tkhd_height=tkhd_height,
    )


def _has_media_data(boxes: list[Mp4Box]) -> bool:
    """True once a complete, non-empty ``mdat`` box has been read."""

    return any(box.type == "mdat" and box.size > box.header_size for box in boxes)


def _walk_top_level(handle: BinaryIO, size: int) -> list[Mp4Box]:
    """Return the top-level boxes.

    Garbage is tolerated only *after* both the movie header and a complete
    ``mdat`` have been read.  Before that, an unreadable or oversized box means
    the file is truncated - a fast-start preview cut anywhere inside ``mdat``
    still has a perfect ``moov``, and must never validate (spec §11, §46).
    """

    boxes: list[Mp4Box] = []
    offset = 0
    while offset < size:
        try:
            box = _read_box_header(handle, offset, size)
        except Mp4ParseError as exc:
            if any(found.type == "moov" for found in boxes) and _has_media_data(boxes):
                break  # trailing garbage after a complete movie
            if not boxes:
                raise Mp4ParseError(
                    f"the file is not an MP4 container ({exc})"
                ) from exc
            raise
        if box is None:
            break
        boxes.append(box)
        offset = box.end
    return boxes


def inspect_mp4(path: Path) -> Mp4Structure:
    """Parse the box structure of an MP4 file, reading only what is needed.

    Raises ``Mp4ParseError`` when the container cannot be understood and
    ``OSError`` when the file cannot be read.
    """

    size = os.stat(path).st_size
    with open(path, "rb") as handle:
        boxes = _walk_top_level(handle, size)
        if not any(box.type == "ftyp" for box in boxes):
            raise Mp4ParseError("the MP4 file has no 'ftyp' box")
        moov = next((box for box in boxes if box.type == "moov"), None)
        if moov is None:
            raise Mp4ParseError("the MP4 file has no 'moov' box")
        if not _has_media_data(boxes):
            raise Mp4ParseError("the MP4 file has no media data ('mdat') box, so it is truncated")
        timescale = 0
        duration: int | None = None
        found_mvhd = False
        tracks: list[Mp4Track] = []
        for child in _iter_boxes(handle, moov.body_offset, moov.end):
            if child.type == "mvhd":
                timescale, duration = _parse_mvhd(handle, child)
                found_mvhd = True
            elif child.type == "trak":
                tracks.append(_parse_trak(handle, child))
        if not found_mvhd:
            raise Mp4ParseError("the 'moov' box has no 'mvhd' header")
        if timescale <= 0:
            raise Mp4ParseError("the MP4 timescale is zero")
    return Mp4Structure(
        size_bytes=size,
        boxes=tuple(boxes),
        timescale=timescale,
        duration=duration,
        tracks=tuple(tracks),
    )


def mp4_box_offsets(path: Path) -> dict[str, int]:
    """Return ``{top-level box type: offset}`` (first occurrence of each type).

    Useful for fast-start checks: a fast-start MP4 has ``moov`` before ``mdat``.
    """

    size = os.stat(path).st_size
    offsets: dict[str, int] = {}
    with open(path, "rb") as handle:
        for box in _walk_top_level(handle, size):
            offsets.setdefault(box.type, box.offset)
    return offsets


@dataclass(frozen=True)
class VideoPreviewValidation:
    valid: bool
    duration_ms: int | None
    width: int | None
    height: int | None
    size_bytes: int
    video_codec: str | None
    message: str


def _invalid(message: str, size_bytes: int = 0) -> VideoPreviewValidation:
    return VideoPreviewValidation(
        valid=False,
        duration_ms=None,
        width=None,
        height=None,
        size_bytes=size_bytes,
        video_codec=None,
        message=message,
    )


def validate_video_preview(path: Path) -> VideoPreviewValidation:
    """Check that ``path`` is a real, parseable MP4 with a video track.

    Pure Python – no ffprobe.  Never raises; ``message`` explains the first
    failing check.  Handles both fast-start (``moov`` first) and ``moov``-at-
    end layouts and reads only the box headers it needs.
    """

    try:
        info = os.lstat(path)
    except OSError as exc:
        return _invalid(f"The preview file could not be read: {os_error_detail(exc)}")
    if stat_module.S_ISLNK(info.st_mode):
        return _invalid("The preview path is a symbolic link, not a regular file.")
    if not stat_module.S_ISREG(info.st_mode):
        return _invalid("The preview path is not a regular file.")
    size_bytes = int(info.st_size)
    if size_bytes <= 0:
        return _invalid("The preview file is empty.", size_bytes)

    try:
        structure = inspect_mp4(path)
    except Mp4ParseError as exc:
        text = str(exc)
        return _invalid(f"{text[:1].upper()}{text[1:]}.", size_bytes)
    except (OSError, struct.error, ValueError) as exc:
        return _invalid(f"The preview file could not be parsed: {_detail(exc)}", size_bytes)

    video_tracks = structure.video_tracks
    if not video_tracks:
        return _invalid("The MP4 file has no video track.", size_bytes)
    duration_ms = structure.duration_ms
    if duration_ms is None:
        return _invalid("The MP4 duration is unknown.", size_bytes)
    if duration_ms < 0:
        return _invalid("The MP4 duration is negative.", size_bytes)

    track = video_tracks[0]
    width = track.width or track.tkhd_width
    height = track.height or track.tkhd_height
    codec = _CODEC_NAMES.get(track.codec or "", track.codec)
    if not width or not height:
        return _invalid("The video track has no dimensions.", size_bytes)
    return VideoPreviewValidation(
        valid=True,
        duration_ms=duration_ms,
        width=int(width),
        height=int(height),
        size_bytes=size_bytes,
        video_codec=codec,
        message="",
    )


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------
class _ProgressReader(threading.Thread):
    """Drain FFmpeg's ``-progress pipe:1`` output on a daemon thread."""

    def __init__(self, stream: BinaryIO | None) -> None:
        super().__init__(name="jvvv-ffmpeg-progress", daemon=True)
        self._stream = stream
        self._lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.out_time_us: int | None = None
        self.finished = False

    def run(self) -> None:
        if self._stream is None:
            return
        try:
            # A pipe read may deliver a whole progress block at once; treat
            # every newline-separated key=value pair individually.
            for raw_chunk in self._stream:
                for raw_line in raw_chunk.splitlines():
                    self._handle_line(raw_line)
        except (OSError, ValueError):
            pass

    def _handle_line(self, raw_line: bytes) -> None:
        line = _decode_output(raw_line).strip()
        with self._lock:
            self.last_activity = time.monotonic()
            key, separator, value = line.partition("=")
            if not separator:
                return
            key = key.strip()
            value = value.strip()
            if key == "out_time_us":
                microseconds = _parse_int(value)
                if microseconds is not None and microseconds >= 0:
                    self.out_time_us = microseconds
            elif key == "out_time_ms" and self.out_time_us is None:
                # Also microseconds in every FFmpeg since 4.x.
                microseconds = _parse_int(value)
                if microseconds is not None and microseconds >= 0:
                    self.out_time_us = microseconds
            elif key == "progress" and value == "end":
                self.finished = True

    def snapshot(self) -> tuple[float, int | None]:
        with self._lock:
            return self.last_activity, self.out_time_us


class _StderrReader(threading.Thread):
    """Keep the last 64 KiB of FFmpeg's stderr on a daemon thread."""

    def __init__(self, stream: BinaryIO | None) -> None:
        super().__init__(name="jvvv-ffmpeg-stderr", daemon=True)
        self._stream = stream
        self._lock = threading.Lock()
        self._buffer = bytearray()

    def run(self) -> None:
        if self._stream is None:
            return
        try:
            while True:
                chunk = self._stream.readline()
                if not chunk:
                    break
                with self._lock:
                    self._buffer.extend(chunk)
                    if len(self._buffer) > STDERR_TAIL_BYTES:
                        del self._buffer[: len(self._buffer) - STDERR_TAIL_BYTES]
        except (OSError, ValueError):
            pass

    @property
    def text(self) -> str:
        with self._lock:
            return _decode_output(bytes(self._buffer))


class VideoPreviewGenerator:
    """Encode one video preview with FFmpeg into a :class:`PreviewCache`."""

    backend_name = f"FFmpeg ({FFMPEG_ENCODER}, {FFMPEG_PIXEL_FORMAT}, MP4 +faststart)"

    def __init__(
        self,
        cache: PreviewCache,
        ffmpeg_path: str,
        *,
        stall_timeout_seconds: float | None = DEFAULT_STALL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        if stall_timeout_seconds is not None and (
            not math.isfinite(stall_timeout_seconds) or stall_timeout_seconds <= 0
        ):
            raise ValueError("stall_timeout_seconds must be None or a positive finite number")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be a positive finite number")
        self.cache = cache
        self.ffmpeg_path = str(ffmpeg_path)
        self.profile: VideoPreviewProfile = cache.video_profile
        self.profile_id = self.profile.profile_id
        self.stall_timeout_seconds = (
            None if stall_timeout_seconds is None else float(stall_timeout_seconds)
        )
        self.poll_interval_seconds = float(poll_interval_seconds)

    def generate(
        self,
        source: Path,
        destination: Path,
        *,
        cancel_callback: CancelCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        expected_duration_ms: int | None = None,
        source_stat: os.stat_result | None = None,
    ) -> PreviewResult:
        """Encode ``source`` into ``destination`` atomically.

        ``source_stat`` is the snapshot taken when the file's SHA-256 was
        computed; the source must still match it before FFmpeg starts (spec
        §32).  Raises ``PreviewCancelled`` when ``cancel_callback`` returns
        true and ``PreviewError`` for every failure; the temporary file is
        always removed on failure and the process is always stopped.
        """

        _raise_if_cancelled(cancel_callback)
        before = _lstat_source(source)
        ensure_source_snapshot(source, before, source_stat)
        self.cache.ensure_parent(destination)
        temp_path = self.cache.temporary_path(destination)
        try:
            return self._encode(
                source,
                destination,
                temp_path,
                before,
                cancel_callback,
                progress_callback,
                expected_duration_ms,
            )
        except BaseException:
            self.cache.discard_temporary(temp_path)
            raise

    def _encode(
        self,
        source: Path,
        destination: Path,
        temp_path: Path,
        before: os.stat_result,
        cancel_callback: CancelCallback | None,
        progress_callback: ProgressCallback | None,
        expected_duration_ms: int | None,
    ) -> PreviewResult:
        arguments = build_ffmpeg_arguments(self.ffmpeg_path, source, temp_path, self.profile)
        try:
            process = subprocess.Popen(arguments, **_popen_options())
        except (OSError, ValueError) as exc:
            raise PreviewError(
                STAGE_FFMPEG_START,
                "FFmpeg could not be started.",
                detail=os_error_detail(exc),
            ) from exc

        progress = _ProgressReader(getattr(process, "stdout", None))
        stderr = _StderrReader(getattr(process, "stderr", None))
        progress.start()
        stderr.start()
        reported_out_time: int | None = None

        def report_progress() -> None:
            nonlocal reported_out_time
            if progress_callback is None:
                return
            _, out_time_us = progress.snapshot()
            if out_time_us is None or out_time_us == reported_out_time:
                return
            reported_out_time = out_time_us
            out_time_ms = out_time_us // 1000
            progress_callback(_fraction(out_time_ms, expected_duration_ms), out_time_ms)

        try:
            while True:
                if cancel_callback and cancel_callback():
                    _stop_process(process)
                    raise PreviewCancelled("Video preview generation cancelled.")
                returncode = process.poll()
                if returncode is not None:
                    break
                if self.stall_timeout_seconds is not None:
                    last_activity, _ = progress.snapshot()
                    if time.monotonic() - last_activity > self.stall_timeout_seconds:
                        _stop_process(process)
                        _join_readers(progress, stderr)
                        raise PreviewError(
                            STAGE_FFMPEG_TIMEOUT,
                            "FFmpeg produced no progress for "
                            f"{self.stall_timeout_seconds:g} seconds.",
                            detail=_detail(stderr.text),
                        )
                report_progress()
                time.sleep(self.poll_interval_seconds)
            _join_readers(progress, stderr)
            report_progress()
        except BaseException:
            _stop_process(process)
            raise
        finally:
            _close_pipes(process)

        stderr_tail = _detail(stderr.text)
        if returncode != 0:
            stage = STAGE_DISK_FULL if _mentions_disk_full(stderr.text) else STAGE_FFMPEG_EXIT
            raise PreviewError(stage, f"FFmpeg exited with code {returncode}.", detail=stderr_tail)
        if _mentions_disk_full(stderr.text):
            raise PreviewError(
                STAGE_DISK_FULL,
                "FFmpeg could not write the preview because the disk is full.",
                detail=stderr_tail,
            )
        _raise_if_cancelled(cancel_callback)
        _ensure_source_unchanged(source, before)

        validation = validate_video_preview(temp_path)
        if not validation.valid:
            raise PreviewError(STAGE_VIDEO_VALIDATE, validation.message, detail=stderr_tail)

        self.cache.publish(temp_path, destination)
        try:
            size = int(destination.stat().st_size)
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_RENAME),
                "The finished preview could not be read back.",
                detail=os_error_detail(exc),
            ) from exc
        return PreviewResult(
            status=PREVIEW_GENERATED,
            media_kind="video",
            profile_id=self.profile_id,
            path=destination,
            bytes_written=size,
            size_bytes=size,
            width=validation.width,
            height=validation.height,
            duration_ms=validation.duration_ms,
        )


def test_video_encode(
    ffmpeg_path: str,
    cache: PreviewCache,
    *,
    timeout_seconds: float = DEFAULT_TEST_ENCODE_TIMEOUT_SECONDS,
) -> str:
    """Prove FFmpeg can really encode into ``cache.root`` (spec §2B).

    Two solid-colour raw RGB frames are piped through stdin (the one
    intentional use of stdin, spec §28) and encoded with the configured
    preset/CRF/pixel format/fast-start.  The output is validated and deleted.
    Raises ``PreviewError`` on any failure; never leaves files behind.
    """

    profile = cache.video_profile
    profile.validate()
    width, height = VIDEO_TEST_SIZE
    target = cache.root / "jvvv-video-test.mp4"
    cache.ensure_parent(target)
    temp_path = cache.temporary_path(target)
    arguments = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(VIDEO_TEST_INPUT_FPS),
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        video_filter_expression(profile),
        "-c:v",
        FFMPEG_ENCODER,
        "-preset",
        str(profile.preset),
        "-crf",
        str(int(profile.crf)),
        "-pix_fmt",
        FFMPEG_PIXEL_FORMAT,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temp_path),
    ]
    frames = _test_frames(width, height)
    try:
        try:
            process = subprocess.Popen(arguments, **_popen_options(stdin=subprocess.PIPE))
        except (OSError, ValueError) as exc:
            raise PreviewError(
                STAGE_FFMPEG_START,
                "FFmpeg could not be started.",
                detail=os_error_detail(exc),
            ) from exc
        try:
            _, stderr = process.communicate(input=frames, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            raise PreviewError(
                STAGE_FFMPEG_TIMEOUT,
                f"FFmpeg did not finish the test encode within {timeout_seconds:g} seconds.",
            ) from exc
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            _stop_process(process)
            raise PreviewError(
                STAGE_FFMPEG_EXIT,
                "FFmpeg communication failed during the test encode.",
                detail=os_error_detail(exc),
            ) from exc
        finally:
            _close_pipes(process)
        stderr_text = _decode_output(stderr)
        returncode = process.returncode
        if returncode != 0:
            stage = STAGE_DISK_FULL if _mentions_disk_full(stderr_text) else STAGE_FFMPEG_EXIT
            raise PreviewError(
                stage,
                f"FFmpeg exited with code {returncode}.",
                detail=_detail(stderr_text),
            )
        if _mentions_disk_full(stderr_text):
            raise PreviewError(
                STAGE_DISK_FULL,
                "FFmpeg could not write the test video because the disk is full.",
                detail=_detail(stderr_text),
            )
        validation = validate_video_preview(temp_path)
        if not validation.valid:
            raise PreviewError(
                STAGE_VIDEO_VALIDATE,
                validation.message,
                detail=_detail(stderr_text),
            )
        size = validation.size_bytes
    finally:
        cache.discard_temporary(temp_path)
    return (
        f"Encoded a {width}x{height} test video ({VIDEO_TEST_FRAME_COUNT} frames) "
        f"with {FFMPEG_ENCODER} preset {profile.preset} CRF {int(profile.crf)} "
        f"({format_size(size)}) in {cache.root}"
    )


def _test_frames(width: int, height: int) -> bytes:
    colours = ((0x33, 0x66, 0xCC), (0xCC, 0x33, 0x33))
    return b"".join(
        bytes(colours[index % len(colours)]) * (width * height)
        for index in range(VIDEO_TEST_FRAME_COUNT)
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _raise_if_cancelled(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback and cancel_callback():
        raise PreviewCancelled("Video preview generation cancelled.")


def _lstat_source(source: Path) -> os.stat_result:
    try:
        return os.lstat(source)
    except OSError as exc:
        raise PreviewError(
            STAGE_FFMPEG_ENCODE,
            "The source video could not be read.",
            detail=os_error_detail(exc),
        ) from exc


def _ensure_source_unchanged(source: Path, before: os.stat_result) -> None:
    message = "The source file changed while its preview was being created."
    try:
        after = os.lstat(source)
    except OSError as exc:
        raise PreviewError(STAGE_SOURCE_CHANGED, message, detail=os_error_detail(exc)) from exc
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise PreviewError(
            STAGE_SOURCE_CHANGED,
            message,
            detail=(
                f"size {before.st_size} -> {after.st_size}, "
                f"mtime_ns {before.st_mtime_ns} -> {after.st_mtime_ns}"
            ),
        )


def _fraction(out_time_ms: int | None, expected_duration_ms: int | None) -> float | None:
    if out_time_ms is None or not expected_duration_ms or expected_duration_ms <= 0:
        return None
    return max(0.0, min(1.0, out_time_ms / expected_duration_ms))


def _parse_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _mentions_disk_full(text: str) -> bool:
    folded = (text or "").casefold()
    return any(phrase in folded for phrase in _DISK_FULL_PHRASES)


def _decode_output(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value or "")


def _detail(value: object) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_DETAIL_LENGTH:
        return text
    return f"{text[: MAX_DETAIL_LENGTH - 1].rstrip()}…"


def _stop_process(process: object) -> None:
    """terminate → wait → kill → wait; tolerate processes that already ended."""

    try:
        if process.poll() is not None:  # type: ignore[attr-defined]
            return
    except (OSError, ValueError, AttributeError):
        return
    try:
        process.terminate()  # type: ignore[attr-defined]
        process.wait(timeout=_PROCESS_STOP_WAIT_SECONDS)  # type: ignore[attr-defined]
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()  # type: ignore[attr-defined]
        process.wait(timeout=_PROCESS_STOP_WAIT_SECONDS)  # type: ignore[attr-defined]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass


def _join_readers(*readers: threading.Thread) -> None:
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)


def _close_pipes(process: object) -> None:
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, name, None)
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except (OSError, ValueError):
                pass
