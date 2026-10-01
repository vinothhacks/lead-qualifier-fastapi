"""Personal-data handling. Pure Python, no dependencies.

Policy
1. Allowlist, not denylist: only company, job title, city, country, source and the scrubbed
   message are ever sent to the LLM. New input fields are never sent by accident.
2. Names are withheld too (data minimisation); the greeting is added locally afterwards.
3. The message is scrubbed: emails, links, phone/ID-like numbers, PAN, the lead's own name,
   and names that follow cues like "my colleague Ananya".
4. Egress check: if the lead's raw email/phone/name, or any email/phone-like pattern, is
   still in the outgoing text, the lead is NOT sent (fail closed).
5. Nothing personal is persisted: duplicate detection uses keyed hashes of email/phone.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[a-z]{2,}$", re.I)
_NOT_NAMES = {"dr", "mr", "mrs", "ms", "prof", "team", "expert", "seo", "buy", "cheap", "followers",
              "unknown", "test", "admin", "sales", "marketing", "support", "info", "the", "and", "for", "ltd", "corp"}

_PATTERNS: list[tuple[str, re.Pattern, str | None]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*"), "[EMAIL]"),
    ("url", re.compile(r"\b(?:https?://|www\.)\S+", re.I), "[URL]"),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[ID]"),
    ("number", re.compile(r"\+?\d[\d\s().-]{6,}\d"), None),  # ≥ 8 digits → phone, Aadhaar, card numbers
]
# "my colleague Ananya", "Mr Kumar", "manager Priya": a third-party name we cannot otherwise know about.
# The cue word is case-insensitive; the name itself must be capitalised.
_NAME_CUE = re.compile(r"\b((?i:colleague|manager|boss|friend|assistant|partner|director|mr|mrs|ms|dr|sir|madam))\.?\s+([A-Z][a-z]{2,})\b")
_INJECTION = [
    re.compile(r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|prompts?|rules)\b", re.I),
    re.compile(r"\b(system prompt|you are now|act as|jailbreak)\b", re.I),
    re.compile(r"\b(give|assign|set)\b.{0,30}\bscore\b", re.I),
]
_EGRESS = [re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), re.compile(r"\+?\d[\d\s().-]{8,}\d")]


@dataclass
class Phone:
    display: str
    e164: str
    valid: bool
    problem: str | None


def normalise_email(value: str) -> tuple[str, bool]:
    email = (value or "").strip().lower()
    return email, bool(EMAIL_RE.match(email))


def normalise_phone(value: str, default_country: str = "91") -> Phone:
    s = " ".join((value or "").split())
    if not s:
        return Phone("", "", False, "missing_phone")
    digits = re.sub(r"[^\d+]", "", s)
    plus = digits.startswith("+")
    digits = digits.replace("+", "")
    if not plus and len(digits) == 10 and digits[0] in "6789":
        digits = default_country + digits                     # Indian mobile without country code
    if not plus and len(digits) == 11 and digits.startswith("0"):
        digits = default_country + digits[1:]
    local = digits[len(default_country):] if digits.startswith(default_country) else digits
    if not 10 <= len(digits) <= 15:
        return Phone(s, "", False, "invalid_phone")
    if len(set(local)) == 1:
        return Phone(s, "", False, "placeholder_phone")       # +91 00000 00000
    return Phone(s, "+" + digits, True, None)


def name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s.,'-]+", name or "")
            if len(t) >= 3 and t.isalpha() and t.lower() not in _NOT_NAMES]


def scrub(text: str, own_name_tokens: list[str], counts: dict[str, int]) -> str:
    out = text or ""
    for kind, pattern, replacement in _PATTERNS:
        def _sub(m: re.Match, kind=kind, replacement=replacement) -> str:
            if replacement is None and len(re.sub(r"\D", "", m.group(0))) < 8:
                return m.group(0)
            counts[kind] = counts.get(kind, 0) + 1
            return replacement or "[NUMBER]"
        out = pattern.sub(_sub, out)

    def _cue(m: re.Match) -> str:
        if m.group(2).lower() in _NOT_NAMES:
            return m.group(0)
        counts["name"] = counts.get("name", 0) + 1
        return f"{m.group(1)} [NAME]"
    out = _NAME_CUE.sub(_cue, out)

    if own_name_tokens:
        own = re.compile(r"\b(" + "|".join(map(re.escape, sorted(own_name_tokens, key=len, reverse=True))) + r")\b", re.I)
        def _own(m: re.Match) -> str:
            counts["name"] = counts.get("name", 0) + 1
            return "[NAME]"
        out = own.sub(_own, out)
    return out


@dataclass
class LLMView:
    """The ONLY lead data the LLM ever sees."""
    company: str
    job_title: str
    city: str
    country: str
    source: str
    message: str
    redactions: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def build_llm_view(*, name: str, company: str, job_title: str, city: str, country: str, source: str, message: str) -> LLMView:
    counts: dict[str, int] = {}
    own = name_tokens(name)
    view = LLMView(
        company=scrub(company, own, counts),          # sole traders often use their own name as company name
        job_title=scrub(job_title, [], counts),
        city=city, country=country, source=source,
        message=scrub(message, own, counts),
    )
    view.redactions = counts
    if counts:
        view.flags.append("personal_data_redacted")
    if any(p.search(message or "") for p in _INJECTION):
        view.flags.append("possible_prompt_injection")
    return view


def egress_violations(outgoing: str, *, email: str, phone: str, name: str) -> list[str]:
    """Fail-closed check on the exact text about to be sent to the LLM."""
    found = []
    low = outgoing.lower()
    if email and email.lower() in low:
        found.append("email")
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) >= 8 and digits in re.sub(r"\D", "", outgoing):
        found.append("phone")
    if name and name_tokens(name) and name.lower() in low:
        found.append("name")
    if any(p.search(outgoing) for p in _EGRESS):
        found.append("contact_pattern")
    return found


def contact_keys(secret: bytes, run_id: str, email: str, email_valid: bool, phone: Phone) -> list[str]:
    """Keyed hashes for duplicate detection, so the run store never holds an email or phone."""
    raw = ([f"e:{email}"] if email_valid else []) + ([f"p:{phone.e164}"] if phone.valid else [])
    return [hmac.new(secret, f"{run_id}|{r}".encode(), hashlib.sha256).hexdigest() for r in raw]


def company_key(company: str) -> str | None:
    key = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|co|company|group)\b|[^a-z0-9]", "", (company or "").lower())
    return key if len(key) >= 3 else None


def greeting(name: str) -> str:
    n = (name or "").strip()
    if not n or re.fullmatch(r"unknown|n/a|test", n, re.I) or re.search(r"\b(team|followers|expert|admin|sales|support|info)\b", n, re.I):
        return "Hello,"
    parts = n.split()
    if re.fullmatch(r"dr\.?", parts[0], re.I) and len(parts) > 1:
        return f"Dear Dr. {parts[-1]},"
    return f"Hi {parts[0]},"
