"""Persistent identity for a drive registered with JVVV."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from uuid import UUID


MARKER_NAME = ".jvvv-drive.json"
MARKER_IDENTITY = "jvvv-drive"


def read_drive_marker(root: Path) -> str:
    path = root / MARKER_NAME
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return ""
        with path.open("rb") as marker:
            data = json.loads(marker.read(1025))
        if data.get("format") == "jvvv-drive-v1":
            return str(UUID(data["volume_uuid"]))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return ""


def write_drive_marker(root: Path, volume_uuid: str) -> None:
    volume_uuid = str(UUID(volume_uuid))
    if read_drive_marker(root) == volume_uuid:
        return
    path = root / MARKER_NAME
    data = {"format": "jvvv-drive-v1", "volume_uuid": volume_uuid}
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
