from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import os
import platform
import posixpath
import subprocess
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VolumeSnapshot:
    source_path: str
    mount_root: str
    source_relative_path: str
    identity_kind: str
    identity_token: str
    identity_label: str = ""
    identity_serial: str = ""
    identity_filesystem: str = ""

    def as_db_fields(self) -> dict[str, str]:
        return {
            "identity_kind": self.identity_kind,
            "identity_token": self.identity_token,
            "identity_label": self.identity_label,
            "identity_serial": self.identity_serial,
            "identity_filesystem": self.identity_filesystem,
            "source_relative_path": self.source_relative_path,
        }


class ConnectedVolumeResolver:
    def __init__(self, snapshots: list[VolumeSnapshot] | None = None) -> None:
        self.snapshots = snapshots if snapshots is not None else list_connected_volume_snapshots()

    def resolve(self, volume: Any) -> str | None:
        kind = _record_value(volume, "identity_kind")
        token = _record_value(volume, "identity_token")
        if not kind or not token:
            return None

        relative = _record_value(volume, "source_relative_path") or ""
        for snapshot in self.snapshots:
            if snapshot.identity_kind == kind and snapshot.identity_token.casefold() == token.casefold():
                source_path = path_with_relative(snapshot.mount_root, relative)
                return source_path if Path(source_path).exists() else None

        source_path = _record_value(volume, "source_path")
        if source_path and Path(source_path).exists():
            snapshot = capture_volume_snapshot(source_path)
            if snapshot and snapshot.identity_kind == kind and snapshot.identity_token.casefold() == token.casefold():
                return snapshot.source_path
        return None


def format_size(num_bytes: int | None) -> str:
    value = float(num_bytes or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1000 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


def percentage_full(used: int | None, capacity: int | None) -> int:
    if not capacity:
        return 0
    return max(0, min(100, round(((used or 0) / capacity) * 100)))


def relative_path_for_display(path: str) -> str:
    return path if path else "/"


def capture_volume_snapshot(source_path: str | Path | None) -> VolumeSnapshot | None:
    if not source_path:
        return None
    path = Path(source_path).expanduser()
    if not path.exists():
        return None

    if platform.system() == "Windows":
        return _capture_windows_volume_snapshot(path)
    return _capture_local_path_snapshot(path)


def list_connected_volume_snapshots() -> list[VolumeSnapshot]:
    if platform.system() == "Windows":
        return _list_windows_volume_snapshots()
    return []


def resolve_volume_source_path(volume: Any) -> str | None:
    return ConnectedVolumeResolver().resolve(volume)


def volume_identity_known(volume: Any) -> bool:
    return bool(_record_value(volume, "identity_kind") and _record_value(volume, "identity_token"))


def path_with_relative(mount_root: str, relative_path: str) -> str:
    path = Path(mount_root)
    for part in posixpath.normpath(relative_path or "").split("/"):
        if part not in {"", "."}:
            path /= part
    return str(path)


def _record_value(record: Any, key: str) -> str:
    if record is None:
        return ""
    try:
        keys = record.keys()
    except AttributeError:
        keys = None
    if keys is not None:
        try:
            if key in keys:
                value = record[key]
                return "" if value is None else str(value)
        except (KeyError, IndexError, TypeError):
            return ""
    if isinstance(record, dict):
        value = record.get(key)
        return "" if value is None else str(value)
    value = getattr(record, key, "")
    return "" if value is None else str(value)


def _capture_local_path_snapshot(path: Path) -> VolumeSnapshot | None:
    try:
        resolved = path.resolve(strict=True)
        stat_result = resolved.stat()
    except OSError:
        return None
    token = str(getattr(stat_result, "st_dev", ""))
    if not token:
        return None
    return VolumeSnapshot(
        source_path=str(resolved),
        mount_root=str(resolved),
        source_relative_path="",
        identity_kind="filesystem-device",
        identity_token=token,
    )


def _list_windows_volume_snapshots() -> list[VolumeSnapshot]:
    snapshots: list[VolumeSnapshot] = []
    for drive_root in _windows_logical_drive_roots():
        snapshot = _capture_windows_volume_snapshot(Path(drive_root))
        if snapshot is not None and snapshot.identity_token:
            snapshots.append(snapshot)
    return snapshots


def _capture_windows_volume_snapshot(path: Path) -> VolumeSnapshot | None:
    source_path = str(path)
    mount_root = _windows_volume_path_name(source_path)
    if mount_root is None:
        return None

    label, serial, filesystem = _windows_volume_information(mount_root)
    guid_path = _windows_volume_guid_path(mount_root)
    if guid_path:
        identity_kind = "windows-volume-guid"
        identity_token = guid_path.casefold()
    elif serial:
        identity_kind = "windows-volume-serial"
        identity_token = f"{filesystem.casefold()}:{serial.casefold()}"
    else:
        identity_kind = ""
        identity_token = ""

    relative_path = _windows_source_relative_path(source_path, mount_root)
    return VolumeSnapshot(
        source_path=str(path),
        mount_root=mount_root,
        source_relative_path=relative_path,
        identity_kind=identity_kind,
        identity_token=identity_token,
        identity_label=label,
        identity_serial=serial,
        identity_filesystem=filesystem,
    )


def _windows_logical_drive_roots() -> list[str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = 256
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetLogicalDriveStringsW(size, buffer)
        if length == 0:
            return []
        if length < size:
            raw = buffer[:length]
            return [drive for drive in raw.split("\x00") if drive]
        size = length + 1


def _windows_volume_path_name(path: str) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    if not kernel32.GetVolumePathNameW(str(path), buffer, size):
        return None
    return buffer.value


def _windows_volume_guid_path(mount_root: str) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = 1024
    buffer = ctypes.create_unicode_buffer(size)
    if not kernel32.GetVolumeNameForVolumeMountPointW(mount_root, buffer, size):
        return ""
    return buffer.value


def _windows_volume_information(mount_root: str) -> tuple[str, str, str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    label_buffer = ctypes.create_unicode_buffer(261)
    fs_buffer = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32(0)
    max_component_length = ctypes.c_uint32(0)
    filesystem_flags = ctypes.c_uint32(0)
    ok = kernel32.GetVolumeInformationW(
        mount_root,
        label_buffer,
        len(label_buffer),
        ctypes.byref(serial),
        ctypes.byref(max_component_length),
        ctypes.byref(filesystem_flags),
        fs_buffer,
        len(fs_buffer),
    )
    if not ok:
        return "", "", ""
    serial_text = f"{serial.value:08X}" if serial.value else ""
    return label_buffer.value, serial_text, fs_buffer.value


def _windows_source_relative_path(source_path: str, mount_root: str) -> str:
    try:
        relative = ntpath.relpath(source_path, mount_root)
    except ValueError:
        return ""
    if relative in {"", "."}:
        return ""
    return relative.replace("\\", "/")


def open_in_file_manager(path: Path, reveal: bool = False) -> None:
    target = path if not reveal else path.parent
    if platform.system() == "Windows":
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        if reveal:
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
