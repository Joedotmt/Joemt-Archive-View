from __future__ import annotations

import hashlib
import io
import os
import pathlib
import random
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from preview_fixtures import (  # noqa: E402
    PORTRAIT_SOURCE_MP4,
    SOURCE_MP4,
    TINY_MP4,
    real_ffmpeg_path,
    tiny_mp4_bytes,
    write_portrait_source_mp4,
    write_source_mp4,
    write_tiny_mp4,
)

from jvvv import video_preview as video_module  # noqa: E402
from jvvv.preview_cache import (  # noqa: E402
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
)
from jvvv.preview_config import ImagePreviewProfile, VideoPreviewProfile  # noqa: E402
from jvvv.video_preview import (  # noqa: E402
    DEFAULT_STALL_TIMEOUT_SECONDS,
    FFMPEG_ENCODER,
    FFMPEG_PIXEL_FORMAT,
    VIDEO_TEST_FRAME_COUNT,
    VIDEO_TEST_SIZE,
    FfmpegCapabilities,
    Mp4ParseError,
    VideoPreviewGenerator,
    build_ffmpeg_arguments,
    find_ffmpeg,
    inspect_mp4,
    mp4_box_offsets,
    parse_ffmpeg_encoders,
    parse_ffmpeg_version,
    probe_ffmpeg,
    require_libx264,
    test_video_encode as run_test_video_encode,
    validate_video_preview,
    video_filter_expression,
)


FAKE_FFMPEG = "C:/Tools/ffmpeg/bin/ffmpeg.exe" if os.name == "nt" else "/opt/ffmpeg/bin/ffmpeg"
PROGRESS_LINES = (
    b"frame=1\nout_time_us=1500000\nprogress=continue\n",
    b"frame=3\nout_time_us=3000000\nprogress=end\n",
)
VERSION_OUTPUT = (
    "ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers\n"
    "built with gcc 12.2.0 (Rev10, Built by MSYS2 project)\n"
    "configuration: --enable-gpl --enable-libx264\n"
    "libavutil      58.  2.100 / 58.  2.100\n"
)
ENCODERS_OUTPUT = (
    "Encoders:\n"
    " V..... = Video\n"
    " A..... = Audio\n"
    " S..... = Subtitle\n"
    " .F.... = Frame-level multithreading\n"
    " ..S... = Slice-level multithreading\n"
    " ...X.. = Codec is experimental\n"
    " ....B. = Supports draw_horiz_band\n"
    " .....D = Supports direct rendering method 1\n"
    " ------\n"
    " V....D a64multi             Multicolor charset for Commodore 64 (codec a64_multi)\n"
    " V..... amv                  AMV Video\n"
    " V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)\n"
    " V....D libx264rgb           libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 RGB (codec h264)\n"
    " V.S... mpeg4                MPEG-4 part 2\n"
    " A....D aac                  AAC (Advanced Audio Coding)\n"
    " S..... srt                  SubRip subtitle\n"
)
ENCODERS_WITHOUT_X264 = "\n".join(
    line for line in ENCODERS_OUTPUT.splitlines() if "libx264" not in line
) + "\n"


