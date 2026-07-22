import ipaddress
import socket
from urllib.parse import urlparse

from app.config import MCP_ALLOWED_PRIVATE_HOSTS
from app.tenants import MCPServerConfig


def validate_mcp_url(server: MCPServerConfig) -> None:
    """Reject MCP URLs that resolve to loopback/private/link-local addresses,
    unless the server config opts in (allow_private) or the hostname is in
    the MCP_ALLOWED_PRIVATE_HOSTS allowlist (e.g. host.docker.internal).

    Raises ValueError. Called at registration and again before each connect
    as a DNS-rebinding hedge.
    """
    parsed = urlparse(server.url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"MCP server '{server.name}': URL scheme must be http(s)")
    host = parsed.hostname
    if not host:
        raise ValueError(f"MCP server '{server.name}': URL has no host")

    if server.allow_private or host in MCP_ALLOWED_PRIVATE_HOSTS:
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"MCP server '{server.name}': cannot resolve host '{host}': {e}")

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            raise ValueError(
                f"MCP server '{server.name}': host '{host}' resolves to non-public "
                f"address {addr}; set allow_private or add to MCP_ALLOWED_PRIVATE_HOSTS "
                f"if intended")
