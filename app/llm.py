"""One OpenAI-compatible chat call (OpenRouter by default), with errors classified for the orchestrator."""
from __future__ import annotations

import time

import httpx

from app.config import Settings
from app.llm_types import ChatResult, LLMError, RateLimiter


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.limiter = RateLimiter(settings.llm_requests_per_minute)
        self._http = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_s,
            transport=transport,  # tests inject httpx.MockTransport here
            headers={"Authorization": f"Bearer {settings.api_key}", "X-Title": "Lead Qualifier POC"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _price(self, model: str, prompt: int, completion: int) -> float | None:
        p = self.s.prices.get(model)
        return None if not p else (prompt * p["prompt"] + completion * p["completion"]) / 1e6

    async def chat(self, model: str, messages: list[dict], response_format: dict | None) -> ChatResult:
        body: dict = {"model": model, "messages": messages, "temperature": self.s.llm_temperature, "max_tokens": self.s.llm_max_tokens}
        if response_format:
            body["response_format"] = response_format
        if self.s.is_openrouter:
            body["provider"] = {"data_collection": self.s.llm_data_collection}  # strict APIs reject unknown fields

        await self.limiter.wait()
        t0 = time.monotonic()
        try:
            res = await self._http.post("/chat/completions", json=body)
        except httpx.HTTPError as e:  # timeouts, DNS, connection resets
            raise LLMError("transient", f"network/timeout: {e.__class__.__name__}", latency_ms=int((time.monotonic() - t0) * 1000)) from e
        latency = int((time.monotonic() - t0) * 1000)

        try:
            data = res.json()
        except ValueError:
            data = {}
        message = str((data.get("error") or {}).get("message") or res.text[:200]) if isinstance(data, dict) else res.text[:200]
        status = res.status_code

        if 200 <= status < 300 and isinstance(data, dict) and data.get("choices"):
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            prompt, completion = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            used = data.get("model") or model
            cost, source = usage.get("cost"), "provider"           # OpenRouter reports cost on every response
            if not isinstance(cost, (int, float)):
                cost = self._price(used, prompt, completion) or self._price(model, prompt, completion)
                source = "price_table" if cost is not None else "unknown"
            return ChatResult(
                content=(choice.get("message") or {}).get("content"), model_used=used, provider=data.get("provider"),
                generation_id=data.get("id"), prompt_tokens=prompt, completion_tokens=completion,
                cost_usd=float(cost) if cost is not None else None, cost_source=source, latency_ms=latency,
                finish_reason=choice.get("finish_reason"))

        retry_after = res.headers.get("retry-after")
        retry_after_s = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None
        if status in (401, 403):
            raise LLMError("fatal", f"auth rejected ({status}): {message}", status, latency_ms=latency)
        if status == 402:
            raise LLMError("fatal", f"payment required (402): {message}", status, latency_ms=latency)
        if status in (400, 404) and response_format and any(w in message.lower() for w in ("response_format", "json_schema", "structured", "parameter", "schema")):
            raise LLMError("no_schema", message, status, latency_ms=latency)
        if status in (408, 429) or status >= 500 or 200 <= status < 300:
            raise LLMError("transient", f"{status}: {message or 'no choices in response'}", status, retry_after_s, latency)
        raise LLMError("model", f"{status}: {message}", status, latency_ms=latency)  # 400/404/422: try the next model
