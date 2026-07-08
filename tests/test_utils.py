from __future__ import annotations

from jvvv import utils


def test_connected_volume_resolver_matches_identity_not_saved_path(tmp_path):
    mounted = tmp_path / "current-drive"
    mounted.mkdir()
    (mounted / "Archive").mkdir()
    resolver = utils.ConnectedVolumeResolver(
        [
            utils.VolumeSnapshot(
                source_path=str(mounted),
                mount_root=str(mounted),
                source_relative_path="",
                identity_kind="test",
                identity_token="drive-b",
            )
        ]
    )

    old_drive_a = {
        "source_path": str(tmp_path / "old-letter"),
        "identity_kind": "test",
        "identity_token": "drive-a",
        "source_relative_path": "Archive",
    }
    current_drive_b = {
        "source_path": str(tmp_path / "old-letter"),
        "identity_kind": "test",
        "identity_token": "drive-b",
        "source_relative_path": "Archive",
    }

    assert resolver.resolve(old_drive_a) is None
    assert resolver.resolve(current_drive_b) == str(mounted / "Archive")
