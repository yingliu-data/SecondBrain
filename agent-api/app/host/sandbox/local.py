"""local — the guest harness as a subprocess (tests, no-KVM machines).

Runs `python -m guest.main --connect unix:{session_dir}/ipc --log-dir
{session_dir}/guest --workspace {session_dir}/workspace` with the harness
directory on its path. The guest then connects to the same
`{session_dir}/ipc/v.sock_{port}` listeners Firecracker would (Endpoints).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from app.config import HARNESS_GUEST_ROOT
from app.host.sandbox import Sandbox

logger = logging.getLogger("host.sandbox")


class LocalSandbox(Sandbox):
    def __init__(self, guest_root: str | Path = HARNESS_GUEST_ROOT,
                 python: str | None = None, extra_args: list[str] | None = None):
        self.guest_root = Path(guest_root)
        self.python = python or sys.executable
        self.extra_args = list(extra_args or [])
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self, session_dir: Path) -> None:
        if self.is_alive():
            return
        session_dir = Path(session_dir)
        log_dir = session_dir / "guest"
        log_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "workspace").mkdir(parents=True, exist_ok=True)
        cmd = [
            self.python, "-m", "guest.main",
            "--connect", f"unix:{session_dir / 'ipc'}",
            "--log-dir", str(log_dir),
            "--workspace", str(session_dir / "workspace"),
            *self.extra_args,
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.guest_root) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._log = open(log_dir / "guest.stderr.log", "ab")
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.guest_root), env=env,
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logger.info("guest started pid=%s (%s)", self.proc.pid, " ".join(cmd))

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()
