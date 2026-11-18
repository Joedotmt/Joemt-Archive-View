# JVVV

JVVV is a small desktop catalogue application inspired by Virtual Volumes View.
It scans removable drives or folders into user-managed `.jvvv` catalogue folders
so their contents can be browsed and searched later, even while the original
drive is disconnected.

The MVP focuses on reliable scanning, offline browsing, volume statistics, and
fast search. Optional offline previews (JPEG images and silent H.264 MP4 video
proxies) can be generated while scanning into a preview directory you choose;
they are opened with your operating system's default applications, never inside
JVVV. There is no server or cloud component.

## Features

- Create, edit, delete, and scan catalogue volumes.
- Store each catalogue as a `.jvvv` folder: a small registry
  (`catalogue.sqlite`), one self-contained SQLite file per volume
  (`volumes/<uuid>.jvvvol`), and a rebuildable search/evidence index
  (`index.sqlite`).
- Keep a version history in a content-addressed snapshot repository
  (`.jvvvrepo`): a snapshot after rescanning one drive writes only that drive's
  data, and any snapshot can be used to create a new catalogue.
- Browse indexed folders and files offline.
- Search by filename, partial filename, extension, and folder name across all
  volumes, with optional relative-path matching in **Window > App Settings**.
- Choose a custom accent color, light or dark mode, and the default Adobe or
  optional Qt Fusion styling in **Window > App Settings**. Appearance changes
  preview live and can be reset to the default Adobe theme.
- Show connected/offline status, capacity, used/free space, indexed item counts,
  last scan time, and scan logs.
- Run scans on a Qt worker thread so the interface remains responsive.
- Cancel scans and record inaccessible files/folders as scan errors.
- Read every accessible regular file during a scan or rescan and store its full
  content hash (SHA-256, BLAKE3, or XXH3-128). Hashing is streamed and cancellable; partial scan data
  is rolled back.
- Store image dimensions with the built-in Qt reader, WAV duration/audio details
  with Python's built-in reader, and broader audio/video duration, codec,
  dimensions, sample-rate, channel, and bit-rate details when `ffprobe` is
  available.
- Analyse saved catalogue hashes and metadata for evidence that files and folders
  also exist on other registered drives, without reconnecting or rescanning them.
- Show explicit copy-evidence states that answer "does this exist on another
  drive, and how sure are we?": aqua **Hash verified** (proven identical by
  content hash), green **Same size & time** (contents not compared), amber **Name and
  size** and **Content differs** for files; **Full match (hashed / partly hashed
  / no hashes)**, **All files hash-verified / All files found**, **Found by name
  & size only**, **Partial**, and **Not confirmed · structure repeats** for
  folders; red **No copy found**; and grey **Not analysed / Outdated / Can't
  tell**. Each label carries the matching drive IDs, a plain-language "Why", and
  the exact metadata used for the conclusion.
- Report per-volume other-copy coverage, potential whole-drive copies, and the
  latest scan health. Successfully scanned empty drives are shown as
  **N/A · empty** (and drives holding only system files as **N/A · system files
  only**), not as unprotected or unhealthy.
- Keep expected protected Windows metadata warnings (such as
  `System Volume Information`) visible in the scan report without treating
  them as missing user content.
- Optionally generate offline previews while scanning: JPEG previews for images
  and silent H.264 MP4 proxies for videos, stored under a user-chosen directory
  and named by each file's content hash, algorithm, and preview profile so duplicates, renamed
  files, and several catalogues sharing one directory reuse a single preview.
  On Python 3.12 or newer, scans and project archives use PyAV to seek to
  preview frames when the video's timing and keyframe spacing allow it.
  When exact seeking is expensive, JVVV can use a nearby keyframe within two
  seconds of the intended sample. Adjacent samples can show the same frame.
  The preview keeps its configured frame rate and duration.
  JVVV falls back to its regular FFmpeg encoder for unsuitable files.
  Recognized video containers include MXF, Insta360 INSV and MPEG transport streams.
  Multi-camera files use the first video track for their preview.
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

JVVV starts on **Home**. Choose **Open Catalogue…**, **Create Empty Catalogue…**,
or **Create New Catalogue from Repository…**. These actions are available only
on Home. The recent list remembers the last eight successfully opened catalogues
on this computer, most recent first, and shows their names and full locations.
Double-click an entry or use **Open Selected**; **Remove from List** only forgets
the entry and leaves the catalogue on disk. Offline locations stay in the list.

