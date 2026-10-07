"""host — the host half of the actor (PLAN.md section 3, 8): one per session.

endpoints.py   per-session Unix-socket listeners for the vsock ports; Firecracker's
               host-side vsock sockets ({uds}_{port}) and the test double are the same code
worker.py      act(): start sandbox, accept :5000, hello + task, relay events, sides, settle
llm_proxy.py   :5010 -> vLLM (LLM_URL); usage; budgets later
sides.py       host skills via SkillRegistry; device relay via SSE + /tool-result; mcp later
state_sync.py  ack after append; events/ segments in the session dir; projections
stream.py      events -> SSE (token, tool_call, avatar_command, question, done, seq, error)
sandbox/       firecracker.py (boot, kill; snapshots in M3), local.py (guest subprocess, tests)

Engine switch: config.AGENT_ENGINE ("legacy" | "harness"), per-tenant `engine`
in tenants.json (routes/chat.py engine_for).

Dependencies: agent-api imports the stdlib-only `protocol` package and runs
the guest from `secondbrain-harness` (pyproject: path ../harness, editable).
The Docker image must `COPY ../harness` next to the app in M1's image step
(the Dockerfile is not edited here); HARNESS_GUEST_ROOT points at it.
"""
