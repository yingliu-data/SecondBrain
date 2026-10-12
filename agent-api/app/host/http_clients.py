"""http_clients — one httpx.Client per upstream per process.

The LLM proxy (:5010) and the gateway relay (:5011) of every session talk
to the same upstreams; a Client per Worker meant a connection pool and its
sockets per session. shared_client(base_url, timeout) returns the one
Client for that (url, timeout), made on first use; httpx.Client is
thread-safe, so the per-request handler threads of every session share it.
No connection cap (a stream or a 45-minute gateway call holds its
connection, as with per-Worker clients); idle keep-alives are bounded.

  shared_client(base_url, timeout) -> httpx.Client
  close_all()                        shutdown (and tests)
"""
from __future__ import annotations

import threading

import httpx

_clients: dict[tuple, httpx.Client] = {}
_lock = threading.Lock()
LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=64)


def shared_client(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
    key = (base_url.rstrip("/"), repr(timeout))
    with _lock:
        client = _clients.get(key)
        if client is None or client.is_closed:
            client = _clients[key] = httpx.Client(base_url=key[0], timeout=timeout,
                                                  limits=LIMITS)
        return client


def close_all() -> None:
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for c in clients:
        c.close()
