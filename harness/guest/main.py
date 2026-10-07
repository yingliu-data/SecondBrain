"""main — the guest process: idle-wait, connect out, hello, run, settle
(PLAN.md section 3 layout, docs/GUEST.md "Lifecycle").

  python3 -m guest.main --connect unix:DIR | vsock[:CID] --log-dir DIR
                        --workspace DIR [--idle-wait FILE] [--hello-timeout S]

Exit codes: 0 settle, 2 fatal protocol error (first control is not a hello,
unusable hello, the coordinator's FatalProtocolError on a second hello),
3 hello timeout.

Two phases, testable apart: run_until_hello(inbound, timeout) drains the
coordinator's queue until the hello control message arrives and turns it into
a GuestConfig; run_session(cfg, ...) builds the log writer, provider,
operation manager and coordinator and runs the session.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
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
IDLE_POLL_S = 0.05
FINAL_ACK_WAIT_S = 10.0


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
    p.add_argument("--idle-wait", default=None, metavar="FILE",
                   help="block until FILE exists before connecting (the snapshot point)")
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


def idle_wait(path: str, poll_s: float = IDLE_POLL_S, timeout: float | None = None) -> bool:
    """Block until `path` exists. In M3 this is where the VM is snapshotted:
    the host creates the file to let the guest proceed. False on timeout."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while not os.path.exists(path):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)
    return True


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


def run_session(cfg: GuestConfig, inbound: queue.Queue, transport: Transport,
                log_dir: str, workspace: str, image_version: str = "dev") -> int:
    """Build the session's parts around the coordinator and run it to settle.
    Returns the process exit code."""
    from protocol.fold import State
    from guest import coordinator as coordinator_mod
    from guest.ops import OperationManager
    from guest.provider import Provider

    eventlog = EventLog(log_dir)
    log_writer = LogWriter(eventlog, transport)
    log_writer.start()
    provider = Provider(transport.connector, port=cfg.ports["llm"], model=cfg.model,
                        timeout_s=cfg.limits.wall_clock_s)
    ops = OperationManager(workspace, cfg.limits, inbound)
    coordinator = coordinator_mod.Coordinator(
        cfg, State(), inbound, log_writer, transport, provider, ops,
        coordinator_mod.SystemClock(), image_version=image_version)
    code = EXIT_OK
    try:
        settle = coordinator.run()
        if not settle:
            log.error("coordinator ended without a settle")
            code = EXIT_PROTOCOL
        else:
            log.info("settled: %s", settle.get("outcome"))
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
    if args.idle_wait:
        log.info("idle-wait on %s", args.idle_wait)
        idle_wait(args.idle_wait)

    inbound: queue.Queue = queue.Queue()
    transport = Transport(connector, inbound, port=args.control_port)
    transport.start()
    try:
        try:
            cfg = run_until_hello(inbound, timeout=args.hello_timeout)
        except HelloTimeout as exc:
            log.error("%s", exc)
            return EXIT_HELLO_TIMEOUT
        except FatalProtocolError as exc:
            log.error("fatal protocol error: %s", exc)
            return EXIT_PROTOCOL
        log.info("hello for session %s (%s/%s)", cfg.session_id,
                 cfg.principal.user, cfg.principal.tenant)
        return run_session(cfg, inbound, transport, args.log_dir, args.workspace,
                           image_version=args.image_version)
    finally:
        transport.stop()


if __name__ == "__main__":
    sys.exit(main())
