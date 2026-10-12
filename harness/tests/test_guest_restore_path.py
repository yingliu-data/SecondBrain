"""The restore path's latency fixes (docs/IMAGE.md "guest.pyz", docs/GUEST.md
"Lifecycle"): session modules are imported before the idle-wait (in the
snapshot's memory), guest.pyz ships bytecode, guest.hooks does not pull in
unittest.mock, the idle poll is 5 ms."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import queue
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from guest import main as guest_main
from guest import transport as transport_mod
from guest.transport import Transport, UnixConnector
from tests.fakes.fake_host import FakeHost

HARNESS = Path(__file__).resolve().parents[1]
IMAGE = HARNESS / "image"
SID = "sess-restore"
HELLO = {"session_id": SID, "principal": {"user": "u", "tenant": "t"},
         "config": {"model": "m"}, "system_block": {"base_prompt": "x"},
         "resume": False, "at": 7}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"image_{name}", IMAGE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pyz_pack = _load("pyz_pack")
measure_imports = _load("measure_imports")


def _fresh(code: str, *path: str) -> dict:
    """Run `code` in a fresh isolated interpreter with `path` first on
    sys.path; it prints one JSON line."""
    prog = "import sys, json\nsys.path[:0] = %r\n%s" % (list(path), code)
    out = subprocess.run([sys.executable, "-I", "-c", prog], check=True,
                         capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------ preload


def test_session_modules_match_the_measurement():
    assert guest_main.SESSION_MODULES == measure_imports.SESSION_MODULES


def test_after_preload_a_session_imports_nothing():
    res = _fresh(
        "import guest.main as m\n"
        "m.preload()\n"
        "before = set(sys.modules)\n"
        "import importlib\n"
        "for n in m.SESSION_MODULES: importlib.import_module(n)\n"
        "print(json.dumps({'new': sorted(set(sys.modules) - before),"
        " 'ctypes': 'ctypes' in sys.modules}))\n",
        str(HARNESS))
    assert res == {"new": [], "ctypes": True}


def test_main_preloads_before_the_transport_starts(tmp_path, monkeypatch):
    """preload() runs before transport.start(): the base snapshot's idle
    detection sees vsock traffic only once every import is done."""
    order = []
    monkeypatch.setattr(guest_main, "preload", lambda: order.append("preload"))
    real_start = Transport.start

    def start(self):
        order.append("start")
        real_start(self)

    monkeypatch.setattr(Transport, "start", start)
    monkeypatch.setattr(guest_main, "run_session", lambda *a, **k: order.append("session") or 0)
    with FakeHost(tmp_path, HELLO) as host:
        code = guest_main.main(["--connect", f"unix:{tmp_path}", "--log-dir", str(tmp_path / "log"),
                                "--workspace", str(tmp_path / "ws"), "--idle-wait", "vsock",
                                "--hello-timeout", "5"])
        host.wait_disconnected()
    assert code == 0 and order == ["preload", "start", "session"]


def test_hooks_does_not_import_unittest_mock():
    res = _fresh("import guest.hooks\n"
                 "print(json.dumps({'mock': 'unittest.mock' in sys.modules}))\n", str(HARNESS))
    assert res == {"mock": False}


def test_purity_guard_still_patches_lazily():
    from guest.hooks import ImpureHook, purity_guard
    with purity_guard():
        with pytest.raises(ImpureHook):
            time.time()
    assert isinstance(time.time(), float)


# ------------------------------------------------------------ idle poll


def test_idle_poll_is_5ms():
    assert transport_mod.IDLE_POLL_S == 0.005


def test_idle_transport_connects_within_a_few_ms_of_the_listener(tmp_path):
    """The default poll: a listener that appears is found in ~2.5 ms on
    average (it was 25 ms at a 50 ms poll)."""
    inbound = queue.Queue()
    t = Transport(UnixConnector(tmp_path), inbound, idle=True)
    t.start()
    try:
        time.sleep(0.05)
        with FakeHost(tmp_path, HELLO):
            t0 = time.monotonic()
            assert t.wait_awake(5)
            # generous for a loaded CI box; the poll itself is 5 ms
            assert time.monotonic() - t0 < 0.25
    finally:
        t.stop()


# ------------------------------------------------------------ guest.pyz bytecode


@pytest.fixture(scope="module")
def built_pyz(tmp_path_factory):
    """build.sh's pyz step, without the shell: stage guest + protocol, pack."""
    import shutil
    d = tmp_path_factory.mktemp("pyz")
    stage = d / "stage"
    for pkg in ("guest", "protocol"):
        shutil.copytree(HARNESS / pkg, stage / pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "test_*.py"))
    (stage / "__main__.py").write_text(
        "import sys\nfrom guest import main as m\nsys.exit(m.main())\n")
    pyz = d / "guest.pyz"
    assert pyz_pack.main(["pack", str(stage), str(pyz)]) == 0
    return stage, pyz


