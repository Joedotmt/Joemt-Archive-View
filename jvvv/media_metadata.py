from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from PySide6.QtGui import QImageReader


IMAGE_EXTENSIONS = frozenset(
    {"bmp", "gif", "heic", "ico", "jpeg", "jpg", "png", "tif", "tiff", "webp"}
)
AUDIO_EXTENSIONS = frozenset(
    {"aac", "flac", "m4a", "mp3", "ogg", "wav", "wave", "wma"}
)
VIDEO_EXTENSIONS = frozenset(
    {"avi", "m4v", "mkv", "mov", "mp4", "webm", "wmv"}
)

MEDIA_STATUSES = frozenset({"complete", "partial", "unavailable", "failed"})
DEFAULT_FFPROBE_TIMEOUT_SECONDS = 15.0
MAX_DETAIL_LENGTH = 500
_SQLITE_INTEGER_MAX = 2**63 - 1

CancelCallback = Callable[[], bool]


class MediaInspectionCancelled(Exception):
    """Raised when the caller cancels an in-progress media inspection."""


@dataclass(frozen=True)
class MediaMetadata:
    """Normalized media facts from a best-effort, read-only inspection.

    ``status`` is deliberately separate from the values. A recognized media
    file always produces a record, even when optional tooling is unavailable
    or a corrupt file cannot be parsed. This lets the catalogue explain why
    fields are absent instead of silently presenting them as unknown.
    """

    status: str
    media_kind: str
    source: str = ""
    container_format: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    video_codecs: tuple[str, ...] = ()
    audio_codecs: tuple[str, ...] = ()
    sample_rate_hz: int | None = None
    channels: int | None = None
    bit_rate: int | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in MEDIA_STATUSES:
            raise ValueError(f"Unsupported media metadata status: {self.status}")
        if self.media_kind not in {"image", "audio", "video"}:
            raise ValueError(f"Unsupported media kind: {self.media_kind}")

    @property
    def video_codec(self) -> str | None:
        return ", ".join(self.video_codecs) or None

    @property
    def audio_codec(self) -> str | None:
        return ", ".join(self.audio_codecs) or None

    def as_db_values(self) -> dict[str, object]:
        """Return scalar values that can be passed directly to SQLite."""

        return {
            "status": self.status,
            "media_kind": self.media_kind,
            "source": self.source,
            "container_format": self.container_format,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "bit_rate": self.bit_rate,
            "message": self.message,
        }


def media_kind_for_extension(extension: str | None) -> str | None:
    normalized = (extension or "").strip().casefold().lstrip(".")
    if normalized in IMAGE_EXTENSIONS:
        return "image"
    if normalized in AUDIO_EXTENSIONS:
        return "audio"
    if normalized in VIDEO_EXTENSIONS:
        return "video"
    return None


def find_ffprobe() -> str | None:
    """Return the installed ffprobe executable, without making it required."""

    return shutil.which("ffprobe")