# ---------------------------------------------------------------------------
# Independent MP4 helpers used to build corrupted / rearranged fixtures.  They
# are deliberately written separately from the module's parser so the two can
# cross-check each other.
# ---------------------------------------------------------------------------
def boxes_in(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[bytes, int, int, int]]:
    """Return ``(type, offset, size, header_size)`` for boxes in ``data[start:end]``."""

    end = len(data) if end is None else end
    result = []
    offset = start
    while offset + 8 <= end:
        size, box_type = struct.unpack(">I4s", data[offset : offset + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = end - offset
        result.append((box_type, offset, size, header))
        if size < header:
            break
        offset += size
    return result


def find_box(data: bytes, path: list[bytes]) -> tuple[int, int, int]:
    """Return ``(offset, size, header_size)`` of the box at the given type path."""

    start, end = 0, len(data)
    found: tuple[int, int, int] | None = None
    for wanted in path:
        found = None
        for box_type, offset, size, header in boxes_in(data, start, end):
            if box_type == wanted:
                found = (offset, size, header)
                break
        if found is None:
            raise AssertionError(f"box path {path!r} not found in fixture")
        start, end = found[0] + found[2], found[0] + found[1]
    assert found is not None
    return found


def rename_box(data: bytes, path: list[bytes], new_type: bytes) -> bytes:
    offset, _, header = find_box(data, path)
    type_offset = offset + (8 if header == 16 else 4)
    return data[:type_offset] + new_type + data[type_offset + 4 :]


def patch_handler(data: bytes, new_handler: bytes) -> bytes:
    offset, _, header = find_box(data, [b"moov", b"trak", b"mdia", b"hdlr"])
    handler_offset = offset + header + 8
    assert data[handler_offset : handler_offset + 4] == b"vide"
    return data[:handler_offset] + new_handler + data[handler_offset + 4 :]


def top_level_slices(data: bytes) -> dict[bytes, bytes]:
    return {box_type: data[offset : offset + size] for box_type, offset, size, _ in boxes_in(data)}


def moov_after_mdat(data: bytes) -> bytes:
    parts = top_level_slices(data)
    return parts[b"ftyp"] + parts.get(b"free", b"") + parts[b"mdat"] + parts[b"moov"]


def with_box_header(body: bytes, box_type: bytes) -> bytes:
    return struct.pack(">I4s", len(body) + 8, box_type) + body


def files_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def temporaries_under(root: Path) -> list[Path]:
    return [path for path in files_under(root) if PreviewCache.is_temporary_name(path.name)]


# ---------------------------------------------------------------------------
# Fake FFmpeg process
# ---------------------------------------------------------------------------
class SlowLines:
    """A stdout stand-in that yields one line at a time with a delay."""

    def __init__(self, lines: list[bytes], delay: float) -> None:
        self.lines = lines
        self.delay = delay
        self.closed = False

    def __iter__(self):
        for index, line in enumerate(self.lines):
            if index:
                time.sleep(self.delay)
            yield line

    def close(self) -> None:
        self.closed = True

    def getvalue(self) -> bytes:
        return b"".join(self.lines)


class FakeProcess:
    """Stand-in for ``subprocess.Popen[bytes]`` fully controlled by the test."""

    def __init__(
        self,
        args,
        kwargs,
        *,
        stdout=b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
        exit_after: float = 0.0,
        output_bytes: bytes | None = None,
        on_start=None,
    ) -> None:
        self.args = list(args)
        self.kwargs = kwargs
        self.stdin = None
        if isinstance(stdout, (bytes, bytearray)):
            self.stdout = io.BytesIO(bytes(stdout)) if kwargs.get("stdout") is subprocess.PIPE else None
        else:
            self.stdout = stdout
        self.stderr = io.BytesIO(stderr) if kwargs.get("stderr") is subprocess.PIPE else None
        self._final_returncode = returncode
        self._hang = hang
        self._exit_after = exit_after
        self._started = time.monotonic()
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.communicate_input = None
        if output_bytes is not None:
            Path(self.args[-1]).write_bytes(output_bytes)
        if on_start is not None:
            on_start(self)

    # -- Popen API -----------------------------------------------------------
    def poll(self):
        if (
            self.returncode is None
            and not self._hang
            and time.monotonic() - self._started >= self._exit_after
        ):
            self.returncode = self._final_returncode
        return self.returncode

    def wait(self, timeout=None):
        returncode = self.poll()
        if returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return returncode

    def terminate(self):
        self.terminated = True
        self._hang = False
        self.returncode = -15

    def kill(self):
        self.killed = True
        self._hang = False
        self.returncode = -9

    def communicate(self, input=None, timeout=None):
        self.communicate_input = input
        if self._hang:
            raise subprocess.TimeoutExpired(self.args, timeout)
        stdout = self.stdout.getvalue() if self.stdout is not None else None
        stderr = self.stderr.getvalue() if self.stderr is not None else None
        self.returncode = self._final_returncode
        return stdout, stderr

    @property
    def stopped(self) -> bool:
        return self.terminated or self.killed


class FakeFfmpeg:
    """Installs itself as ``subprocess.Popen`` and records every call."""

    def __init__(self, monkeypatch, factory=None, **defaults) -> None:
        self.calls: list[FakeProcess] = []
        self.factory = factory
        self.defaults = defaults
        monkeypatch.setattr(video_module.subprocess, "Popen", self)

    def __call__(self, args, **kwargs):
        if self.factory is not None:
            process = self.factory(args, kwargs)
        else:
            process = FakeProcess(args, kwargs, **self.defaults)
        self.calls.append(process)
        return process

    @property
    def last(self) -> FakeProcess:
        assert self.calls, "FFmpeg was never started"
        return self.calls[-1]


def successful_ffmpeg(monkeypatch, **overrides) -> FakeFfmpeg:
    options = {
        "stdout": b"".join(PROGRESS_LINES),
        "output_bytes": tiny_mp4_bytes(),
        "returncode": 0,
    }
    options.update(overrides)
    return FakeFfmpeg(monkeypatch, **options)


def probe_ffmpeg_fake(monkeypatch, *, version=VERSION_OUTPUT, encoders=ENCODERS_OUTPUT):
    def factory(args, kwargs):
        if "-version" in args:
            return FakeProcess(args, kwargs, stdout=version.encode("utf-8"))
        assert "-encoders" in args
        return FakeProcess(args, kwargs, stdout=encoders.encode("utf-8"))

    return FakeFfmpeg(monkeypatch, factory=factory)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def cache(tmp_path: Path) -> PreviewCache:
    return PreviewCache(tmp_path / "previews", ImagePreviewProfile(), VideoPreviewProfile())


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return write_source_mp4(tmp_path / "source" / "holiday.mp4")


def destination_for(cache: PreviewCache, source: Path) -> Path:
    return cache.preview_path("video", hashlib.sha256(source.read_bytes()).digest())


@pytest.fixture
def real_ffmpeg() -> str:
    path = real_ffmpeg_path()
    if path is None:
        pytest.skip("real FFmpeg not available (set JVVV_TEST_FFMPEG)")
    return path


# ---------------------------------------------------------------------------
# Pure-Python MP4 validator
# ---------------------------------------------------------------------------
def test_validate_tiny_fixture_reports_dimensions_duration_and_codec():
    validation = validate_video_preview(TINY_MP4)

    assert validation.valid is True
    assert validation.message == ""
    assert validation.duration_ms == 3000
    assert (validation.width, validation.height) == (64, 48)
    assert validation.video_codec == "h264"
    assert validation.size_bytes == TINY_MP4.stat().st_size == len(tiny_mp4_bytes())


def test_validate_mpeg4_source_fixture_with_moov_after_mdat():
    validation = validate_video_preview(SOURCE_MP4)

    assert validation.valid is True
    assert (validation.width, validation.height) == (320, 180)
    assert validation.duration_ms == 2000
    assert validation.video_codec == "mp4v"
    offsets = mp4_box_offsets(SOURCE_MP4)
    assert offsets["mdat"] < offsets["moov"], "fixture is expected to be non-faststart"


def test_validate_portrait_fixture():
    validation = validate_video_preview(PORTRAIT_SOURCE_MP4)

    assert validation.valid is True
    assert (validation.width, validation.height) == (180, 320)
    assert validation.duration_ms == 2000
    assert validation.video_codec == "h264"


def test_validate_zero_byte_file_is_invalid(tmp_path):
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"")

    validation = validate_video_preview(path)

    assert validation.valid is False
    assert validation.size_bytes == 0
    assert "empty" in validation.message


def test_validate_missing_file_and_directory_never_raise(tmp_path):
    missing = validate_video_preview(tmp_path / "missing.mp4")
    directory = validate_video_preview(tmp_path)

    assert missing.valid is False
    assert "could not be read" in missing.message
    assert directory.valid is False
    assert "not a regular file" in directory.message


@pytest.mark.parametrize(
    "garbage",
    [
        random.Random(1234).randbytes(4096),
        b"This is not an MP4 file at all.\n" * 64,
        b"\x00" * 1024,
        b"\x00\x00\x00\x08",  # a lone 8-byte header claiming 8 bytes, but only 4 present
    ],
)
def test_validate_garbage_is_invalid_without_raising(tmp_path, garbage):
    path = tmp_path / "garbage.mp4"
    path.write_bytes(garbage)

    validation = validate_video_preview(path)

    assert validation.valid is False
    assert validation.size_bytes == len(garbage)
    assert "MP4" in validation.message
    assert validation.width is None and validation.height is None
    assert validation.duration_ms is None


def test_validate_truncated_fixture_is_invalid(tmp_path):
    path = tmp_path / "truncated.mp4"
    path.write_bytes(tiny_mp4_bytes()[:200])

    validation = validate_video_preview(path)

    assert validation.valid is False
    assert "truncated" in validation.message.casefold()
    assert "moov" in validation.message


def test_validate_renamed_moov_box_means_no_moov(tmp_path):
    data = rename_box(tiny_mp4_bytes(), [b"moov"], b"xxxx")
    assert b"moov" not in data[:40]
    path = tmp_path / "no-moov.mp4"
    path.write_bytes(data)

    validation = validate_video_preview(path)

    assert validation.valid is False
    assert "moov" in validation.message
    assert "no 'moov'" in validation.message


def test_validate_audio_only_handler_means_no_video_stream(tmp_path):
    data = patch_handler(tiny_mp4_bytes(), b"soun")
    path = tmp_path / "audio-only.mp4"
    path.write_bytes(data)

    validation = validate_video_preview(path)

    assert validation.valid is False
    assert "no video track" in validation.message
    structure = inspect_mp4(path)
    assert structure.audio_track_count == 1
    assert structure.video_tracks == ()


def test_validate_missing_ftyp_is_invalid(tmp_path):
    parts = top_level_slices(tiny_mp4_bytes())
    path = tmp_path / "no-ftyp.mp4"
    path.write_bytes(parts[b"moov"] + parts[b"free"] + parts[b"mdat"])

    validation = validate_video_preview(path)

    assert validation.valid is False
    assert "ftyp" in validation.message


def test_validate_missing_mvhd_and_zero_timescale(tmp_path):
    data = tiny_mp4_bytes()
    no_mvhd = tmp_path / "no-mvhd.mp4"
    no_mvhd.write_bytes(rename_box(data, [b"moov", b"mvhd"], b"xxxx"))
    mvhd_offset, _, header = find_box(data, [b"moov", b"mvhd"])
    timescale_offset = mvhd_offset + header + 12
    assert struct.unpack(">I", data[timescale_offset : timescale_offset + 4])[0] == 1000
    zero_timescale = tmp_path / "zero-timescale.mp4"
    zero_timescale.write_bytes(
        data[:timescale_offset] + b"\x00\x00\x00\x00" + data[timescale_offset + 4 :]
    )

    first = validate_video_preview(no_mvhd)
    second = validate_video_preview(zero_timescale)

    assert first.valid is False and "mvhd" in first.message
    assert second.valid is False and "timescale" in second.message


def test_validate_handles_moov_after_mdat_built_from_tiny_fixture(tmp_path):
    data = moov_after_mdat(tiny_mp4_bytes())
    assert len(data) == len(tiny_mp4_bytes())
    path = tmp_path / "moov-last.mp4"
    path.write_bytes(data)

    validation = validate_video_preview(path)

    assert validation.valid is True
    assert (validation.width, validation.height) == (64, 48)
    assert validation.duration_ms == 3000
    assert validation.video_codec == "h264"
    offsets = mp4_box_offsets(path)
    assert offsets["ftyp"] == 0
    assert offsets["mdat"] < offsets["moov"]
    assert inspect_mp4(path).faststart is False


def test_faststart_helper_reports_moov_before_mdat_for_fixture():
    offsets = mp4_box_offsets(TINY_MP4)

    assert offsets["ftyp"] == 0
    assert offsets["moov"] < offsets["mdat"]
    structure = inspect_mp4(TINY_MP4)
    assert structure.faststart is True
    assert [box.type for box in structure.boxes] == ["ftyp", "moov", "free", "mdat"]
    assert structure.timescale == 1000
    assert structure.duration == 3000
    assert structure.duration_ms == 3000
    assert structure.audio_track_count == 0
    assert len(structure.video_tracks) == 1
    track = structure.video_tracks[0]
    assert track.codec == "avc1"
    assert (track.width, track.height) == (64, 48)
    assert (track.tkhd_width, track.tkhd_height) == (64, 48)


def test_validate_tolerates_trailing_garbage_after_complete_moov(tmp_path):
    path = tmp_path / "trailing.mp4"
    path.write_bytes(tiny_mp4_bytes() + b"\x00\x01garbage trailing bytes")

    validation = validate_video_preview(path)

    assert validation.valid is True
    assert validation.duration_ms == 3000


def test_validate_handles_64bit_and_to_end_of_file_box_sizes(tmp_path):
    data = tiny_mp4_bytes()
    mdat_offset, mdat_size, header = find_box(data, [b"mdat"])
    assert header == 8
    large = (
        data[:mdat_offset]
        + struct.pack(">I4sQ", 1, b"mdat", mdat_size + 8)
        + data[mdat_offset + 8 :]
    )
    to_eof = data[:mdat_offset] + struct.pack(">I4s", 0, b"mdat") + data[mdat_offset + 8 :]
    large_path = tmp_path / "large.mp4"
    large_path.write_bytes(large)
    eof_path = tmp_path / "eof.mp4"
    eof_path.write_bytes(to_eof)

    for path in (large_path, eof_path):
        validation = validate_video_preview(path)
        assert validation.valid is True, validation.message
        assert validation.duration_ms == 3000
        offsets = mp4_box_offsets(path)
        assert offsets["moov"] < offsets["mdat"]


def test_inspect_mp4_counts_audio_and_video_tracks(tmp_path):
    data = tiny_mp4_bytes()
    parts = top_level_slices(data)
    moov = parts[b"moov"]
    children = {box_type: moov[offset : offset + size] for box_type, offset, size, _ in boxes_in(moov, 8)}
    video_trak = children[b"trak"]
    audio_trak = patch_handler(
        with_box_header(video_trak, b"moov") + parts[b"mdat"], b"soun"
    )
    audio_trak = top_level_slices(audio_trak)[b"moov"][8:]
    new_moov = with_box_header(children[b"mvhd"] + video_trak + audio_trak + children[b"udta"], b"moov")
    path = tmp_path / "two-tracks.mp4"
    path.write_bytes(parts[b"ftyp"] + new_moov + parts[b"free"] + parts[b"mdat"])

    structure = inspect_mp4(path)
    validation = validate_video_preview(path)

    assert len(structure.tracks) == 2
    assert structure.audio_track_count == 1
    assert len(structure.video_tracks) == 1
    assert validation.valid is True
    assert (validation.width, validation.height) == (64, 48)


def test_validator_reads_headers_not_the_whole_file(tmp_path, monkeypatch):
    data = tiny_mp4_bytes()
    mdat_offset, _, _ = find_box(data, [b"mdat"])
    payload = 4 * 1024 * 1024
    big = data[:mdat_offset] + struct.pack(">I4s", payload + 8, b"mdat") + b"\x00" * payload
    path = tmp_path / "big-mdat.mp4"
    path.write_bytes(big)
    counted = {"bytes": 0}
    real_open = open

    class CountingFile:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            chunk = self._handle.read(size)
            counted["bytes"] += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

    def counting_open(file, mode="r", *args, **kwargs):
        return CountingFile(real_open(file, mode, *args, **kwargs))

    monkeypatch.setattr(video_module, "open", counting_open, raising=False)

    validation = validate_video_preview(path)

    assert validation.valid is True
    assert validation.size_bytes == len(big)
    assert counted["bytes"] < 8192


def test_inspect_mp4_raises_parse_error_for_garbage(tmp_path):
    path = tmp_path / "garbage.mp4"
    path.write_bytes(b"definitely not an mp4")

    with pytest.raises(Mp4ParseError):
        inspect_mp4(path)


# ---------------------------------------------------------------------------
# Argument construction
# ---------------------------------------------------------------------------
def test_video_filter_expression_uses_configured_fps_and_height_without_upscaling():
    assert video_filter_expression(VideoPreviewProfile()) == (
        "fps=1,scale=w=-2:h=2*trunc(min(ih\\,240)/2)"
    )
    assert video_filter_expression(VideoPreviewProfile(fps=0.5, max_height=720)) == (
        "fps=0.5,scale=w=-2:h=2*trunc(min(ih\\,720)/2)"
    )
    assert "," not in video_filter_expression(VideoPreviewProfile(fps=2.5)).split("scale=")[1].replace("\\,", "")


def test_build_ffmpeg_arguments_matches_the_contract(tmp_path):
    profile = VideoPreviewProfile(fps=2.5, max_height=360, crf=28, preset="fast")
    source = tmp_path / "in put.mov"
    temp = tmp_path / ".abc.mp4.tmp-0123456789abcdef"

    args = build_ffmpeg_arguments(FAKE_FFMPEG, source, temp, profile)

    assert args[0] == FAKE_FFMPEG
    assert args[1:7] == ["-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i"]
    assert args[7] == str(source)
    text = " ".join(args)
    assert "-map 0:v:0" in text
    assert "-an" in args and "-sn" in args and "-dn" in args
    assert "-vf fps=2.5,scale=w=-2:h=2*trunc(min(ih\\,360)/2)" in text
    assert f"-c:v {FFMPEG_ENCODER}" in text
    assert "-preset fast" in text
    assert "-crf 28" in text
    assert f"-pix_fmt {FFMPEG_PIXEL_FORMAT}" in text
    assert "-movflags +faststart" in text
    assert "-progress pipe:1" in text
    assert "-nostats" in args
    assert args[-3:] == ["-f", "mp4", str(temp)]
    assert all(isinstance(value, str) for value in args)

    without_progress = build_ffmpeg_arguments(FAKE_FFMPEG, source, temp, profile, progress=False)
    assert "-progress" not in without_progress and "-nostats" not in without_progress
    assert without_progress[-3:] == ["-f", "mp4", str(temp)]


# ---------------------------------------------------------------------------
# FFmpeg discovery and probing
# ---------------------------------------------------------------------------
def test_find_ffmpeg_explicit_missing_path_returns_none_without_path_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(video_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    assert find_ffmpeg(str(tmp_path / "missing" / "ffmpeg.exe")) is None


def test_find_ffmpeg_explicit_existing_path_is_returned(tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"MZ")

    assert find_ffmpeg(str(executable)) == str(executable)
    assert find_ffmpeg(f"  {executable}  ") == str(executable)
    assert find_ffmpeg(str(tmp_path)) is None, "a directory is not an executable"


def test_find_ffmpeg_expands_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"MZ")

    found = find_ffmpeg("~/ffmpeg.exe")

    assert found is not None
    assert Path(found) == executable


def test_find_ffmpeg_without_explicit_path_uses_path_lookup(monkeypatch):
    calls = []

    def fake_which(name):
        calls.append(name)
        return "/usr/local/bin/ffmpeg"

    monkeypatch.setattr(video_module.shutil, "which", fake_which)

    assert find_ffmpeg() == "/usr/local/bin/ffmpeg"
    assert find_ffmpeg("") == "/usr/local/bin/ffmpeg"
    assert find_ffmpeg("   ") == "/usr/local/bin/ffmpeg"
    assert calls == ["ffmpeg", "ffmpeg", "ffmpeg"]

    monkeypatch.setattr(video_module.shutil, "which", lambda name: None)
    assert find_ffmpeg(None) is None


def test_parse_helpers_extract_version_and_encoder_names():
    assert parse_ffmpeg_version(VERSION_OUTPUT) == (
        "ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers"
    )
    assert parse_ffmpeg_version("\n  ffmpeg version n7.0\n") == "ffmpeg version n7.0"
    assert parse_ffmpeg_version("") is None
    assert parse_ffmpeg_version("Usage: ffmpeg [options]") is None

    encoders = parse_ffmpeg_encoders(ENCODERS_OUTPUT)
    assert {"libx264", "libx264rgb", "aac", "amv", "a64multi", "mpeg4", "srt"} <= encoders
    assert "=" not in encoders
    assert "Encoders:" not in encoders
    assert "libx264" not in parse_ffmpeg_encoders(ENCODERS_WITHOUT_X264)


def test_probe_ffmpeg_parses_version_and_encoders_with_safe_process_options(monkeypatch):
    fake = probe_ffmpeg_fake(monkeypatch)

    capabilities = probe_ffmpeg(FAKE_FFMPEG)

    assert capabilities == FfmpegCapabilities(
        path=FAKE_FFMPEG,
        version="ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers",
        encoders=capabilities.encoders,
    )
    assert capabilities.has_libx264 is True
    assert "aac" in capabilities.encoders
    assert [call.args[:2] for call in fake.calls] == [
        [FAKE_FFMPEG, "-hide_banner"],
        [FAKE_FFMPEG, "-hide_banner"],
    ]
    assert fake.calls[0].args[2:] == ["-version"]
    assert fake.calls[1].args[2:] == ["-encoders"]
    for call in fake.calls:
        assert call.kwargs["shell"] is False
        assert call.kwargs["stdin"] is subprocess.DEVNULL
        assert call.kwargs["stdout"] is subprocess.PIPE
        assert call.kwargs["stderr"] is subprocess.PIPE
        if os.name == "nt":
            assert call.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    require_libx264(capabilities)  # must not raise


def test_probe_detects_missing_libx264_and_require_raises_with_exact_wording(monkeypatch):
    probe_ffmpeg_fake(monkeypatch, encoders=ENCODERS_WITHOUT_X264)

    capabilities = probe_ffmpeg(FAKE_FFMPEG)

    assert capabilities.has_libx264 is False
    assert "aac" in capabilities.encoders
    with pytest.raises(PreviewError) as info:
        require_libx264(capabilities)
    assert info.value.stage == STAGE_FFMPEG_ENCODER
    assert info.value.message == (
        f"FFmpeg was found at:\n{FAKE_FFMPEG}\n\n"
        f"However, the H.264 {FFMPEG_ENCODER} encoder is not available."
    )
    assert "libx264" in str(info.value)


def test_probe_start_failure_is_ffmpeg_start(monkeypatch):
    def failing_popen(args, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified", args[0])

    monkeypatch.setattr(video_module.subprocess, "Popen", failing_popen)

    with pytest.raises(PreviewError) as info:
        probe_ffmpeg(FAKE_FFMPEG)

    assert info.value.stage == STAGE_FFMPEG_START
    assert info.value.message == "FFmpeg could not be started."
    assert "cannot find the file" in info.value.detail


def test_probe_nonzero_exit_is_ffmpeg_exit(monkeypatch):
    FakeFfmpeg(monkeypatch, stdout=b"", stderr=b"Unrecognized option 'version'.\n", returncode=1)

    with pytest.raises(PreviewError) as info:
        probe_ffmpeg(FAKE_FFMPEG)

    assert info.value.stage == STAGE_FFMPEG_EXIT
    assert info.value.message == "FFmpeg exited with code 1."
    assert "Unrecognized option" in info.value.detail


def test_probe_without_version_line_is_ffmpeg_version(monkeypatch):
    FakeFfmpeg(monkeypatch, stdout=b"Something else entirely\nffmpeg version 6.0\n")

    with pytest.raises(PreviewError) as info:
        probe_ffmpeg(FAKE_FFMPEG)

    assert info.value.stage == STAGE_FFMPEG_VERSION
    assert info.value.message == "FFmpeg did not report a valid version."


def test_probe_timeout_stops_the_process_and_is_ffmpeg_timeout(monkeypatch):
    fake = FakeFfmpeg(monkeypatch, hang=True)

    with pytest.raises(PreviewError) as info:
        probe_ffmpeg(FAKE_FFMPEG, timeout_seconds=0.05)

    assert info.value.stage == STAGE_FFMPEG_TIMEOUT
    assert "0.05 seconds" in info.value.message
    assert fake.last.stopped


# ---------------------------------------------------------------------------
# VideoPreviewGenerator with a fake FFmpeg
# ---------------------------------------------------------------------------
def test_generate_success_publishes_validated_output_and_reports_progress(monkeypatch, cache, source):
    fake = successful_ffmpeg(
        monkeypatch,
        stdout=SlowLines(list(PROGRESS_LINES), delay=0.3),
        exit_after=0.6,
    )
    destination = destination_for(cache, source)
    reports: list[tuple[float | None, int | None]] = []
    generator = VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.02)

    result = generator.generate(
        source,
        destination,
        progress_callback=lambda fraction, out_time_ms: reports.append((fraction, out_time_ms)),
        expected_duration_ms=3000,
    )

    assert result.status == PREVIEW_GENERATED
    assert result.ok is True
    assert result.media_kind == "video"
    assert result.profile_id == cache.video_profile.profile_id == "h264-1fps-240p-crf35-veryfast"
    assert result.path == destination
    assert result.size_bytes == result.bytes_written == len(tiny_mp4_bytes())
    assert (result.width, result.height, result.duration_ms) == (64, 48, 3000)
    assert result.stage is None and result.message == ""
    assert destination.read_bytes() == tiny_mp4_bytes()
    assert files_under(cache.root) == [destination]
    assert temporaries_under(cache.root) == []

    assert reports, "progress callback was never invoked"
    assert (0.5, 1500) in reports
    assert reports[-1] == (1.0, 3000)
    assert all(0.0 <= fraction <= 1.0 for fraction, _ in reports)

    process = fake.last
    temp_argument = Path(process.args[-1])
    assert temp_argument.parent == destination.parent
    assert PreviewCache.is_temporary_name(temp_argument.name)
    assert not temp_argument.exists()
    assert process.args == build_ffmpeg_arguments(
        FAKE_FFMPEG, source, temp_argument, cache.video_profile
    )
    assert process.kwargs["shell"] is False
    assert process.kwargs["stdin"] is subprocess.DEVNULL
    assert process.kwargs["stdout"] is subprocess.PIPE
    assert process.kwargs["stderr"] is subprocess.PIPE
    if os.name == "nt":
        assert process.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    text = " ".join(process.args)
    assert "fps=1," in text and "min(ih\\,240)" in text
    assert "-crf 35" in text and "-preset veryfast" in text
    assert "-an" in process.args
    assert f"-pix_fmt {FFMPEG_PIXEL_FORMAT}" in text
    assert "-movflags +faststart" in text
    assert f"-c:v {FFMPEG_ENCODER}" in text
    assert "-map 0:v:0" in text
    assert "-progress pipe:1" in text


def test_generate_reports_out_time_without_fraction_when_duration_unknown(monkeypatch, cache, source):
    successful_ffmpeg(monkeypatch)
    reports = []

    result = VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(
        source,
        destination_for(cache, source),
        progress_callback=lambda fraction, out_time_ms: reports.append((fraction, out_time_ms)),
    )

    assert result.ok
    assert reports
    assert all(fraction is None for fraction, _ in reports)
    assert reports[-1][1] == 3000


def test_generate_uses_configured_profile_values(monkeypatch, tmp_path, source):
    profile = VideoPreviewProfile(fps=0.5, max_height=480, crf=40, preset="medium")
    cache = PreviewCache(tmp_path / "previews", ImagePreviewProfile(), profile)
    fake = successful_ffmpeg(monkeypatch)

    result = VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(
        source, destination_for(cache, source)
    )

    assert result.profile_id == "h264-0.5fps-480p-crf40-medium"
    text = " ".join(fake.last.args)
    assert "fps=0.5," in text and "min(ih\\,480)" in text
    assert "-crf 40" in text and "-preset medium" in text
    assert str(result.path).startswith(str(cache.root / "videos" / "h264-0.5fps-480p-crf40-medium"))


def test_generate_start_failure_is_ffmpeg_start_and_leaves_nothing(monkeypatch, cache, source):
    def failing_popen(args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    monkeypatch.setattr(video_module.subprocess, "Popen", failing_popen)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG).generate(source, destination)

    assert info.value.stage == STAGE_FFMPEG_START
    assert info.value.message == "FFmpeg could not be started."
    assert "No such file" in info.value.detail
    assert files_under(cache.root) == []
    assert not destination.exists()


def test_generate_nonzero_exit_is_ffmpeg_exit_with_stderr_detail(monkeypatch, cache, source):
    FakeFfmpeg(
        monkeypatch,
        stdout=b"",
        stderr=b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] moov atom not found\n"
        b"Invalid data found when processing input\n",
        returncode=1,
        output_bytes=b"partial output",
    )
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert info.value.stage == STAGE_FFMPEG_EXIT
    assert info.value.message == "FFmpeg exited with code 1."
    assert "Invalid data found when processing input" in info.value.detail
    assert "\n" not in info.value.detail
    assert files_under(cache.root) == []
    assert not destination.exists()


