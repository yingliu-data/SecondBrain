"""host — the host half of the actor: one per session.

endpoints.py   per-session Unix-socket listeners for the vsock ports; Firecracker's
               host-side vsock sockets ({uds}_{port}) and the test double are the same code
worker.py      act(): start sandbox, accept :5000, hello + task, relay events, sides, settle
llm_proxy.py   :5010 -> vLLM (LLM_URL); usage; budgets later
sides.py       host skills via SkillRegistry; device relay via SSE + /tool-result
sanitize.py    the after_tool rewrite of host/device results
gateway_forwarder.py  :5011 -> mcp-gateway (POST /mcp only), identity headers, held calls
uds_http.py    the threaded HTTP server llm_proxy and gateway_forwarder share
state_sync.py  ack after append; events/ segments in the session dir; projections
stream.py      events -> SSE (token, tool_call, avatar_command, question, done, seq, error)
uds.py         Unix sockets past sun_path's limit (bind; connect from protocol.uds)
sandbox/       firecracker.py (boot, kill, snapshots), base_snapshot.py (the BASE
               snapshot build CLI), local.py (guest subprocess, tests)

Every tenant runs this engine (the in-process legacy loop, AGENT_ENGINE
and the per-tenant `engine` switch are gone).

Dependencies: agent-api imports the stdlib-only `protocol` package and runs
the guest from `secondbrain-harness` (a member of the one uv workspace, like
`flows`). The Docker image copies harness/ next to the app; HARNESS_GUEST_ROOT
points at it.
"""
