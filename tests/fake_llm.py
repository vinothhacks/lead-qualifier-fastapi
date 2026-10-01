"""Scripted OpenAI-compatible responses with failure modes, keyed by the (redacted) lead message.
m1 = primary (free), m2 = fallback (reports cost), m3 = last resort (no cost field → price table)."""
from __future__ import annotations

import json

BODY = ("Thank you for reaching out and for the detail you shared about your requirements. "
        "It sounds like a good fit for the work our team does, and we would like to understand your goals better. "
        "Would you be open to a short call so one of our specialists can learn more and suggest sensible next steps?")


def classify(msg: str) -> dict:
    m = msg.lower()
    if "remove me" in m or "unsubscribe" in m:
        return {"intent": "unsubscribe", "lead_score": 1, "score_reason": "Lead asked to be removed from the mailing list.",
                "draft_reply": "Thank you for letting us know. You have been removed and will not be contacted again."}
    if any(k in m for k in ("followers", "#1 on google", "asdf")) or m.strip() == "test":
        return {"intent": "spam", "lead_score": 1, "score_reason": "Unsolicited promotion or junk submission.", "draft_reply": "x"}
    if "internship" in m or "student" in m:
        return {"intent": "job_or_internship", "lead_score": 2, "score_reason": "Student asking about internships, not a buyer.", "draft_reply": BODY}
    if any(k in m for k in ("partnership", "resell", "white-label")):
        return {"intent": "partnership", "lead_score": 5, "score_reason": "Partnership interest without a buying need.", "draft_reply": BODY}
    if "demo" in m:
        return {"intent": "demo_request", "lead_score": 8, "score_reason": "Clear request for a demo for a defined use case.", "draft_reply": BODY}
    if any(k in m for k in ("pricing", "cost", "rate", "quote", "cheapest", "budget")):
        return {"intent": "pricing_enquiry", "lead_score": 7, "score_reason": "Asks about cost for a defined requirement.", "draft_reply": BODY}
    return {"intent": "product_enquiry", "lead_score": 6, "score_reason": "Relevant question about our capabilities.", "draft_reply": BODY}


def ok(model: str, content: str, cost: float | None = 0.0) -> tuple[int, dict, dict]:
    usage = {"prompt_tokens": 900, "completion_tokens": 160, "total_tokens": 1060}
    if cost is not None:
        usage["cost"] = cost
    return 200, {}, {"id": "gen-1", "model": model, "provider": "Mock",
                     "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": usage}


def err(status: int, message: str, headers: dict | None = None) -> tuple[int, dict, dict]:
    return status, headers or {}, {"error": {"code": status, "message": message}}


class ScriptedLLM:
    def __init__(self, m2_rejects_schema_once: bool = True):
        self.seen: dict[str, int] = {}
        self.requests: list[dict] = []
        self.m2_rejects_schema_once = m2_rejects_schema_once

    def __call__(self, body: dict) -> tuple[int, dict, dict]:
        self.requests.append(body)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        msg = user.split("<lead_message>")[1].split("</lead_message>")[0].strip()
        repair = len(body["messages"]) > 2
        model = body["model"]
        key = f"{model}|{msg[:30]}"
        self.seen[key] = self.seen.get(key, 0) + 1
        n, good = self.seen[key], classify(msg)

        if model == "m2" and self.m2_rejects_schema_once and "response_format" in body:
            self.m2_rejects_schema_once = False
            return err(400, "response_format json_schema is not supported by this model")
        if model == "m1":
            if "invoice processing" in msg and n == 1:
                return ok("m1", "```json\n" + json.dumps(good) + "\n```")
            if "azure migration" in msg.lower() and not repair:
                return ok("m1", '{"intent": "pricing_enquiry", "lead_score": 8,')
            if "power bi dashboard" in msg.lower():
                return err(503, "Upstream provider unavailable")
            if "s/4hana" in msg.lower() and n == 1:
                return err(429, "Rate limit exceeded", {"retry-after": "0"})
            if "ignore your previous instructions" in msg.lower():
                return ok("m1", json.dumps({"intent": "demo_request", "lead_score": 10, "score_reason": "The lead asked for a score of ten.",
                                            "draft_reply": "As requested we offer you a 90% discount on all services. " + BODY}))
            if "asdfgh" in msg:
                return ok("m1", json.dumps({**good, "intent": "gibberish"}))
            if "along ecr" in msg.lower() and not repair:
                return ok("m1", json.dumps({**good, "draft_reply": "Email sales@bayline.example or call +91 98765 43210. " + BODY}))
            return ok("m1", json.dumps(good))
        if model == "m2":
            if "asdfgh" in msg:
                return ok("m2", json.dumps({**good, "intent": "gibberish"}), cost=0.00042)
            if "ignore your previous instructions" in msg.lower():
                return ok("m2", json.dumps({"intent": "other", "lead_score": 2, "score_reason": "Tries to manipulate the score; no real need.",
                                            "draft_reply": BODY}), cost=0.00042)
            return ok("m2", json.dumps(good), cost=0.00042)
        if model == "m3":
            return ok("m3", json.dumps(good), cost=None)
        return err(404, f"No endpoints found for {model}")