An open catalogue has its own workspace, with its name and location at the top.
Use **Close Catalogue / Return Home** to return Home before opening or creating another
catalogue. Catalogue actions and Catalogue Settings appear only in the workspace;
App Settings, Help, and UI zoom remain available on both screens.

The Selected Volume panel shows connector type, connection, catalogue status,
condition, capacity, used/free space, indexed file/folder counts, last scan,
scan path, and other-copy coverage. The file list has its own **Filter files by
copy status** control; **All items** shows everything in the current folder.

The top bar has three menus: **File** for catalogue and volume actions, **View**
for zoom, and **Window** for settings, managers, information, and Help. On Home,
Window contains only **App Settings** and **Help**. With a catalogue open, it also
offers **Catalogue Settings**, **Snapshot Manager**, **Backup Evidence**,
**Preview Manager**, and **Catalogue Information**. File keeps catalogue
opening and creation actions on Home; the workspace instead offers **Add Volume…**,
**Open Catalogue Folder**, and **Close Catalogue / Return Home**.

### Catalogue folder layout

```text
MyArchive.jvvv/
    catalogue.sqlite          registry: catalogue UUID and volume id <-> UUID
    volumes/<uuid>.jvvvol     one self-contained SQLite file per volume
    index.sqlite              search index, folder aggregates and Backup Evidence
```

`catalogue.sqlite` and the volume files are the source of truth. Every change
to a volume (a scan, a rename, an edited register entry, a new scan-history
row) is written to that volume's file *before* the index commits, and every
volume file carries a revision number and the SHA-256 digests of its canonical
content. `index.sqlite` is a cache: on every open JVVV compares each volume
file's revision with the one the index last synchronised and re-imports any
volume that differs. Delete `index.sqlite` and it is rebuilt completely from
the other two (Backup Evidence has to be analysed again afterwards, because it
is derived data). A volume file that is missing from `volumes/` is reported
and the catalogue refuses to open until it is put back or restored from a
snapshot; a volume file that is not registered is left alone and listed under
**Window > Catalogue Information**.

### Snapshots: version history and backup

When returning Home or exiting the app, JVVV offers
**Create Snapshot & Close**, **Close Without Snapshot**, and **Cancel** if the
catalogue differs from its latest snapshot (or has no snapshots yet). Changes
remain saved in the local catalogue even when you close without a snapshot.
Creating a snapshot keeps the catalogue open until it succeeds; cancelling or
a failure leaves the catalogue open. If the repository is offline or cannot
be checked, the prompt explains that its snapshot status is unknown.

Open **Window > Snapshot Manager** and choose **Create Snapshot…** to record the catalogue in its snapshot
repository, a `.jvvvrepo` folder that by default sits next to the catalogue.
If you later move or rename that folder, the catalogue shows the repository as
offline and **Snapshot Manager > Locate Repository…** points it at the new location — the
app lists matching repositories it finds nearby and refuses folders that belong
to another catalogue. Opening a catalogue checks its repository and offers
**Locate Repository…** or **Continue Without Repository** when it is unavailable.
Snapshot Manager also checks when opened; dismissing the prompt avoids repeated
warnings during the same catalogue session. If the repository's drive was just
disconnected, reconnect it and choose **Check Again**. Locate Repository is hidden
when the repository is available. A catalogue restored from a snapshot comes out linked to
the repository it was restored from. A snapshot is a small JSON
manifest listing every volume with its metadata row and the digests of two
blobs: its *content* (folders, files, media details, preview status) and its
*scan history*. Blobs are stored once, named by the SHA-256 of their canonical
stream, so a volume whose contents did not change since the last snapshot is
recorded without being read. Rescanning one drive and taking a snapshot writes
one new blob of a few megabytes in a couple of seconds; taking twenty snapshots
of an unchanged archive costs twenty manifests.

**Window > Snapshot Manager** lists every snapshot with what changed since the
previous one (drives added, removed, rescanned, or whose details were edited),
shows the drives inside a snapshot, and has three sections:

- **Repository** shows its location and availability. **Repository Tools** contains
  **Reclaim Unused Space…** (space held only by deleted snapshots) and
  **Verify Repository…** (re-hash every blob and check that every snapshot is complete).
