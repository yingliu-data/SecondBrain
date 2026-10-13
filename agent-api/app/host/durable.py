"""durable — group-commit appends for the host's logs (docs/HOST.md).

Every durable line the host writes on the event loop's behalf (a guest
event accepted by StateSync, a host-log record) goes through one
process-wide GroupWriter instead of open + write + fsync + close on the
loop. A writer thread drains whatever is queued, writes each file's
records with one write and one fsync on a file descriptor it keeps open,
then resolves each record's future. A future resolves only after its
bytes were fsynced: StateSync acks a guest event only then.

  writer()                          the process's GroupWriter (started lazily)
  GroupWriter(shards=4, max_open=64, fsync=os.fsync)
    .append(path, data, group=None) -> concurrent.futures.Future
                                    the file's inode once fsynced; the OSError
                                    otherwise.
                                    Records of one path are written in the
                                    order appended (one path, one shard).
    .reset(group)                   clear a failed group (below)
    .flush(timeout=None)            wait for everything appended so far

Shards: `shards` writer threads, a path's shard fixed by its directory, so
the fsyncs of different sessions run side by side and one session's
records stay in order.

Failure: a batch whose write or fsync fails is cut back to the size the
file had before it (no torn line stays behind) and its futures get the
error (always an OSError: anything else is wrapped in DurableWriteError).
With `group` (StateSync passes itself), the failure also poisons
the group: every later record of that group fails at once, whatever its
path, until reset(group); a log with a hole in its seqs is never written.

Descriptors: kept open per path, at most `max_open` per shard (least
recently used closed first). Before a batch the path is stat()ed: replaced
by another file (a run directory swapped in) -> reopened; gone (the
session deleted) -> the batch goes to the old, unlinked file and is lost,
as a write to a deleted session should be; never opened -> created.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import threading
import weakref
from collections import OrderedDict

logger = logging.getLogger("host.durable")

_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)


class PoisonedError(OSError):
    """An earlier record of the same group failed: this one is not written."""


class DurableWriteError(OSError):
    """A commit failed with something other than an OSError: its waiters get
    this (StateSync catches OSError), "TypeName: message"."""

    @classmethod
    def wrap(cls, exc: BaseException) -> "DurableWriteError":
        return cls(f"{type(exc).__name__}: {exc}")


class _Shard:
    def __init__(self, name: str, max_open: int, poisoned, poison_lock: threading.Lock,
                 fsync=os.fsync):
        self.name = name
        self._fsync = fsync
        self.max_open = max(1, int(max_open))
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._fds: OrderedDict[str, tuple[int, int]] = OrderedDict()   # path -> (fd, inode)
        self._poisoned = poisoned
        self._poison_lock = poison_lock
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def submit(self, path: str, data: bytes, group) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if self._thread is None:
            with self._start_lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, daemon=True,
                                                    name=self.name)
                    self._thread.start()
        self._q.put((path, data, group, fut))
        return fut

    def barrier(self) -> concurrent.futures.Future | None:
        """A future resolved once everything queued before it was handled
        (None: the thread never started, nothing was queued). Internal, so
        never cancelled by a waiter, unlike the records' own futures."""
        if self._thread is None:
            return None
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((None, b"", None, fut))
        return fut

    # ------------------------------------------------------------ the thread
    def _run(self) -> None:
        while True:
            items = [self._q.get()]
            while True:
                try:
                    items.append(self._q.get_nowait())
                except queue.Empty:
                    break
            by_path: dict[str, list] = {}
            barriers = []
            for item in items:
                if item[0] is None:
                    barriers.append(item[3])
                else:
                    by_path.setdefault(item[0], []).append(item)
            for path, recs in by_path.items():
                try:
                    self._commit(path, recs)
                except Exception as exc:    # never let the shard's thread die
                    logger.exception("durable commit to %s failed", path)
                    self._fail(recs, DurableWriteError.wrap(exc))
            for fut in barriers:
                _resolve(fut, result=None)

    def _commit(self, path: str, recs: list) -> None:
        live = []
        for rec in recs:
            group = rec[2]
            if group is not None and self._is_poisoned(group):
                _resolve(rec[3], error=PoisonedError(f"{path}: an earlier write of its log failed"))
            else:
                live.append(rec)
        if not live:
            return
        fd = None
        size = None
        try:
            fd = self._fd(path)
            size = os.fstat(fd).st_size
            buf = memoryview(b"".join(r[1] for r in live))
            while buf:
                n = os.write(fd, buf)
                buf = buf[n:]
            self._fsync(fd)
        except Exception as exc:            # not only OSError: never a torn batch left behind
            # an OSError for every waiter (poisoned() -> OSError, StateSync
            # catches OSError), whatever went wrong
            e = exc if isinstance(exc, OSError) else DurableWriteError.wrap(exc)
            logger.error("durable append to %s failed: %s", path, e)
            if fd is not None and size is not None:
                try:
                    os.ftruncate(fd, size)
                    os.fsync(fd)
                except OSError:
                    pass
            self._close(path)
            self._fail(live, e)
            return
        ino = self._fds[path][1]
        for rec in live:
            _resolve(rec[3], result=ino)

    def _fd(self, path: str) -> int:
        cached = self._fds.get(path)
        if cached is not None:
            fd, ino = cached
            try:
                now = os.stat(path).st_ino
            except FileNotFoundError:
                now = ino                   # deleted: the old inode takes it (lost)
            if now == ino:
                self._fds.move_to_end(path)
                return fd
            self._close(path)               # replaced by another file: reopen
        fd = os.open(path, _FLAGS, 0o644)
        self._fds[path] = (fd, os.fstat(fd).st_ino)
        while len(self._fds) > self.max_open:
            old, (ofd, _) = self._fds.popitem(last=False)
            _close_fd(ofd)
        return fd

    def _close(self, path: str) -> None:
        cached = self._fds.pop(path, None)
        if cached is not None:
            _close_fd(cached[0])

    def _fail(self, recs: list, error: OSError) -> None:
        """Every record of a failed commit gets `error`, and its group is
        poisoned: no later record of it makes a hole. (A record already
        resolved, e.g. refused as poisoned, keeps its own result.)"""
        for rec in recs:
            if rec[2] is not None:
                self._poison(rec[2], error)
            _resolve(rec[3], error=error)

    def _is_poisoned(self, group) -> bool:
        with self._poison_lock:
            return group in self._poisoned

    def _poison(self, group, error: OSError) -> None:
        with self._poison_lock:
            self._poisoned[group] = error