class MediaMetadataExtractor:
    """Inspect media using existing Qt support and an optional ffprobe tool.

    The executable lookup happens once per extractor, so a scanner should
    create one instance and reuse it for the whole drive. Pass
    ``discover_ffprobe=False`` to deliberately use only built-in readers.
    """

    def __init__(
        self,
        ffprobe_path: str | os.PathLike[str] | None = None,
        *,
        discover_ffprobe: bool = True,
        ffprobe_timeout_seconds: float = DEFAULT_FFPROBE_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(ffprobe_timeout_seconds) or ffprobe_timeout_seconds <= 0:
            raise ValueError("ffprobe_timeout_seconds must be a positive finite number")
        discovered = find_ffprobe() if ffprobe_path is None and discover_ffprobe else ffprobe_path
        self.ffprobe_path = os.fspath(discovered) if discovered else None
        self.ffprobe_timeout_seconds = float(ffprobe_timeout_seconds)

    def inspect(
        self,
        path: str | os.PathLike[str],
        *,
        extension: str | None = None,
        cancel_callback: CancelCallback | None = None,
    ) -> MediaMetadata | None:
        """Return normalized metadata, or ``None`` for a non-media extension.

        File-content or tool errors are returned as explicit status records.
        Cancellation is different: it raises ``MediaInspectionCancelled`` so
        the enclosing scan can roll back instead of persisting partial work.
        """

        file_path = Path(path)
        suffix = extension if extension is not None else file_path.suffix
        media_kind = media_kind_for_extension(suffix)
        if media_kind is None:
            return None
        _raise_if_cancelled(cancel_callback)

        if media_kind == "image":
            image_result = _inspect_image(file_path, cancel_callback)
            if image_result.status in {"complete", "partial"}:
                return image_result
            if self.ffprobe_path:
                ffprobe_result = self._inspect_with_ffprobe(
                    file_path,
                    media_kind,
                    cancel_callback,
                )
                if ffprobe_result.status in {"complete", "partial"}:
                    return ffprobe_result
                return _prefer_useful_failure(image_result, ffprobe_result)
            return image_result

        ffprobe_result: MediaMetadata | None = None
        if self.ffprobe_path:
            ffprobe_result = self._inspect_with_ffprobe(
                file_path,
                media_kind,
                cancel_callback,
            )
            if ffprobe_result.status in {"complete", "partial"}:
                return ffprobe_result

        if suffix.casefold().lstrip(".") in {"wav", "wave"}:
            wave_result = _inspect_wave(file_path, cancel_callback)
            if wave_result.status in {"complete", "partial"}:
                return wave_result
            if ffprobe_result is not None:
                return _combine_failures(ffprobe_result, wave_result)
            return wave_result

        if ffprobe_result is not None:
            return ffprobe_result
        return MediaMetadata(
            status="unavailable",
            media_kind=media_kind,
            message="ffprobe was not available during this scan.",
        )

    def _inspect_with_ffprobe(
        self,
        path: Path,
        media_kind: str,
        cancel_callback: CancelCallback | None,
    ) -> MediaMetadata:
        assert self.ffprobe_path is not None
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            (
                "format=format_name,duration,bit_rate:"
                "stream=codec_type,codec_name,width,height,sample_rate,channels,duration,bit_rate"
            ),
            "-i",
            os.fspath(path),
        ]
        popen_options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name == "nt":
            popen_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            process = subprocess.Popen(command, **popen_options)
        except (OSError, ValueError) as exc:
            return MediaMetadata(
                status="unavailable",
                media_kind=media_kind,
                source="ffprobe",
                message=_detail(f"ffprobe could not be started: {exc}"),
            )

        deadline = time.monotonic() + self.ffprobe_timeout_seconds
        while True:
            if cancel_callback and cancel_callback():
                _stop_process(process)
                raise MediaInspectionCancelled("Media inspection cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                return MediaMetadata(
                    status="failed",
                    media_kind=media_kind,
                    source="ffprobe",
                    message=(
                        "ffprobe timed out after "
                        f"{self.ffprobe_timeout_seconds:g} seconds."
                    ),
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                _stop_process(process)
                return MediaMetadata(
                    status="failed",
                    media_kind=media_kind,
                    source="ffprobe",
                    message=_detail(f"ffprobe communication failed: {exc}"),
                )

        _raise_if_cancelled(cancel_callback)
        stdout_text = _decode_process_output(stdout)
        stderr_text = _decode_process_output(stderr)
        if process.returncode != 0:
            detail = stderr_text or f"ffprobe exited with code {process.returncode}."
            return MediaMetadata(
                status="failed",
                media_kind=media_kind,
                source="ffprobe",
                message=_detail(detail),
            )

        try:
            payload = json.loads(stdout_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return MediaMetadata(
                status="failed",
                media_kind=media_kind,
                source="ffprobe",
                message=_detail(f"ffprobe returned invalid metadata: {exc}"),
            )
        if not isinstance(payload, Mapping):
            return MediaMetadata(
                status="failed",
                media_kind=media_kind,
                source="ffprobe",
                message="ffprobe returned metadata in an unexpected format.",
            )
        return _metadata_from_ffprobe(payload, media_kind)

def _inspect_image(
    path: Path,
    cancel_callback: CancelCallback | None,
) -> MediaMetadata:
    reader = QImageReader(os.fspath(path))
    size = reader.size()
    _raise_if_cancelled(cancel_callback)
    image_format = bytes(reader.format()).decode("ascii", "replace").casefold() or None
    if size.isValid() and size.width() > 0 and size.height() > 0:
        return MediaMetadata(
            status="complete",
            media_kind="image",
            source="qt-image",
            container_format=image_format,
            width=size.width(),
            height=size.height(),
        )

    message = _detail(reader.errorString() or "Qt could not read the image header.")
    status = "unavailable" if "unsupported" in message.casefold() else "failed"
    return MediaMetadata(
        status=status,
        media_kind="image",
        source="qt-image",
        container_format=image_format,
        message=message,
    )


def _inspect_wave(
    path: Path,
    cancel_callback: CancelCallback | None,
) -> MediaMetadata:
    try:
        with wave.open(os.fspath(path), "rb") as reader:
            _raise_if_cancelled(cancel_callback)
            channels = _positive_int(reader.getnchannels())
            sample_rate = _positive_int(reader.getframerate())
            frames = max(0, int(reader.getnframes()))
            sample_width = _positive_int(reader.getsampwidth())
            compression_type = str(reader.getcomptype() or "").strip()
            _raise_if_cancelled(cancel_callback)
    except MediaInspectionCancelled:
        raise
    except (EOFError, OSError, wave.Error, ValueError) as exc:
        return MediaMetadata(
            status="failed",
            media_kind="audio",
            source="python-wave",
            container_format="wav",
            message=_detail(f"The WAV header could not be read: {exc}"),
        )

    duration_ms = None
    if sample_rate:
        duration_ms = min(
            _SQLITE_INTEGER_MAX,
            (frames * 1000 + sample_rate // 2) // sample_rate,
        )
    bit_rate = None
    if sample_rate and channels and sample_width:
        bit_rate = min(
            _SQLITE_INTEGER_MAX,
            sample_rate * channels * sample_width * 8,
        )
    codec = _wave_codec(compression_type, sample_width)
    status = "complete" if duration_ms is not None and channels and sample_rate else "partial"
    message = "" if status == "complete" else "The WAV header contained incomplete metadata."
    return MediaMetadata(
        status=status,
        media_kind="audio",
        source="python-wave",
        container_format="wav",
        duration_ms=duration_ms,
        audio_codecs=(codec,) if codec else (),
        sample_rate_hz=sample_rate,
        channels=channels,
        bit_rate=bit_rate,
        message=message,
    )


def _metadata_from_ffprobe(payload: Mapping[str, object], media_kind: str) -> MediaMetadata:
    raw_format = payload.get("format")
    format_values = raw_format if isinstance(raw_format, Mapping) else {}
    container = _clean_optional_text(format_values.get("format_name"))
    duration_ms = _seconds_to_ms(format_values.get("duration"))
    bit_rate = _positive_int(format_values.get("bit_rate"))

    raw_streams = payload.get("streams")
    streams: Sequence[object] = raw_streams if isinstance(raw_streams, list) else ()
    video_codecs: list[str] = []
    audio_codecs: list[str] = []
    stream_durations: list[int] = []
    stream_bit_rates: list[int] = []
    width = height = sample_rate = channels = None
    for raw_stream in streams:
        if not isinstance(raw_stream, Mapping):
            continue
        stream_type = _clean_optional_text(raw_stream.get("codec_type"))
        codec = _clean_optional_text(raw_stream.get("codec_name"))
        stream_duration = _seconds_to_ms(raw_stream.get("duration"))
        if stream_duration is not None:
            stream_durations.append(stream_duration)
        stream_bit_rate = _positive_int(raw_stream.get("bit_rate"))
        if stream_bit_rate is not None:
            stream_bit_rates.append(stream_bit_rate)
        if stream_type == "video":
            if codec and codec not in video_codecs:
                video_codecs.append(codec)
            if width is None:
                width = _positive_int(raw_stream.get("width"))
            if height is None:
                height = _positive_int(raw_stream.get("height"))
        elif stream_type == "audio":
            if codec and codec not in audio_codecs:
                audio_codecs.append(codec)
            if sample_rate is None:
                sample_rate = _positive_int(raw_stream.get("sample_rate"))
            if channels is None:
                channels = _positive_int(raw_stream.get("channels"))

    if duration_ms is None and stream_durations:
        duration_ms = max(stream_durations)
    if bit_rate is None and stream_bit_rates:
        bit_rate = min(_SQLITE_INTEGER_MAX, sum(stream_bit_rates))

    facts_present = any(
        (
            container,
            duration_ms is not None,
            width,
            height,
            video_codecs,
            audio_codecs,
            sample_rate,
            channels,
            bit_rate,
        )
    )
    complete = {
        "image": bool(width and height),
        "audio": bool(
            duration_ms is not None
            and audio_codecs
            and sample_rate
            and channels
        ),
        "video": bool(duration_ms is not None and width and height and video_codecs),
    }[media_kind]
    status = "complete" if complete else "partial" if facts_present else "failed"
    message = ""
    if status == "partial":
        message = "ffprobe returned only part of the expected media metadata."
    elif status == "failed":
        message = "ffprobe did not report recognizable media metadata."
    return MediaMetadata(
        status=status,
        media_kind=media_kind,
        source="ffprobe",
        container_format=container,
        duration_ms=duration_ms,
        width=width,
        height=height,
        video_codecs=tuple(video_codecs),
        audio_codecs=tuple(audio_codecs),
        sample_rate_hz=sample_rate,
        channels=channels,
        bit_rate=bit_rate,
        message=message,
    )


def _seconds_to_ms(value: object) -> int | None:
    try:
        seconds = float(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    milliseconds = round(seconds * 1000)
    if milliseconds < 0 or milliseconds > _SQLITE_INTEGER_MAX:
        return None
    return int(milliseconds)


def _positive_int(value: object) -> int | None:
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if integer <= 0 or integer > _SQLITE_INTEGER_MAX:
        return None
    return integer


def _wave_codec(compression_type: str, sample_width: int | None) -> str | None:
    if compression_type and compression_type != "NONE":
        return compression_type.casefold()
    if sample_width is None:
        return "pcm"
    bits = sample_width * 8
    return "pcm_u8" if bits == 8 else f"pcm_s{bits}le"


def _clean_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text or None


def _decode_process_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _detail(value: object) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_DETAIL_LENGTH:
        return text
    return f"{text[: MAX_DETAIL_LENGTH - 1].rstrip()}…"


def _raise_if_cancelled(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback and cancel_callback():
        raise MediaInspectionCancelled("Media inspection cancelled.")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _prefer_useful_failure(
    first: MediaMetadata,
    second: MediaMetadata,
) -> MediaMetadata:
    if second.status != "unavailable":
        return second
    return first


def _combine_failures(
    ffprobe_result: MediaMetadata,
    wave_result: MediaMetadata,
) -> MediaMetadata:
    message = _detail(
        "; ".join(
            value
            for value in (ffprobe_result.message, wave_result.message)
            if value
        )
    )
    return MediaMetadata(
        status="failed",
        media_kind="audio",
        source="ffprobe + python-wave",
        container_format="wav",
        message=message or "Neither ffprobe nor the WAV reader could read the file.",
    )