def test_generate_long_stderr_detail_is_truncated_to_single_line(monkeypatch, cache, source):
    FakeFfmpeg(monkeypatch, stderr=(b"error line %d\n" % 0) * 400, returncode=1)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(
            source, destination_for(cache, source)
        )

    assert info.value.stage == STAGE_FFMPEG_EXIT
    assert len(info.value.detail) <= 500
    assert "\n" not in info.value.detail


@pytest.mark.parametrize("output_bytes", [b"garbage", None, tiny_mp4_bytes()[:200]])
def test_generate_exit_zero_but_unusable_output_is_video_validate(monkeypatch, cache, source, output_bytes):
    FakeFfmpeg(monkeypatch, stdout=b"progress=end\n", returncode=0, output_bytes=output_bytes)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert info.value.stage == STAGE_VIDEO_VALIDATE
    assert info.value.message
    assert files_under(cache.root) == []
    assert not destination.exists()


@pytest.mark.parametrize("returncode", [1, 0])
def test_generate_disk_full_stderr_is_disk_full_stage(monkeypatch, cache, source, returncode):
    FakeFfmpeg(
        monkeypatch,
        stderr=b"av_interleaved_write_frame(): No space left on device\n",
        returncode=returncode,
        output_bytes=tiny_mp4_bytes(),
    )
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert info.value.stage == STAGE_DISK_FULL
    assert "No space left on device" in info.value.detail
    if returncode:
        assert info.value.message == f"FFmpeg exited with code {returncode}."
    assert files_under(cache.root) == []


