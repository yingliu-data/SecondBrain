"""agent-api's side of the MCP gateway (harness/docs/M4-CONTRACTS.md).

client.py          headers and GET /internal/tools (the worker's session tool list)
events.py          POST /internal/gateway-events -> the session's host log

The guest-facing relay is app/host/gateway_forwarder.py. M6: the legacy
engine's GatewayUpstreamSkill (upstream_skill.py) is deleted.
"""
