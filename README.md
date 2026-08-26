# JVVV

JVVV is a small desktop catalogue application inspired by Virtual Volumes View.
It scans removable drives or folders into user-managed `.jvvv` catalogue files
so their contents can be browsed and searched later, even while the original
drive is disconnected.

The MVP focuses on reliable scanning, offline browsing, volume statistics, and
fast search. It does not generate thumbnails, previews, or use any server/cloud
component.

## Features

- Create, edit, delete, and scan catalogue volumes.
- Store each catalogue as a single SQLite-backed `.jvvv` file.
- Browse indexed folders and files offline.
- Search by filename, partial filename, extension, and folder name across all
  volumes, with optional relative-path matching in **Settings > Preferences**.
- Choose a custom accent color, light or dark mode, and the default Adobe or
  optional Qt Fusion styling in **Settings > Preferences**. Appearance changes
  preview live and can be reset to the default Adobe theme.
- Show connected/offline status, capacity, used/free space, indexed item counts,
  last scan time, and scan logs.
- Run scans on a Qt worker thread so the interface remains responsive.
- Cancel scans and record inaccessible files/folders as scan errors.
- Analyse saved catalogue metadata for evidence that files and folders also
  exist on other registered drives, without reconnecting or rescanning those
  drives.
- Show explicit **Strong**, **Possible/Partial**, **None found**, and
  **Unknown/Outdated** copy-evidence states with the matching drive IDs and the
  exact metadata used for each conclusion.
- Report per-volume other-copy coverage, potential whole-drive copies, and the
  latest scan health. Successfully scanned empty drives are shown as
  **Empty / N/A**, not as unprotected or unhealthy.
- Keep expected protected Windows metadata warnings (such as
  `System Volume Information`) visible in the scan report without treating
  them as missing user content.

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

JVVV starts without opening a catalogue. Use **File > New Catalogue** to create
a `.jvvv` file, or **File > Open Catalogue** to open an existing one. The file
is a valid SQLite database and contains the full catalogue.

## Usage

1. Choose **File > New Catalogue** and save a `.jvvv` file.
2. Click **New Volume**.
3. Enter a volume name and choose a connected drive or folder. JVVV starts
   scanning when the volume is added.
4. Browse the saved folder tree and file list after the scan completes.
5. Use the search bar to search across all indexed volumes.
6. Use **Scan** again to refresh an existing catalogue.

Use **Catalogue > Backup Evidence** to compare records already stored in the
catalogue. This analysis reads the `.jvvv` database only: it does not access
source drives, read file contents, or calculate content checksums. A strong
match is therefore strong metadata evidence of another copy, not byte-for-byte
verification. Re-run the analysis when the application marks its results as
outdated after catalogue contents change.

The labels are deliberately conservative and explain their evidence in the
interface. **Strong** file evidence requires the normalized filename, exact
byte size, modified time, and parent path to agree on another drive.
**Possible** means only normalized filename and exact size agree. A folder is
**Complete** only when a bounded, same-named structure containing at least two
content files matches on one other drive and both scan denominators are
trustworthy. Renamed or partial structures remain Possible. Overly common or
competing metadata is shown as **Too common** rather than being guessed, and
known operating-system bookkeeping is excluded from coverage.

While a scan is running, use **Cancel Scan** beside the progress bar to cancel
it. Partial results are discarded, so the existing catalogue remains intact.

When an existing catalogue has changed, the app shows the added, changed, and
no-longer-present file counts and the indexed-size difference before applying
the update. Cancelling the confirmation leaves the existing catalogue intact.

If a result belongs to a connected volume, use the result buttons to open the
real file or reveal it in the operating system file manager.

## Tests

```bash
pytest
```

The automated tests cover database initialization, volume operations, scanning,
change review and rollback, and search.

## Packaging With PyInstaller

Install PyInstaller in your virtual environment:

```bash
pip install pyinstaller
```

Build a one-folder application:

```bash
pyinstaller --name JVVV --windowed --collect-all PySide6 --collect-data jvvv jvvv_app.py
```

For a single executable:

```bash
pyinstaller --name JVVV --onefile --windowed --collect-all PySide6 --collect-data jvvv jvvv_app.py
```

The generated application will be in `dist/`. Catalogue records are saved in
the `.jvvv` files users create or open.
