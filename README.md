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
- Create a compact, lossless semantic backup as one versioned `.zip`, and
  restore it atomically to a normal `.jvvv` catalogue.
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
- Read every accessible regular file during a scan or rescan and store its full
  SHA-256 content hash. Hashing is streamed and cancellable; partial scan data
  is rolled back.
- Store image dimensions with the built-in Qt reader, WAV duration/audio details
  with Python's built-in reader, and broader audio/video duration, codec,
  dimensions, sample-rate, channel, and bit-rate details when `ffprobe` is
  available.
- Analyse saved catalogue hashes and metadata for evidence that files and folders
  also exist on other registered drives, without reconnecting or rescanning them.
- Show explicit **Hash verified**, **Strong metadata**, **Possible/Partial**,
  **None found**, and
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
is a valid SQLite database and contains the full catalogue. This build opens
only its current `.jvvv` schema and does not migrate catalogues from older
schema versions.

Use **File > Create Catalogue Backup…** to save the open catalogue as a single
ZIP. The backup stores irreducible catalogue state (including IDs,
relationships, paths, hashes, media observations, user metadata, scan history,
and ID high-water marks) while omitting only data JVVV can reconstruct safely,
such as ordinary indexes, FTS search structures, canonical folder aggregates,
volume counts, and current Backup Evidence results. Identical saved hash blobs
are deduplicated. Noncanonical aggregate values and stale/old-rule evidence are
retained as lossless exceptions rather than silently changed.

Use **File > Restore Catalogue from Backup…** to validate the manifest,
component inventory, SHA-256 checksum, SQLite integrity, schema, row counts, and
relationships before creating anything at the chosen destination. JVVV builds
and verifies a temporary catalogue, regenerates omitted data, and atomically
replaces the destination only after the complete restore succeeds. The result
is a normal `.jvvv` file.

## Usage

1. Choose **File > New Catalogue** and save a `.jvvv` file.
2. Click **New Volume**.
3. Enter a volume name and choose a connected drive or folder. JVVV starts
   scanning when the volume is added.
4. Browse the saved folder tree and file list after the scan completes.
5. Use the search bar to search across all indexed volumes.
6. Use **Scan** again to refresh an existing catalogue.

Scanning now reads the complete contents of every accessible regular file and
recalculates its SHA-256, even when its size and timestamp appear unchanged.
This catches same-size changes and makes content identity independent of the
filename, folder, or date, but it also means scans can take roughly as long as
reading all data on the drive. The scan bar and Scan Log report hashing progress,
bytes read, and files whose hash could not be recorded. Cancelling or declining a
rescan restores the previously applied hashes and catalogue records atomically.
If a file keeps changing or disappears while it is being hashed, it is skipped
and reported as an incomplete scan area instead of saving a known-stale snapshot.

Media details are descriptive and are never used as file identity. Image sizes
and WAV details require no additional software. For broader audio/video details,
install FFmpeg and make its `ffprobe` executable available on `PATH`; otherwise
Properties explicitly says that those details were not collected. If a later
probe fails, earlier details are retained only when SHA-256 proves the content is
unchanged, and Properties labels them as partial with the latest failure reason.

Use **Catalogue > Backup Evidence** to compare records already stored in the
catalogue. This analysis reads the `.jvvv` database only: it does not access or
rescan source drives and does not reread file contents. **Hash verified** means
the full-file SHA-256 values previously recorded by scans are identical, even if
the files have different names, paths, or timestamps. Re-run the analysis when
the application marks its results as outdated after catalogue contents change.

The labels are deliberately conservative and explain their evidence in the
interface. When one of the two records has no comparable hash, **Strong
metadata** requires the normalized filename, exact byte size, modified time,
and parent path to agree; **Possible** means only normalized filename and exact
size agree. Two comparable but different hashes are never treated as a metadata
match. A folder is **Complete** only when a bounded, same-named, hash-aware
structure containing at least two content files matches on one other drive and
both scan denominators are trustworthy. Mixed hashed/legacy, renamed, or partial
structures remain Possible. Overly repetitive hashes or metadata are shown as
**Too common** rather than being guessed, and known operating-system bookkeeping
is excluded from coverage.

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

The automated tests cover database initialization and current-schema validation, volume
operations, streaming hashes, media inspection, scan cancellation/change review
and rollback, hash-first backup evidence, semantic backup/restore integrity and
atomicity, offline browsing, and search.

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
