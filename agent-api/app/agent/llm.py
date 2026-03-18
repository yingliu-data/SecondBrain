import asyncio, httpx, logging
from app.config import (
    LLM_URL, LLM_MODEL, LLM_TIMEOUT,
    LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_FALLBACK_URL, LLM_FALLBACK_KEY,
)

logger = logging.getLogger("llm")

# Transient HTTP status codes worth retrying
_RETRYABLE_STATUS = {502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds: 1s, 2s, 4s


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
        """Send a chat completion request with retry + exponential backoff.
        Retries transient errors up to 3 times, then falls back to cloud API."""
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

        last_error = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._local.post("/v1/chat/completions", json=payload)
                if resp.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"LLM returned {resp.status_code}",
                        request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(f"Local LLM attempt {attempt+1} failed ({e}), retrying in {delay}s")
                    await asyncio.sleep(delay)

        # All local retries exhausted — try cloud fallback
        if self._fallback:
            logger.warning(f"Local LLM failed after {_MAX_RETRIES} attempts ({last_error}), falling back to cloud API")
            resp = await self._fallback.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()

        raise last_error

    async def health(self) -> dict:
        try:
            r = await self._local.get("/health")
            return {"status": "ok", "llm": r.json()}
        except Exception as e:
            return {"status": "degraded", "error": str(e)}