def test_generate_cancellation_stops_process_and_removes_temp(monkeypatch, cache, source):
    written: list[Path] = []

    def remember_temp(process):
        written.append(Path(process.args[-1]))

    fake = FakeFfmpeg(monkeypatch, hang=True, output_bytes=b"partial", on_start=remember_temp)
    destination = destination_for(cache, source)
    checks = {"count": 0}

    def cancelled() -> bool:
        checks["count"] += 1
        return checks["count"] >= 3

    with pytest.raises(PreviewCancelled):
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(
            source, destination, cancel_callback=cancelled
        )

    assert fake.last.stopped
    assert fake.last.terminated, "graceful termination must be attempted first"
    assert written and not written[0].exists()
    assert files_under(cache.root) == []
    assert not destination.exists()


def test_generate_cancelled_before_start_never_runs_ffmpeg(monkeypatch, cache, source):
    fake = successful_ffmpeg(monkeypatch)

    with pytest.raises(PreviewCancelled):
        VideoPreviewGenerator(cache, FAKE_FFMPEG).generate(
            source, destination_for(cache, source), cancel_callback=lambda: True
        )

    assert fake.calls == []
    assert files_under(cache.root) == []


def test_generate_stall_watchdog_is_ffmpeg_timeout(monkeypatch, cache, source):
    fake = FakeFfmpeg(monkeypatch, hang=True, stdout=b"", output_bytes=b"partial")
    destination = destination_for(cache, source)
    started = time.monotonic()

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(
            cache, FAKE_FFMPEG, stall_timeout_seconds=0.2, poll_interval_seconds=0.02
        ).generate(source, destination)

    elapsed = time.monotonic() - started
    assert info.value.stage == STAGE_FFMPEG_TIMEOUT
    assert info.value.message == "FFmpeg produced no progress for 0.2 seconds."
    assert 0.2 <= elapsed < 10
    assert fake.last.stopped
    assert files_under(cache.root) == []
    assert not destination.exists()


