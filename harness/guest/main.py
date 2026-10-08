"""main — the guest process: idle-wait, connect out, hello, run, settle
(PLAN.md section 3 layout, docs/GUEST.md "Lifecycle", docs/M3-CONTRACTS.md).

  python3 -m guest.main --connect unix:DIR | vsock[:CID] --log-dir DIR
                        --workspace DIR [--idle-wait vsock]
                        [--mount-workspace DEV] [--hello-timeout S]

Exit codes: 0 the host stopped the guest or an abort settled the task, 2
fatal protocol error (first control is not a hello, unusable hello, the
coordinator's FatalProtocolError on a second hello, a resume whose log could
not be fetched or folded), 3 hello timeout.

Idle-wait (the snapshot point, M3-CONTRACTS section 1): with `--idle-wait
vsock` the transport polls :5000 every IDLE_POLL_S (50 ms), silently, until
the host answers (Transport.wait_awake). The base snapshot is taken in this
loop. Then `--mount-workspace DEV` mounts DEV on the workspace (`mount -o
noatime`) unless it is already a mountpoint; a missing DEV is skipped (tests,
Local). The hello timeout counts from the end of idle-wait.

Phases, testable apart: run_until_hello(inbound, timeout) drains the
coordinator's queue until the hello control message arrives and turns it into
a GuestConfig; run_session(cfg, ...) builds the log writer, provider,
operation manager and coordinator and runs the session. With hello{resume:
true} run_session first fetches the host's log on :5001 (bulk get, after_seq
0), rebuilds the local event buffer from it, continues seq after it and hands
it to Coordinator.restore (M3-CONTRACTS section 4).

One lifecycle: the guest stays up after a settle and takes the next task (a
session-long VM); after a park it returns to idle-wait. It ends when the host
stops it (SIGTERM from init, or the VM is stopped), on an abort, or on a
fatal error.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import time

from guest.config import GuestConfig, from_hello
from guest.eventlog import EventLog, LogWriter
from guest.transport import CONTROL_PORT, Transport, UnixConnector, VsockConnector

log = logging.getLogger("guest.main")

EXIT_OK = 0
EXIT_PROTOCOL = 2
EXIT_HELLO_TIMEOUT = 3
HELLO_TIMEOUT_S = 30.0
FINAL_ACK_WAIT_S = 10.0
LOG_RETURN_ATTEMPTS = 3
LOG_RETURN_RETRY_S = 0.5


class HelloTimeout(Exception):
    """No hello arrived in time."""


class FatalProtocolError(Exception):
    """The host broke the control protocol before the coordinator existed."""


# ---------------------------------------------------------------- arguments


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="guest.main", description="SecondBrain guest harness")
    p.add_argument("--connect", required=True,
                   help="unix:DIR (Unix sockets {DIR}/v.sock_{port}) or vsock[:CID] (default CID 2)")
    p.add_argument("--log-dir", required=True, help="where events/*.jsonl are buffered")
    p.add_argument("--workspace", required=True, help="the task's working directory")
    p.add_argument("--idle-wait", default=None, choices=["vsock"],
                   help="'vsock': poll :5000 every 50 ms until the host answers (the snapshot point)")
    p.add_argument("--mount-workspace", default=None, metavar="DEV",
                   help="after idle-wait, mount DEV on the workspace unless already mounted")
    p.add_argument("--hello-timeout", type=float, default=HELLO_TIMEOUT_S)
    p.add_argument("--control-port", type=int, default=CONTROL_PORT)
    p.add_argument("--image-version", default="dev", help="recorded in session_start")
    return p.parse_args(argv)


def make_connector(spec: str):
    """'unix:DIR' -> UnixConnector(DIR); 'vsock' or 'vsock:CID' -> VsockConnector."""
    if spec.startswith("unix:"):
        dir = spec[len("unix:"):]
        if not dir:
            raise ValueError("--connect unix:DIR needs a directory")
        return UnixConnector(dir)
    if spec == "vsock":
        return VsockConnector()
    if spec.startswith("vsock:"):
        try:
            return VsockConnector(int(spec[len("vsock:"):]))
        except ValueError:
            raise ValueError(f"--connect vsock:CID needs an integer CID, got {spec!r}") from None
    raise ValueError(f"--connect must be unix:DIR or vsock[:CID], got {spec!r}")


def mount_workspace(dev: str, workspace: str, runner=subprocess.run) -> str:
    """Mount `dev` on `workspace` (noatime). Returns what happened:
    "mounted", "already" (workspace is a mountpoint), "absent" (no such
    device: tests and LocalSandbox) or "failed" (logged; the guest goes on
    with whatever the directory holds)."""
    if os.path.ismount(workspace):
        return "already"
    if not os.path.exists(dev):
        log.info("workspace device %s absent; not mounting", dev)
        return "absent"
    os.makedirs(workspace, exist_ok=True)
    try:
        proc = runner(["mount", "-o", "noatime", dev, workspace],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("mount %s on %s failed: %s", dev, workspace, exc)
        return "failed"
    if proc.returncode != 0:
        out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else proc.stdout
        log.error("mount %s on %s failed (exit %s): %s", dev, workspace, proc.returncode, out)
        return "failed"
    log.info("mounted %s on %s", dev, workspace)
    return "mounted"


# ---------------------------------------------------------------- phases


def run_until_hello(inbound: queue.Queue, timeout: float = HELLO_TIMEOUT_S) -> GuestConfig:
    """Drain `inbound` until the hello control message arrives and build the
    config from it. Link and ack items before it are ignored. Raises
    HelloTimeout, or FatalProtocolError when the first control message is
    not a usable hello."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelloTimeout(f"no hello within {timeout:g}s")
        try:
            item = inbound.get(timeout=remaining)
        except queue.Empty:
            raise HelloTimeout(f"no hello within {timeout:g}s") from None
        if item[0] != "control":
            continue
        msg = item[1]
        if msg["type"] != "hello":
            raise FatalProtocolError(
                f"first control message is {msg['type']!r} (cseq {msg['cseq']}), expected hello")
        try:
            return from_hello(msg["body"])
        except ValueError as exc:
            raise FatalProtocolError(f"unusable hello: {exc}") from None


