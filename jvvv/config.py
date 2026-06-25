from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "JVVV"
DB_FILENAME = "jvvv.sqlite3"


def app_data_dir() -> Path:
    override = os.environ.get("JVVV_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        path = Path(root).expanduser() / APP_NAME if root else Path.home() / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        root = os.environ.get("XDG_DATA_HOME")
        path = Path(root).expanduser() / "jvvv" if root else Path.home() / ".local" / "share" / "jvvv"

    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db_path() -> Path:
    override = os.environ.get("JVVV_DB_PATH")
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return app_data_dir() / DB_FILENAME
