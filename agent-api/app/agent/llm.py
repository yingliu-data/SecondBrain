import asyncio, httpx, logging
from app.config import LLM_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE

logger = logging.getLogger("llm")

_RETRYABLE_STATUS = {502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0


class LLMProvider:
    """Local vLLM provider. Auto-starts the container via the Docker socket
    if the endpoint is unreachable."""

    def __init__(self):
        self._local: httpx.AsyncClient | None = None

    def _get_local(self) -> httpx.AsyncClient:
        if self._local is None:
            self._local = httpx.AsyncClient(
                base_url=LLM_URL, timeout=httpx.Timeout(LLM_TIMEOUT)
            )
        return self._local

    async def chat_completion(
        self,
        messages: list,
        tools: list | None = None,
        max_tokens: int | None = None,
        chat_template_kwargs: dict | None = None,
    ) -> dict:
        payload = self._build_payload(LLM_MODEL, messages, tools, max_tokens, chat_template_kwargs)
        local = self._get_local()

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await local.post("/v1/chat/completions", json=payload)
                if resp.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"Local LLM returned {resp.status_code}",
                        request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                return resp.json()
            except httpx.ConnectError as e:
                last_error = e
                await self._start_local_llm()
            except (httpx.ReadTimeout, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(f"Local LLM attempt {attempt+1} failed ({e}), retrying in {delay}s")
                    await asyncio.sleep(delay)

        raise RuntimeError(f"Local LLM unavailable after {_MAX_RETRIES} attempts: {last_error}")

    async def _start_local_llm(self):
        """Auto-start the local LLM container via Docker socket API."""
        logger.warning("Local LLM not running, starting container...")
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
                base_url="http://localhost",
            ) as docker:
                r = await docker.post("/containers/secondbrain-llm/start", timeout=30)
                if r.status_code not in (204, 304):  # 204=started, 304=already running
                    raise RuntimeError(f"Docker API returned {r.status_code}: {r.text}")
        except Exception as e:
            raise RuntimeError(f"Failed to start local LLM container: {e}")

        local = self._get_local()
        for i in range(60):  # 60 × 5s = 300s max
            await asyncio.sleep(5)
            try:
                r = await local.get("/health")
                if r.status_code == 200:
                    logger.info("Local LLM is ready")
                    return
            except Exception:
                pass
            logger.info(f"Waiting for local LLM... ({(i+1)*5}s)")
        raise RuntimeError("Local LLM failed to start within 300s")

    def _build_payload(
        self,
        model: str,
        messages: list,
        tools: list | None,
        max_tokens: int | None,
        chat_template_kwargs: dict | None,
    ) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": max_tokens if max_tokens is not None else LLM_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        return payload

    async def health(self) -> dict:
        try:
            local = self._get_local()
            r = await local.get("/health")
            return {"status": "ok", "local": r.json() if r.headers.get("content-type", "").startswith("application/json") else "ok"}
        except Exception as e:
            return {"status": "degraded", "local": f"not_running: {e}"}
