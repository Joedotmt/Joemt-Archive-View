"""One bounded stream for the connected writers of one archive project."""
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import ExitStack
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from .archive_io import ProjectGuard


class ProjectSession:
    """Own source handles across writers and between applied drive swaps."""
    def __init__(self):
        self.lock = Lock()
        self.resources = ExitStack()
        self.entries = None
        self.guard = None
        self.owner = None

    def _open(self, source, scan, check):
        with ExitStack() as opening:
            guard = opening.enter_context(ProjectGuard(source))
            def check_source():
                check()
                guard.check()
            entries = scan(source, check_source, guard)
            check_source()
            self.resources = opening.pop_all()
            self.guard, self.entries = guard, entries

    def prepare(self, source, scan, check):
        with self.lock:
            if self.entries is None:
                # Windows cancels the directory change notification when its
                # initiating thread exits, even if its handle remains open.
                if self.owner is None:
                    self.owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-protection")
                self.owner.submit(self._open, source, scan, check).result()
            if self.guard.source != source:
                raise OSError("The selected source differs from the protected project.")
            self.guard.check()
            return self.guard, self.entries

    def close(self, *, wait=True):
        with self.lock:
            if self.owner is not None:
                self.owner.submit(self.resources.close)
                self.owner.shutdown(wait=wait)
                self.owner = None
            self.guard = self.entries = None


class SharedArchiveStream:
    def __init__(self, job_ids):
        self.readers = {key: (Queue(maxsize=2), Event()) for key in job_ids}
        self.lock = Lock()
        self.thread = None
        self.finished = Future()
        self.source_bytes = 0

    def abandoned(self):
        return all(stopped.is_set() for _, stopped in self.readers.values())

    def start(self, produce):
        with self.lock:
            if self.thread is None:
                def run():
                    try:
                        self.finished.set_result(produce(self.send))
                    except BaseException as error:
                        self.finished.set_exception(error)
                self.thread = Thread(target=run, name="archive-source")
                self.thread.start()

    def send(self, relative, block, digest):
        if block is not None:
            self.source_bytes += len(block)
        for channel, stopped in self.readers.values():
            while not stopped.is_set():
                try:
                    channel.put((relative, block, digest), timeout=0.1)
                    break
                except Full:
                    continue
        if self.abandoned():
            raise InterruptedError("All archive writers stopped.")

    def read(self, job_id, relative, check):
        channel, _ = self.readers[job_id]
        while True:
            check()
            try:
                path, block, digest = channel.get(timeout=0.1)
            except Empty:
                if self.finished.done() and channel.empty():
                    self.finished.result()
                    raise OSError("The shared project stream ended early.")
                continue
            if path != relative:
                raise OSError("The shared project file list changed.")
            yield block, digest
            if block is None:
                return

    def media_results(self, check):
        while not self.finished.done():
            check()
            try:
                self.finished.result(timeout=0.1)
            except TimeoutError:
                pass
        return self.finished.result()

    def detach(self, job_id):
        with self.lock:
            self.readers[job_id][1].set()
            thread = self.thread if self.abandoned() else None
        if thread is not None:
            thread.join()
