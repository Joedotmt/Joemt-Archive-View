# JVVV

JVVV is a small desktop catalogue application inspired by Virtual Volumes View.
It scans removable drives or folders into a local SQLite database so their
contents can be browsed and searched later, even while the original drive is
disconnected.

The MVP focuses on reliable scanning, offline browsing, volume statistics, and
fast search. It does not generate thumbnails, previews, or use any server/cloud
component.

## Features

- Create, edit, delete, scan, and rescan catalogue volumes.
- Store folder structure and file metadata in SQLite.
- Browse indexed folders and files offline.
- Search by filename, partial filename, extension, folder name, and relative
  path across all volumes.
- Show connected/offline status, capacity, used/free space, indexed item counts,
  last scan time, and scan logs.
- Run scans on a Qt worker thread so the interface remains responsive.
- Cancel scans and record inaccessible files/folders as scan errors.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate the environment with:

```powershell
.venv\Scripts\activate
```

## Running

```bash
python -m jvvv
```

The database is stored in the standard application-data directory for your
operating system:

- Windows: `%APPDATA%\JVVV\jvvv.sqlite3`
- macOS: `~/Library/Application Support/JVVV/jvvv.sqlite3`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/jvvv/jvvv.sqlite3`

For development or tests, you can override the database path:

```bash
JVVV_DB_PATH=/tmp/jvvv-dev.sqlite3 python -m jvvv
```

## Usage

1. Click **Add Volume**.
2. Enter a catalogue name and choose a connected drive or folder.
3. Select the new volume and click **Scan**.
4. Browse the saved folder tree and file list after the scan completes.
5. Use the search bar to search across all indexed volumes.
6. Use **Rescan** to refresh an existing catalogue.

When rescanning, the app asks whether removed files should be deleted from the
catalogue or marked as missing.

If a result belongs to a connected volume, use the result buttons to open the
real file or reveal it in the operating system file manager.

## Tests

```bash
pytest
```

The automated tests cover database initialization, volume operations, scanning,
rescanning, missing-file handling, and search.

## Packaging With PyInstaller

Install PyInstaller in your virtual environment:

```bash
pip install pyinstaller
```

Build a one-folder application:

```bash
pyinstaller --name JVVV --windowed --collect-all PySide6 jvvv_app.py
```

For a single executable:

```bash
pyinstaller --name JVVV --onefile --windowed --collect-all PySide6 jvvv_app.py
```

The generated application will be in `dist/`. The SQLite database is still
created in the user's standard application-data directory at runtime.
