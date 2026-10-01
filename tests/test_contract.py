"""The output contract: one pydantic model = JSON Schema for the model + validator for its answer."""
import json

from app.contract import output_schema, validate_llm_output

GOOD = {"intent": "demo_request", "lead_score": 8, "score_reason": "Clear demo request from an operations head.",
        "draft_reply": "Thank you for getting in touch about automating invoice processing. We would be glad to show you how "
                       "teams like yours handle this today. Would a short call next week work so a specialist can understand your setup?"}


def test_accepts_valid_and_repairs_cosmetics():
    v = validate_llm_output("```json\n" + json.dumps({**GOOD, "intent": "Demo Request", "lead_score": "8", "extra": 1,
                                                       "draft_reply": "Hi there,\n" + GOOD["draft_reply"] + "\n\nBest regards,\nSam"}) + "\n```")
    assert v.ok, v.errors
    assert v.value.intent == "demo_request" and v.value.lead_score == 8
    assert not v.value.draft_reply.lower().startswith("hi") and "regards" not in v.value.draft_reply.lower()


def test_rejects_meaning_changing_problems():
    cases = {
        "bad enum": {**GOOD, "intent": "gibberish"},
        "score range": {**GOOD, "lead_score": 11},
        "discount": {**GOOD, "draft_reply": "We can offer a 90% discount. " + GOOD["draft_reply"]},
        "contact": {**GOOD, "draft_reply": "Call +91 98765 43210. " + GOOD["draft_reply"]},
        "placeholder": {**GOOD, "draft_reply": "Thanks [NAME]. " + GOOD["draft_reply"]},
        "spam scored high": {**GOOD, "intent": "spam", "lead_score": 9},
        "too short": {**GOOD, "draft_reply": "Thanks, we will call."},
    }
    for name, payload in cases.items():
        assert not validate_llm_output(json.dumps(payload)).ok, name


def test_truncated_json_is_reported_as_such():
    v = validate_llm_output('{"intent": "pricing_enquiry", "lead_score": 8,')
    assert not v.ok and "truncated" in v.errors[0]


def test_spam_draft_is_blanked():
    v = validate_llm_output(json.dumps({**GOOD, "intent": "spam", "lead_score": 1, "draft_reply": "whatever"}))
    assert v.ok and v.value.draft_reply == ""


def test_schema_is_closed():
    s = output_schema()
    assert s["additionalProperties"] is False and set(s["required"]) == {"intent", "lead_score", "score_reason", "draft_reply"}
