"""bulk — the :5001 bulk channel framing (PLAN.md 4.1, 4.5, docs/PROTOCOL.md §7).

Pure data: no sockets, no threads, no clock. The guest's `guest/bulk.py` and
the host's bulk server both lean on this module for what a line means.

One transfer per connection. The guest always connects (it never listens):

  put   guest -> host   header line, then exactly `bytes` raw bytes;
                        host answers one reply line.
        header {"op":"put","session_id","kind","path","bytes","sha256"}
        kind   "spill" | "workspace" | "manifest"
        path   relative, no "..", no leading "/" (spill: ".spill/<name>";
               workspace: the file's path under /workspace;
               manifest: "manifest.json")
        reply  {"ok":true} | {"ok":false,"error":str}
        The host verifies `bytes` and `sha256` before replying ok and writes
        under the session dir (spill -> workspace/.spill/, workspace ->
        workspace/, manifest -> workspace/manifest.json).

  get   guest -> host   header line only; host answers a reply line, then
                        exactly reply.bytes raw bytes.
        header {"op":"get","session_id","kind":"log","after_seq":int}
        reply  {"ok":true,"bytes":int,"sha256":str,"last_seq":int}
               | {"ok":false,"error":str}
        payload: the host's ACKED guest events with seq > after_seq, JSONL,
        one canonical event per line, in seq order (the log return on
        resume, PROTOCOL.md §6).

Lines are UTF-8 JSON terminated by "\n", at most MAX_HEADER bytes.

Workspace manifest (settle.manifest and the "manifest" put):
  {"version":1,"files":[{"path","bytes","sha256","mode"}...],"total_bytes"}
  files sorted by path; ".spill/" and ".git/" excluded.
"""
from __future__ import annotations

import hashlib
import json

OPS = frozenset({"put", "get"})
PUT_KINDS = frozenset({"spill", "workspace", "manifest"})
GET_KINDS = frozenset({"log"})
MAX_HEADER = 64 * 1024
MANIFEST_VERSION = 1
MANIFEST_EXCLUDE = (".spill/", ".git/")


class BulkError(ValueError):
    """A bulk header or reply breaks the contract."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_path(path) -> str:
    if not isinstance(path, str) or not path:
        raise BulkError("path must be a non-empty str")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise BulkError(f"path not relative: {path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise BulkError(f"path has an empty, '.' or '..' part: {path!r}")
    return path


def _int(v, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise BulkError(f"{name} must be an int >= 0")
    return v


def put_header(session_id: str, kind: str, path: str, data: bytes) -> dict:
    return check_header({"op": "put", "session_id": session_id, "kind": kind,
                         "path": path, "bytes": len(data), "sha256": sha256_hex(data)})


def get_header(session_id: str, after_seq: int = 0) -> dict:
    return check_header({"op": "get", "session_id": session_id, "kind": "log",
                         "after_seq": after_seq})


def check_header(h) -> dict:
    if not isinstance(h, dict):
        raise BulkError("header must be an object")
    op = h.get("op")
    if op not in OPS:
        raise BulkError(f"unknown op {op!r}")
    if not isinstance(h.get("session_id"), str) or not h["session_id"]:
        raise BulkError("session_id must be a non-empty str")
    if op == "put":
        if h.get("kind") not in PUT_KINDS:
            raise BulkError(f"unknown put kind {h.get('kind')!r}")
        check_path(h.get("path"))
        _int(h.get("bytes"), "bytes")
        sha = h.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise BulkError("sha256 must be 64 hex chars")
    else:
        if h.get("kind") not in GET_KINDS:
            raise BulkError(f"unknown get kind {h.get('kind')!r}")
        _int(h.get("after_seq"), "after_seq")
    return h


def ok_reply(**extra) -> dict:
    return {"ok": True, **extra}


def error_reply(error: str) -> dict:
    return {"ok": False, "error": str(error)}


def encode_line(obj: dict) -> bytes:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(line) > MAX_HEADER:
        raise BulkError("line too long")
    return line


def decode_line(line: bytes) -> dict:
    if len(line) > MAX_HEADER:
        raise BulkError("line too long")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BulkError(f"bad line: {e}") from None
    if not isinstance(obj, dict):
        raise BulkError("line must be an object")
    return obj


def read_line(rfile) -> bytes:
    """Read one "\\n"-terminated line from a binary file object, bounded."""
    line = rfile.readline(MAX_HEADER + 1)
    if not line.endswith(b"\n"):
        raise BulkError("truncated or oversized line")
    return line


def read_exact(rfile, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = rfile.read(min(n - len(buf), 1 << 20))
        if not chunk:
            raise BulkError(f"short payload: {len(buf)} of {n} bytes")
        buf += chunk
    return bytes(buf)


def manifest(files: list[dict]) -> dict:
    """files: [{"path","bytes","sha256","mode"}]; returns the sorted manifest."""
    out = []
    for f in files:
        p = check_path(f["path"])
        if any(p.startswith(x) for x in MANIFEST_EXCLUDE):
            continue
        out.append({"path": p, "bytes": _int(f["bytes"], "bytes"),
                    "sha256": f["sha256"], "mode": _int(f.get("mode", 0o644), "mode")})
    out.sort(key=lambda f: f["path"])
    return {"version": MANIFEST_VERSION, "files": out,
            "total_bytes": sum(f["bytes"] for f in out)}
