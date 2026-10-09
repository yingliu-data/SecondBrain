"""agent-api's side of the MCP gateway (harness/docs/M4-CONTRACTS.md).

client.py          headers and the /internal/* calls (worker tool list,
                   legacy upstream tools and calls)
upstream_skill.py  GatewayUpstreamSkill: one legacy-engine skill per upstream
events.py          POST /internal/gateway-events -> the session's host log

The guest-facing relay is app/host/gateway_forwarder.py.
"""