def _resolve(fut: concurrent.futures.Future, result=None, error: BaseException | None = None
             ) -> None:
    """Resolve a record's future. A waiter may have cancelled it meanwhile
    (asyncio.wrap_future: a cancelled awaiter cancels it, e.g. a worker's
    pump cancelled at close): its record is written all the same (a log
    never gets a hole), only nobody hears of it. Never raises: an
    InvalidStateError here would end the shard's thread, and with it every
    later append of every session on the shard."""
    if fut.done():
        return
    try:
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)
    except concurrent.futures.InvalidStateError:
        pass                                # cancelled between the check and the set


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class GroupWriter:
    def __init__(self, shards: int = 4, max_open: int = 64, *, fsync=os.fsync) -> None:
        # group -> its error (weak: a dropped StateSync takes its entry along)
        self._poisoned: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._poison_lock = threading.Lock()
        self._shards = [_Shard(f"durable-{i}", max_open, self._poisoned, self._poison_lock,
                               fsync) for i in range(max(1, int(shards)))]

    def append(self, path, data: bytes, group=None) -> concurrent.futures.Future:
        path = os.fspath(path)
        i = hash(os.path.dirname(path)) % len(self._shards)
        return self._shards[i].submit(path, data, group)

    def reset(self, group) -> None:
        with self._poison_lock:
            self._poisoned.pop(group, None)

    def poisoned(self, group) -> OSError | None:
        with self._poison_lock:
            return self._poisoned.get(group)

    def flush(self, timeout: float | None = None) -> None:
        """Wait for every record appended so far (errors are the callers').
        A barrier per shard, not the last record's future: that one may have
        been cancelled by its waiter while its record is still queued."""
        barriers = [b for b in (s.barrier() for s in self._shards) if b is not None]
        concurrent.futures.wait(barriers, timeout=timeout)


_writer: GroupWriter | None = None
_writer_lock = threading.Lock()


def writer() -> GroupWriter:
    """The process-wide GroupWriter."""
    global _writer
    if _writer is None:
        with _writer_lock:
            if _writer is None:
                _writer = GroupWriter()
    return _writer

