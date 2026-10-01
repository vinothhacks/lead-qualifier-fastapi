"""Rules for the LLM's answer that a JSON Schema can't express. Pure Python, no dependencies.

Cosmetic problems are *repaired* (code fences, greeting lines, "Demo Request" → demo_request).
Anything that changes meaning is *rejected* so the caller retries with the error list.
"""
from __future__ import annotations

import json
import re

INTENTS = (
    "demo_request", "pricing_enquiry", "product_enquiry", "partnership",
    "job_or_internship", "unsubscribe", "spam", "other",
)

_GREETING_LINE = re.compile(r"^(hi|hello|dear|greetings)\b[^\n]{0,60}[,!]?\s*\n+", re.I)
_GREETING_INLINE = re.compile(r"^(hi|hello|dear)\b[^,\n]{0,40},\s*", re.I)
_SIGN_OFF = re.compile(r"\n+\s*(best|kind|warm)?\s*(regards|wishes)[\s\S]{0,120}$", re.I)
_TRAILING_PLACEHOLDER = re.compile(r"\n+\s*\[[^\]\n]{1,40}\]\s*$")

_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.\w+|\b(?:https?://|www\.)|\d[\d\s-]{7,}\d", re.I)
_COMMITMENT = re.compile(r"\b\d+\s?%|\bdiscount|\bguarantee|\bfree of (?:charge|cost)|(?:₹|\brs\.?|\binr|\$|\busd)\s?\d", re.I)
_PLACEHOLDER = re.compile(r"\[(NAME|EMAIL|URL|NUMBER|ID|PHONE)\]", re.I)


def normalise_intent(value: object) -> object:
    return re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else value


def clean_reason(value: object) -> object:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else value


def strip_greeting_and_sign_off(value: object) -> object:
    """The greeting and sign-off are added locally (the model never sees the name)."""
    if not isinstance(value, str):
        return value
    d = value.replace("\r", "").strip()
    d = _GREETING_LINE.sub("", d)
    d = _GREETING_INLINE.sub("", d)
    d = _SIGN_OFF.sub("", d)
    d = _TRAILING_PLACEHOLDER.sub("", d).strip()
    return d[:1].upper() + d[1:] if d else d


def extract_json_object(text: str | None) -> dict:
    """Accepts bare JSON or JSON wrapped in ``` fences / chatter. Raises ValueError otherwise."""
    if not text or not text.strip():
        raise ValueError("empty response content")
    s = re.sub(r"^```(?:json)?\s*|```\s*$", "", text.strip(), flags=re.I)
    start, end = s.find("{"), s.rfind("}")
    if start == -1:
        raise ValueError("response contains no JSON object")
    if end <= start:
        raise ValueError("invalid JSON: object is not closed (output truncated?)")
    try:
        obj = json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e.msg}") from e
    if not isinstance(obj, dict):
        raise ValueError("response JSON is not an object")
    return obj


def business_rule_errors(intent: str, lead_score: int, draft_reply: str) -> list[str]:
    errors: list[str] = []
    words = len(draft_reply.split())
    if intent in ("spam", "unsubscribe") and lead_score > 2:
        errors.append(f'intent "{intent}" must have lead_score 1 or 2')
    if intent != "spam":
        minimum = 8 if intent == "unsubscribe" else 25
        if words < minimum:
            errors.append(f'draft_reply must be at least {minimum} words for intent "{intent}"')
        if words > 180:
            errors.append("draft_reply must be at most 180 words")
    if _CONTACT.search(draft_reply):
        errors.append("draft_reply must not contain email addresses, links or phone numbers")
    if _COMMITMENT.search(draft_reply):
        errors.append("draft_reply must not mention prices, amounts, percentages, discounts or guarantees")
    if _PLACEHOLDER.search(draft_reply):
        errors.append("draft_reply must not contain redaction placeholders like [NAME]")
    return errors