def fetch_log(bulk, attempts: int = LOG_RETURN_ATTEMPTS, retry_s: float = LOG_RETURN_RETRY_S) -> list:
    """The log return: the host's acked events (bulk get, after_seq 0)."""
    from protocol.bulk import BulkError
    for attempt in range(1, attempts + 1):
        try:
            events, last_seq = bulk.get_log(0)
            log.info("log return: %d events through seq %d", len(events), last_seq)
            return events
        except BulkError as exc:
            if attempt == attempts:
                raise FatalProtocolError(f"log return failed: {exc}") from None
            log.warning("log return attempt %d failed: %s", attempt, exc)
            time.sleep(retry_s)


def rebuild_eventlog(log_dir: str, events: list) -> EventLog:
    """The local buffer after a log return: whatever an earlier process left
    in log_dir moves to events.before-resume-N, and the returned events (all
    acked: the host holds them) become the new buffer."""
    events_dir = os.path.join(log_dir, "events")
    if os.path.isdir(events_dir) and os.listdir(events_dir):
        n = 1
        while os.path.exists(f"{events_dir}.before-resume-{n}"):
            n += 1
        os.rename(events_dir, f"{events_dir}.before-resume-{n}")
    eventlog = EventLog(log_dir)
    eventlog.extend(events)
    if events:
        eventlog.mark_acked(events[-1]["seq"])
    return eventlog


