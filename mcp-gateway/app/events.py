"""Gateway events → agent-api (M4-CONTRACTS.md §7). Fire-and-forget: a
failed post is logged and never fails the call."""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("gateway.events")

POST_TIMEOUT_S = 5


class EventPoster:
    def __init__(self, agent_api_url: str, token: str, client: httpx.AsyncClient | None = None):
        self._url = agent_api_url.rstrip("/") + "/internal/gateway-events"
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.AsyncClient(timeout=POST_TIMEOUT_S)
        self._tasks: set[asyncio.Task] = set()

    def post(self, ident_fields: dict, op: str, **fields) -> None:
        body = {**ident_fields, "op": op, **{k: v for k, v in fields.items() if v is not None}}
        task = asyncio.create_task(self._send(body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, body: dict) -> None:
        try:
            r = await self._client.post(self._url, json=body, headers=self._headers)
            if r.status_code >= 300:
                logger.warning("gateway event %s rejected: HTTP %s", body.get("op"), r.status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("gateway event %s not posted: %s", body.get("op"), e)

    async def drain(self, timeout: float = POST_TIMEOUT_S) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def aclose(self) -> None:
        await self.drain()
        await self._client.aclose()
