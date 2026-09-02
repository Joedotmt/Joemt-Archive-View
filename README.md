# JVVV

JVVV is a small desktop catalogue application inspired by Virtual Volumes View.
It scans removable drives or folders into user-managed `.jvvv` catalogue files
so their contents can be browsed and searched later, even while the original
drive is disconnected.

The MVP focuses on reliable scanning, offline browsing, volume statistics, and
fast search. Optional offline previews (JPEG images and silent H.264 MP4 video
proxies) can be generated while scanning into a preview directory you choose;
they are opened with your operating system's default applications, never inside
JVVV. There is no server or cloud component.

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
- Optionally generate offline previews while scanning: JPEG previews for images
  and silent H.264 MP4 proxies for videos, stored under a user-chosen directory
  and named by each file's SHA-256 and preview profile so duplicates, renamed
  files, and several catalogues sharing one directory reuse a single preview.
  Every preview that could not be created is reported at the end of the scan.

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

JVVV catalogue backups do not include offline preview files. The per-file
preview status recorded in the catalogue is backed up, but the preview
directory itself may be many terabytes; to back up previews, copy the
configured preview directory separately with your normal file-copy or backup
software.

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

## Offline previews

Offline previews are disabled by default. To enable them, open **Settings >
Preferences > Offline Previews**, choose a preview storage directory (a local
disk, removable disk, mapped network drive, or UNC path; it does not need to sit
beside the `.jvvv` file), optionally point JVVV at a specific `ffmpeg`
executable if it is not on `PATH`, pick the image and video quality you want to
spend storage on, and tick **Generate offline previews while scanning**.

Ticking the box immediately proves the configuration works: JVVV writes and
removes a test file in the preview directory, encodes a tiny test image with the
Qt JPEG writer, starts FFmpeg, checks its version and its H.264 `libx264`
encoder, and encodes a tiny test video into the preview directory. If any step
fails, the box turns itself back off and a dialog states exactly what failed.
**Test Preview Configuration** runs the same checks without changing the
setting and reports the directory, free space, FFmpeg path and version, encoder
availability, both test encodes, and an overall PASS or FAIL. Changing the
directory or quality settings while previews are enabled re-validates the new
configuration before it becomes active; on failure the last known-good
configuration is restored and explained.

Preview quality is yours to choose within these ranges: image maximum
dimension 320–8192 px and JPEG quality 40–100; video 0.1–10 frames per second,
maximum height 120–2160 px, CRF 18–45, and the `ultrafast` … `slow` x264
presets. Each combination is a *preview profile* whose ID appears in Settings
(for example `jpeg-max1600-q82` and `h264-1fps-240p-crf35-veryfast`). Previews
live under `<preview directory>/images/<image profile>/<hh>/<sha256>.jpg` and
`<preview directory>/videos/<video profile>/<hh>/<sha256>.mp4`, where `hh` is
the first two hex characters of the file's SHA-256. Changing settings creates a
new profile directory; nothing from an older profile is deleted or converted
automatically, and changing the preview directory does not move existing
previews.

During a scan, previews are generated only after a file's SHA-256 has been
recorded and the file has passed the stability checks. An existing preview with
the same SHA-256 and profile is validated and reused; a corrupt one is
regenerated. Output is always written to a temporary file in the target
directory, validated (JPEG decode, or an MP4 structure check for a video
stream and duration), and only then renamed into place. Cancelling a scan stops
any running FFmpeg process and removes temporary files. Video proxies are
silent, use `yuv420p`, keep the original duration and aspect ratio, respect
rotation metadata, are never upscaled, and are written with `+faststart`.

Before every scan with previews enabled, JVVV re-checks the preview directory,
FFmpeg, and the image backend. If that preflight fails you can open Settings,
scan without previews just once (the scan report records why), or cancel. At
the end of a scan with previews enabled, an **Offline Preview Summary** shows
generated, reused, and failed counts for images and videos, the preview
directory, and the space written by the scan. If anything failed, the scan is
reported as **completed with preview errors** and **View Preview Failures**
lists every failed item with its stage and technical detail. If the preview
disk fills up or the directory becomes unavailable, generation stops for the
rest of that scan, catalogue indexing continues, and the report distinguishes
direct failures from previews that were not attempted. Preview failures never
affect the catalogue records, scan health, or copy-evidence analysis.

In the catalogue and search views, an image or video's context menu offers
**Open Preview** / **Play Preview** and **Reveal Preview** whenever the preview
file exists, even while the original volume is offline; the search tab also has
an **Open Preview** button. Both use the operating system's default image or
video application — JVVV never contains an embedded viewer or player.
Properties show whether a preview is available, its profile, dimensions,
duration, size, and location, or why the last scan could not create it.

**Catalogue > Preview Cache…** shows the preview directory, its free space, the
current profiles, and (after **Scan Preview Store**) how many image and video
previews it holds and how much space they use. **Show Unreferenced Previews**
lists files under the current profiles whose SHA-256 is not referenced by the
open catalogue. Because other catalogues may share the same directory, nothing
is deleted automatically: select the entries you want to remove and confirm
**Delete Selected Unreferenced Previews**.

## Tests

```bash
pytest
```

The automated tests cover database initialization and current-schema validation, volume
operations, streaming hashes, media inspection, scan cancellation/change review
and rollback, hash-first backup evidence, semantic backup/restore integrity and
atomicity, offline browsing, search, and the offline preview system (profile
IDs, preview-root validation, image and video generation, atomic output,
cancellation, failure reporting, scanner integration, and the Settings UI).
Tests that need a real FFmpeg are skipped unless `ffmpeg` is on `PATH` or the
`JVVV_TEST_FFMPEG` environment variable points at an executable.

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