def test_pyz_has_a_legacy_pyc_per_module(built_pyz):
    _, pyz = built_pyz
    names = set(zipfile.ZipFile(pyz).namelist())
    sources = {n for n in names if n.endswith(".py")}
    assert "guest/main.py" in sources and "protocol/fold.py" in sources
    assert {n + "c" for n in sources} <= names
    assert not any("__pycache__" in n for n in names)
    assert pyz.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    pyc = zipfile.ZipFile(pyz).read("guest/main.pyc")
    assert pyc[:4] == importlib.util.MAGIC_NUMBER
    assert int.from_bytes(pyc[4:8], "little") == 0b01      # unchecked hash


def test_pyz_is_reproducible(built_pyz, tmp_path):
    stage, pyz = built_pyz
    again = tmp_path / "again.pyz"
    pyz_pack.main(["pack", str(stage), str(again)])
    assert hashlib.sha256(again.read_bytes()).digest() == hashlib.sha256(pyz.read_bytes()).digest()
    # recompile (what Dockerfile.rootfs runs with the guest's python) is a fixpoint
    pyz_pack.main(["recompile", str(again)])
    assert again.read_bytes() == pyz.read_bytes()


def test_pyz_imports_from_bytecode(built_pyz):
    _, pyz = built_pyz
    res = _fresh("import guest.main as m\nm.preload()\nimport guest.coordinator as c\n"
                 "print(json.dumps({'main': m.__file__, 'coord': c.__file__,"
                 " 'co': c.Coordinator.__init__.__code__.co_filename}))\n", str(pyz))
    assert res["main"].endswith("guest/main.pyc")
    assert res["coord"].endswith("guest/coordinator.pyc")
    assert res["co"] == "/opt/harness/guest.pyz/guest/coordinator.py"


def test_pyz_falls_back_to_source_on_a_foreign_magic(built_pyz, tmp_path):
    """A pyc from another Python (build host != guest) is ignored, not fatal."""
    _, pyz = built_pyz
    files = pyz_pack.read_pyz(str(pyz))
    bad = tmp_path / "bad.pyz"
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in sorted(files.items()):
            zf.writestr(n, (b"\0\0\r\n" + b[4:]) if n.endswith(".pyc") else b)
    bad.write_bytes(b"#!/usr/bin/env python3\n" + buf.getvalue())
    res = _fresh("import guest.main as m\nm.preload()\n"
                 "print(json.dumps({'main': m.__file__}))\n", str(bad))
    assert res["main"].endswith("guest/main.py")


def test_pyz_runs(built_pyz):
    _, pyz = built_pyz
    out = subprocess.run([sys.executable, str(pyz), "--help"], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0 and "--idle-wait" in out.stdout


def test_measure_imports_post_hello_is_empty(built_pyz):
    """The measurement script against the built pyz: after preload, the
    session imports nothing (image/measure_imports.py prints the timings)."""
    _, pyz = built_pyz
    res = measure_imports.measure(str(pyz), runs=1)
    assert res["preload"] is True
    assert res["post_hello_modules"] == []
    assert res["post_hello_ms"]["min"] < 20
