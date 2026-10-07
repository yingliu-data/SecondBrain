"""protocol — the contract shared by the guest harness, the host half, the
gateway and the tests. Standard library only (tests/test_purity.py enforces it).

Modules:
  canonical   canonical JSON, sha256, derived ids
  messages    vsock envelope (seq | cseq), acks, resend bookkeeping
  events      event schema, versions, validation rules
  fold        fold(events) -> State, checkpoint verify, replay verifier
  allowlist   effective tool list and sides from hello
"""
SCHEMA_VERSION = 1
