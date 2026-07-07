from __future__ import annotations

from jvvv.app import connected_volume_signature, include_content_timestamp
from jvvv.utils import VolumeSnapshot


def test_content_date_guess_skips_invalid_timestamps():
    assert include_content_timestamp("2024-01-01", "2024-01-02", float("nan")) == (
        "2024-01-01",
        "2024-01-02",
    )


def test_connected_volume_signature_detects_identity_and_mount_root():
    snapshots = [
        VolumeSnapshot(
            source_path="E:\\",
            mount_root="E:\\",
            source_relative_path="",
            identity_kind="Windows-Volume-Guid",
            identity_token="\\\\?\\Volume{BBB}\\",
        ),
        VolumeSnapshot(
            source_path="D:\\",
            mount_root="D:\\",
            source_relative_path="",
            identity_kind="windows-volume-guid",
            identity_token="\\\\?\\Volume{AAA}\\",
        ),
        VolumeSnapshot(
            source_path="Z:\\",
            mount_root="Z:\\",
            source_relative_path="",
            identity_kind="",
            identity_token="",
        ),
    ]

    assert connected_volume_signature(snapshots) == (
        ("windows-volume-guid", "\\\\?\\volume{aaa}\\", "d:\\"),
        ("windows-volume-guid", "\\\\?\\volume{bbb}\\", "e:\\"),
    )
