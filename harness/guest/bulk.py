"""guest.bulk — the :5001 bulk channel from the guest's side
(docs/PROTOCOL.md).

  BulkClient(connector, session_id, port=5001, timeout_s=60)
    put(kind, path, data: bytes, sha256=None) -> None   spill | workspace | manifest
    get_log(after_seq=0) -> (events, last_seq) the host's acked events, in seq order
  workspace_files(workspace, digests=None) -> ([{path, bytes, sha256, mode}], {path: abs})
  settle_manifest(workspace, client=None, uploaded=None, digests=None) -> manifest dict
  MAX_UPLOAD_BYTES                              files at or above this stay manifest-only

One transfer per connection; the framing (header line, raw bytes, reply line)
is protocol/bulk.py. Every failure is a BulkError (a socket error, a short
read, a reply with ok false, a payload whose sha256 does not match): the
callers decide whether it is fatal (the log return on resume is; a spill or a
settle upload is logged and the task goes on).

The workspace walk does not descend into the trees protocol.bulk.manifest()
excludes (`.spill/`, `.git/`; manifest() owns that rule and the sort) nor into
the guest's own top-level `.harness/` (local spill files, todo.json;
guest.ops.HARNESS_DIR). It follows no symlinks and lists regular files only.
Paths protocol.bulk.check_path rejects (a backslash in a name) are left out
with a warning. A digest cache keyed by (size, mtime_ns, ino) spares
re-hashing unchanged files across settles.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import stat

from guest.ops import HARNESS_DIR, _file_digest
from protocol import bulk as proto
from protocol.bulk import BulkError
from protocol.canonical import canonical_json, is_int

log = logging.getLogger("guest.bulk")

BULK_PORT = 5001
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 60.0


class BulkClient:
    def __init__(self, connector, session_id: str, port: int = BULK_PORT,
                 timeout_s: float = TIMEOUT_S):
        self.connector = connector
        self.session_id = session_id
        self.port = port
        self.timeout_s = timeout_s

    # ------------------------------------------------------------ transfers
    def put(self, kind: str, path: str, data: bytes, sha256: str | None = None) -> None:
        """Send one file; returns once the host replied ok (it verified the
        byte count and sha256 and stored the file). `sha256`: the digest of
        `data` when the caller already has it."""
        header = proto.put_header(self.session_id, kind, path, data, sha256)
        with self._connect() as sock:
            send_error = None
            try:
                sock.sendall(proto.encode_line(header))
                sock.sendall(data)
            except OSError as exc:      # the host may refuse the header and close early:
                send_error = exc        # its reply line says why
            try:
                rfile = sock.makefile("rb")
                reply = proto.decode_line(proto.read_line(rfile))
            except (OSError, BulkError) as exc:
                raise BulkError(f"put {kind} {path}: {send_error or exc}") from None
        if reply.get("ok") is not True:
            raise BulkError(f"put {kind} {path}: host said {reply.get('error')!r}")

    def get_log(self, after_seq: int = 0) -> tuple[list[dict], int]:
        """The host's acked events with seq > after_seq, in order, and the
        host's last_seq. Verifies byte count, sha256 and that the seqs run
        gapless from after_seq + 1 to last_seq."""
        header = proto.get_header(self.session_id, after_seq)
        with self._connect() as sock:
            try:
                sock.sendall(proto.encode_line(header))
                rfile = sock.makefile("rb")
                reply = proto.decode_line(proto.read_line(rfile))
                if reply.get("ok") is not True:
                    raise BulkError(f"get log: host said {reply.get('error')!r}")
                n = reply.get("bytes")
                if not is_int(n, 0):
                    raise BulkError(f"get log: bad reply.bytes {n!r}")
                payload = proto.read_exact(rfile, n)
            except OSError as exc:
                raise BulkError(f"get log: {exc}") from None
        if proto.sha256_hex(payload) != reply.get("sha256"):
            raise BulkError("get log: payload sha256 does not match the reply")
        events = []
        for line in payload.splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise BulkError(f"get log: bad event line: {exc}") from None
            if not isinstance(ev, dict):
                raise BulkError("get log: event line is not an object")
            events.append(ev)
        last_seq = reply.get("last_seq")
        if not is_int(last_seq, after_seq):
            raise BulkError(f"get log: bad reply.last_seq {last_seq!r}")
        seqs = [e.get("seq") for e in events]
        if seqs != list(range(after_seq + 1, after_seq + 1 + len(events))) or (
                events and seqs[-1] != last_seq) or (not events and last_seq != after_seq):
            raise BulkError(f"get log: seqs are not {after_seq + 1}..{last_seq} gapless")
        return events, last_seq

    def _connect(self) -> socket.socket:
        try:
            sock = self.connector.connect(self.port)
        except OSError as exc:
            raise BulkError(f"connect :{self.port}: {exc}") from None
        sock.settimeout(self.timeout_s)
        return sock


# ---------------------------------------------------------------- workspace
def workspace_files(workspace, digests: dict | None = None) -> tuple[list[dict], dict[str, str]]:
    """([{path, bytes, sha256, mode}], {path: absolute path}) for every regular
    file under `workspace` except the guest's own top-level `.harness/`.
    Excluded trees (protocol.bulk.excluded) are not descended into; filtering
    and sorting are protocol.bulk.manifest()'s job.

    `digests` ({path: ((size, mtime_ns, ino), sha256)}, kept by the caller
    across settles) skips re-hashing files whose stat key is unchanged; it is
    replaced in place by what this walk saw."""
    root = os.fspath(workspace)
    files: list[dict] = []
    where: dict[str, str] = {}
    cache = digests if digests is not None else {}
    seen: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        dirnames[:] = [d for d in dirnames if not proto.excluded(rel_dir + d + "/")
                       and not (rel_dir == "" and d == HARNESS_DIR)]
        for name in filenames:
            rel = rel_dir + name
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                proto.check_path(rel)
            except BulkError as exc:
                log.warning("left out of the manifest: %s", exc)
                continue
            key = (st.st_size, st.st_mtime_ns, st.st_ino)
            cached = cache.get(rel)
            digest = cached[1] if cached is not None and cached[0] == key else _file_digest(full)
            if digest is None:
                continue
            seen[rel] = (key, digest)
            files.append({"path": rel, "bytes": st.st_size, "sha256": digest,
                          "mode": stat.S_IMODE(st.st_mode)})
            where[rel] = full
    if digests is not None:
        digests.clear()
        digests.update(seen)
    return files, where


def settle_manifest(workspace, client: BulkClient | None = None,
                    uploaded: dict[str, str] | None = None,
                    digests: dict | None = None) -> dict:
    """The workspace manifest (protocol.bulk.manifest). With a client: puts
    every file whose sha256 differs from `uploaded` (path -> sha256 already
    on the host; updated in place) and is under MAX_UPLOAD_BYTES, then the
    manifest itself. `digests`: workspace_files' stat-keyed cache. Upload
    failures are logged; the manifest is returned either way."""
    files, where = workspace_files(workspace, digests)
    manifest = proto.manifest(files)
    if client is None:
        return manifest
    uploaded = uploaded if uploaded is not None else {}
    for f in manifest["files"]:
        if f["bytes"] >= MAX_UPLOAD_BYTES or uploaded.get(f["path"]) == f["sha256"]:
            continue
        try:
            with open(where[f["path"]], "rb") as fh:
                data = fh.read()
            digest = proto.sha256_hex(data)
            if digest != f["sha256"]:
                log.warning("%s changed while uploading; sent as it is now", f["path"])
            client.put("workspace", f["path"], data, sha256=digest)
            uploaded[f["path"]] = digest
        except (OSError, BulkError) as exc:
            log.warning("workspace upload of %s failed: %s", f["path"], exc)
    try:
        client.put("manifest", proto.MANIFEST_PATH, canonical_json(manifest).encode())
    except BulkError as exc:
        log.warning("manifest upload failed: %s", exc)
    return manifest
