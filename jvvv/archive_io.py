"""File protection and destination read-back for HDD archive jobs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import re
import stat

from .content_hash import new_hasher


CHUNK_SIZE = 4 * 1024 * 1024
ARCHIVE_STAGE_PREFIX = ".jvvv-archive-"
ARCHIVE_STAGE_SUFFIX = ".partial"
ARCHIVE_STAGE_NAME = re.compile(r"\.jvvv-archive-[0-9a-f]{32}\.partial", re.IGNORECASE)


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def file_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    return (*file_identity(info), info.st_size, info.st_mtime_ns)


def _kernel():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _win_path(path: Path) -> str:
    name = str(path)
    if name.startswith("\\\\?\\"):
        return name
    if name.startswith("\\\\"):
        return "\\\\?\\UNC\\" + name[2:]
    return "\\\\?\\" + name


@contextmanager
def _open_handle(kernel, path: Path, access: int, sharing: int, flags: int):
    open_file = kernel.CreateFileW
    open_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    open_file.restype = wintypes.HANDLE
    handle = open_file(_win_path(path), access, sharing, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        yield handle
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def hold_folder(path: Path):
    """On Windows, keep the directory from being moved while paths use it."""
    if os.name == "nt":
        with _open_handle(_kernel(), path, 0x80, 0x3, 0x02200000):
            yield
    else:
        yield


class ProjectGuard:
    """Hold Windows read leases and watch for changes to the source directory tree."""

    def __init__(self, source: Path):
        self.source = source
        self.stack = ExitStack()
        self.kernel = _kernel() if os.name == "nt" else None
        self.notification = None

    def __enter__(self):
        if self.kernel is not None:
            watch = self.kernel.FindFirstChangeNotificationW
            watch.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
            watch.restype = wintypes.HANDLE
            # File names, directory names, attributes, size and last-write time.
            self.notification = watch(_win_path(self.source), True, 0x1F)
            if self.notification == ctypes.c_void_p(-1).value:
                raise OSError(f"Windows could not monitor the source project: {self.source}")
            close = self.kernel.FindCloseChangeNotification
            close.argtypes = [wintypes.HANDLE]
            self.stack.callback(close, self.notification)
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        return self

    def hold(self, path: Path, *, directory: bool) -> None:
        if self.kernel is None:
            return
        try:
            # File handles deny write and delete sharing. Directory handles deny
            # deletion/rename. The watcher catches additions inside directories.
            access = 0x80 if directory else 0x80000000
            sharing = 0x3 if directory else 0x1
            flags = 0x02200000 if directory else 0x00200000
            self.stack.enter_context(_open_handle(self.kernel, path, access, sharing, flags))
        except OSError as exc:
            raise OSError(f"Could not protect {path}. Close apps using the project. {exc}") from exc

    def check(self) -> None:
        if self.notification is not None:
            result = self.kernel.WaitForSingleObject(self.notification, 0)
            if result == 0:
                raise OSError("The source project changed during archiving. Its copy was not registered.")
            if result != 258:  # WAIT_TIMEOUT means no changes are pending.
                raise OSError("Windows stopped monitoring the source project.")

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


class DestinationReader:
    """Reuse a sector-aligned buffer for Windows cache-bypassing reads."""

    def __enter__(self):
        self.kernel = _kernel() if os.name == "nt" else None
        if self.kernel is not None:
            alloc = self.kernel.VirtualAlloc
            alloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
            alloc.restype = wintypes.LPVOID
            self.buffer = alloc(None, CHUNK_SIZE, 0x3000, 4)
            if not self.buffer:
                raise ctypes.WinError(ctypes.get_last_error())
            self.view = memoryview((ctypes.c_ubyte * CHUNK_SIZE).from_address(self.buffer)).cast("B")
            self.kernel.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
            self.kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        return self

    def __exit__(self, *_):
        if self.kernel is not None:
            self.view.release()
            self.kernel.VirtualFree(self.buffer, 0, 0x8000)

    def digest(self, path: Path, size: int, algorithm: str, check, cache=None) -> bytes:
        hasher = new_hasher(algorithm)
        read = 0
        if self.kernel is None:
            with path.open("rb") as input_file:
                while (block := input_file.read(CHUNK_SIZE)):
                    check()
                    hasher.update(block)
                    if cache is not None:
                        cache.write(block)
                    read += len(block)
        else:
            # NO_BUFFERING | SEQUENTIAL_SCAN | OPEN_REPARSE_POINT. Only use
            # the actual bytes returned for the last, potentially short, block.
            with _open_handle(self.kernel, path, 0x80000000, 0x1, 0x28200000) as handle:
                while read < size:
                    check()
                    count = wintypes.DWORD()
                    if not self.kernel.ReadFile(handle, self.buffer, CHUNK_SIZE, ctypes.byref(count), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if count.value == 0:
                        break
                    block = self.view[:count.value]
                    hasher.update(block)
                    if cache is not None:
                        cache.write(block)
                    read += count.value
        if read != size:
            raise OSError(f"Unexpected size during destination verification: {path}")
        return hasher.digest()