def test_generate_progress_output_resets_the_stall_watchdog(monkeypatch, cache, source):
    # Six progress blocks 0.1 s apart over ~0.5 s, then exit: with a 0.5 s stall
    # limit the encode only survives if every block resets the watchdog.
    lines = [b"frame=%d\nout_time_us=%d\nprogress=continue\n" % (index, index * 500000) for index in range(1, 7)]
    successful_ffmpeg(monkeypatch, stdout=SlowLines(lines, delay=0.1), exit_after=0.7)
    reports = []

    result = VideoPreviewGenerator(
        cache, FAKE_FFMPEG, stall_timeout_seconds=0.5, poll_interval_seconds=0.02
    ).generate(
        source,
        destination_for(cache, source),
        progress_callback=lambda fraction, out_time_ms: reports.append((fraction, out_time_ms)),
        expected_duration_ms=3000,
    )

    assert result.ok
    assert temporaries_under(cache.root) == []
    out_times = [out_time_ms for _, out_time_ms in reports]
    assert out_times == sorted(out_times) and len(set(out_times)) == len(out_times)
    assert out_times[-1] == 3000
    assert reports[-1][0] == 1.0
    assert len(reports) >= 3, reports


def test_generate_without_stall_timeout_waits_until_cancelled(monkeypatch, cache, source):
    fake = FakeFfmpeg(monkeypatch, hang=True, stdout=b"")
    started = time.monotonic()

    with pytest.raises(PreviewCancelled):
        VideoPreviewGenerator(
            cache, FAKE_FFMPEG, stall_timeout_seconds=None, poll_interval_seconds=0.02
        ).generate(
            source,
            destination_for(cache, source),
            cancel_callback=lambda: time.monotonic() - started > 0.3,
        )

    assert fake.last.stopped
    assert files_under(cache.root) == []


