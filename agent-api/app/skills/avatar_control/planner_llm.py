"""The plan_movement planner's own LLM call (one non-streamed chat completion
against LLM_URL). The legacy LLMProvider is gone; this host skill is the
only in-process LLM caller left (the guest's calls go through the :5010
proxy, app/host/llm_proxy.py). `planner_llm` is the process's one instance:
the avatar skill calls it and /health asks it about the LLM server."""
from __future__ import annotations

import asyncio

import httpx

from app.config import LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT, LLM_URL

HEALTH_TIMEOUT_S = 5


class PlannerLLM:
    def __init__(self, base_url: str = LLM_URL, timeout_s: float = LLM_TIMEOUT):
        self.base_url = base_url
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _http(self) -> httpx.AsyncClient:
        """The one client, made on first use (and again on another event
        loop: its pooled connections belong to the loop that opened them)."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(base_url=self.base_url,
                                             timeout=httpx.Timeout(self.timeout_s))
            self._loop = loop
        return self._client

    async def chat_completion(self, messages: list, tools=None, max_tokens: int | None = None,
                              chat_template_kwargs: dict | None = None) -> dict:
        payload = {"model": LLM_MODEL, "messages": messages, "stream": False,
                   "temperature": LLM_TEMPERATURE}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        resp = await self._http().post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def health(self):
        """The LLM server's own /health: its JSON body, else "ok". Raises
        when it cannot be reached."""
        r = await self._http().get("/health", timeout=HEALTH_TIMEOUT_S)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return "ok"


planner_llm = PlannerLLM()
