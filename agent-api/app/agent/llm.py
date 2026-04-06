import asyncio, time, httpx, logging
from app.config import (
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_URL,
    LLM_URL, LLM_MODEL, LLM_TIMEOUT,
    LLM_MAX_TOKENS, LLM_TEMPERATURE,
)

logger = logging.getLogger("llm")

_RETRYABLE_STATUS = {502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0
_DEFAULT_QUOTA_COOLDOWN = 3600  # 1 hour fallback if no Retry-After header


class LLMProvider:
    """Gemini-primary LLM provider with local llama-server fallback.
    Local client is created lazily to save resources when Gemini handles everything."""

    def __init__(self):
        # Primary: Gemini (always ready if key is set)
        self._gemini = None
        if GEMINI_API_KEY:
            self._gemini = httpx.AsyncClient(
                base_url=GEMINI_URL,
                timeout=httpx.Timeout(LLM_TIMEOUT),
                headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
            )

        # Fallback: local llama-server (lazy — created on first use)
        self._local = None

        # Quota tracking
        self._gemini_exhausted = False
        self._gemini_exhausted_until: float | None = None

    def _get_local(self) -> httpx.AsyncClient:
        """Lazily create the local LLM client."""
        if self._local is None:
            self._local = httpx.AsyncClient(
                base_url=LLM_URL, timeout=httpx.Timeout(LLM_TIMEOUT)
            )
        return self._local

    def _is_quota_error(self, resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.status_code == 403:
            try:
                body = resp.json()
                msg = body.get("error", {}).get("message", "").lower()
                return "quota" in msg or "limit" in msg
            except Exception:
                return False
        return False

    def _mark_gemini_exhausted(self, resp: httpx.Response):
        self._gemini_exhausted = True
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                self._gemini_exhausted_until = time.monotonic() + int(retry_after)
            except ValueError:
                self._gemini_exhausted_until = time.monotonic() + _DEFAULT_QUOTA_COOLDOWN
        else:
            self._gemini_exhausted_until = time.monotonic() + _DEFAULT_QUOTA_COOLDOWN
        logger.warning(
            f"Gemini quota exhausted, falling back to local LLM. "
            f"Will retry Gemini after {self._gemini_exhausted_until - time.monotonic():.0f}s"
        )

    def _check_quota_reset(self):
        if self._gemini_exhausted and self._gemini_exhausted_until:
            if time.monotonic() >= self._gemini_exhausted_until:
                self._gemini_exhausted = False
                self._gemini_exhausted_until = None
                logger.info("Gemini quota cooldown expired, retrying Gemini")

    async def chat_completion(self, messages: list, tools: list | None = None) -> dict:
        self._check_quota_reset()

        # Try Gemini first (if available and not exhausted)
        if self._gemini and not self._gemini_exhausted:
            payload = self._build_payload(GEMINI_MODEL, messages, tools)
            last_error = None

            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await self._gemini.post("/chat/completions", json=payload)

                    if self._is_quota_error(resp):
                        self._mark_gemini_exhausted(resp)
                        break  # fall through to local

                    if resp.status_code in _RETRYABLE_STATUS:
                        raise httpx.HTTPStatusError(
                            f"Gemini returned {resp.status_code}",
                            request=resp.request, response=resp,
                        )
                    resp.raise_for_status()
                    logger.debug("Using Gemini")
                    return resp.json()
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as e:
                    last_error = e
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(f"Gemini attempt {attempt+1} failed ({e}), retrying in {delay}s")
                        await asyncio.sleep(delay)
            else:
                # All retries failed (not quota) — still try local
                logger.warning(f"Gemini failed after {_MAX_RETRIES} attempts ({last_error}), trying local LLM")

        # Fallback: local llama-server
        logger.info("Using local LLM fallback")
        payload = self._build_payload(LLM_MODEL, messages, tools)
        local = self._get_local()
        try:
            resp = await local.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            await self._start_local_llm()
            resp = await local.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()

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
        for i in range(24):  # 24 × 5s = 120s max
            await asyncio.sleep(5)
            try:
                r = await local.get("/health")
                if r.status_code == 200:
                    logger.info("Local LLM is ready")
                    return
            except Exception:
                pass
            logger.info(f"Waiting for local LLM... ({(i+1)*5}s)")
        raise RuntimeError("Local LLM failed to start within 120s")

    def _build_payload(self, model: str, messages: list, tools: list | None) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def health(self) -> dict:
        result = {"status": "ok"}

        # Check Gemini
        if self._gemini:
            if self._gemini_exhausted:
                result["gemini"] = "quota_exhausted"
            else:
                result["gemini"] = "configured"
        else:
            result["gemini"] = "not_configured"

        # Check local
        try:
            local = self._get_local()
            r = await local.get("/health")
            result["local"] = r.json()
        except Exception:
            result["local"] = "not_running"

        if result["gemini"] != "configured" and result["local"] == "not_running":
            result["status"] = "degraded"

        return result