def test_generate_default_stall_timeout_is_not_a_short_fixed_timeout(cache):
    generator = VideoPreviewGenerator(cache, FAKE_FFMPEG)

    assert generator.stall_timeout_seconds == DEFAULT_STALL_TIMEOUT_SECONDS == 600.0
    with pytest.raises(ValueError):
        VideoPreviewGenerator(cache, FAKE_FFMPEG, stall_timeout_seconds=0)
    with pytest.raises(ValueError):
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0)


def test_generate_source_modified_during_encode_is_source_changed(monkeypatch, cache, source):
    def modify_source(process):
        with open(source, "ab") as handle:
            handle.write(b"\x00" * 16)
        later = time.time() + 5
        os.utime(source, (later, later))

    successful_ffmpeg(monkeypatch, on_start=modify_source)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert info.value.message == "The source file changed while its preview was being created."
    assert files_under(cache.root) == []
    assert not destination.exists()


def test_generate_source_removed_during_encode_is_source_changed(monkeypatch, cache, source):
    successful_ffmpeg(monkeypatch, on_start=lambda process: source.unlink())
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert info.value.stage == STAGE_SOURCE_CHANGED
    assert files_under(cache.root) == []


def test_generate_unreadable_source_fails_before_starting_ffmpeg(monkeypatch, cache, tmp_path):
    fake = successful_ffmpeg(monkeypatch)
    missing = tmp_path / "missing.mov"

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG).generate(
            missing, cache.preview_path("video", "ab" * 32)
        )

    assert info.value.stage == STAGE_FFMPEG_ENCODE
    assert info.value.message == "The source video could not be read."
    assert info.value.detail
    assert fake.calls == []