def run_session(cfg: GuestConfig, inbound: queue.Queue, transport: Transport,
                log_dir: str, workspace: str, image_version: str = "dev") -> int:
    """Build the session's parts around the coordinator and run its tasks,
    one after the other, until an abort, a fatal error or the host stops the
    process. Returns the process exit code."""
    from protocol.fold import FoldError, State
    from guest import coordinator as coordinator_mod
    from guest.bulk import BulkClient
    from guest.ops import OperationManager
    from guest.provider import Provider

    bulk = BulkClient(transport.connector, cfg.session_id, port=cfg.ports.get("bulk", 5001))
    returned: list = []
    if cfg.resume:
        try:
            returned = fetch_log(bulk)
        except FatalProtocolError as exc:
            log.error("%s", exc)
            return EXIT_PROTOCOL
        eventlog = rebuild_eventlog(log_dir, returned)
        if returned:
            transport.seed_seq(returned[-1]["seq"])
    else:
        eventlog = EventLog(log_dir)
    log_writer = LogWriter(eventlog, transport)
    log_writer.start()
    provider = Provider(transport.connector, port=cfg.ports["llm"], model=cfg.model)
    ops = OperationManager(workspace, cfg.limits, inbound, bulk=bulk)
    coordinator = coordinator_mod.Coordinator(
        cfg, State(), inbound, log_writer, transport, provider, ops,
        coordinator_mod.SystemClock(), image_version=image_version,
        workspace=workspace, bulk=bulk)
    code = EXIT_OK
    try:
        if returned:
            coordinator.restore(returned)
        while True:
            settle = coordinator.run()
            if not settle:
                log.error("coordinator ended without a settle")
                code = EXIT_PROTOCOL
                break
            log.info("settled: %s", settle.get("outcome"))
            if settle.get("reason") == "abort":
                break
            log_writer.flush(timeout=FINAL_ACK_WAIT_S)
    except (FoldError, ValueError) as exc:
        log.error("the returned log does not restore: %s", exc)
        code = EXIT_PROTOCOL
    except coordinator_mod.FatalProtocolError as exc:
        log.error("fatal protocol error: %s", exc)
        code = EXIT_PROTOCOL
    except Exception:
        log.exception("coordinator failed")
        code = EXIT_PROTOCOL
    finally:
        # settle: flush the log and wait (bounded) for the host's final ack
        log_writer.flush(timeout=FINAL_ACK_WAIT_S)
        if eventlog.last_seq and not transport.wait_acked(eventlog.last_seq, FINAL_ACK_WAIT_S):
            log.warning("host did not ack seq %d within %gs", eventlog.last_seq, FINAL_ACK_WAIT_S)
        # the coordinator stopped reading acks at settle: record the final one
        eventlog.mark_acked(min(transport.acked_seq, eventlog.last_seq))
        log_writer.stop()
        eventlog.close()
        ops.shutdown()
    return code


# ---------------------------------------------------------------- entry


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        connector = make_connector(args.connect)
    except ValueError as exc:
        log.error("%s", exc)
        return EXIT_PROTOCOL
    inbound: queue.Queue = queue.Queue()
    transport = Transport(connector, inbound, port=args.control_port,
                          idle=args.idle_wait is not None)
    transport.start()
    try:
        transport.wait_awake()              # silent until the host answers (after a restore)
        if args.mount_workspace:
            mount_workspace(args.mount_workspace, args.workspace)
        try:
            cfg = run_until_hello(inbound, timeout=args.hello_timeout)
        except HelloTimeout as exc:
            log.error("%s", exc)
            return EXIT_HELLO_TIMEOUT
        except FatalProtocolError as exc:
            log.error("fatal protocol error: %s", exc)
            return EXIT_PROTOCOL
        log.info("hello for session %s (%s/%s)%s", cfg.session_id,
                 cfg.principal.user, cfg.principal.tenant, " resume" if cfg.resume else "")
        return run_session(cfg, inbound, transport, args.log_dir, args.workspace,
                           image_version=args.image_version)
    finally:
        transport.stop()


if __name__ == "__main__":
    sys.exit(main())
