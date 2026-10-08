"""guest.bulk — the :5001 bulk channel from the guest's side (PLAN.md 4.1,
docs/PROTOCOL.md section 7, docs/M3-CONTRACTS.md sections 4–5).

  BulkClient(connector, session_id, port=5001, timeout_s=60)
    put(kind, path, data: bytes) -> None       spill | workspace | manifest
    get_log(after_seq=0) -> (events, last_seq) the host's acked events, in seq order
  workspace_files(workspace) -> list[{path, bytes, sha256, mode}]
  settle_manifest(workspace, client=None, uploaded=None) -> manifest dict
  MAX_UPLOAD_BYTES                              files at or above this stay manifest-only

One transfer per connection; the framing (header line, raw bytes, reply line)
is protocol/bulk.py. Every failure is a BulkError (a socket error, a short
read, a reply with ok false, a payload whose sha256 does not match): the
callers decide whether it is fatal (the log return on resume is; a spill or a
settle upload is logged and the task goes on).

The workspace walk skips `.spill/` and `.git/` (protocol.bulk.MANIFEST_EXCLUDE)
and the guest's own `.harness/` (the local spill files and other harness
state; guest.ops.HARNESS_DIR), follows no symlinks and lists regular files
only. Paths protocol.bulk.check_path rejects (a backslash in a name) are left
out with a warning.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import stat

from protocol import bulk as proto
from protocol.bulk import BulkError

log = logging.getLogger("guest.bulk")

BULK_PORT = 5001
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 60.0
HARNESS_DIR = ".harness"          # guest.ops.HARNESS_DIR (not imported: ops imports this module)
MANIFEST_PATH = "manifest.json"


class BulkClient:
    def __init__(self, connector, session_id: str, port: int = BULK_PORT,
                 timeout_s: float = TIMEOUT_S):
        self.connector = connector
        self.session_id = session_id
        self.port = port
        self.timeout_s = timeout_s

    # ------------------------------------------------------------ transfers
    def put(self, kind: str, path: str, data: bytes) -> None:
        """Send one file; returns once the host replied ok (it verified the
        byte count and sha256 and stored the file)."""
        header = proto.put_header(self.session_id, kind, path, data)
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
                if not isinstance(n, int) or isinstance(n, bool) or n < 0:
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
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < after_seq:
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
def workspace_files(workspace) -> tuple[list[dict], dict[str, str]]:
    """([{path, bytes, sha256, mode}], {path: absolute path}) for every regular
    file under `workspace`, minus the excluded trees. Sorted by path."""
    root = os.fspath(workspace)
    files: list[dict] = []
    where: dict[str, str] = {}
    excluded = tuple(proto.MANIFEST_EXCLUDE) + (HARNESS_DIR + "/",)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        dirnames[:] = sorted(d for d in dirnames if (rel_dir + d + "/") not in excluded)
        for name in sorted(filenames):
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
            digest = _sha256_file(full)
            if digest is None:
                continue
            files.append({"path": rel, "bytes": st.st_size, "sha256": digest,
                          "mode": stat.S_IMODE(st.st_mode)})
            where[rel] = full
    files.sort(key=lambda f: f["path"])
    return files, where


def _sha256_file(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def settle_manifest(workspace, client: BulkClient | None = None,
                    uploaded: dict[str, str] | None = None) -> dict:
    """The workspace manifest (protocol.bulk.manifest). With a client: puts
    every file whose sha256 differs from `uploaded` (path -> sha256 already
    on the host; updated in place) and is under MAX_UPLOAD_BYTES, then the
    manifest itself. Upload failures are logged; the manifest is returned
    either way."""
    files, where = workspace_files(workspace)
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
            if proto.sha256_hex(data) != f["sha256"]:
                log.warning("%s changed while uploading; sent as it is now", f["path"])
            client.put("workspace", f["path"], data)
            uploaded[f["path"]] = proto.sha256_hex(data)
        except (OSError, BulkError) as exc:
            log.warning("workspace upload of %s failed: %s", f["path"], exc)
    try:
        client.put("manifest", MANIFEST_PATH, manifest_bytes(manifest))
    except BulkError as exc:
        log.warning("manifest upload failed: %s", exc)
    return manifest
