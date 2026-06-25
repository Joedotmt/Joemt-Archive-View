from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


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