def test_generate_publish_failure_propagates_and_discards_temp(monkeypatch, cache, source):
    successful_ffmpeg(monkeypatch)
    destination = destination_for(cache, source)

    def failing_publish(temp_path, final_path):
        raise PreviewError(STAGE_RENAME, "Could not move the finished preview into place.")

    monkeypatch.setattr(cache, "publish", failing_publish)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert info.value.stage == STAGE_RENAME
    assert files_under(cache.root) == []
    assert not destination.exists()


def test_generate_progress_callback_exception_stops_process_and_cleans_up(monkeypatch, cache, source):
    fake = successful_ffmpeg(monkeypatch, exit_after=5.0)
    destination = destination_for(cache, source)

    def exploding(fraction, out_time_ms):
        raise RuntimeError("ui went away")

    with pytest.raises(RuntimeError):
        VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(
            source, destination, progress_callback=exploding, expected_duration_ms=3000
        )

    assert fake.last.stopped
    assert files_under(cache.root) == []


def test_generate_replaces_existing_destination_atomically(monkeypatch, cache, source):
    successful_ffmpeg(monkeypatch)
    destination = destination_for(cache, source)
    cache.ensure_parent(destination)
    destination.write_bytes(b"corrupt old preview")

    result = VideoPreviewGenerator(cache, FAKE_FFMPEG, poll_interval_seconds=0.01).generate(source, destination)

    assert result.ok
    assert destination.read_bytes() == tiny_mp4_bytes()
    assert files_under(cache.root) == [destination]


# ---------------------------------------------------------------------------
# test_video_encode with a fake FFmpeg
# ---------------------------------------------------------------------------
def test_test_video_encode_success_reports_and_cleans_up(monkeypatch, cache):
    fake = FakeFfmpeg(monkeypatch, output_bytes=tiny_mp4_bytes())

    message = run_test_video_encode(FAKE_FFMPEG, cache)

    width, height = VIDEO_TEST_SIZE
    assert f"{width}x{height}" in message
    assert f"({VIDEO_TEST_FRAME_COUNT} frames)" in message
    assert FFMPEG_ENCODER in message
    assert "preset veryfast" in message and "CRF 35" in message
    assert str(cache.root) in message
    assert "KB" in message or " B" in message
    assert cache.root.is_dir()
    assert files_under(cache.root) == []

    process = fake.last
    assert process.kwargs["stdin"] is subprocess.PIPE
    assert process.kwargs["shell"] is False
    if os.name == "nt":
        assert process.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert process.communicate_input is not None
    assert len(process.communicate_input) == VIDEO_TEST_FRAME_COUNT * width * height * 3
    text = " ".join(process.args)
    assert process.args[0] == FAKE_FFMPEG
    assert "-f rawvideo" in text and "-pix_fmt rgb24" in text
    assert f"-s {width}x{height}" in text and "-r 2" in text
    assert "-i pipe:0" in text and "-an" in process.args
    assert f"-c:v {FFMPEG_ENCODER}" in text and "-preset veryfast" in text and "-crf 35" in text
    assert f"-pix_fmt {FFMPEG_PIXEL_FORMAT}" in text
    assert "-movflags +faststart" in text
    assert process.args[-3:-1] == ["-f", "mp4"]
    temp = Path(process.args[-1])
    assert temp.parent == cache.root
    assert PreviewCache.is_temporary_name(temp.name)
    assert not temp.exists()


def test_test_video_encode_nonzero_exit_is_ffmpeg_exit(monkeypatch, cache):
    FakeFfmpeg(monkeypatch, stderr=b"Unknown encoder 'libx264'\n", returncode=1, output_bytes=b"x")

    with pytest.raises(PreviewError) as info:
        run_test_video_encode(FAKE_FFMPEG, cache)

    assert info.value.stage == STAGE_FFMPEG_EXIT
    assert info.value.message == "FFmpeg exited with code 1."
    assert "Unknown encoder" in info.value.detail
    assert files_under(cache.root) == []


def test_test_video_encode_disk_full_is_disk_full(monkeypatch, cache):
    FakeFfmpeg(monkeypatch, stderr=b"Error writing trailer: No space left on device\n", returncode=1)

    with pytest.raises(PreviewError) as info:
        run_test_video_encode(FAKE_FFMPEG, cache)

    assert info.value.stage == STAGE_DISK_FULL
    assert files_under(cache.root) == []


