"""ports — the vsock ports the guest connects to, one definition for the
guest, the host, the sandboxes, the bench and the test doubles. Stdlib only.

  PORTS              {"control": 5000, "bulk": 5001, "llm": 5010, "gateway": 5011}
  CONTROL, BULK, LLM, GATEWAY   the same numbers by name
  socket_name(port)  "v.sock_{port}": the Unix socket a guest-initiated vsock
                     connection to `port` lands on, under the session's ipc/
                     (Firecracker's uds_path naming; the test doubles use it too)
"""
from __future__ import annotations

CONTROL = 5000      # control channel: hello, task, events, acks (protocol.messages)
BULK = 5001         # bulk channel: spill / workspace / manifest puts, the log return (protocol.bulk)
LLM = 5010          # the host's LLM proxy
GATEWAY = 5011      # the host's MCP gateway forwarder

PORTS = {"control": CONTROL, "bulk": BULK, "llm": LLM, "gateway": GATEWAY}

SOCKET_PREFIX = "v.sock"


def socket_name(port: int) -> str:
    """"v.sock_{port}" (Firecracker: `{uds_path}_{port}` with uds_path `.../v.sock`)."""
    return f"{SOCKET_PREFIX}_{int(port)}"