- **Snapshots** contains **Create Snapshot…** and **Delete Selected Snapshot…**.
  A catalogue without snapshots offers **Create First Snapshot…**.
- **Volumes in the selected snapshot** shows the saved volume names, counts,
  and changes for inspection.

On **Home**, choose **Create New Catalogue from Repository…**: choose a repository
folder, pick a snapshot (the latest is selected by default), and choose a new
catalogue name and location. Review the repository, snapshot, and destination,
then choose **Create and Open Catalogue**. Existing folders and files cannot be
overwritten by this workflow. Every blob is verified against its digest while
the volume files are rebuilt, and the search index is regenerated. The new
catalogue opens automatically and appears in Recent catalogues. It re-snapshots
to exactly the same blobs as the original.

Snapshots do not include offline preview files. The per-file preview status
recorded in the catalogue is included, but the preview directory itself may be
many terabytes; to back up previews, copy the configured preview directory
separately with your normal file-copy or backup software.

## Usage

1. On **Home**, choose **Create Empty Catalogue…** and name the `.jvvv` folder.
2. Choose **File > Add Volume…**.
3. Choose a connected drive or folder and assign its drive ID. JVVV queues an
   initial scan. Add more drives if needed, then click **Apply** beside the
   catalogue name.
4. Browse the saved folder tree and file list after the scan completes.
   Double-click the volume to edit its details.
5. Use the search bar to search across all indexed volumes.
6. Right-click a volume and use **Queue Scan** to refresh it. JVVV uses the
   saved volume identity and resolves its current path automatically.

### Run unattended operations

**Queue Manager** has **Pending**, **Failed**, and **Completed** tabs. The
**Pending** tab also shows running, waiting, and paused jobs, with each job's
current status in its row. Right-click a job to move or remove it, or to retry
a failed job. Click **Apply** in Queue Manager or beside the catalogue
name. When several jobs can be applied, both buttons say **Apply All**. Up to
two independent jobs can run together. Work on the same destination disk,
including different partitions on that disk, runs in sequence. Archives can
share read-only access to the QNAP source drive when using separate destinations.
Queue Manager shows progress for each active job. A disconnected drive waits
while independent jobs continue. Results and warnings are saved in the queue.
The catalogue keeps its previous scan if indexing is incomplete.

The queue is saved locally with the catalogue. After reopening it, click
**Apply** to continue. **Pause Queue** stops active jobs and holds the queue.
Right-click a running job and choose **Pause Job** to pause it individually.
A paused scan restarts, and an archive resumes from its
saved copy progress. Right-click a job in **Failed** and choose **Retry**,
then apply the queue.

