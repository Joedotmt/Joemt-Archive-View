"""Catalogue-local scheduling and persistence for unattended operations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, gettempdir
from uuid import uuid4

from .archive_job import check_archive_write_locations
from .preview_service import preflight_preview_configuration
from .utils import physical_disk_key


DONE = {"completed", "warning"}
ACTIONABLE = {"ready", "waiting", "relink"}
STATUSES = {
    "queued": "Queued", "ready": "Ready", "running": "Running", "paused": "Paused",
    "waiting": "Waiting for Drive", "relink": "Relink Required", "failed": "Failed",
    "completed": "Completed", "warning": "Completed with Warnings",
}
STATUS_TABS = {
    "queued": "Pending", "ready": "Pending", "running": "Pending",
    "waiting": "Pending", "relink": "Pending", "paused": "Pending",
    "failed": "Failed", "completed": "Completed", "warning": "Completed",
}


def prepare_operation_previews(settings, source=None):
    if settings is None or not settings.enabled:
        return None, None
    try:
        locations = [Path(gettempdir())]
        if settings.root_path is not None:
            locations.append(settings.root_path)
        check_archive_write_locations(source, *locations)
        report = preflight_preview_configuration(settings)
        if report.passed:
            return settings, None
        failure = report.first_failure
        return None, f"{failure.label}: {failure.detail}" if failure else "Preview preflight failed."
    except (OSError, ValueError) as exc:
        return None, str(exc)


@dataclass(frozen=True)
class Operation:
    id: str
    kind: str
    volume_uuid: str
    volume_label: str
    source: str = ""
    requires: str = ""
    state: str = "queued"
    detail: str = ""
    legacy_journal: bool = False

    @property
    def tab(self) -> str:
        return STATUS_TABS[self.state]

    @property
    def reorderable(self) -> bool:
        return self.tab == "Pending" and self.state != "running"

    @property
    def kind_label(self) -> str:
        return "Scan Drive" if self.kind == "scan" else "Archive Project"

    @property
    def title(self) -> str:
        if self.source:
            return f"{self.kind_label} · {Path(self.source).name} → {self.volume_label}"
        return f"{self.kind_label} · {self.volume_label}"


@dataclass(frozen=True)
class OperationDisks:
    destination: tuple[int, int] | None
    source: tuple[int, int] | None = None
    has_source: bool = False
    shared_qnap: bool = False

    @classmethod
    def resolve(cls, job: Operation, destination: str):
        source = Path(job.source) if job.source else None
        return cls(
            physical_disk_key(destination),
            physical_disk_key(source) if source else None,
            source is not None,
            source is not None and source.drive.casefold() == "q:",
        )

    def conflicts_with(self, other: OperationDisks) -> bool:
        if self.destination is None or other.destination is None or self.destination == other.destination:
            return True
        for writer, reader in ((self, other), (other, self)):
            if reader.has_source and (
                writer.destination == reader.source or (reader.source is None and not reader.shared_qnap)
            ):
                return True
        return (
            self.has_source and other.has_source and self.source == other.source
            and not (self.shared_qnap and other.shared_qnap)
        )


class OperationsQueue:
    def __init__(self, path: Path, catalogue_uuid: str):
        self.path = path / "operations-queue.json"
        self.catalogue_uuid = catalogue_uuid
        self.jobs: list[Operation] = []
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved["catalogue_uuid"] != catalogue_uuid:
                raise OSError("The saved queue belongs to another catalogue.")
            self.jobs = [
                replace(job, state="paused", detail="Interrupted. Press Apply to resume.")
                if job.state == "running" else job
                for job in (Operation(**row) for row in saved["jobs"])
            ]

    def _save(self, jobs: list[Operation]) -> None:
        check_archive_write_locations(None, self.path.parent, self.path)
        payload = {"catalogue_uuid": self.catalogue_uuid, "jobs": [asdict(job) for job in jobs]}
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    prefix=".jvvv-queue-", delete=False) as output:
                temporary = Path(output.name)
                json.dump(payload, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.jobs = jobs

    def get(self, job_id: str) -> Operation | None:
        return next((job for job in self.jobs if job.id == job_id), None)

    def pending_scan(self, volume_uuid: str) -> Operation | None:
        return next((job for job in reversed(self.jobs)
                     if job.kind == "scan" and job.volume_uuid == volume_uuid and job.state not in DONE), None)

    def add(self, kind: str, volume, label: str, *, source="", requires="", legacy=False) -> Operation:
        job = Operation(uuid4().hex, kind, volume["uuid"], label, source, requires,
                        "paused" if legacy else "queued", legacy_journal=legacy)
        self._save([*self.jobs, job])
        return job

    def set_state(self, job_id: str, state: str, detail: str = "") -> None:
        current = self.get(job_id)
        if current is not None and (state, detail) != (current.state, current.detail):
            self._save([replace(job, state=state, detail=detail) if job.id == job_id else job
                        for job in self.jobs])

    def apply(self) -> None:
        jobs = [replace(job, state="ready", detail="")
                if job.state in {"queued", "paused"} else job for job in self.jobs]
        if jobs != self.jobs:
            self._save(jobs)

    def apply_count(self, *, active: bool) -> int:
        states = {"queued", "paused"}
        if not active:
            states |= ACTIONABLE
        return sum(job.state in states for job in self.jobs)

    def move(self, job_id: str, offset: int) -> None:
        job = self.get(job_id)
        if job is None or not job.reorderable:
            return
        jobs = self.jobs.copy()
        tab_positions = [i for i, item in enumerate(jobs) if item.tab == job.tab]
        row = next(row for row, i in enumerate(tab_positions) if jobs[i].id == job_id)
        if not 0 <= row + offset < len(tab_positions):
            return
        index, target = tab_positions[row], tab_positions[row + offset]
        if not jobs[target].reorderable:
            return
        jobs[index], jobs[target] = jobs[target], jobs[index]
        positions = {job.id: i for i, job in enumerate(jobs)}
        if any(job.requires and positions.get(job.requires, -1) >= i for i, job in enumerate(jobs)):
            raise ValueError("Keep the required scan above its archive job.")
        self._save(jobs)

    def remove(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None or job.state == "running":
            return
        if any(item.requires == job_id and item.state not in DONE for item in self.jobs):
            raise ValueError("Remove the dependent archive jobs before removing this scan.")
        self._save([item for item in self.jobs if item.id != job_id])

    def next_ready(self, availability, can_start=None) -> Operation | None:
        """Skip disconnected drives and blocked dependencies, preserving order per drive."""
        blocked = set()
        for job in self.jobs:
            if job.state in DONE or job.state == "failed" or job.state == "queued":
                continue
            if job.volume_uuid in blocked:
                continue
            blocked.add(job.volume_uuid)
            if job.state not in ACTIONABLE:
                continue
            dependency = self.get(job.requires) if job.requires else None
            if job.requires and (dependency is None or dependency.state not in DONE):
                self.set_state(job.id, "waiting", "Waiting for the required drive scan.")
                continue
            state, detail = availability(job)
            if state == "ready":
                if can_start is None or can_start(job):
                    return job
                continue
            self.set_state(job.id, state, detail)
        return None
