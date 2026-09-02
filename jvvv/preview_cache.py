"""Hash-addressed offline preview store (spec §4, §6, §12, §14, §18, §22).

Layout under the user-selected preview root::

    <root>/images/<image-profile-id>/<hh>/<sha256hex>.jpg
    <root>/videos/<video-profile-id>/<hh>/<sha256hex>.mp4

``hh`` is the first two hex characters of the source file's SHA-256, so no
single directory accumulates a huge number of files.  Paths are fully
deterministic from (root, media kind, profile ID, SHA-256): two catalogues
that share a root share previews, and previews copied between disks are
recognised immediately (spec §25, §47).

This module is deliberately Qt-free.  It owns the exception types every other
preview module raises, the outcome/stage constants, the failure record, and
the store walker used by the Preview Cache Manager.  Nothing here ever loads
the whole store into memory: iteration streams ``os.scandir`` one directory
at a time and byte counts are plain Python integers (spec §18).
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import time
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jvvv.preview_config import (
    PREVIEW_EXTENSIONS,
    PREVIEW_MEDIA_KINDS,
    ImagePreviewProfile,
    PreviewConfigError,
    VideoPreviewProfile,
    preview_extension_for,
)
from jvvv.utils import format_size


CancelCallback = Callable[[], bool]
ProgressCallback = Callable[[int, str], None]

# Outcome states (spec §14 + §17).
PREVIEW_GENERATED = "generated"
PREVIEW_REUSED = "reused"
PREVIEW_FAILED = "failed"
PREVIEW_SKIPPED_DISABLED = "skipped-disabled"
PREVIEW_SKIPPED_UNSUPPORTED = "skipped-unsupported"
PREVIEW_SKIPPED_STORAGE = "skipped-storage-unavailable"
PREVIEW_STATUSES = frozenset(
    {
        PREVIEW_GENERATED,
        PREVIEW_REUSED,
        PREVIEW_FAILED,
        PREVIEW_SKIPPED_DISABLED,
        PREVIEW_SKIPPED_UNSUPPORTED,
        PREVIEW_SKIPPED_STORAGE,
    }
)

# Failure stages (spec §14).
STAGE_PREVIEW_ROOT = "preview-root"
STAGE_IMAGE_DECODE = "image-decode"
STAGE_IMAGE_TRANSFORM = "image-transform"
STAGE_IMAGE_ENCODE = "image-encode"
STAGE_IMAGE_VALIDATE = "image-validate"
STAGE_FFMPEG_START = "ffmpeg-start"
STAGE_FFMPEG_ENCODE = "ffmpeg-encode"
STAGE_FFMPEG_TIMEOUT = "ffmpeg-timeout"
STAGE_FFMPEG_EXIT = "ffmpeg-exit"
STAGE_FFMPEG_VERSION = "ffmpeg-version"
STAGE_FFMPEG_ENCODER = "ffmpeg-encoder"
STAGE_VIDEO_VALIDATE = "video-validate"
STAGE_TEMP_FILE = "temp-file"
STAGE_RENAME = "rename"
STAGE_PERMISSION = "permission"
STAGE_DISK_FULL = "disk-full"
STAGE_CANCELLED = "cancelled"
STAGE_SOURCE_CHANGED = "source-changed"
STAGE_HASH_UNAVAILABLE = "hash-unavailable"
STAGE_CONFIGURATION = "configuration"
# A generator raised something other than PreviewError/OSError; the preview is
# reported as failed instead of letting a programming error abort the scan.
STAGE_UNEXPECTED_ERROR = "unexpected-error"
# Stages that mean preview storage itself is gone: stop generating (spec §17).
STORAGE_STAGES = frozenset({STAGE_PREVIEW_ROOT, STAGE_DISK_FULL})

MAX_DETAIL_LENGTH = 500
ROOT_CHECK_NAME = "jvvv-preview-root-check"
PROGRESS_INTERVAL = 1000

_KIND_DIRECTORIES: dict[str, str] = {"image": "images", "video": "videos"}
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PREFIX_DIRECTORY_RE = re.compile(r"^[0-9a-f]{2}$")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
_PREVIEW_NAME_RE = re.compile(
    r"^([0-9a-f]{64})\.("
    + "|".join(sorted(re.escape(ext) for ext in set(PREVIEW_EXTENSIONS.values())))
    + r")$"
)
_DISK_FULL_ERRNOS = frozenset(
    code for code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if code is not None
)
_DISK_FULL_WINERRORS = frozenset({39, 112})  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_DISK_FULL_PHRASES = ("no space left", "not enough space", "disk full")
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
_PERMISSION_WINERRORS = frozenset({5, 32})  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION


# ---------------------------------------------------------------------------
# Exceptions and error classification
# ---------------------------------------------------------------------------


class PreviewError(Exception):
    """A preview operation failed at a known ``stage`` with a human ``message``.

    ``detail`` carries the technical text (OS error, FFmpeg stderr tail) that
    the end-of-scan failure list shows on its ``Detail:`` line.
    """

    def __init__(self, stage: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.stage = str(stage)
        self.message = str(message)
        self.detail = "" if detail is None else str(detail)

    def __str__(self) -> str:
        detail = _detail(self.detail)
        if detail and self.message:
            return f"{self.message} — {detail}"
        return self.message or detail


class PreviewCancelled(Exception):
    """Raised (never returned) when a cancel callback reports cancellation."""


def _detail(value: object) -> str:
    """Collapse to one line and cap the length, like ``media_metadata._detail``."""

    text = " ".join(str(value or "").split())
    if len(text) <= MAX_DETAIL_LENGTH:
        return text
    return f"{text[: MAX_DETAIL_LENGTH - 1].rstrip()}…"


def _errno_of(exc: BaseException) -> int | None:
    value = getattr(exc, "errno", None)
    return value if isinstance(value, int) else None


def _winerror_of(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    return value if isinstance(value, int) else None


def is_disk_full_error(exc: BaseException) -> bool:
    """True for ENOSPC/EDQUOT, Windows disk-full codes, or disk-full wording."""

    if isinstance(exc, PreviewError) and exc.stage == STAGE_DISK_FULL:
        return True
    if _errno_of(exc) in _DISK_FULL_ERRNOS:
        return True
    if _winerror_of(exc) in _DISK_FULL_WINERRORS:
        return True
    text = str(exc).casefold()
    return any(phrase in text for phrase in _DISK_FULL_PHRASES)


def is_permission_error(exc: BaseException) -> bool:
    """True for ``PermissionError``, EACCES/EPERM, or Windows access/sharing codes."""

    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, PreviewError) and exc.stage == STAGE_PERMISSION:
        return True
    if _errno_of(exc) in _PERMISSION_ERRNOS:
        return True
    return _winerror_of(exc) in _PERMISSION_WINERRORS


def classify_os_error(exc: BaseException, default_stage: str) -> str:
    """Map an OS-level failure to ``disk-full``, ``permission`` or the default stage."""

    if is_disk_full_error(exc):
        return STAGE_DISK_FULL
    if is_permission_error(exc):
        return STAGE_PERMISSION
    return default_stage


def os_error_detail(exc: BaseException) -> str:
    """``str(exc)`` collapsed to one line, e.g. ``[WinError 5] Access is denied: 'E:\\\\...'``."""

    text = _detail(str(exc))
    return text or type(exc).__name__


def sha256_hex(digest: bytes | str) -> str:
    """Normalise a SHA-256 given as 32 raw bytes or 64 hex characters to lowercase hex."""

    if isinstance(digest, (bytes, bytearray, memoryview)):
        raw = bytes(digest)
        if len(raw) != 32:
            raise PreviewError(
                STAGE_CONFIGURATION,
                f"A SHA-256 digest must be 32 bytes long, not {len(raw)}.",
            )
        return raw.hex()
    if isinstance(digest, str):
        if _SHA256_HEX_RE.fullmatch(digest):
            return digest.lower()
        raise PreviewError(
            STAGE_CONFIGURATION,
            "A SHA-256 hash must be exactly 64 hexadecimal characters.",
            detail=repr(digest) if len(digest) <= 80 else f"{len(digest)} characters",
        )
    raise PreviewError(
        STAGE_CONFIGURATION,
        f"A SHA-256 hash must be bytes or text, not {type(digest).__name__}.",
    )


def _raise_if_cancelled(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback is not None and cancel_callback():
        raise PreviewCancelled("Preview operation cancelled.")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewResult:
    """The explicit outcome of one ensure/generate call (spec §14)."""

    status: str
    media_kind: str
    profile_id: str
    path: Path | None = None
    bytes_written: int = 0
    size_bytes: int = 0
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    stage: str | None = None
    message: str = ""
    detail: str = ""
    replaced_corrupt: bool = False

    def __post_init__(self) -> None:
        if self.status not in PREVIEW_STATUSES:
            raise ValueError(f"Unsupported preview status: {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status in {PREVIEW_GENERATED, PREVIEW_REUSED}

    def as_dict(self) -> dict[str, object]:
        values: dict[str, object] = asdict(self)
        values["path"] = None if self.path is None else str(self.path)
        return values


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _media_kind_label(media_kind: str) -> str:
    text = (media_kind or "").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


@dataclass(frozen=True)
class PreviewFailure:
    """Everything needed to report one failed preview at the end of a scan (spec §14)."""

    source_name: str
    relative_path: str
    volume_id: int
    volume_label: str
    media_kind: str
    sha256: str | None
    preview_path: str | None
    profile_id: str
    stage: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> PreviewFailure:
        return cls(
            source_name=_text(values.get("source_name")),
            relative_path=_text(values.get("relative_path")),
            volume_id=_int(values.get("volume_id")),
            volume_label=_text(values.get("volume_label")),
            media_kind=_text(values.get("media_kind")),
            sha256=_optional_text(values.get("sha256")),
            preview_path=_optional_text(values.get("preview_path")),
            profile_id=_text(values.get("profile_id")),
            stage=_text(values.get("stage")),
            message=_text(values.get("message")),
            detail=_text(values.get("detail")),
        )

    def display_lines(self) -> list[str]:
        lines = [f"Type: {_media_kind_label(self.media_kind)}"]
        if self.profile_id:
            lines.append(f"Profile: {self.profile_id}")
        lines.append(f"Error: {self.message or self.stage or 'Unknown error.'}")
        if self.detail:
            lines.append(f"Detail: {self.detail}")
        return lines


@dataclass(frozen=True)
class PreviewEntry:
    """One preview file discovered in the store."""

    path: Path
    media_kind: str
    profile_id: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ProfileStatistics:
    count: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class PreviewStoreStatistics:
    image_count: int
    video_count: int
    image_bytes: int
    video_bytes: int
    total_bytes: int
    temporary_files: int
    profiles: dict[tuple[str, str], ProfileStatistics] = field(default_factory=dict)
    cancelled: bool = False


@dataclass(frozen=True)
class RootValidation:
    root: Path
    created: bool
    total_bytes: int | None
    free_bytes: int | None
    message: str


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def ensure_source_snapshot(
    source: Path,
    current: os.stat_result,
    expected: os.stat_result | None,
) -> None:
    """Raise ``source-changed`` when ``current`` differs from the hash-time snapshot.

    The scanner hashes a file, stores its record, probes its media details and
    only then asks for a preview.  A file replaced inside that window must not
    be previewed under the old SHA-256 (spec §9, §32), so generators compare
    their own fresh ``lstat`` against the snapshot taken when the hash was
    computed before they read a single byte.
    """

    if expected is None:
        return
    changed = (
        current.st_size != expected.st_size
        or current.st_mtime_ns != expected.st_mtime_ns
        or (expected.st_ino and current.st_ino and current.st_ino != expected.st_ino)
    )
    if changed:
        raise PreviewError(
            STAGE_SOURCE_CHANGED,
            "The source file changed between hashing and preview generation.",
            detail=(
                f"size {expected.st_size} -> {current.st_size} bytes, "
                f"mtime_ns {expected.st_mtime_ns} -> {current.st_mtime_ns}"
            ),
        )


# Temporaries younger than this may belong to a scan running in another JVVV window.
STALE_TEMPORARY_AGE_SECONDS = 24 * 60 * 60


class PreviewCache:
    """Hash-addressed preview store. Root layout (spec §4, §6)::

        <root>/images/<image-profile-id>/<hh>/<sha256hex>.jpg
        <root>/videos/<video-profile-id>/<hh>/<sha256hex>.mp4  (hh = first two hex chars)
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        image_profile: ImagePreviewProfile,
        video_profile: VideoPreviewProfile,
    ) -> None:
        # Keep the text so an empty selection can be reported; never resolve()
        # the path, UNC and mapped-drive roots must stay exactly as given.
        self.root_text = os.fspath(root)
        self.root = Path(self.root_text).expanduser()
        self.image_profile = image_profile
        self.video_profile = video_profile

    # -- profiles and paths ---------------------------------------------------
    def profile(self, media_kind: str) -> ImagePreviewProfile | VideoPreviewProfile:
        if media_kind == "image":
            return self.image_profile
        if media_kind == "video":
            return self.video_profile
        raise _unsupported_kind(media_kind)

    def profile_id(self, media_kind: str) -> str:
        profile = self.profile(media_kind)
        try:
            return profile.profile_id
        except PreviewConfigError as exc:
            raise PreviewError(STAGE_CONFIGURATION, str(exc)) from exc

    def kind_directory(self, media_kind: str) -> Path:
        try:
            return self.root / _KIND_DIRECTORIES[media_kind]
        except (KeyError, TypeError) as exc:
            raise _unsupported_kind(media_kind) from exc

    def profile_directory(self, media_kind: str, profile_id: str | None = None) -> Path:
        kind_directory = self.kind_directory(media_kind)
        resolved_id = self.profile_id(media_kind) if profile_id is None else profile_id
        return kind_directory / _checked_profile_id(resolved_id)

    def preview_path(
        self,
        media_kind: str,
        sha256: bytes | str,
        profile_id: str | None = None,
    ) -> Path:
        directory = self.profile_directory(media_kind, profile_id)
        digest = sha256_hex(sha256)
        return directory / digest[:2] / f"{digest}.{_extension_for(media_kind)}"

    def temporary_path(self, final_path: Path) -> Path:
        final_path = Path(final_path)
        return final_path.parent / f".{final_path.name}.tmp-{secrets.token_hex(8)}"

    @staticmethod
    def is_temporary_name(name: str) -> bool:
        return name.startswith(".") and ".tmp-" in name

    @staticmethod
    def parse_preview_name(name: str) -> tuple[str, str] | None:
        match = _PREVIEW_NAME_RE.fullmatch(name)
        if match is None:
            return None
        return match.group(1), match.group(2)

    # -- writing --------------------------------------------------------------
    def ensure_parent(self, final_path: Path) -> None:
        try:
            Path(final_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_PREVIEW_ROOT),
                "Could not create the preview directory.",
                detail=os_error_detail(exc),
            ) from exc

    def publish(self, temp_path: Path, final_path: Path) -> None:
        """Atomically move a finished, validated temporary file into place."""

        try:
            os.replace(temp_path, final_path)
        except OSError as exc:
            self.discard_temporary(temp_path)
            raise PreviewError(
                classify_os_error(exc, STAGE_RENAME),
                "Could not move the finished preview into place.",
                detail=os_error_detail(exc),
            ) from exc

    def discard_temporary(self, temp_path: Path | None) -> None:
        """Best-effort removal of a temporary file; never raises.

        Only names produced by :meth:`temporary_path` are removed, so a
        previously valid final preview can never be deleted by mistake.
        """

        if temp_path is None:
            return
        path = Path(temp_path)
        if not self.is_temporary_name(path.name):
            return
        try:
            os.remove(path)
        except OSError:
            pass

    # -- root validation ------------------------------------------------------
    def validate_root(self, *, create: bool = True) -> RootValidation:
        """Prove the root is usable (spec §2A); raise ``PreviewError`` otherwise.

        The root must exist (or be creatable), be a directory, and let JVVV
        create, read back *and remove* a small validation file.  A failure at
        any of those steps is reported, never swallowed.
        """

        if not self.root_text.strip():
            raise PreviewError(
                STAGE_PREVIEW_ROOT,
                "A preview storage directory has not been selected.",
            )
        root = self.root
        created = False
        temp_path: Path | None = None
        try:
            if not root.exists():
                if not create:
                    raise PreviewError(
                        STAGE_PREVIEW_ROOT,
                        f"JVVV cannot write to: {root}",
                        detail="The directory does not exist.",
                    )
                root.mkdir(parents=True, exist_ok=True)
                created = True
            if not root.is_dir():
                raise PreviewError(
                    STAGE_PREVIEW_ROOT,
                    f"JVVV cannot write to: {root}",
                    detail="The path exists but is not a directory.",
                )
            temp_path = self.temporary_path(root / ROOT_CHECK_NAME)
            payload = b"JVVV preview root check " + secrets.token_hex(16).encode("ascii") + b"\n"
            temp_path.write_bytes(payload)
            if temp_path.read_bytes() != payload:
                raise PreviewError(
                    STAGE_PREVIEW_ROOT,
                    f"JVVV cannot write to: {root}",
                    detail="The validation file did not read back with the bytes that were written.",
                )
            # Spec §2A: JVVV must be able to create *and remove* the validation
            # file, so delete it explicitly and report a failure instead of
            # letting the best-effort cleanup in ``finally`` swallow it.  Once
            # the removal has been attempted the ``finally`` has nothing to do.
            check_file, temp_path = temp_path, None
            try:
                os.remove(check_file)
            except OSError as exc:
                raise _root_check_removal_error(root, check_file, exc) from exc
        except PreviewError:
            raise
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_PREVIEW_ROOT),
                f"JVVV cannot write to: {root}",
                detail=os_error_detail(exc),
            ) from exc
        finally:
            self.discard_temporary(temp_path)

        usage = self.free_space()
        total_bytes, free_bytes = usage if usage is not None else (None, None)
        state = "was created and is writable" if created else "is writable"
        if free_bytes is None:
            message = f"{root} {state}."
        else:
            message = f"{root} {state} ({format_size(free_bytes)} free)."
        return RootValidation(
            root=root,
            created=created,
            total_bytes=total_bytes,
            free_bytes=free_bytes,
            message=message,
        )

    def free_space(self) -> tuple[int, int] | None:
        try:
            usage = shutil.disk_usage(os.fspath(self.root))
        except (OSError, ValueError):
            return None
        return int(usage.total), int(usage.free)

    def contains(self, path: Path) -> bool:
        """True when ``path`` lies strictly inside the preview root."""

        try:
            # realpath: a junction or symlink inside the store must not make a
            # file elsewhere look like part of it (spec §22 deletion safety).
            root_text = os.path.normcase(os.path.realpath(os.fspath(self.root)))
            candidate = os.path.normcase(os.path.realpath(os.fspath(path)))
        except (TypeError, ValueError, OSError):
            return False
        prefix = root_text.rstrip("\\/") + os.sep
        return candidate.startswith(prefix)

    # -- reading the store ----------------------------------------------------
    def iter_profile_ids(self, media_kind: str) -> list[str]:
        """Sorted profile directory names under the kind directory (missing → ``[]``)."""

        kind_directory = self.kind_directory(media_kind)
        names = [
            entry.name
            for entry in self._scan_directory(kind_directory)
            if _PROFILE_ID_RE.fullmatch(entry.name) and _is_directory(entry)
        ]
        return sorted(names)

    def iter_previews(
        self,
        media_kind: str,
        profile_id: str | None = None,
        *,
        cancel_callback: CancelCallback | None = None,
    ) -> Iterator[PreviewEntry]:
        """Stream every valid preview under ``<kind>/<profile>/<hh>/``.

        Temporaries, directories, and names that do not parse are skipped.
        Nothing is buffered: each directory is walked with ``os.scandir`` as
        the consumer pulls entries, and cancellation is checked between
        directories (raising ``PreviewCancelled``).
        """

        self.kind_directory(media_kind)
        extension = _extension_for(media_kind)
        if profile_id is None:
            profile_ids = self.iter_profile_ids(media_kind)
        else:
            profile_ids = [_checked_profile_id(profile_id)]
        for current_id in profile_ids:
            for prefix_directory, entry in self._walk_profile(media_kind, current_id, cancel_callback):
                parsed = self._parse_entry(entry, prefix_directory, extension)
                if parsed is None:
                    continue
                size = self._entry_size(entry)
                if size is None:
                    continue
                yield PreviewEntry(
                    path=prefix_directory / entry.name,
                    media_kind=media_kind,
                    profile_id=current_id,
                    sha256=parsed,
                    size_bytes=size,
                )

    def store_statistics(
        self,
        *,
        cancel_callback: CancelCallback | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> PreviewStoreStatistics:
        """Count previews and bytes per kind and profile without buffering the store.

        Cancellation returns the partial figures with ``cancelled=True``
        rather than raising.  ``progress_callback(previews_counted, directory)``
        fires at most once per ``PROGRESS_INTERVAL`` entries examined.
        """

        counts: dict[str, int] = {kind: 0 for kind in PREVIEW_MEDIA_KINDS}
        totals: dict[str, int] = {kind: 0 for kind in PREVIEW_MEDIA_KINDS}
        profiles: dict[tuple[str, str], ProfileStatistics] = {}
        temporaries = 0
        examined = 0
        cancelled = False
        for media_kind in PREVIEW_MEDIA_KINDS:
            if cancelled:
                break
            extension = _extension_for(media_kind)
            try:
                _raise_if_cancelled(cancel_callback)
                profile_ids = self.iter_profile_ids(media_kind)
            except PreviewCancelled:
                cancelled = True
                break
            for current_id in profile_ids:
                key = (media_kind, current_id)
                count = 0
                total = 0
                try:
                    for prefix_directory, entry in self._walk_profile(
                        media_kind, current_id, cancel_callback
                    ):
                        examined += 1
                        if self.is_temporary_name(entry.name):
                            temporaries += 1
                        elif self._parse_entry(entry, prefix_directory, extension) is not None:
                            size = self._entry_size(entry)
                            if size is not None:
                                count += 1
                                total += size
                        if progress_callback is not None and examined % PROGRESS_INTERVAL == 0:
                            progress_callback(
                                sum(counts.values()) + count,
                                os.fspath(prefix_directory),
                            )
                except PreviewCancelled:
                    cancelled = True
                profiles[key] = ProfileStatistics(count=count, bytes=total)
                counts[media_kind] += count
                totals[media_kind] += total
                if cancelled:
                    break
        return PreviewStoreStatistics(
            image_count=counts["image"],
            video_count=counts["video"],
            image_bytes=totals["image"],
            video_bytes=totals["video"],
            total_bytes=totals["image"] + totals["video"],
            temporary_files=temporaries,
            profiles=profiles,
            cancelled=cancelled,
        )

    def iter_temporary_files(
        self, *, cancel_callback: CancelCallback | None = None
    ) -> Iterator[Path]:
        """Stream every ``.<name>.tmp-<hex>`` file under the store (profiles and the root)."""

        for media_kind in PREVIEW_MEDIA_KINDS:
            for profile_id in self.iter_profile_ids(media_kind):
                for prefix_directory, entry in self._walk_profile(media_kind, profile_id, cancel_callback):
                    if self.is_temporary_name(entry.name):
                        yield prefix_directory / entry.name
        _raise_if_cancelled(cancel_callback)
        for entry in self._scan_directory(self.root):
            if _is_regular_file(entry) and self.is_temporary_name(entry.name):
                yield self.root / entry.name

    def remove_stale_temporary(
        self,
        path: Path,
        *,
        now: float | None = None,
        min_age_seconds: float = STALE_TEMPORARY_AGE_SECONDS,
    ) -> bool:
        """Delete one leftover temporary file; ``True`` when it was removed.

        Temporaries normally vanish with the generation that wrote them; the
        ones that survive a crash or power loss can only be removed here.  A
        file younger than ``min_age_seconds`` is kept because another JVVV
        window sharing the root may still be writing it.  Anything that is not
        a temporary file strictly inside the root is refused.
        """

        target = Path(path)
        if not self.contains(target):
            raise PreviewError(
                STAGE_CONFIGURATION,
                "Refusing to delete a file outside the preview storage directory.",
                detail=os.fspath(target),
            )
        if not self.is_temporary_name(target.name):
            raise PreviewError(
                STAGE_CONFIGURATION,
                "Refusing to delete a file that is not a preview temporary file.",
                detail=os.fspath(target),
            )
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_TEMP_FILE),
                f"Could not read the temporary file {target}.",
                detail=os_error_detail(exc),
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise PreviewError(
                STAGE_CONFIGURATION,
                "Refusing to delete something that is not a regular file.",
                detail=os.fspath(target),
            )
        current = time.time() if now is None else float(now)
        if current - info.st_mtime < min_age_seconds:
            return False
        try:
            os.remove(target)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_TEMP_FILE),
                f"Could not delete the temporary file {target}.",
                detail=os_error_detail(exc),
            ) from exc
        return True

    def remove_preview(self, path: Path) -> None:
        """Delete one preview file; refuses anything outside the root or not preview-named."""

        target = Path(path)
        if not self.contains(target):
            raise PreviewError(
                STAGE_CONFIGURATION,
                "Refusing to delete a file outside the preview storage directory.",
                detail=os.fspath(target),
            )
        if self.parse_preview_name(target.name) is None:
            raise PreviewError(
                STAGE_CONFIGURATION,
                "Refusing to delete a file that is not a preview.",
                detail=os.fspath(target),
            )
        try:
            os.remove(target)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PreviewError(
                classify_os_error(exc, STAGE_PREVIEW_ROOT),
                "Could not delete the preview.",
                detail=os_error_detail(exc),
            ) from exc

    # -- private helpers --------------------------------------------------------
    def _entry_size(self, entry: os.DirEntry[str]) -> int | None:
        """Size of a scandir entry; ``None`` when it vanished mid-walk."""

        try:
            return int(entry.stat(follow_symlinks=False).st_size)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _store_read_error(Path(entry.path), exc) from exc

    def _parse_entry(
        self,
        entry: os.DirEntry[str],
        prefix_directory: Path,
        extension: str,
    ) -> str | None:
        """Return the hash of a valid, correctly placed preview entry, else ``None``."""

        if self.is_temporary_name(entry.name):
            return None
        parsed = self.parse_preview_name(entry.name)
        if parsed is None:
            return None
        digest, found_extension = parsed
        if found_extension != extension or digest[:2] != prefix_directory.name:
            return None
        return digest

    def _walk_profile(
        self,
        media_kind: str,
        profile_id: str,
        cancel_callback: CancelCallback | None,
    ) -> Iterator[tuple[Path, os.DirEntry[str]]]:
        """Yield ``(prefix_directory, file_entry)`` for every regular file in a profile."""

        profile_directory = self.profile_directory(media_kind, profile_id)
        _raise_if_cancelled(cancel_callback)
        prefix_names = sorted(
            entry.name
            for entry in self._scan_directory(profile_directory)
            if _PREFIX_DIRECTORY_RE.fullmatch(entry.name) and _is_directory(entry)
        )
        for prefix_name in prefix_names:
            _raise_if_cancelled(cancel_callback)
            prefix_directory = profile_directory / prefix_name
            for entry in self._scan_directory(prefix_directory):
                if _is_regular_file(entry):
                    yield prefix_directory, entry

    def _scan_directory(self, directory: Path) -> Iterator[os.DirEntry[str]]:
        """Stream ``os.scandir``; a missing directory yields nothing."""

        try:
            iterator = os.scandir(os.fspath(directory))
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as exc:
            raise _store_read_error(directory, exc) from exc
        with iterator:
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    return
                except OSError as exc:
                    raise _store_read_error(directory, exc) from exc
                yield entry