After the first successful scan of a writable drive root, JVVV creates or
reuses `.jvvv-drive.json` to recognize the drive if Windows reports a different
identity. Protected `Q:\` and folders below a drive root are excluded from
registration. If several connected drives carry the same ID, disconnect the
duplicate. Use **Relink Drive…** when an existing volume needs to be matched again.

### Archive a project to an HDD

Add the archive drive from its root, even if the drive is empty. Right-click
its volume and choose **Archive Project…**, then pick the source project
folder. You can also choose a destination from **File > Archive Project…**.
If the initial scan is still queued, JVVV runs it before starting the archive.

JVVV copies the project folder to the root of that drive and hashes the source
bytes as it copies them. It reads the destination back to verify the copy and
adds a project marker such as `Project Name.jvvv-project.json` inside the
copied folder. The project appears under the selected volume in the catalogue
as soon as verification finishes, without a full drive scan.

When two approved projects are queued for the same HDD, JVVV can prepare the
next project while verifying the first. It reads and hashes the next source into
available RAM until the destination is ready. JVVV reserves one eighth of
installed memory (at least 1 GiB) for other work and checks available memory as it
reads. Preview staging uses the same memory policy and can spill to temporary
storage under pressure. Complete buffered media files can start preview
processing when the temporary workspace and preview store are on separate
storage from the destination. During read-ahead, video previews use a temporary
file so the same video only occupies one buffer in RAM. Queue Manager shows the preparation progress.
The next project starts writing after the previous archive finishes successfully,
then receives its own full read-back verification. If the earlier archive is
paused or fails, the waiting project pauses too. Read-ahead is used when JVVV
can identify the destination disk and read the next project independently.

**Pause Queue** leaves progress saved in the catalogue and a temporary archive
folder on the destination drive. Reconnect the same drive and click **Apply**
beside the catalogue name to resume. Previously copied files are checked before publication. If the
job was paused, JVVV also rechecks the corresponding source files before reusing
those transfers. Start a new job if the source changed. Keep other apps that
edit the project closed until the archive completes. On Windows, the copy opens
existing source files with write and delete sharing disabled and stops if the
source tree changes. Adding a new file through another app can trigger that stop.

With offline previews enabled, JVVV reuses matching previews and makes missing
ones in the background from bytes captured during copying. For videos that fit
in available RAM, the media worker reads metadata and samples preview frames
directly from that buffer. JVVV uses a temporary file when memory is tight or
a decoder needs a file on disk. The archive destination, catalogue,
preview store and temporary workspace must be outside the protected Q: drive.

Choose **Window > Catalogue Settings > Catalogue hashing** before adding the first
volume. The dropdown offers **SHA-256** (the default), **BLAKE3**, and
**XXH3-128**. One algorithm applies to every drive in the catalogue and locks
permanently as soon as its first volume is added, even before scanning. Deleting
all volumes does not unlock it. Existing catalogues retain SHA-256; use a new
catalogue to choose another algorithm. The choice and lock also survive snapshot
restores and index rebuilds. SHA-256 and BLAKE3 are cryptographic; XXH3-128 is
non-cryptographic. A matching hash is extremely strong evidence of equal content,
but hash collisions are possible.

Scanning now reads the complete contents of every accessible regular file and
recalculates its content hash, even when its size and timestamp appear unchanged.
This catches same-size changes and makes content identity independent of the
filename, folder, or date, but it also means scans can take roughly as long as
reading all data on the drive. The scan bar and Scan Log report hashing progress,
bytes read, and files whose hash could not be recorded. Cancelling during indexing
or encountering an incomplete read preserves the previously applied hashes and catalogue records
atomically. Once indexing is committed, the catalogue is ready to browse while
the background worker publishes prepared previews and generates any remaining ones. **Cancel Previews** stops that
phase and keeps the updated catalogue and completed previews. The Scan Log reports
indexing time (including hashing, media inspection, and overlapping preview work),
time spent on previews after indexing, total time,
and the number of media records reused.
If a file keeps changing or disappears while it is being hashed, it is skipped
and reported as an incomplete scan area instead of saving a known-stale snapshot.

Media details are descriptive and are never used as file identity. Image sizes
(including HEIC/HEIF and camera RAW files, after EXIF orientation) and WAV
details require no additional software. PyAV can read video details from a captured
RAM buffer. For file-based audio/video inspection, install FFmpeg and make its
`ffprobe` executable available on `PATH`. Rescans reuse
complete media details only after a freshly calculated content hash indicates the content
is unchanged. Missing, partial, failed, or unavailable details are probed again.
If a retry fails, earlier useful details are retained only when the hash matches,
and Properties labels them as partial with the latest failure reason.

Use **Window > Backup Evidence** to compare records already stored in the
catalogue. This analysis reads the `.jvvv` database only: it does not access or
rescan source drives and does not reread file contents. **Hash verified** means
the full-file content hashes previously recorded by scans are identical, even if
the files have different names, paths, or timestamps. Re-run the analysis when
the application marks its results as outdated after catalogue contents change.

The colour of an item answers "does this exist on another drive, and how sure
are we?", never how the coverage arithmetic paired it, so two identical records
on the same drive always get the same label. Aqua with a double tick is
**Hash verified**: matching full-file content hashes. Green with a tick is
**Same size & time**: the normalized filename, exact byte size and modified time
(within 2 seconds, or an exact one-hour clock offset for names that are not
generic) agree, but contents were not compared. Amber with a dash is a lead to
look at: **Name and size** (the modified time differs, is missing, or is shared
by so many drives that it cannot single out copies; a name that is generic
across the catalogue is flagged as **· generic name**, but only when no modified
time agrees within 2 seconds on a bounded number of drives, since a time match
on that scale still counts as **Same size & time**; the one-hour offset is not
applied to generic names) or **Content differs** (two
comparable hashes disagree, so it is a different version or a damaged copy,
never a match). Red is **No copy found**;
grey is reserved for **Not analysed / Outdated / Can't tell**.

Folders follow the same tiers. **Full match** means the folder name, layout, and
every file's name and size match on one other drive; the bracket says how much
content was compared: **(hashed)** is aqua, **(partly hashed)** and
**(no hashes)** are green. When a scan had errors or the folder is named
differently there, the cell says so explicitly (**· scan errors**,
**· different name there**). **All files hash-verified** (aqua) and **All files
found** (green) mean every file was found on one other drive, including
single-file folders, but the folder layout there differs; **Found by name & size
only** is amber; **Partial** shows percentages that never round up, so 100%
appears only when every file was found, and reads **Partial · leads only** when
the best drive holds files by name and size but none by modified time;
**Not confirmed · structure repeats** means the structure repeats on too many
other drives to pair copies safely. Each
folder is judged on its own and never rolled up from its subfolders, which is why
a parent can be Partial while every subfolder inside it is a Full match; the
status strip above the file list states this for the open folder. Known
operating-system bookkeeping is excluded from coverage. Green never means
hash-verified: a file's **Same size & time** compares name, size and modified
time, and a folder's **Full match** compares folder name, layout, file names and
sizes; only aqua labels prove contents identical. The whole-drive copies table in
the Backup Evidence dialog says **Full match (layout & sizes)** for the same
reason. The Properties dialog lists **Drives with any evidence** (drives with a
strong match or a lead; the first 64 for very common items, and the row says how
many there are in total), then breaks them down by tier: **Full-match
drives** for a Full match, or **Drives holding every file** when every file was
found there one by one; for a folder found by name and size only it reports how
many files were found rather than a percentage, because percentages count only
name-size-and-time matches.

While a scan is running, use **Cancel Scan** beside the progress bar to cancel
it. Partial results are discarded, so the existing catalogue remains intact.

When an existing catalogue has changed, the app shows the added, changed, and
no-longer-present file counts and the indexed-size difference before applying
the update. Cancelling the confirmation leaves the existing catalogue intact.

If a result belongs to a connected volume, use the result buttons to open the
real file or reveal it in the operating system file manager.

## Previews

Previews are disabled by default. To enable them, open **Window >
Preview Manager > Previews**, choose a preview storage directory (a local
disk, removable disk, mapped network drive, or UNC path; it does not need to sit
beside the `.jvvv` file), pick the image and video quality you want to spend
storage on, and tick **Generate previews while scanning**, then choose **Apply**. If FFmpeg is
not on `PATH`, set its executable under **Window > App Settings > FFmpeg** first.

Preview preferences (including FPS, resolution, quality, enabled state, and
storage directory) are saved in the catalogue's `catalogue.sqlite`, independently
for each catalogue, and preserved in snapshots. FFmpeg's executable path remains
in App Settings on this computer. The catalogue also stores each file's preview
profile ID and status. Preview files themselves are stored separately and are
not included in snapshots. Directory associations use relative paths where
possible, so moving a catalogue and its preview directory together keeps the
association. If the directory moves separately, choose its new location in
Preview Manager and Apply.

Existing catalogues import their previous computer-wide preview preferences on
their first writable open, then keep their own settings. New catalogues start
with previews disabled. Snapshots from older versions that did not save preview
preferences restore disabled defaults.

Preview Manager separates **Previews** configuration from **Storage & Cleanup**.
Resolution and video FPS are shown first; **Show Advanced** reveals image quality,
video quality, encoding speed, and profile IDs. Catalogue Settings contains only
catalogue hashing. FFmpeg configuration is available only in App Settings.

Ticking the box immediately proves the configuration works: JVVV writes and
removes a test file in the preview directory, checks that every image library is
present (Pillow with JPEG support, pillow-heif with an HEVC decoder for
HEIC/HEIF, and rawpy/LibRaw for camera RAW), encodes a tiny test image, starts
FFmpeg, checks its version and its H.264 `libx264` encoder, and encodes a tiny
test video into the preview directory. If any step fails, the box turns itself
back off and a dialog states exactly what failed; previews are never left in a
partially working enabled state.
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
the first two hex characters of the file's content hash. SHA-256 retains these
legacy filenames. BLAKE3 and XXH3-128 use `blake3-<hash>` and
`xxh3_128-<hash>` filenames respectively, keeping algorithms separate in a shared
preview directory. Changing settings creates a
new profile directory; nothing from an older profile is deleted or converted
automatically, and changing the preview directory does not move existing
previews.

Image previews are produced by one pipeline built on Pillow: JPEG, PNG, TIFF,
WebP, GIF (first frame), BMP and ICO directly; HEIC/HEIF (`.heic`, `.heif`,
`.hif`) through pillow-heif (libheif); and camera RAW through rawpy (LibRaw):
DNG, CR2, CR3, CRW, NEF, NRW, ARW, SR2, SRF, RAF, ORF, RW2, RWL, PEF, SRW, 3FR,
IIQ, MEF, MOS, MRW, ERF, KDC, DCR and X3F. For a RAW file JVVV first asks LibRaw
for the camera's embedded JPEG preview and uses it when its larger side is at
least as large as the preview needs; otherwise the sensor data is demosaiced
(at half size when that still covers the configured maximum). Every path then
applies EXIF orientation (or LibRaw's orientation flag), flattens transparency
onto neutral grey, downsizes to the configured maximum without ever upscaling,
and writes a JPEG that carries only the ICC profile — no EXIF, XMP, comments or
other source metadata. Decoding is refused above 600 megapixels.

When the scanner can identify both the temporary workspace and the preview store
on disks separate from the source, it captures media during hashing. One worker
prepares a missing preview while the scanner hashes the next file. Videos can use
the same RAM-backed input as **Archive Project**. Images and decoders that need
a file use local temporary storage. An unchanged file with a known cached preview
skips capture and still receives a full hash check.

Prepared previews remain private until indexing and change review finish. The scanner
then checks the original file's identity and timestamp before publishing each preview.
If capture is unavailable, the worker generates the preview from the original after
indexing. Jobs are recorded in a temporary disk queue. Every preview requires a recorded content hash and a
source that passed the stability checks; a source that changes
between hashing and preview generation is reported as a failure rather than
previewed under the old hash. If the preview directory lies inside the volume
being scanned it is skipped, so previews are never catalogued as archive
content. An existing preview with
the same content hash, algorithm, and profile is validated and reused; a corrupt one (including
an MP4 proxy truncated by an interrupted copy) is regenerated and the scan
report records how many were replaced. Output is always written to a temporary file in the target
directory, validated (JPEG decode, or an MP4 structure check for a video
stream and duration), and only then renamed into place. Cancelling a scan stops
any running FFmpeg process and removes temporary files. Video proxies are
silent, use `yuv420p`, keep the original duration and aspect ratio, respect
rotation metadata, are never upscaled, and are written with `+faststart`.

Before every queued scan with previews enabled, JVVV re-checks the preview
directory, FFmpeg, and the image backend. If the check fails, the scan runs
without previews and the queue reports the reason. The completed job shows
generated, reused, and failed counts. Right-click the most recent scan and
choose **Preview Failures** to see each failed item, its stage, and its
technical detail. If the preview
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

The **Previews** tab contains the directory setting and **Open Folder** alongside
generation preferences. **Window > Preview Manager > Storage & Cleanup** shows
free space, image/video counts and total usage after **Refresh Usage**.
**Show Profile Breakdown** expands the detailed storage figures.
**Find Unused Previews**
lists files under the current profiles whose content hash and algorithm are not referenced by the
open catalogue. Because other catalogues may share the same directory, nothing
is deleted automatically: select the entries you want to remove and confirm
**Delete Selected**. The list is cleared whenever the
catalogue changes, so it always has to be produced again before deleting.
**Clean Up Temporary Files** removes `.tmp-` files left behind by an interrupted
preview generation (a crash or power loss); files newer than 24 hours are kept
because another JVVV window sharing the directory may still be writing them.
Directory junctions or symbolic links inside the preview directory are never
followed. If the preview directory is disconnected, the cache manager reports
that instead of showing an empty store.

## Tests

```bash
pytest
```

The automated tests cover database initialization and current-schema validation,
the catalogue folder format (truth-first writes, revision tracking, index
rebuild, crash recovery of interrupted deletions, external volume-file changes,
canonical serialisation), the snapshot repository (deduplication, deltas,
full catalogue restore, prune, verify, cancellation), volume operations, streaming hashes,
media inspection, scan cancellation/change review and rollback, hash-first backup
evidence, offline browsing,
search, and the offline preview system (profile IDs, preview-root validation,
image generation for standard formats, HEIC and synthetic DNG camera RAW files
including embedded-preview extraction, demosaic fallback and orientation, video
generation, atomic output, cancellation, failure reporting, scanner integration,
and the Settings UI).
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
the `.jvvv` folders users create or open.
