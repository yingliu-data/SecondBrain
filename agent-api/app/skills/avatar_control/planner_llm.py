"""The plan_movement planner's own LLM call (one non-streamed chat completion
against LLM_URL). M6: the legacy LLMProvider is gone; this host skill is the
only in-process LLM caller left (the guest's calls go through the :5010
proxy, app/host/llm_proxy.py)."""
from __future__ import annotations

import httpx

from app.config import LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT, LLM_URL


class PlannerLLM:
    def __init__(self, base_url: str = LLM_URL, timeout_s: float = LLM_TIMEOUT):
        self.base_url = base_url
        self.timeout_s = timeout_s

    async def chat_completion(self, messages: list, tools=None, max_tokens: int | None = None,
                              chat_template_kwargs: dict | None = None) -> dict:
        payload = {"model": LLM_MODEL, "messages": messages, "stream": False,
                   "temperature": LLM_TEMPERATURE}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        async with httpx.AsyncClient(base_url=self.base_url,
                                     timeout=httpx.Timeout(self.timeout_s)) as client:
            resp = await client.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()
