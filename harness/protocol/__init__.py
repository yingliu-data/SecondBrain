"""protocol — the contract shared by the guest harness, the host half, the
gateway and the tests. Standard library only (tests/test_purity.py enforces it).

Modules:
  canonical   canonical JSON, sha256, derived ids
  messages    vsock envelope (seq | cseq), acks, resend bookkeeping
  events      event schema, versions, validation rules
  fold        fold(events) -> State, checkpoint verify, replay verifier
  allowlist   effective tool list and sides from hello
  bulk        the :5001 framing, the workspace manifest, the log return codec
  replay      on-disk layout, segment reader, replay / verify CLI
  mcp         MCP wire names, the internal bearer and routes
  sse         server-sent events framing
  ports       the vsock ports and their socket names
  limits      DEFAULT_LIMITS, SEGMENT_EVENTS and shared timeouts
  fsio        atomic writes, file digests
  usage       normalise_usage: one total_tokens rule
  clock       now_ms, now_iso (the event ts format), elapsed_ms
  uds         Unix-socket connect past sun_path's limit
  version     HARNESS_VERSION
"""
SCHEMA_VERSION = 1
