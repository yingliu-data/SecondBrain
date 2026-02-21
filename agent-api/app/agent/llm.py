import httpx, logging
from app.config import (
    LLM_URL, LLM_MODEL, LLM_TIMEOUT,
    LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_FALLBACK_URL, LLM_FALLBACK_KEY,
)

logger = logging.getLogger("llm")


class LLMProvider:
    """Abstraction over LLM inference. Swap providers via LLM_PROVIDER env var.
    Supports local (llama-server), cloud fallback, and future multi-model routing."""

    def __init__(self):
        self._local = httpx.AsyncClient(
            base_url=LLM_URL, timeout=httpx.Timeout(LLM_TIMEOUT)
        )
        self._fallback = None
        if LLM_FALLBACK_URL:
            self._fallback = httpx.AsyncClient(
                base_url=LLM_FALLBACK_URL,
                timeout=httpx.Timeout(LLM_TIMEOUT),
                headers={"Authorization": f"Bearer {LLM_FALLBACK_KEY}"},
            )

    async def chat_completion(self, messages: list, tools: list | None = None) -> dict:
        """Send a chat completion request. Returns the OpenAI-compatible response dict.
        Falls back to cloud API if local inference fails and fallback is configured."""
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "stream": False,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = await self._local.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if self._fallback:
                logger.warning(f"Local LLM failed ({e}), falling back to cloud API")
                resp = await self._fallback.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
                return resp.json()
            raise

    async def health(self) -> dict:
        try:
            r = await self._local.get("/health")
            return {"status": "ok", "llm": r.json()}
        except Exception as e:
            return {"status": "degraded", "error": str(e)}
