"""Types shared by the HTTP client and the orchestrator. Pure Python (asyncio only)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

ErrorKind = Literal["transient", "model", "fatal", "no_schema"]


@dataclass
class ChatResult:
    content: str | None
    model_used: str
    provider: str | None
    generation_id: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    cost_source: str          # "provider" | "price_table" | "unknown"
    latency_ms: int
    finish_reason: str | None


class LLMError(Exception):
    """transient: retry same model (429, 5xx, timeout) · model: this model can't serve us, try the next ·
    fatal: account problem (401/402), stop · no_schema: model rejected response_format, retry without it."""

    def __init__(self, kind: ErrorKind, message: str, status: int | None = None, retry_after: float | None = None, latency_ms: int = 0):
        super().__init__(message)
        self.kind, self.status, self.retry_after, self.latency_ms = kind, status, retry_after, latency_ms


class RateLimiter:
    """Spaces request starts across ALL concurrent API requests (free tier: 20 requests/min)."""

    def __init__(self, requests_per_minute: int):
        self._gap = 60.0 / max(1, requests_per_minute)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._gap
        if start > now:
            await asyncio.sleep(start - now)


class CircuitBreaker:
    """After N consecutive hard failures a model is skipped for a cooldown, so an outage
    doesn't cost every request a full retry cycle on the dead model first."""

    def __init__(self, failures: int, cooldown_s: float):
        self.failures, self.cooldown_s = failures, cooldown_s
        self._count: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, model: str) -> bool:
        return time.monotonic() < self._open_until.get(model, 0.0)

    def success(self, model: str) -> None:
        self._count[model] = 0

    def failure(self, model: str) -> None:
        self._count[model] = self._count.get(model, 0) + 1
        if self._count[model] >= self.failures:
            self._open_until[model] = time.monotonic() + self.cooldown_s
            self._count[model] = 0
