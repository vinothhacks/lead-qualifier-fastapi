"""Retry, repair and fallback for ONE lead. Pure Python: the HTTP call and the validator are injected.

For each model in order (primary, then fallbacks):
  call → validate → if invalid, retry the SAME model with the exact errors (repair turn)
  transport errors (429/5xx/timeout) back off and retry without using a validation attempt
  model errors (400/404) move to the next model; account errors (401/402) stop everything
Invalid output is never returned: the outcome is either `qualified` with a validated value,
or `quarantined` with the reasons.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from app.llm_types import ChatResult, CircuitBreaker, LLMError
from app.prompts import repair_message, scrub_echo


class Validation(Protocol):
    ok: bool
    value: Any
    errors: list[str]


ChatFn = Callable[[str, list[dict], bool], Awaitable[ChatResult]]
ValidateFn = Callable[[str | None], Validation]


@dataclass
class RetryPolicy:
    models: list[str]
    attempts_per_model: int = 2
    max_attempts_per_lead: int = 4
    transport_retries: int = 2
    backoff_s: float = 2.0
    use_structured_output: bool = True


@dataclass
class Outcome:
    status: Literal["qualified", "quarantined"]
    value: Any = None
    final_model: str | None = None
    reason: str | None = None
    errors: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)


class FatalLLMError(Exception):
    def __init__(self, message: str, calls: list[dict]):
        super().__init__(message)
        self.calls = calls


async def qualify(
    system: str,
    user: str,
    *,
    policy: RetryPolicy,
    chat: ChatFn,
    validate: ValidateFn,
    breaker: CircuitBreaker,
    schema_unsupported: set[str],
    egress_check: Callable[[str], list[str]],
    budget_exceeded: Callable[[], bool] = lambda: False,
) -> Outcome:
    calls: list[dict] = []
    total = 0
    last_errors: list[str] = []
    base = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    for model in policy.models:
        if breaker.is_open(model):
            calls.append({"attempt": len(calls) + 1, "model_requested": model, "outcome": "skipped_circuit_open"})
            continue
        messages = list(base)
        tries = transport = 0
        while tries < policy.attempts_per_model and total < policy.max_attempts_per_lead:
            if budget_exceeded():
                return Outcome("quarantined", reason="run_budget_reached", errors=last_errors, calls=calls)
            leaks = egress_check("\n".join(m["content"] for m in messages if m["role"] != "system"))
            if leaks:
                return Outcome("quarantined", reason="pii_egress_blocked", errors=[f"blocked before sending: {', '.join(leaks)}"], calls=calls)

            use_schema = policy.use_structured_output and model not in schema_unsupported
            log: dict = {"attempt": len(calls) + 1, "model_requested": model, "structured_output": use_schema}
            calls.append(log)
            total += 1
            try:
                r = await chat(model, messages, use_schema)
            except LLMError as e:
                log.update(outcome=e.kind, http_status=e.status, error=str(e)[:300], latency_ms=e.latency_ms,
                           prompt_tokens=0, completion_tokens=0, cost_usd=0.0, cost_source="not_billed")
                if e.kind == "fatal":
                    raise FatalLLMError(str(e), calls) from e
                if e.kind == "no_schema":
                    schema_unsupported.add(model)          # remember for every later request
                    total -= 1
                    continue
                if e.kind == "transient" and transport < policy.transport_retries:
                    transport += 1
                    total -= 1                              # transport retries don't use a validation attempt
                    await asyncio.sleep(min(e.retry_after, 30) if e.retry_after is not None else policy.backoff_s * 2 ** (transport - 1))
                    continue
                breaker.failure(model)
                last_errors = [str(e)[:300]]
                break                                       # next model

            breaker.success(model)
            tries += 1
            log.update(model_used=r.model_used, provider=r.provider, generation_id=r.generation_id, latency_ms=r.latency_ms,
                       prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
                       cost_usd=r.cost_usd, cost_source=r.cost_source, http_status=200)
            v = validate(r.content)
            if v.ok:
                log["outcome"] = "valid"
                return Outcome("qualified", value=v.value, final_model=r.model_used, calls=calls)
            last_errors = list(v.errors)
            if r.finish_reason == "length":
                last_errors.append("output was cut off by max_tokens")
            log.update(outcome="invalid_output", errors=last_errors)
            # Repair turn: show the model its own (scrubbed) answer and exactly what was wrong.
            messages = base + [{"role": "assistant", "content": scrub_echo(r.content)},
                               {"role": "user", "content": repair_message(last_errors)}]
        if total >= policy.max_attempts_per_lead:
            break

    reason = "invalid_output_after_retries" if any(c.get("outcome") == "invalid_output" for c in calls) else "all_models_failed"
    return Outcome("quarantined", reason=reason, errors=last_errors, calls=calls)
