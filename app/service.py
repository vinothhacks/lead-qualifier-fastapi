"""Processes ONE lead end to end. The only place that sees both the contact details and the result."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field

from app.config import Settings
from app.contract import output_schema, response_format, validate_llm_output
from app.llm import LLMClient
from app.llm_types import CircuitBreaker
from app.pii import build_llm_view, company_key, contact_keys, egress_violations, greeting, normalise_email, normalise_phone
from app.prompts import system_prompt, user_prompt
from app.qualifier import FatalLLMError, Outcome, RetryPolicy, qualify
from app.schemas import CallLog, LeadIn, Qualification, QualifyResponse, Usage
from app.store import RunStore

log = logging.getLogger("lead_qualifier")
SENT_TO_LLM = ["company", "job_title", "city", "country", "source", "message (scrubbed)"]


@dataclass
class Deps:
    settings: Settings
    llm: LLMClient
    store: RunStore
    breaker: CircuitBreaker
    semaphore: asyncio.Semaphore
    schema_unsupported: set[str] = field(default_factory=set)
    hash_secret: bytes = b""

    @classmethod
    def build(cls, s: Settings, llm: LLMClient) -> "Deps":
        secret = s.contact_hash_secret.get_secret_value().encode() or secrets.token_bytes(32)
        return cls(settings=s, llm=llm, store=RunStore(s.database_path),
                   breaker=CircuitBreaker(s.llm_breaker_failures, s.llm_breaker_cooldown_s),
                   semaphore=asyncio.Semaphore(s.llm_max_concurrency), hash_secret=secret)


def _action_hint(intent: str, score: int, flags: list[str], duplicate_of: str | None, same_company: list[str]) -> str:
    if intent == "unsubscribe":
        return "DO NOT CONTACT: add to suppression list. Send the opt-out confirmation only if your policy requires it."
    if intent == "spam":
        return "No reply. Discard."
    if "no_usable_contact" in flags:
        return "No valid email or phone: cannot reply until contact details are found."
    notes = ["Reply today." if score >= 8 else "Reply this week." if score >= 5 else "Low priority."]
    if duplicate_of:
        notes.append(f"Same person as {duplicate_of}: reply once, in one thread.")
    if same_company:
        notes.append(f"Colleague(s) at the same company: {', '.join(same_company)}. Coordinate one account thread.")
    if "possible_prompt_injection" in flags:
        notes.append("Message tried to instruct the AI: double-check the score and draft.")
    return " ".join(notes)


async def process_lead(lead: LeadIn, run_id: str, deps: Deps) -> QualifyResponse:
    s = deps.settings
    flags: list[str] = []

    email, email_ok = normalise_email(lead.email)
    if not email:
        flags.append("missing_email")
    elif not email_ok:
        flags.append("invalid_email")
    phone = normalise_phone(lead.phone)
    if phone.problem:
        flags.append(phone.problem)
    if not email_ok and not phone.valid:
        flags.append("no_usable_contact")

    keys = contact_keys(deps.hash_secret, run_id, email, email_ok, phone)
    duplicate_of, same_company = deps.store.link_lead(run_id, lead.lead_id, keys, company_key(lead.company))
    if duplicate_of:
        flags.append("possible_duplicate")

    view = build_llm_view(name=lead.name, company=lead.company, job_title=lead.job_title, city=lead.city,
                          country=lead.country, source=lead.source, message=lead.message)
    flags += view.flags

    if not lead.message.strip():
        outcome = Outcome("quarantined", reason="empty_message")      # nothing to qualify: don't pay a model to guess
    else:
        policy = RetryPolicy(models=s.models, attempts_per_model=s.llm_attempts_per_model,
                             max_attempts_per_lead=s.llm_max_attempts_per_lead, transport_retries=s.llm_transport_retries,
                             backoff_s=s.llm_backoff_s, use_structured_output=s.llm_structured_output)
        async with deps.semaphore:
            try:
                outcome = await qualify(
                    system_prompt(s.business_name, s.business_profile, output_schema()), user_prompt(view),
                    policy=policy,
                    chat=lambda model, msgs, schema: deps.llm.chat(model, msgs, response_format() if schema else None),
                    validate=validate_llm_output,
                    breaker=deps.breaker,
                    schema_unsupported=deps.schema_unsupported,
                    egress_check=lambda text: egress_violations(text, email=lead.email, phone=lead.phone, name=lead.name),
                    budget_exceeded=lambda: deps.store.spent_usd(run_id) >= s.run_budget_usd,
                )
            except FatalLLMError as e:
                deps.store.record(run_id, lead.lead_id, {"lead_id": lead.lead_id, "status": "quarantined", "reason": "provider_account_error"}, e.calls)
                raise

    calls = [CallLog(**c) for c in outcome.calls if c.get("outcome") != "skipped_circuit_open"]
    unknown = any(c.cost_usd is None and c.outcome in ("valid", "invalid_output") for c in calls)
    usage = Usage(
        llm_calls=len(calls),
        prompt_tokens=sum(c.prompt_tokens for c in calls),
        completion_tokens=sum(c.completion_tokens for c in calls),
        cost_usd=None if unknown else round(sum(c.cost_usd or 0 for c in calls), 6),
        wasted_cost_usd=round(sum(c.cost_usd or 0 for c in calls if c.outcome != "valid"), 6),
        calls=calls,
    )
    if outcome.status == "qualified" and any(c.outcome == "valid" and c.model_requested != s.models[0] for c in calls):
        flags.append("fallback_model_used")

    qualification, hint = None, None
    if outcome.status == "qualified":
        v = outcome.value
        body = "" if v.intent == "spam" else v.draft_reply
        draft = "" if v.intent == "spam" else f"{greeting(lead.name)}\n\n{body}\n\n{s.reply_sign_off}"
        qualification = Qualification(intent=v.intent, lead_score=v.lead_score, score_reason=v.score_reason, draft_reply=draft)
        hint = _action_hint(v.intent, v.lead_score, flags, duplicate_of, same_company)

    response = QualifyResponse(
        run_id=run_id, lead_id=lead.lead_id, status=outcome.status, qualification=qualification,
        quarantine_reason=outcome.reason if outcome.status == "quarantined" else None, errors=outcome.errors,
        action_hint=hint, flags=sorted(set(flags)), duplicate_of=duplicate_of, same_company_as=same_company,
        model_used=outcome.final_model, usage=usage,
        privacy={"sent_to_llm": SENT_TO_LLM, "never_sent": ["name", "email", "phone", "any other field"], "redactions": view.redactions},
    )

    # Persist WITHOUT personal data: no name, email, phone, greeting or original message.
    public = {
        "lead_id": lead.lead_id, "status": outcome.status, "reason": response.quarantine_reason, "errors": outcome.errors,
        "intent": qualification.intent if qualification else None, "lead_score": qualification.lead_score if qualification else None,
        "score_reason": qualification.score_reason if qualification else None,
        "draft_body": outcome.value.draft_reply if qualification and qualification.intent != "spam" else "",
        "action_hint": hint, "flags": response.flags, "duplicate_of": duplicate_of, "same_company_as": same_company,
        "final_model": outcome.final_model, "llm_calls": usage.llm_calls, "tokens": usage.prompt_tokens + usage.completion_tokens,
        "cost_usd": usage.cost_usd,
    }
    deps.store.record(run_id, lead.lead_id, public, outcome.calls)
    log.info(json.dumps({"event": "lead_processed", "run_id": run_id, "lead_id": lead.lead_id, "status": outcome.status,
                         "reason": response.quarantine_reason, "model": outcome.final_model, "llm_calls": usage.llm_calls,
                         "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens, "cost_usd": usage.cost_usd}))
    return response