def test_test_video_encode_garbage_output_is_video_validate(monkeypatch, cache):
    FakeFfmpeg(monkeypatch, output_bytes=b"not an mp4")

    with pytest.raises(PreviewError) as info:
        run_test_video_encode(FAKE_FFMPEG, cache)

    assert info.value.stage == STAGE_VIDEO_VALIDATE
    assert files_under(cache.root) == []


def test_test_video_encode_start_failure_is_ffmpeg_start(monkeypatch, cache):
    def failing_popen(args, **kwargs):
        raise PermissionError(13, "Permission denied", args[0])

    monkeypatch.setattr(video_module.subprocess, "Popen", failing_popen)

    with pytest.raises(PreviewError) as info:
        run_test_video_encode(FAKE_FFMPEG, cache)

    assert info.value.stage == STAGE_FFMPEG_START
    assert "Permission denied" in info.value.detail
    assert files_under(cache.root) == []


def test_test_video_encode_timeout_stops_process(monkeypatch, cache):
    fake = FakeFfmpeg(monkeypatch, hang=True, output_bytes=b"partial")

    with pytest.raises(PreviewError) as info:
        run_test_video_encode(FAKE_FFMPEG, cache, timeout_seconds=0.05)

    assert info.value.stage == STAGE_FFMPEG_TIMEOUT
    assert "0.05 seconds" in info.value.message
    assert fake.last.stopped
    assert files_under(cache.root) == []


# ---------------------------------------------------------------------------
# Real FFmpeg (skipped when unavailable)
# ---------------------------------------------------------------------------
def test_real_probe_reports_version_and_libx264(real_ffmpeg):
    capabilities = probe_ffmpeg(real_ffmpeg)

    assert capabilities.path == real_ffmpeg
    assert capabilities.version.startswith("ffmpeg version")
    assert capabilities.has_libx264 is True
    require_libx264(capabilities)


def test_real_find_ffmpeg_accepts_the_explicit_executable(real_ffmpeg):
    assert find_ffmpeg(real_ffmpeg) == real_ffmpeg


def test_real_test_video_encode_leaves_root_empty(real_ffmpeg, cache):
    message = run_test_video_encode(real_ffmpeg, cache)

    assert isinstance(message, str)
    assert "64x48" in message and FFMPEG_ENCODER in message and "CRF 35" in message
    assert cache.root.is_dir()
    assert files_under(cache.root) == []


def test_real_generate_landscape_source_honours_profile(real_ffmpeg, tmp_path, source):
    profile = VideoPreviewProfile(fps=2, max_height=120, crf=35, preset="veryfast")
    cache = PreviewCache(tmp_path / "previews", ImagePreviewProfile(), profile)
    destination = destination_for(cache, source)
    reports = []

    result = VideoPreviewGenerator(cache, real_ffmpeg).generate(
        source,
        destination,
        progress_callback=lambda fraction, out_time_ms: reports.append((fraction, out_time_ms)),
        expected_duration_ms=2000,
    )

    assert result.status == PREVIEW_GENERATED
    assert result.path == destination and destination.is_file()
    assert result.profile_id == "h264-2fps-120p-crf35-veryfast"
    assert result.size_bytes == result.bytes_written == destination.stat().st_size > 0
    assert files_under(cache.root) == [destination]
    assert temporaries_under(cache.root) == []

    validation = validate_video_preview(destination)
    assert validation.valid is True, validation.message
    assert validation.video_codec == "h264"
    assert validation.height == 120 <= profile.max_height
    assert validation.width % 2 == 0 and validation.height % 2 == 0
    assert abs(validation.width - 320 * 120 / 180) <= 2
    assert 1400 <= validation.duration_ms <= 2600
    assert (result.width, result.height, result.duration_ms) == (
        validation.width,
        validation.height,
        validation.duration_ms,
    )

    structure = inspect_mp4(destination)
    assert structure.audio_track_count == 0
    assert len(structure.video_tracks) == 1
    offsets = mp4_box_offsets(destination)
    assert offsets["moov"] < offsets["mdat"], "output must be fast-start"
    assert reports and all(fraction is None or 0.0 <= fraction <= 1.0 for fraction, _ in reports)


def test_real_generate_portrait_source_keeps_orientation(real_ffmpeg, tmp_path):
    profile = VideoPreviewProfile(fps=2, max_height=120, crf=35, preset="veryfast")
    cache = PreviewCache(tmp_path / "previews", ImagePreviewProfile(), profile)
    source = write_portrait_source_mp4(tmp_path / "source" / "portrait.mp4")
    destination = destination_for(cache, source)

    result = VideoPreviewGenerator(cache, real_ffmpeg).generate(source, destination)

    validation = validate_video_preview(destination)
    assert result.ok and validation.valid, validation.message
    assert validation.height <= 120
    assert validation.width < validation.height
    assert validation.width % 2 == 0 and validation.height % 2 == 0
    assert abs(validation.width - 180 * validation.height / 320) <= 2
    assert inspect_mp4(destination).audio_track_count == 0
    assert temporaries_under(cache.root) == []


def test_real_generate_does_not_upscale_small_source(real_ffmpeg, tmp_path):
    profile = VideoPreviewProfile(fps=2, max_height=240, crf=35, preset="veryfast")
    cache = PreviewCache(tmp_path / "previews", ImagePreviewProfile(), profile)
    source = write_tiny_mp4(tmp_path / "source" / "tiny.mp4")
    destination = destination_for(cache, source)

    result = VideoPreviewGenerator(cache, real_ffmpeg).generate(source, destination)

    validation = validate_video_preview(destination)
    assert result.ok and validation.valid, validation.message
    assert (validation.width, validation.height) == (64, 48)
    assert 2400 <= validation.duration_ms <= 3600
    assert validation.video_codec == "h264"
    assert files_under(cache.root) == [destination]


def test_real_generate_undecodable_source_is_a_visible_ffmpeg_exit(real_ffmpeg, cache, tmp_path):
    source = tmp_path / "source" / "broken.mov"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"this is not a video" * 200)
    destination = destination_for(cache, source)

    with pytest.raises(PreviewError) as info:
        VideoPreviewGenerator(cache, real_ffmpeg).generate(source, destination)

    assert info.value.stage == STAGE_FFMPEG_EXIT
    assert info.value.message.startswith("FFmpeg exited with code ")
    assert info.value.detail
    assert files_under(cache.root) == []
    assert not destination.exists()