def _unsupported_kind(media_kind: object) -> PreviewError:
    return PreviewError(
        STAGE_CONFIGURATION,
        f"Previews are not supported for media kind {media_kind!r}.",
    )


def _extension_for(media_kind: str) -> str:
    try:
        return preview_extension_for(media_kind)
    except PreviewConfigError as exc:
        raise _unsupported_kind(media_kind) from exc


def _checked_profile_id(profile_id: object) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise PreviewError(
            STAGE_CONFIGURATION,
            f"Profile ID contains unsafe filename characters: {profile_id!r}",
        )
    return profile_id


def _root_check_removal_error(root: Path, check_file: Path, exc: OSError) -> PreviewError:
    """The validation file was written but could not be deleted again (spec §2A)."""

    detail = os_error_detail(exc)
    if check_file.name not in detail:
        detail = f"{detail} ({check_file})"
    return PreviewError(
        classify_os_error(exc, STAGE_PREVIEW_ROOT),
        f"JVVV cannot write to: {root}",
        detail=f"The validation file could not be removed: {detail}",
    )


def _store_read_error(directory: Path, exc: OSError) -> PreviewError:
    return PreviewError(
        classify_os_error(exc, STAGE_PREVIEW_ROOT),
        f"Could not read the preview store directory: {directory}",
        detail=os_error_detail(exc),
    )


def _is_directory(entry: os.DirEntry[str]) -> bool:
    """A real subdirectory: junctions and directory symlinks are never walked."""

    try:
        if not entry.is_dir(follow_symlinks=False):
            return False
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    except OSError:
        return False


def _is_regular_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
