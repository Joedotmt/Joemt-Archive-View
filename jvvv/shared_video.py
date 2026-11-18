"""Seekable archive video staging shared with the isolated media worker."""

import mmap
import os
import sys
from pathlib import Path
from uuid import uuid4


class SharedVideo:
    def __init__(self, size: int):
        self.size = size
        self.fd = None
        if os.name == "nt":
            self.name = "jvvv-video-" + uuid4().hex
            self.mapping = mmap.mmap(-1, size, tagname=self.name)
        elif hasattr(os, "memfd_create") and Path("/proc/self/fd").is_dir():
            fd = os.memfd_create("jvvv-video")
            try:
                os.posix_fallocate(fd, 0, size)
                self.mapping = mmap.mmap(fd, size)
            except BaseException:
                os.close(fd)
                raise
            self.fd = fd
            self.name = f"/proc/{os.getpid()}/fd/{fd}"
        else:
            raise OSError("Shared video input is unavailable on this platform.")

    def reader_arguments(self) -> list[str]:
        if self.mapping.tell() != self.size:
            raise ValueError("The staged video is incomplete.")
        return ["--shared-video", self.name, str(self.size)]

    def close(self) -> None:
        self.mapping.close()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def open_shared_video(name: str, size: int):
    if os.name == "nt":
        return mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)
    return open(name, "rb")


def video_worker_command() -> list[str]:
    worker = "--sample-video" if getattr(sys, "frozen", False) else str(Path(__file__).with_name("sampled_preview.py"))
    return [sys.executable, worker]
