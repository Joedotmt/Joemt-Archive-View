from __future__ import annotations

import json
import os
import subprocess
import wave

import pytest
from PIL import Image

from jvvv import media_metadata as media_module
from jvvv.media_metadata import (
    MediaInspectionCancelled,
    MediaMetadata,
    MediaMetadataExtractor,
    media_kind_for_extension,
)


class FinishedProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode


class HangingProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired("ffprobe", timeout)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_media_kind_detection_is_case_insensitive_and_non_media_is_none():
    assert media_kind_for_extension(".JPG") == "image"
    assert media_kind_for_extension("WAV") == "audio"
    assert media_kind_for_extension(".mKv") == "video"
    assert media_kind_for_extension("txt") is None
    assert media_kind_for_extension(None) is None


def test_non_media_file_does_not_create_a_status_record(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not media", encoding="utf-8")

    result = MediaMetadataExtractor(discover_ffprobe=False).inspect(path)

    assert result is None


def test_pillow_collects_image_dimensions_without_optional_tools(tmp_path):
    path = tmp_path / "frame.png"
    Image.new("RGB", (13, 7), (0x33, 0x66, 0x99)).save(path, "PNG")

    result = MediaMetadataExtractor(discover_ffprobe=False).inspect(path)

    assert result == MediaMetadata(
        status="complete",
        media_kind="image",
        source="pillow",
        container_format="png",
        width=13,
        height=7,
    )


def test_image_dimensions_follow_exif_orientation(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (200, 100), (10, 10, 10)).save(path, "JPEG", exif=exif.tobytes())

    result = MediaMetadataExtractor(discover_ffprobe=False).inspect(path)

    assert (result.width, result.height, result.container_format) == (100, 200, "jpeg")


def test_camera_raw_and_heic_are_image_media_with_dimensions(tmp_path):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from preview_fixtures import write_dng, write_heic

    dng = write_dng(tmp_path / "shot.dng", 96, 64, orientation=6)
    heic = write_heic(tmp_path / "phone.heic", 120, 80)
    extractor = MediaMetadataExtractor(discover_ffprobe=False)

    raw_result = extractor.inspect(dng)
    heic_result = extractor.inspect(heic)

    assert raw_result == MediaMetadata(
        status="complete", media_kind="image", source="pillow", container_format="dng", width=64, height=96
    )
    assert heic_result == MediaMetadata(
        status="complete", media_kind="image", source="pillow", container_format="heif", width=120, height=80
    )
    assert media_kind_for_extension(".CR3") == "image"
    assert media_kind_for_extension("heif") == "image"


def test_unreadable_image_returns_an_explicit_status_instead_of_raising(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not a jpeg")

    result = MediaMetadataExtractor(discover_ffprobe=False).inspect(path)

    assert result is not None
    assert result.media_kind == "image"
    assert result.status in {"unavailable", "failed"}
    assert result.source == "pillow"
    assert result.message


def test_wave_fallback_collects_duration_codec_and_audio_shape(tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00" * 800)

    result = MediaMetadataExtractor(discover_ffprobe=False).inspect(path)

    assert result == MediaMetadata(
        status="complete",
        media_kind="audio",
        source="python-wave",
        container_format="wav",
        duration_ms=100,
        audio_codecs=("pcm_s16le",),
        sample_rate_hz=8000,
        channels=1,
        bit_rate=128000,
    )


def test_audio_without_a_builtin_reader_explains_missing_ffprobe(tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"")

    result = MediaMetadataExtractor(discover_ffprobe=False).inspect(path)

    assert result is not None
    assert result.status == "unavailable"
    assert result.media_kind == "audio"
    assert result.source == ""
    assert result.message == "ffprobe was not available during this scan."


def test_ffprobe_uses_safe_argv_and_normalizes_selected_metadata(monkeypatch, tmp_path):
    path = tmp_path / "name beginning with spaces.mp4"
    path.write_bytes(b"video placeholder")
    payload = {
        "format": {
            "format_name": "mov,mp4,m4a",
            "duration": "12.3456",
            "bit_rate": "7500000",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            },
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "audio", "codec_name": "ac3"},
        ],
    }
    calls = []

    def fake_popen(command, **options):
        calls.append((command, options))
        return FinishedProcess(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(media_module.subprocess, "Popen", fake_popen)

    result = MediaMetadataExtractor(
        "C:/Tools/ffprobe.exe",
        discover_ffprobe=False,
    ).inspect(path)

    assert result == MediaMetadata(
        status="complete",
        media_kind="video",
        source="ffprobe",
        container_format="mov,mp4,m4a",
        duration_ms=12346,
        width=1920,
        height=1080,
        video_codecs=("h264",),
        audio_codecs=("aac", "ac3"),
        sample_rate_hz=48000,
        channels=2,
        bit_rate=7500000,
    )
    command, options = calls[0]
    assert command[0] == "C:/Tools/ffprobe.exe"
    assert command[-2:] == ["-i", str(path)]
    assert options["shell"] is False
    assert options["stdin"] is subprocess.DEVNULL
    if os.name == "nt":
        assert options["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_ffprobe_uses_stream_duration_and_marks_incomplete_results_partial(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "track.flac"
    path.write_bytes(b"audio placeholder")
    payload = {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "flac",
                "duration": "2.5",
                "sample_rate": "not-known",
            }
        ]
    }
    monkeypatch.setattr(
        media_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FinishedProcess(json.dumps(payload).encode("utf-8")),
    )

    result = MediaMetadataExtractor("ffprobe", discover_ffprobe=False).inspect(path)

    assert result is not None
    assert result.status == "partial"
    assert result.duration_ms == 2500
    assert result.audio_codecs == ("flac",)
    assert result.sample_rate_hz is None


def test_ffprobe_failure_is_a_persistable_failure_record(monkeypatch, tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"broken")
    monkeypatch.setattr(
        media_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FinishedProcess(
            b"",
            b"Invalid data found when processing input\n",
            returncode=1,
        ),
    )

    result = MediaMetadataExtractor("ffprobe", discover_ffprobe=False).inspect(path)

    assert result is not None
    assert result.status == "failed"
    assert result.source == "ffprobe"
    assert result.message == "Invalid data found when processing input"
    assert result.as_db_values()["video_codec"] is None


def test_ffprobe_communication_error_stops_process_and_returns_failure(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"broken")
    process = HangingProcess()

    def communication_error(timeout=None):
        raise OSError("pipe read failed")

    process.communicate = communication_error
    monkeypatch.setattr(media_module.subprocess, "Popen", lambda *args, **kwargs: process)

    result = MediaMetadataExtractor("ffprobe", discover_ffprobe=False).inspect(path)

    assert result is not None
    assert result.status == "failed"
    assert result.source == "ffprobe"
    assert "communication failed" in result.message
    assert "pipe read failed" in result.message
    assert process.terminated or process.killed


def test_invalid_ffprobe_json_does_not_escape_as_an_exception(monkeypatch, tmp_path):
    path = tmp_path / "broken.mkv"
    path.write_bytes(b"broken")
    monkeypatch.setattr(
        media_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FinishedProcess(b"not json"),
    )

    result = MediaMetadataExtractor("ffprobe", discover_ffprobe=False).inspect(path)

    assert result is not None
    assert result.status == "failed"
    assert "invalid metadata" in result.message


def test_ffprobe_timeout_stops_the_process_and_returns_failure(monkeypatch, tmp_path):
    path = tmp_path / "stuck.webm"
    path.write_bytes(b"stuck")
    process = HangingProcess()
    monkeypatch.setattr(media_module.subprocess, "Popen", lambda *args, **kwargs: process)

    result = MediaMetadataExtractor(
        "ffprobe",
        discover_ffprobe=False,
        ffprobe_timeout_seconds=0.001,
    ).inspect(path)

    assert result is not None
    assert result.status == "failed"
    assert "timed out" in result.message
    assert process.terminated or process.killed


def test_cancellation_stops_a_running_ffprobe_and_raises(monkeypatch, tmp_path):
    path = tmp_path / "long.mov"
    path.write_bytes(b"long")
    process = HangingProcess()
    monkeypatch.setattr(media_module.subprocess, "Popen", lambda *args, **kwargs: process)
    checks = {"count": 0}

    def cancelled():
        checks["count"] += 1
        return checks["count"] >= 2

    with pytest.raises(MediaInspectionCancelled):
        MediaMetadataExtractor("ffprobe", discover_ffprobe=False).inspect(
            path,
            cancel_callback=cancelled,
        )

    assert process.terminated or process.killed


def test_invalid_timeout_and_status_are_rejected():
    with pytest.raises(ValueError):
        MediaMetadataExtractor(discover_ffprobe=False, ffprobe_timeout_seconds=0)
    with pytest.raises(ValueError):
        MediaMetadata(status="mystery", media_kind="audio")
