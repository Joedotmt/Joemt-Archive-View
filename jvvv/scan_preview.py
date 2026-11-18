"""Prepare offline previews privately from the disk scan's hash read."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import tempfile
from threading import Event

from .archive_job import MediaInput, check_archive_write_locations
from .database import format_timestamp
from .media_metadata import MediaInspectionCancelled
from .preview_cache import PreviewError
from .utils import physical_disk_key


def separate_disks(source, scratch, store):
    if os.name == "nt":
        disk = physical_disk_key(source)
        other = [physical_disk_key(path) for path in (scratch, store)]
        return disk is not None and all(key is not None and key != disk for key in other)
    return all(source.stat().st_dev != path.stat().st_dev for path in (scratch, store))


class ScanPreviewPreparation:
    """One encoder, one input being hashed. Only compressed previews accumulate."""

    @classmethod
    def open(cls, source, service, cancelled):
        scratch = Path(tempfile.gettempdir())
        store = service.root
        try:
            if not separate_disks(source, scratch, store):
                return None
            check_archive_write_locations(source, scratch, store)
            with ExitStack() as resources:
                inputs = resources.enter_context(tempfile.TemporaryDirectory(prefix="jvvv-scan-input-", dir=scratch))
                outputs = resources.enter_context(tempfile.TemporaryDirectory(prefix=".jvvv-scan.tmp-", dir=store))
                worker = resources.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-preview"))
                return cls(service, cancelled, Path(inputs), Path(outputs), worker, resources.pop_all())
        except OSError:
            return None

    def __init__(self, service, cancelled, inputs, outputs, worker, resources):
        self.service = service
        self.cancelled = cancelled
        self.inputs = inputs
        self.outputs = outputs
        self.worker = worker
        self.resources = resources
        self.stop = Event()
        self.pending = None
        self.current = None

    def begin(self, kind, suffix, info, previous):
        self.discard_input()
        if kind not in {"image", "video"}:
            return
        try:
            if (previous is not None and previous["content_hash"]
                    and previous["content_hash_algorithm"] == self.service.cache.hash_algorithm
                    and previous["size_bytes"] == info.st_size
                    and previous["modified_at"] == format_timestamp(info.st_mtime)):
                if self.service.cache.preview_path(kind, previous["content_hash"]).is_file():
                    return  # The normal post-hash validation also handles changed files.
            if info.st_size > shutil.disk_usage(self.inputs).free // 2:
                return
            self.current = MediaInput(self.inputs, suffix, info.st_size, share_video=kind == "video", check=self._check)
        except (OSError, PreviewError):
            self.discard_input()

    def write(self, block):
        try:
            self.current.write(block)
        except (OSError, MemoryError, ValueError):
            # A growing file can exceed its mapping. Let the hash stability
            # check decide whether to retry the file.
            self.discard_input()

    def _check(self):
        if self.cancelled():
            raise MediaInspectionCancelled("Media inspection cancelled.")

    def _check_worker(self):
        if self.stop.is_set():
            raise MediaInspectionCancelled("Media inspection cancelled.")

    def inspect(self, extractor, extension):
        try:
            return self.current.inspect(extractor, extension, self.cancelled)
        except (OSError, ValueError, MemoryError):
            self.discard_input()
            return None

    def discard_input(self):
        if self.current is not None:
            try:
                self.current.close()
            except OSError:
                pass
            self.current = None

    def output(self, kind, digest):
        name = self.service.cache.preview_path(kind, digest).name
        return self.outputs / digest.hex()[:2] / name

    def submit(self, kind, digest, metadata):
        if self.current is None:
            return
        try:
            if self.service.cache.preview_path(kind, digest).is_file():
                return
            self.finish()  # Bound scratch use while the next file is being hashed.
            if self.stop.is_set():
                return
            output = self.output(kind, digest)
            if not output.exists():
                self.current.check = self._check_worker
                self.pending = self.worker.submit(self._encode, self.current, output, kind, metadata)
                self.current = None  # The worker now owns the input.
        except OSError:
            # A lost scratch or preview drive does not invalidate the hash.
            pass
        finally:
            self.discard_input()

    def _encode(self, captured, output, kind, metadata):
        cancelled = self.stop.is_set
        try:
            if kind == "image":
                self.service.image_generator.generate(captured.materialize(), output, cancel_callback=cancelled)
            elif self.service.video_generator is not None:
                staged = captured.shared is not None
                self.service.video_generator.generate(
                    captured.path if staged else captured.materialize(), output,
                    **({"staged_video": captured} if staged else {}),
                    expected_duration_ms=metadata.duration_ms if metadata else None,
                    video_codec=metadata.video_codec if metadata else None, cancel_callback=cancelled,
                )
        except Exception:
            # Preparation is optional. The normal preview phase diagnoses failures
            # against the original and records the usual per-file outcome.
            output.unlink(missing_ok=True)
        finally:
            captured.close()

    def finish(self):
        if self.pending is not None:
            try:
                # Only the scan thread calls the caller's cancellation callback.
                while not self.pending.done():
                    if self.cancelled():
                        self.stop.set()
                    wait((self.pending,), timeout=0.1)
                self.pending.result()
            except OSError:
                # The captured input or private output vanished during cleanup.
                pass
            finally:
                self.pending = None

    def close(self):
        self.stop.set()
        self.discard_input()
        try:
            self.resources.close()
        except OSError as exc:
            self.service._log(f"Could not clean up temporary scan previews: {exc}")
