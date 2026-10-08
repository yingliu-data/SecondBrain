"""bulk_server — the host side of the :5001 bulk channel (PLAN.md 4.1,
docs/PROTOCOL.md §6–7, harness/docs/M3-CONTRACTS.md §4–5).

One transfer per connection; the guest always connects. The framing is
`protocol.bulk` (header line, raw payload, reply line):

  put   spill      -> {session}/workspace/.spill/<name>   (path ".spill/<name>")
        workspace  -> {session}/workspace/<path>
        manifest   -> {session}/manifest.json  (path "manifest.json"; outside
                      workspace/, so it never collides with a user's file)
        The payload streams into a temp file next to the target in 1 MiB
        chunks while it is hashed; `bytes` and `sha256` are verified before
        the rename (so a reader never sees half a file, a mismatch leaves
        nothing) and before the reply says ok. A spill path must be under
        ".spill/". A path that leaves the workspace (".." or
        an absolute path, rejected by protocol.bulk.check_path; or a symlink
        inside the workspace pointing out, rejected after resolving) is an
        error reply, nothing written.
  get   log        -> the host's ACKED guest events with seq > after_seq of
                      the current run, canonical JSONL in seq order, plus
                      `last_seq` = the last returned event's seq, or after_seq
                      when none (the log return on resume, M3-CONTRACTS §4)

  BulkServer(listener, session_dir, session_id, read_log, on_put=None)
    .start() / .stop()       an accept thread; one short-lived thread per transfer
    .handle(conn)            one transfer (tests call it directly)

`read_log(after_seq) -> events` is StateSync.read: it bounds the read by the
last acked seq taken under the state-sync lock, so the payload is a
consistent prefix of what was acked.
`on_put(kind, path, bytes, sha256)` lets the worker log a `bulk` host event.
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Callable

from protocol.bulk import (
    MANIFEST_PATH, SPILL_DIR, BulkError, check_header, decode_line, encode_line, error_reply,
    ok_reply, read_line, sha256_hex,
)
from protocol.canonical import canonical_json

from app.config import HARNESS_BULK_MAX_BYTES

logger = logging.getLogger("host.bulk")

CHUNK = 1 << 20


class BulkServer:
    def __init__(self, listener: socket.socket, session_dir: Path, session_id: str,
                 read_log: Callable[[int], list[dict]], *,
                 on_put: Callable[[str, str, int, str], None] | None = None,
                 max_bytes: int = HARNESS_BULK_MAX_BYTES,
                 io_timeout_s: float = 60.0) -> None:
        self.listener = listener
        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "workspace"
        self.session_id = session_id
        self.read_log = read_log
        self.on_put = on_put or (lambda *a: None)
        self.max_bytes = int(max_bytes)
        self.io_timeout_s = io_timeout_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.served: list[dict] = []        # headers handled ok (tests, debugging)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "BulkServer":
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name=f"bulk:{self.session_id}")
        self._thread.start()
        return self

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        self.listener.settimeout(0.5)       # wake up to see _stop
        while not self._stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return      # listener closed
            conn.settimeout(self.io_timeout_s)
            threading.Thread(target=self._serve, args=(conn,), daemon=True,
                             name=f"bulk-conn:{self.session_id}").start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            self.handle(conn)
        except Exception:   # a bad transfer never takes the server down
            logger.exception("bulk transfer failed")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------ one transfer
    def handle(self, conn: socket.socket) -> dict:
        """Serve one transfer; returns the reply sent."""
        rfile = conn.makefile("rb")
        try:
            try:
                header = check_header(decode_line(read_line(rfile)))
                if header["session_id"] != self.session_id:
                    raise BulkError(f"session_id {header['session_id']!r} is not this session")
                if header["op"] == "put":
                    reply, payload = self._put(header, rfile), b""
                else:
                    reply, payload = self._get(header)
            except BulkError as e:
                logger.warning("bulk request rejected: %s", e)
                reply, payload = error_reply(str(e)), b""
            conn.sendall(encode_line(reply) + payload)
            return reply
        finally:
            rfile.close()

    def _target(self, kind: str, path: str) -> Path:
        under_spill = path.startswith(SPILL_DIR + "/")
        if kind == "manifest":
            if path != MANIFEST_PATH:
                raise BulkError(f"manifest path must be {MANIFEST_PATH!r}, got {path!r}")
            # outside workspace/: a user's own workspace/manifest.json never collides
            return self.session_dir / MANIFEST_PATH
        if kind == "spill" and not under_spill:
            raise BulkError(f"spill path must be under {SPILL_DIR}/, got {path!r}")
        if kind == "workspace" and (under_spill or path == SPILL_DIR):
            raise BulkError(f"workspace path must not be under {SPILL_DIR}/")
        root = self.workspace.resolve()
        target = self.workspace / path
        # a symlink (or a symlinked parent) inside the workspace must not lead out
        parent = target.parent
        parent_resolved = parent.resolve()
        if parent_resolved != root and root not in parent_resolved.parents:
            raise BulkError(f"path escapes the workspace: {path!r}")
        return parent_resolved / target.name

    def _put(self, h: dict, rfile) -> dict:
        n = h["bytes"]
        if n > self.max_bytes:
            raise BulkError(f"payload of {n} bytes exceeds the {self.max_bytes}-byte limit")
        target = self._target(h["kind"], h["path"])   # before reading: reject early
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self._target(h["kind"], h["path"])   # the mkdir may not create a way out
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".bulk-", suffix=".tmp")
        try:
            hasher = hashlib.sha256()
            with os.fdopen(fd, "wb") as f:
                left = n
                while left:
                    chunk = rfile.read(min(left, CHUNK))
                    if not chunk:
                        raise BulkError(f"short payload: {n - left} of {n} bytes")
                    hasher.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
                digest = hasher.hexdigest()
                if digest != h["sha256"]:
                    raise BulkError(f"sha256 mismatch: header {h['sha256'][:12]}..., "
                                    f"payload {digest[:12]}...")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)       # replaces a symlink itself, never its target
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.served.append(h)
        try:
            self.on_put(h["kind"], h["path"], n, digest)
        except Exception:
            logger.exception("bulk on_put hook failed")
        return ok_reply()

    def _get(self, h: dict) -> tuple[dict, bytes]:
        events = self.read_log(h["after_seq"])
        # exact: the last returned event's seq, or after_seq when there is none
        last_seq = events[-1]["seq"] if events else h["after_seq"]
        payload = b"".join(canonical_json(ev).encode("utf-8") + b"\n" for ev in events)
        self.served.append(h)
        return ok_reply(bytes=len(payload), sha256=sha256_hex(payload), last_seq=last_seq), payload
