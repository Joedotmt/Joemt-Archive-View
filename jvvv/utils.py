from __future__ import annotations

import ntpath
import os
import platform
import subprocess
import time
from pathlib import Path


class VolumeEjectError(RuntimeError):
    pass


def format_size(num_bytes: int | None) -> str:
    value = float(num_bytes or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def percentage_full(used: int | None, capacity: int | None) -> int:
    if not capacity:
        return 0
    return max(0, min(100, round(((used or 0) / capacity) * 100)))


def relative_path_for_display(path: str) -> str:
    return path if path else "/"


def eject_volume_supported(source_path: str | Path | None) -> bool:
    if platform.system() != "Windows":
        return False

    drive_root = _windows_drive_root(source_path)
    if drive_root is None:
        return False

    system_drive = os.environ.get("SystemDrive", "").rstrip("\\/")
    drive_name = drive_root.rstrip("\\/")
    return not system_drive or drive_name.casefold() != system_drive.casefold()


def eject_volume(source_path: str | Path) -> str:
    if platform.system() != "Windows":
        raise VolumeEjectError("Volume ejection is only available on Windows.")

    drive_root = _windows_drive_root(source_path)
    if drive_root is None:
        raise VolumeEjectError("Only local drive-letter volumes can be ejected.")

    if not eject_volume_supported(source_path):
        raise VolumeEjectError(f"{drive_root} is the system drive and cannot be ejected from JVVV.")

    if not Path(drive_root).exists():
        raise VolumeEjectError(f"{drive_root} is not currently connected.")

    _request_windows_shell_eject(drive_root)
    _wait_for_windows_volume_disconnect(drive_root)
    return drive_root


def _windows_drive_root(source_path: str | Path | None) -> str | None:
    if not source_path:
        return None

    drive, _tail = ntpath.splitdrive(str(source_path))
    if not drive or drive.startswith("\\\\"):
        return None

    drive_name = drive.rstrip("\\/")
    return f"{drive_name}\\"


def _request_windows_shell_eject(drive_root: str) -> None:
    script = r"""
$ErrorActionPreference = 'Stop'
$drive = $args[0]
$shell = New-Object -ComObject Shell.Application
$computer = $shell.Namespace(17)
if ($null -eq $computer) {
    throw 'Unable to open the Windows computer shell namespace.'
}

$item = $computer.ParseName($drive)
if ($null -eq $item) {
    $normalized = $drive.TrimEnd('\')
    foreach ($candidate in @($computer.Items())) {
        $candidatePath = $null
        try {
            $candidatePath = $candidate.Path
        } catch {}
        if ($candidatePath -and $candidatePath.TrimEnd('\').Equals($normalized, [System.StringComparison]::OrdinalIgnoreCase)) {
            $item = $candidate
            break
        }
    }
}

if ($null -eq $item) {
    throw "Windows could not find $drive in This PC."
}

$verb = $null
foreach ($candidate in @($item.Verbs())) {
    $name = ($candidate.Name -replace '&', '').Trim()
    if ($name -match '(?i)eject|auswerfen|safely remove') {
        $verb = $candidate
        break
    }
}

if ($null -ne $verb) {
    $verb.DoIt()
} else {
    $item.InvokeVerb('Eject')
}
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, drive_root],
            capture_output=True,
            text=True,
            timeout=15,
            startupinfo=_windows_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except FileNotFoundError as exc:
        raise VolumeEjectError("PowerShell was not found, so Windows could not be asked to eject the volume.") from exc
    except subprocess.TimeoutExpired as exc:
        raise VolumeEjectError("Windows did not respond to the eject request in time.") from exc

    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        if not details:
            details = f"Windows rejected the eject request for {drive_root}."
        raise VolumeEjectError(details)


def _windows_startupinfo() -> subprocess.STARTUPINFO | None:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _wait_for_windows_volume_disconnect(drive_root: str) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if not Path(drive_root).exists():
            return
        time.sleep(0.25)

    raise VolumeEjectError(
        f"Windows did not eject {drive_root}. Close any open files or Explorer windows on that drive and try again."
    )


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
