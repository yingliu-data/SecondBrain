#!/usr/bin/env python3
"""harness/image/pyz_pack.py -- pack guest.pyz with precompiled bytecode
(docs/IMAGE.md "guest.pyz").

    pyz_pack.py pack STAGE_DIR OUT.pyz [--archive-path P]   stage dir -> pyz
    pyz_pack.py recompile PYZ [--archive-path P]            refresh the .pyc in place

Every `x.py` in the archive gets a legacy-layout `x.pyc` next to it (zipimport
looks for `guest/main.pyc`, never `__pycache__/`), compiled by the RUNNING
interpreter as an unchecked-hash pyc: no source mtime in it and no source
check at import (the rootfs is read-only), so the same sources and the same
interpreter give the same bytes. A pyc whose magic does not match the guest's
Python is ignored by zipimport (it falls back to the source), which is why
Dockerfile.rootfs runs `recompile` with the guest's own python3.

Reproducible: entries sorted, fixed timestamps (1980-01-01), fixed modes,
deflate. The shebang is `#!/usr/bin/env python3`. Standard library only.

`--archive-path` is the path the guest loads the pyz from (default
/opt/harness/guest.pyz); it becomes each code object's co_filename, exactly
what zipimport would have set compiling from source, so tracebacks name
`/opt/harness/guest.pyz/guest/main.py`.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import marshal
import os
import sys
import zipfile

SHEBANG = b"#!/usr/bin/env python3\n"
EPOCH = (1980, 1, 1, 0, 0, 0)
DEFAULT_ARCHIVE_PATH = "/opt/harness/guest.pyz"
SKIP_DIRS = {"__pycache__"}


def pyc_bytes(source: bytes, name: str, archive_path: str) -> bytes:
    """An unchecked-hash pyc (PEP 552, flags 0b01) for `source`."""
    code = compile(source, f"{archive_path}/{name}", "exec", dont_inherit=True, optimize=0)
    flags = 0b01                                   # hash-based, check_source off
    return (importlib.util.MAGIC_NUMBER
            + flags.to_bytes(4, "little")
            + importlib.util.source_hash(source)
            + marshal.dumps(code))


def _entry(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=EPOCH)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    zi.create_system = 3                           # unix, whatever the build host
    return zi


def write_pyz(files: dict[str, bytes], out: str, archive_path: str) -> int:
    """Write {name: bytes} (sources and data; no .pyc) plus a .pyc per .py.
    Returns the number of .pyc written."""
    entries = {n: b for n, b in files.items() if not n.endswith(".pyc")}
    n_pyc = 0
    for name, data in list(entries.items()):
        if name.endswith(".py"):
            entries[name + "c"] = pyc_bytes(data, name, archive_path)
            n_pyc += 1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(entries):
            zf.writestr(_entry(name), entries[name], compresslevel=9)
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(SHEBANG)
        f.write(buf.getvalue())
    os.chmod(tmp, 0o755)
    os.replace(tmp, out)
    return n_pyc


def read_stage(stage: str) -> dict[str, bytes]:
    files = {}
    for root, dirs, names in os.walk(stage):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in names:
            if n.endswith(".pyc"):
                continue
            path = os.path.join(root, n)
            rel = os.path.relpath(path, stage).replace(os.sep, "/")
            with open(path, "rb") as f:
                files[rel] = f.read()
    return files


def read_pyz(pyz: str) -> dict[str, bytes]:
    with zipfile.ZipFile(pyz) as zf:        # zipfile skips the shebang prefix
        return {zi.filename: zf.read(zi) for zi in zf.infolist() if not zi.is_dir()}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pyz_pack.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pack")
    a.add_argument("stage")
    a.add_argument("out")
    a.add_argument("--archive-path", default=DEFAULT_ARCHIVE_PATH)
    r = sub.add_parser("recompile")
    r.add_argument("pyz")
    r.add_argument("--archive-path", default=DEFAULT_ARCHIVE_PATH)
    args = p.parse_args(argv)
    if args.cmd == "pack":
        files, out = read_stage(args.stage), args.out
    else:
        files, out = read_pyz(args.pyz), args.pyz
    if "__main__.py" not in files:
        print("pyz_pack: no __main__.py", file=sys.stderr)
        return 2
    n = write_pyz(files, out, args.archive_path)
    tag = sys.implementation.cache_tag
    print(f"[image] {out}: {n} modules precompiled ({tag}, unchecked-hash)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
