"""Explicit retirement of a verified project's original on the production share."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import json
import os
from pathlib import Path

from .archive_io import _kernel, _open_handle, file_identity, hold_folder, is_link
from .archive_job import _checked_folder, _scan_tree
from .utils import ConnectedVolumeResolver, physical_disk_key


PROJECT_DRIVE = Path("Q:/")


def may_be_project_source(source):
    # Mapped drive letters can be saved as their canonical UNC paths.
    # The worker resolves Q: and checks the exact share before touching files.
    path = Path(source)
    return PROJECT_DRIVE in path.parents or path.drive.startswith("\\\\")


def registered_copies(db, project_id):
    volumes = {v["uuid"]: v for v in db.list_volumes()}
    return [(volumes[row["volume_uuid"]], row) for row in db.archive_records.copy_details(project_id)
            if row["volume_uuid"] in volumes]


def _check_copies(db, copies, files, folders, algorithm):
    # Use the last known catalogue contents, including any more recent rescans.
    distinct = set()
    resolver = ConnectedVolumeResolver()
    for volume, copy in copies:
        prefix = copy["folder"] + "/"
        bounds = (volume["id"], prefix, copy["folder"] + "0")
        root = db.get_folder_by_path(volume["id"], copy["folder"])
        if root is None or root["missing"]:
            continue
        current = {row[0][len(prefix):]: row[1] for row in db.connection.execute(
            "SELECT relative_path,content_hash FROM files WHERE volume_id=? AND relative_path>=? "
            "AND relative_path<? AND missing=0 AND content_hash_algorithm=?", (*bounds, algorithm))}
        current_dirs = {row[0][len(prefix):] for row in db.connection.execute(
            "SELECT relative_path FROM folders WHERE volume_id=? AND relative_path>=? AND relative_path<? AND missing=0",
            bounds)}
        if not folders.issubset(current_dirs) or any(current.get(path) != digest for path, digest in files.items()):
            continue
        attached, status = resolver.resolve_details(volume)
        if status == "Relink Required":
            continue
        disk = physical_disk_key(attached) if attached else None
        distinct.add(("disk", disk) if disk is not None else (volume["identity_kind"], volume["identity_token"].casefold()))
    if len(distinct) < 2:
        raise OSError("Two distinct verified archive drives must still contain the project in the catalogue.")


def _mark_for_deletion(kernel, handle):
    # FILE_DISPOSITION_INFO on an exclusively held object. Windows removes that
    # exact object on close and refuses to remove a directory with new children.
    disposition = ctypes.c_ubyte(1)
    set_info = kernel.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise ctypes.WinError(ctypes.get_last_error())


class _LockedProject:
    def __init__(self, source, rows, resuming, locks):
        self.source = source
        self.rows = rows
        self.resuming = resuming
        self.locks = locks
        self.kernel = _kernel()
        self.handles = {}

    def hold(self, path, *, directory):
        relative = path.relative_to(self.source).as_posix() if path != self.source else ""
        saved = self.rows.get(relative)
        if saved is None or bool(saved["directory"]) != directory:
            raise OSError(f"The project file list changed: {path}")
        # Pin this object before checking metadata. A replacement cannot take
        # its place between comparison and deletion via the same handle.
        owner = self.locks.enter_context(ExitStack())
        handle = owner.enter_context(_open_handle(self.kernel, path, 0x10080, 3 if directory else 1, 0x02200000))
        info = path.lstat()
        if is_link(info) or (not directory and info.st_size != saved["size"]):
            raise OSError(f"The original project changed: {path}")
        if (not directory or not self.resuming) and info.st_mtime_ns != saved["mtime"]:
            raise OSError(f"The modification time changed: {path}")
        self.handles[relative] = (handle, owner)

    def remove(self, entry):
        handle, owner = self.handles[entry.relative]
        try:
            _mark_for_deletion(self.kernel, handle)
            owner.close()
        except OSError as exc:
            raise OSError(f"Deletion stopped at {self.source / entry.relative}. "
                          f"The project record was kept. {exc}") from exc


def delete_project_original(db, project_id, progress=lambda message: None, *, queued_sources=()):
    records = db.archive_records
    project = records.project(project_id)
    removal = records.removal(project_id)
    if db.read_only or project is None or (removal and removal["deleted_at"]):
        raise OSError("This project has no original available for deletion.")
    source = Path(project["source"])
    drive = PROJECT_DRIVE.resolve(strict=True)
    if source.is_relative_to(PROJECT_DRIVE):
        source = drive / source.relative_to(PROJECT_DRIVE)
    if os.name != "nt" or drive not in source.parents or ".." in source.parts:
        raise OSError("Only a saved project folder inside Q:\\ can be deleted.")
    if db.path.resolve().is_relative_to(source):
        raise OSError("The open catalogue is inside the project.")
    for queued in queued_sources:
        other = Path(queued).resolve()
        if source.is_relative_to(other) or other.is_relative_to(source):
            raise OSError("Finish or remove the outstanding archive task for this source first.")

    rows = {r["path"]: r for r in records.registry.execute(
        "SELECT path,directory,size,mtime,digest FROM archive_entries WHERE project_id=?", (project_id,))}
    if "" not in rows or not rows[""]["directory"]:
        raise OSError("The saved project file list is missing.")
    progress("Checking the two archive records…")
    _check_copies(db, registered_copies(db, project_id),
                  {p: r["digest"] for p, r in rows.items() if not r["directory"]},
                  {p for p, r in rows.items() if p and r["directory"]}, project["algorithm"])

    with ExitStack() as locks:
        # Pin ancestors before opening descendants. A junction or swapped path
        # must never redirect deletion to a different folder.
        parent = drive
        for part in ("", *source.parent.relative_to(drive).parts):
            parent = parent / part
            _checked_folder(parent)
            locks.enter_context(hold_folder(parent))
            _checked_folder(parent)
        parent_id = list(file_identity(source.parent.lstat()))
        try:
            original = _checked_folder(source)
        except FileNotFoundError:
            if removal and json.loads(removal["identity"])[:2] == parent_id:
                # A crash after the last handle closed but before SQLite committed.
                records.finish_removal(project_id)
                return
            raise
        identity = json.dumps(parent_id + list(file_identity(original)))
        if not original.st_ino or not parent_id[1] or (removal and removal["identity"] != identity):
            raise OSError("The original project folder cannot be identified safely.")

        progress("Checking source names, sizes and modification times…")
        locked = _LockedProject(source, rows, bool(removal), locks)
        entries = _scan_tree(source, lambda: None, locked)
        if file_identity(_checked_folder(source)) != file_identity(original):
            raise OSError("The original folder changed while preparing deletion.")
        if not removal and rows.keys() != locked.handles.keys():
            raise OSError("The source file list differs from the verified archive.")
        if {entry.relative for entry in _scan_tree(source, lambda: None)} != locked.handles.keys():
            raise OSError("The source file list changed during the check.")
        if not removal:
            records.begin_removal(project_id, identity)
        progress("Deleting the checked original files…")
        for entry in reversed(entries):
            locked.remove(entry)
        try:
            source.lstat()
        except FileNotFoundError:
            if list(file_identity(source.parent.lstat())) != parent_id:
                raise OSError("The project share disconnected before deletion could be confirmed.")
        else:
            raise OSError("The source folder is still present. Its project record was kept.")
        records.finish_removal(project_id)
