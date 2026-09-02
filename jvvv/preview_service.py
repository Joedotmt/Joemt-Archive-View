"""Orchestration for offline previews: validation, preflight, and scan-time use.

The service sits between the scanner and the two generator backends.  It owns
the per-scan statistics, converts every generator outcome into an explicit
state (``generated``, ``reused``, ``failed``, ``skipped-*``), records a
:class:`PreviewFailure` for anything that did not produce a preview, and stops
generating once preview storage has become unavailable (disk full, root gone).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .image_preview import (
    IMAGE_BACKEND_NAME,
    ImagePreviewGenerator,
    image_backend_available,
    test_image_backend,
    validate_image_preview,
)
from .preview_cache import (
    PREVIEW_FAILED,
    PREVIEW_GENERATED,
    PREVIEW_REUSED,
    PREVIEW_SKIPPED_DISABLED,
    PREVIEW_SKIPPED_STORAGE,
    PREVIEW_SKIPPED_UNSUPPORTED,
    STAGE_CONFIGURATION,
    STAGE_FFMPEG_START,
    STAGE_HASH_UNAVAILABLE,
    STAGE_PREVIEW_ROOT,
    STAGE_TEMP_FILE,
    STAGE_UNEXPECTED_ERROR,
    STORAGE_STAGES,
    PreviewCache,
    PreviewCancelled,
    PreviewError,
    PreviewFailure,
    PreviewResult,
    classify_os_error,
    os_error_detail,
    sha256_hex,
)
from .preview_config import (
    PREVIEW_MEDIA_KINDS,
    PreviewConfigError,
    PreviewSettings,
)
from .utils import format_size
from .video_preview import (
    FFMPEG_ENCODER,
    FfmpegCapabilities,
    VideoPreviewGenerator,
    find_ffmpeg,
    probe_ffmpeg,
    require_libx264,
    test_video_encode,
    validate_video_preview,
)


CancelCallback = Callable[[], bool]
LogCallback = Callable[[str], None]

# Persisted per-file statuses (database.PREVIEW_STATUS_VALUES).
DB_STATUS_AVAILABLE = "available"
DB_STATUS_FAILED = "failed"
DB_STATUS_MISSING = "missing"
DB_STATUS_UNSUPPORTED = "unsupported"

# Scan-level preview modes (database.PREVIEW_SCAN_MODES).
MODE_DISABLED = "disabled"
MODE_ENABLED = "enabled"
MODE_SKIPPED_PREFLIGHT = "skipped-preflight"

STAGE_STORAGE_UNAVAILABLE = "storage-unavailable"
SCAN_OUTCOME_COMPLETED_WITH_WARNINGS = "completed_with_warnings"


@dataclass
class PreviewStatistics:
    """Counters and failure records for one scan (spec §14, §15, §17, §31)."""

    mode: str = MODE_ENABLED
    image_generated: int = 0
    image_reused: int = 0
    image_failed: int = 0
    video_generated: int = 0
    video_reused: int = 0
    video_failed: int = 0
    bytes_written: int = 0
    storage_skipped: int = 0
    corrupt_replaced: int = 0
    storage_unavailable_reason: str | None = None
    message: str = ""
    failures: list[PreviewFailure] = field(default_factory=list)

    @property
    def total_generated(self) -> int:
        return self.image_generated + self.video_generated

    @property
    def total_reused(self) -> int:
        return self.image_reused + self.video_reused

    @property
    def total_failed(self) -> int:
        return self.image_failed + self.video_failed

    @property
    def total_attempted(self) -> int:
        return self.total_generated + self.total_reused + self.total_failed + self.storage_skipped

    @property
    def has_problems(self) -> bool:
        return bool(self.total_failed or self.storage_skipped or self.storage_unavailable_reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "image_generated": int(self.image_generated),
            "image_reused": int(self.image_reused),
            "image_failed": int(self.image_failed),
            "video_generated": int(self.video_generated),
            "video_reused": int(self.video_reused),
            "video_failed": int(self.video_failed),
            "bytes_written": int(self.bytes_written),
            "storage_skipped": int(self.storage_skipped),
            "corrupt_replaced": int(self.corrupt_replaced),
            "storage_unavailable_reason": self.storage_unavailable_reason,
            "message": self.message,
            "failures": [failure.as_dict() for failure in self.failures],
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any] | None) -> PreviewStatistics:
        data = values or {}
        failures = [
            PreviewFailure.from_dict(item)
            for item in data.get("failures") or ()
            if isinstance(item, Mapping)
        ]
        reason = data.get("storage_unavailable_reason")
        return cls(
            mode=str(data.get("mode") or MODE_DISABLED),
            image_generated=int(data.get("image_generated") or 0),
            image_reused=int(data.get("image_reused") or 0),
            image_failed=int(data.get("image_failed") or 0),
            video_generated=int(data.get("video_generated") or 0),
            video_reused=int(data.get("video_reused") or 0),
            video_failed=int(data.get("video_failed") or 0),
            bytes_written=int(data.get("bytes_written") or 0),
            storage_skipped=int(data.get("storage_skipped") or 0),
            corrupt_replaced=int(data.get("corrupt_replaced") or 0),
            storage_unavailable_reason=str(reason) if reason else None,
            message=str(data.get("message") or ""),
            failures=failures,
        )

    def summary_text(self, root: str | None) -> str:
        """The end-of-scan "Offline Preview Summary" block (spec §15)."""

        width = max(
            len(f"{value:,}")
            for value in (
                self.image_generated,
                self.image_reused,
                self.image_failed,
                self.video_generated,
                self.video_reused,
                self.video_failed,
                1,
            )
        )

        def row(label: str, value: int) -> str:
            return f"  {label:<11}{value:>{width},}"

        lines = [
            "Offline Preview Summary",
            "",
            "Images",
            row("Generated:", self.image_generated),
            row("Reused:", self.image_reused),
            row("Failed:", self.image_failed),
            "",
            "Videos",
            row("Generated:", self.video_generated),
            row("Reused:", self.video_reused),
            row("Failed:", self.video_failed),
            "",
            "Preview storage:",
            f"  {root or 'Not configured'}",
            "",
            "Space used by previews created this scan:",
            f"  {format_size(self.bytes_written)}",
        ]
        if self.corrupt_replaced:
            lines.extend(
                [
                    "",
                    "Existing previews that failed validation and were regenerated:",
                    f"  {self.corrupt_replaced:,}",
                ]
            )
        if self.storage_skipped or self.storage_unavailable_reason:
            lines.extend(
                [
                    "",
                    "Preview generation stopped because preview storage became unavailable.",
                    f"  Previews not attempted afterwards: {self.storage_skipped:,}",
                ]
            )
            if self.storage_unavailable_reason:
                lines.append(f"  Reason: {self.storage_unavailable_reason}")
        return "\n".join(lines)


def scan_outcome(status: str, statistics: PreviewStatistics | Mapping[str, Any] | None) -> str:
    """Return the user-facing outcome for a scan status plus preview results.

    Catalogue indexing results are never downgraded: the persisted scan status
    stays ``completed`` (other subsystems key on it), but the reported outcome
    becomes ``completed_with_warnings`` when previews failed (spec §16).
    """

    if status != "completed" or statistics is None:
        return status
    if isinstance(statistics, PreviewStatistics):
        problems = statistics.has_problems
    else:
        problems = bool(
            int(statistics.get("image_failed") or 0)
            or int(statistics.get("video_failed") or 0)
            or int(statistics.get("storage_skipped") or 0)
            or statistics.get("storage_unavailable_reason")
        )
    return SCAN_OUTCOME_COMPLETED_WITH_WARNINGS if problems else status


def preview_warning_message(statistics: PreviewStatistics) -> str:
    """Explicit scan-report sentence required by spec §16."""

    parts = []
    if statistics.total_failed:
        parts.append(
            f"{statistics.total_failed:,} offline preview"
            + ("s were" if statistics.total_failed != 1 else " was")
            + " not created"
        )
    if statistics.storage_skipped:
        parts.append(
            f"{statistics.storage_skipped:,} preview"
            + ("s were" if statistics.storage_skipped != 1 else " was")
            + " not attempted because preview storage became unavailable"
        )
    if not parts:
        return ""
    return "Catalogue indexing succeeded, but " + " and ".join(parts) + "."


# --------------------------------------------------------------------------
# Configuration validation (spec §2, §3, §27)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationStep:
    key: str
    label: str
    passed: bool
    detail: str
    stage: str | None = None
    skipped: bool = False

    @property
    def status_text(self) -> str:
        if self.skipped:
            return "Not run"
        return "PASS" if self.passed else "FAIL"


@dataclass(frozen=True)
class PreviewValidationReport:
    passed: bool
    steps: tuple[ValidationStep, ...]
    settings: PreviewSettings
    root: str
    free_bytes: int | None
    total_bytes: int | None
    ffmpeg_path: str | None
    ffmpeg_version: str | None
    encoder_available: bool | None
    image_backend: str
    image_profile_id: str | None
    video_profile_id: str | None
    include_encode_tests: bool

    @property
    def first_failure(self) -> ValidationStep | None:
        for step in self.steps:
            if not step.passed and not step.skipped:
                return step
        return None

    def step(self, key: str) -> ValidationStep | None:
        for step in self.steps:
            if step.key == key:
                return step
        return None

    def failure_summary(self, heading: str = "Offline previews could not be enabled.") -> str:
        failure = self.first_failure
        if failure is None:
            return ""
        lines = [heading, "", f"{failure.label} failed."]
        if failure.detail:
            lines.extend(["", failure.detail])
        return "\n".join(lines)

    def report_text(self) -> str:
        """Full PASS/FAIL report for the Test Preview Configuration button."""

        lines = [
            f"Preview Configuration Test: {'PASS' if self.passed else 'FAIL'}",
            "",
            f"Preview storage directory: {self.root or 'Not selected'}",
            "Available free space: "
            + (format_size(self.free_bytes) if self.free_bytes is not None else "Unknown"),
            f"FFmpeg path: {self.ffmpeg_path or 'Not found'}",
            f"FFmpeg version: {self.ffmpeg_version or 'Unavailable'}",
            f"H.264 encoder ({FFMPEG_ENCODER}): "
            + (
                "available"
                if self.encoder_available
                else "NOT available"
                if self.encoder_available is False
                else "Unknown"
            ),
            f"Image backend: {self.image_backend}",
            f"Image profile: {self.image_profile_id or 'Invalid'}",
            f"Video profile: {self.video_profile_id or 'Invalid'}",
            "",
        ]
        for step in self.steps:
            line = f"[{step.status_text}] {step.label}"
            if step.detail:
                line += f" — {step.detail}"
            lines.append(line)
        lines.extend(["", f"Overall: {'PASS' if self.passed else 'FAIL'}"])
        return "\n".join(lines)


def validate_preview_configuration(
    settings: PreviewSettings,
    *,
    include_encode_tests: bool = True,
    ffmpeg_finder: Callable[[str | None], str | None] = find_ffmpeg,
    ffmpeg_prober: Callable[[str], FfmpegCapabilities] = probe_ffmpeg,
    image_backend_check: Callable[[], tuple[bool, str]] = image_backend_available,
    image_tester: Callable[[PreviewCache], str] = test_image_backend,
    video_tester: Callable[[str, PreviewCache], str] = test_video_encode,
) -> PreviewValidationReport:
    """Prove that the configuration works.  Never raises.

    ``include_encode_tests=False`` is the lightweight scan-start preflight
    (spec §27): it still checks the root, the image backend, FFmpeg and its
    encoder, but does not encode test media.
    """

    steps: list[ValidationStep] = []
    root_text = settings.root_directory.strip()
    free_bytes: int | None = None
    total_bytes: int | None = None
    ffmpeg_path: str | None = None
    ffmpeg_version: str | None = None
    encoder_available: bool | None = None
    image_backend = IMAGE_BACKEND_NAME
    image_profile_id: str | None = None
    video_profile_id: str | None = None

    def add(
        key: str,
        label: str,
        passed: bool,
        detail: str,
        *,
        stage: str | None = None,
        skipped: bool = False,
    ) -> None:
        steps.append(ValidationStep(key, label, passed, detail, stage, skipped))

    def report() -> PreviewValidationReport:
        return PreviewValidationReport(
            passed=all(step.passed or step.skipped for step in steps)
            and not any(step.skipped and step.key in {"preview-root"} for step in steps),
            steps=tuple(steps),
            settings=settings,
            root=root_text,
            free_bytes=free_bytes,
            total_bytes=total_bytes,
            ffmpeg_path=ffmpeg_path,
            ffmpeg_version=ffmpeg_version,
            encoder_available=encoder_available,
            image_backend=image_backend,
            image_profile_id=image_profile_id,
            video_profile_id=video_profile_id,
            include_encode_tests=include_encode_tests,
        )

    # D. configuration values
    try:
        settings.validate(require_root=True)
        image_profile_id = settings.image.profile_id
        video_profile_id = settings.video.profile_id
    except PreviewConfigError as exc:
        add("configuration", "Configuration values", False, str(exc), stage=STAGE_CONFIGURATION)
        return report()
    add(
        "configuration",
        "Configuration values",
        True,
        f"image profile {image_profile_id}; video profile {video_profile_id}",
    )

    # A. preview root
    cache = PreviewCache(settings.root_path or Path(root_text), settings.image, settings.video)
    root_ok = False
    try:
        root_validation = cache.validate_root(create=True)
        free_bytes = root_validation.free_bytes
        total_bytes = root_validation.total_bytes
        root_ok = True
        add("preview-root", "Preview storage directory", True, root_validation.message)
    except PreviewError as exc:
        add("preview-root", "Preview storage directory", False, str(exc), stage=exc.stage)
    except Exception as exc:  # pragma: no cover - defensive
        add(
            "preview-root",
            "Preview storage directory",
            False,
            f"JVVV cannot use the preview directory {root_text}: {exc}",
            stage=STAGE_PREVIEW_ROOT,
        )

    # C. image backend
    try:
        backend_ok, backend_message = image_backend_check()
    except Exception as exc:  # pragma: no cover - defensive
        backend_ok, backend_message = False, f"The image backend could not be checked: {exc}"
    add("image-backend", "Image preview backend", backend_ok, backend_message)
    if include_encode_tests:
        if root_ok and backend_ok:
            try:
                add("image-test", "Image preview test encode", True, image_tester(cache))
            except PreviewError as exc:
                add("image-test", "Image preview test encode", False, str(exc), stage=exc.stage)
            except Exception as exc:  # pragma: no cover - defensive
                add("image-test", "Image preview test encode", False, str(exc))
        else:
            add(
                "image-test",
                "Image preview test encode",
                False,
                "Not run because an earlier check failed.",
                skipped=True,
            )

    # B. FFmpeg
    explicit = settings.ffmpeg_path_or_none
    try:
        ffmpeg_path = ffmpeg_finder(explicit)
    except Exception as exc:  # pragma: no cover - defensive
        ffmpeg_path = None
        find_error = str(exc)
    else:
        find_error = ""
    if ffmpeg_path:
        add("ffmpeg-found", "FFmpeg executable", True, ffmpeg_path)
    else:
        if explicit:
            detail = (
                f"The configured FFmpeg executable does not exist:\n{explicit}\n\n"
                "Choose the ffmpeg executable in Settings or clear the field to search PATH."
            )
        else:
            detail = (
                "FFmpeg could not be found on PATH. Install FFmpeg (with libx264) or "
                "choose its ffmpeg executable in Settings."
            )
        if find_error:
            detail += f"\n\n{find_error}"
        add("ffmpeg-found", "FFmpeg executable", False, detail, stage=STAGE_FFMPEG_START)

    capabilities: FfmpegCapabilities | None = None
    if ffmpeg_path:
        try:
            capabilities = ffmpeg_prober(ffmpeg_path)
            ffmpeg_version = capabilities.version
            add("ffmpeg-version", "FFmpeg version", True, capabilities.version)
        except PreviewError as exc:
            add("ffmpeg-version", "FFmpeg version", False, str(exc), stage=exc.stage)
        except Exception as exc:  # pragma: no cover - defensive
            add("ffmpeg-version", "FFmpeg version", False, str(exc), stage=STAGE_FFMPEG_START)
    else:
        add("ffmpeg-version", "FFmpeg version", False, "Not run because FFmpeg was not found.", skipped=True)

    if capabilities is not None:
        encoder_available = capabilities.has_libx264
        try:
            require_libx264(capabilities)
            add("ffmpeg-encoder", f"H.264 encoder ({FFMPEG_ENCODER})", True, "available")
        except PreviewError as exc:
            add("ffmpeg-encoder", f"H.264 encoder ({FFMPEG_ENCODER})", False, str(exc), stage=exc.stage)
    else:
        add(
            "ffmpeg-encoder",
            f"H.264 encoder ({FFMPEG_ENCODER})",
            False,
            "Not run because FFmpeg could not be started.",
            skipped=True,
        )

    if include_encode_tests:
        if root_ok and ffmpeg_path and capabilities is not None and encoder_available:
            try:
                add("video-test", "Video preview test encode", True, video_tester(ffmpeg_path, cache))
            except PreviewError as exc:
                add("video-test", "Video preview test encode", False, str(exc), stage=exc.stage)
            except Exception as exc:  # pragma: no cover - defensive
                add("video-test", "Video preview test encode", False, str(exc))
        else:
            add(
                "video-test",
                "Video preview test encode",
                False,
                "Not run because an earlier check failed.",
                skipped=True,
            )

    result = report()
    # A skipped step means an earlier failure already exists, so the overall
    # verdict is FAIL whenever anything was skipped.
    if any(step.skipped for step in steps):
        result = replace(result, passed=False)
    return result


def preflight_preview_configuration(settings: PreviewSettings) -> PreviewValidationReport:
    """The lightweight scan-start revalidation (spec §27)."""

    return validate_preview_configuration(settings, include_encode_tests=False)


# --------------------------------------------------------------------------
# Status records persisted per file (database.file_preview_status)
# --------------------------------------------------------------------------


def status_record_for(result: PreviewResult, content_hash: bytes | None) -> dict[str, Any] | None:
    """Translate a :class:`PreviewResult` into ``file_preview_status`` values."""

    source_hash: bytes | None = None
    if content_hash is not None:
        try:
            source_hash = bytes.fromhex(sha256_hex(content_hash))
        except PreviewError:
            # Only a real 32-byte SHA-256 may be stored; anything else is left
            # blank rather than making the catalogue write fail.
            source_hash = None
    if result.status in {PREVIEW_GENERATED, PREVIEW_REUSED}:
        return {
            "media_kind": result.media_kind,
            "profile_id": result.profile_id,
            "status": DB_STATUS_AVAILABLE,
            "source_hash": source_hash,
            "preview_size": int(result.size_bytes),
            "preview_width": result.width,
            "preview_height": result.height,
            "preview_duration_ms": result.duration_ms,
            "generated_at": _utc_now(),
            "error_stage": None,
            "error_message": "",
        }
    if result.status == PREVIEW_FAILED:
        message = result.message
        if result.detail:
            message = f"{message} — {result.detail}" if message else result.detail
        return {
            "media_kind": result.media_kind,
            "profile_id": result.profile_id,
            "status": DB_STATUS_FAILED,
            "source_hash": source_hash,
            "error_stage": result.stage,
            "error_message": message,
        }
    if result.status == PREVIEW_SKIPPED_STORAGE:
        message = result.message
        if result.detail:
            message = f"{message} — {result.detail}" if message else result.detail
        return {
            "media_kind": result.media_kind,
            "profile_id": result.profile_id,
            "status": DB_STATUS_MISSING,
            "source_hash": source_hash,
            "error_stage": STAGE_STORAGE_UNAVAILABLE,
            "error_message": message,
        }
    if result.status == PREVIEW_SKIPPED_UNSUPPORTED:
        if result.media_kind not in PREVIEW_MEDIA_KINDS:
            return None
        return {
            "media_kind": result.media_kind,
            "profile_id": result.profile_id or "-",
            "status": DB_STATUS_UNSUPPORTED,
            "source_hash": source_hash,
            "error_stage": result.stage,
            "error_message": result.message,
        }
    return None


def hash_unavailable_status_record(media_kind: str, profile_id: str) -> dict[str, Any]:
    return {
        "media_kind": media_kind,
        "profile_id": profile_id,
        "status": DB_STATUS_MISSING,
        "source_hash": None,
        "error_stage": STAGE_HASH_UNAVAILABLE,
        "error_message": (
            "No SHA-256 hash could be recorded for this file, so no preview was attempted."
        ),
    }


def _utc_now() -> str:
    from .database import utc_now

    return utc_now()


# --------------------------------------------------------------------------
# Lookups used by the UI (Properties, context menus)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewFileInfo:
    media_kind: str
    profile_id: str
    path: Path
    exists: bool
    valid: bool
    size_bytes: int
    width: int | None
    height: int | None
    duration_ms: int | None
    message: str


def preview_cache_for(settings: PreviewSettings) -> PreviewCache | None:
    root = settings.root_path
    if root is None:
        return None
    try:
        settings.image.validate()
        settings.video.validate()
    except PreviewConfigError:
        return None
    return PreviewCache(root, settings.image, settings.video)


def inspect_preview_file(
    settings: PreviewSettings,
    media_kind: str,
    content_hash: bytes | str | None,
) -> PreviewFileInfo | None:
    """Locate and validate the preview for a file without any generator.

    Returns ``None`` when no path can be derived (no root, unsupported kind,
    or no hash).  Never raises.
    """

    if media_kind not in PREVIEW_MEDIA_KINDS or content_hash is None:
        return None
    cache = preview_cache_for(settings)
    if cache is None:
        return None
    try:
        digest = sha256_hex(content_hash)
        path = cache.preview_path(media_kind, digest)
        profile_id = cache.profile_id(media_kind)
    except PreviewError:
        return None
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        return PreviewFileInfo(
            media_kind,
            profile_id,
            path,
            False,
            False,
            0,
            None,
            None,
            None,
            "The preview file does not exist at the expected location.",
        )
    try:
        if media_kind == "image":
            validation = validate_image_preview(path)
            duration_ms = None
        else:
            video_validation = validate_video_preview(path)
            validation = video_validation
            duration_ms = video_validation.duration_ms
    except Exception as exc:  # pragma: no cover - validators never raise
        return PreviewFileInfo(
            media_kind, profile_id, path, True, False, 0, None, None, None, str(exc)
        )
    return PreviewFileInfo(
        media_kind,
        profile_id,
        path,
        True,
        bool(validation.valid),
        int(validation.size_bytes),
        validation.width,
        validation.height,
        duration_ms,
        validation.message if not validation.valid else "",
    )


# --------------------------------------------------------------------------
# The scan-time service
# --------------------------------------------------------------------------


class PreviewService:
    """Generate or reuse previews for one scan and keep the statistics."""

    def __init__(
        self,
        settings: PreviewSettings,
        *,
        ffmpeg_path: str | None = None,
        cache: PreviewCache | None = None,
        image_generator: Any | None = None,
        video_generator: Any | None = None,
        log_callback: LogCallback | None = None,
    ) -> None:
        settings.validate(require_root=True)
        self.settings = settings
        self.cache = cache or PreviewCache(
            settings.root_path or Path(settings.root_directory),
            settings.image,
            settings.video,
        )
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg(settings.ffmpeg_path_or_none)
        self.image_generator = image_generator or ImagePreviewGenerator(self.cache)
        if video_generator is not None:
            self.video_generator = video_generator
        elif self.ffmpeg_path:
            self.video_generator = VideoPreviewGenerator(self.cache, self.ffmpeg_path)
        else:
            self.video_generator = None
        self.statistics = PreviewStatistics(mode=MODE_ENABLED)
        self.log_callback = log_callback
        self._in_flight: set[Path] = set()

    # -- helpers -----------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.cache.root

    def _log(self, message: str) -> None:
        if self.log_callback is not None:
            self.log_callback(message)

    def _count(self, media_kind: str, outcome: str) -> None:
        attribute = f"{media_kind}_{outcome}"
        setattr(self.statistics, attribute, getattr(self.statistics, attribute) + 1)

    def _record_failure(
        self,
        error: PreviewError,
        *,
        media_kind: str,
        profile_id: str,
        destination: Path | None,
        digest: str | None,
        relative_path: str,
        source_name: str,
        volume_id: int,
        volume_label: str,
    ) -> PreviewResult:
        self._count(media_kind, "failed")
        failure = PreviewFailure(
            source_name=source_name,
            relative_path=relative_path,
            volume_id=int(volume_id),
            volume_label=volume_label or "",
            media_kind=media_kind,
            sha256=digest,
            preview_path=str(destination) if destination is not None else None,
            profile_id=profile_id,
            stage=error.stage,
            message=error.message,
            detail=error.detail,
        )
        self.statistics.failures.append(failure)
        self._log(f"Offline preview failed ({error.stage}) for {relative_path}: {error}")
        if error.stage in STORAGE_STAGES and self.statistics.storage_unavailable_reason is None:
            reason = error.message
            if error.detail:
                reason = f"{reason} — {error.detail}"
            self.statistics.storage_unavailable_reason = reason
            self._log(
                "Preview storage became unavailable; no further previews will be "
                f"attempted during this scan: {reason}"
            )
        return PreviewResult(
            status=PREVIEW_FAILED,
            media_kind=media_kind,
            profile_id=profile_id,
            path=destination,
            stage=error.stage,
            message=error.message,
            detail=error.detail,
        )

    def _skip_for_storage(self, media_kind: str, profile_id: str, destination: Path) -> PreviewResult:
        """Explicit ``skipped-storage-unavailable`` outcome for one candidate (spec §17)."""

        self.statistics.storage_skipped += 1
        return PreviewResult(
            status=PREVIEW_SKIPPED_STORAGE,
            media_kind=media_kind,
            profile_id=profile_id,
            path=destination,
            stage=STAGE_STORAGE_UNAVAILABLE,
            message="Not attempted because preview storage became unavailable earlier in this scan.",
            detail=self.statistics.storage_unavailable_reason,
        )

    # -- main entry point ----------------------------------------------------
    def ensure_preview(
        self,
        *,
        media_kind: str,
        source: Path,
        content_hash: bytes | str,
        relative_path: str,
        source_name: str,
        volume_id: int,
        volume_label: str = "",
        cancel_callback: CancelCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
        expected_duration_ms: int | None = None,
    ) -> PreviewResult:
        """Return the explicit outcome for one hashed, stable source file."""

        if media_kind not in PREVIEW_MEDIA_KINDS:
            return PreviewResult(
                status=PREVIEW_SKIPPED_UNSUPPORTED,
                media_kind=media_kind,
                profile_id="",
                message="Offline previews are generated for images and videos only.",
            )
        profile_id = self.cache.profile_id(media_kind)
        try:
            digest = sha256_hex(content_hash)
        except PreviewError as exc:
            return self._record_failure(
                exc,
                media_kind=media_kind,
                profile_id=profile_id,
                destination=None,
                digest=None,
                relative_path=relative_path,
                source_name=source_name,
                volume_id=volume_id,
                volume_label=volume_label,
            )
        destination = self.cache.preview_path(media_kind, digest)

        if cancel_callback is not None and cancel_callback():
            raise PreviewCancelled("Preview generation cancelled.")

        replaced_corrupt = False
        try:
            existing = destination.is_file()
        except OSError:
            existing = False
        if existing:
            if media_kind == "image":
                image_validation = validate_image_preview(destination)
                valid = image_validation.valid
                width, height = image_validation.width, image_validation.height
                duration_ms = None
                size_bytes = image_validation.size_bytes
                reason = image_validation.message
            else:
                video_validation = validate_video_preview(destination)
                valid = video_validation.valid
                width, height = video_validation.width, video_validation.height
                duration_ms = video_validation.duration_ms
                size_bytes = video_validation.size_bytes
                reason = video_validation.message
            if valid:
                self._count(media_kind, "reused")
                return PreviewResult(
                    status=PREVIEW_REUSED,
                    media_kind=media_kind,
                    profile_id=profile_id,
                    path=destination,
                    size_bytes=int(size_bytes),
                    width=width,
                    height=height,
                    duration_ms=duration_ms,
                )
            if self.statistics.storage_unavailable_reason is not None:
                # Spec §11 asks for the corrupt file to be logged; it cannot be
                # regenerated now, so it is skipped like any other candidate.
                self._log(
                    "Existing preview failed validation but cannot be regenerated because "
                    f"preview storage became unavailable earlier in this scan: {destination} "
                    f"({reason})"
                )
            else:
                # Counted in ``corrupt_replaced`` only once regeneration succeeds,
                # so the summary's "were regenerated" line never over-counts a
                # corrupt preview whose replacement then failed (spec §11, §15).
                replaced_corrupt = True
                self._log(
                    f"Existing preview failed validation and will be regenerated: {destination} "
                    f"({reason})"
                )

        # Reuse only reads the store, so it stays possible after preview storage
        # failed; only *generation* stops (spec §10, §17).  Checked after the
        # reuse path so an existing valid preview is never reported as skipped.
        if self.statistics.storage_unavailable_reason is not None:
            return self._skip_for_storage(media_kind, profile_id, destination)

        if media_kind == "video" and self.video_generator is None:
            return self._record_failure(
                PreviewError(
                    STAGE_FFMPEG_START,
                    "FFmpeg is not available, so the video preview could not be created.",
                    detail=(
                        f"Configured FFmpeg path: {self.settings.ffmpeg_path_or_none}"
                        if self.settings.ffmpeg_path_or_none
                        else "No ffmpeg executable was found on PATH."
                    ),
                ),
                media_kind=media_kind,
                profile_id=profile_id,
                destination=destination,
                digest=digest,
                relative_path=relative_path,
                source_name=source_name,
                volume_id=volume_id,
                volume_label=volume_label,
            )

        if destination in self._in_flight:
            # Single-worker generation makes this unreachable, but never start
            # two competing encoders for one final path (spec §13).
            return self._record_failure(
                PreviewError(
                    STAGE_TEMP_FILE,
                    "Another preview for the same content is still being generated.",
                ),
                media_kind=media_kind,
                profile_id=profile_id,
                destination=destination,
                digest=digest,
                relative_path=relative_path,
                source_name=source_name,
                volume_id=volume_id,
                volume_label=volume_label,
            )

        self._in_flight.add(destination)
        try:
            if media_kind == "image":
                result = self.image_generator.generate(
                    Path(source),
                    destination,
                    cancel_callback=cancel_callback,
                )
            else:
                result = self.video_generator.generate(
                    Path(source),
                    destination,
                    cancel_callback=cancel_callback,
                    progress_callback=_video_progress_adapter(progress_callback),
                    expected_duration_ms=expected_duration_ms,
                )
        except PreviewCancelled:
            raise
        except PreviewError as exc:
            return self._record_failure(
                exc,
                media_kind=media_kind,
                profile_id=profile_id,
                destination=destination,
                digest=digest,
                relative_path=relative_path,
                source_name=source_name,
                volume_id=volume_id,
                volume_label=volume_label,
            )
        except OSError as exc:
            return self._record_failure(
                PreviewError(
                    classify_os_error(exc, STAGE_TEMP_FILE),
                    "Could not write preview.",
                    detail=os_error_detail(exc),
                ),
                media_kind=media_kind,
                profile_id=profile_id,
                destination=destination,
                digest=digest,
                relative_path=relative_path,
                source_name=source_name,
                volume_id=volume_id,
                volume_label=volume_label,
            )
        except Exception as exc:
            # A preview failure must never abort or roll back an otherwise
            # valid catalogue scan (spec §16, §51-F), so even a programming
            # error inside a backend becomes a visible per-file failure.
            return self._record_failure(
                PreviewError(
                    STAGE_UNEXPECTED_ERROR,
                    "The preview generator failed unexpectedly.",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
                media_kind=media_kind,
                profile_id=profile_id,
                destination=destination,
                digest=digest,
                relative_path=relative_path,
                source_name=source_name,
                volume_id=volume_id,
                volume_label=volume_label,
            )
        finally:
            self._in_flight.discard(destination)

        self._count(media_kind, "generated")
        self.statistics.bytes_written += int(result.bytes_written)
        if replaced_corrupt:
            self.statistics.corrupt_replaced += 1
            result = replace(result, replaced_corrupt=True)
        return result


def _video_progress_adapter(
    progress_callback: Callable[[str], None] | None,
) -> Callable[[float | None, int | None], None] | None:
    if progress_callback is None:
        return None

    def report(fraction: float | None, out_time_ms: int | None) -> None:
        if fraction is not None:
            progress_callback(f"{int(round(max(0.0, min(1.0, fraction)) * 100))}% of preview encode")
        elif out_time_ms is not None:
            progress_callback(f"{out_time_ms / 1000:.0f} s of video encoded")
        else:
            progress_callback("encoding preview")

    return report


def disabled_statistics(reason: str = "") -> PreviewStatistics:
    return PreviewStatistics(mode=MODE_DISABLED, message=reason)


def skipped_preflight_statistics(reason: str) -> PreviewStatistics:
    return PreviewStatistics(
        mode=MODE_SKIPPED_PREFLIGHT,
        message=(
            "Offline preview generation was skipped for this scan because the preview "
            f"configuration failed its preflight check and the user chose to continue: {reason}"
        ),
    )


__all__ = [
    "DB_STATUS_AVAILABLE",
    "DB_STATUS_FAILED",
    "DB_STATUS_MISSING",
    "DB_STATUS_UNSUPPORTED",
    "MODE_DISABLED",
    "MODE_ENABLED",
    "MODE_SKIPPED_PREFLIGHT",
    "PREVIEW_SKIPPED_DISABLED",
    "PreviewFileInfo",
    "PreviewService",
    "PreviewStatistics",
    "PreviewValidationReport",
    "SCAN_OUTCOME_COMPLETED_WITH_WARNINGS",
    "STAGE_STORAGE_UNAVAILABLE",
    "ValidationStep",
    "disabled_statistics",
    "hash_unavailable_status_record",
    "inspect_preview_file",
    "preflight_preview_configuration",
    "preview_cache_for",
    "preview_warning_message",
    "scan_outcome",
    "skipped_preflight_statistics",
    "status_record_for",
    "validate_preview_configuration",
]
