"""End-to-end through the real FastAPI app; only the LLM's HTTP endpoint is faked."""
from __future__ import annotations

import json
import re

from tests.fake_llm import ScriptedLLM, err

RUN = {"X-Run-Id": "run-test"}


def post_all(client, leads):
    return {l["lead_id"]: client.post("/v1/leads/qualify", json=l, headers=RUN).json() for l in leads}


def test_one_row_in_one_result_out(make_client, leads):
    client, _ = make_client()
    r = client.post("/v1/leads/qualify", json=leads[0], headers=RUN)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "qualified" and body["lead_id"] == "L001"
    assert body["qualification"]["draft_reply"].startswith("Hi Ananya,\n\n")
    assert body["usage"]["llm_calls"] == 1 and body["usage"]["cost_usd"] == 0.0


def test_privacy_no_email_phone_or_name_in_any_llm_request(make_client, leads):
    client, fake = make_client()
    post_all(client, leads)
    sent = "\n".join(json.dumps(b["messages"]) for b in fake.requests)
    digits = re.sub(r"\D", "", "".join(m["content"] for b in fake.requests for m in b["messages"] if m["role"] != "system"))
    for l in leads:
        if l["email"]:
            assert l["email"].lower() not in sent.lower(), l["lead_id"]
        d = re.sub(r"\D", "", l["phone"])[-10:]
        if len(d) >= 8 and set(d) != {"0"}:
            assert d not in digits, l["lead_id"]
        if re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", l["name"]):
            assert l["name"] not in sent, l["lead_id"]
    assert "98400 12345" not in sent and "siddiq.personal" not in sent      # typed inside L018's message
    assert "Ananya" not in sent                                            # "my colleague Ananya" (L026)
    assert "98765 43210" not in sent                                       # invented by a model, not echoed in repair


def test_nothing_personal_is_stored(make_client, leads):
    client, _ = make_client()
    post_all(client, leads)
    stored = json.dumps(client.get("/v1/runs/run-test/results").json())
    for l in leads:
        if l["email"]:
            assert l["email"].lower() not in stored.lower()
        if re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", l["name"]):
            assert l["name"] not in stored


def test_retries_fallback_and_quarantine(make_client, leads):
    client, _ = make_client()
    res = post_all(client, leads)
    outcomes = lambda lid: [c["outcome"] for c in res[lid]["usage"]["calls"]]  # noqa: E731
    assert outcomes("L001") == ["valid"]                                   # ```json fences repaired, no retry
    assert outcomes("L002") == ["invalid_output", "valid"]                 # truncated JSON → repair turn
    assert res["L003"]["model_used"] == "m2" and "fallback_model_used" in res["L003"]["flags"]   # 503 × 3 → fallback
    assert [c["http_status"] for c in res["L024"]["usage"]["calls"]] == [429, 200]               # Retry-After honoured
    q = res["L038"]                                                        # never valid
    assert q["status"] == "quarantined" and q["quarantine_reason"] == "invalid_output_after_retries"
    assert q["qualification"] is None and q["usage"]["llm_calls"] <= 5
    assert any("intent" in e for e in q["errors"])


def test_prompt_injection_output_never_reaches_review(make_client, leads):
    client, _ = make_client()
    r = post_all(client, leads)["L016"]
    assert r["status"] == "qualified" and r["qualification"]["lead_score"] <= 3
    assert "discount" not in r["qualification"]["draft_reply"].lower()
    assert "possible_prompt_injection" in r["flags"]


def test_sales_details_duplicates_colleagues_spam_unsubscribe(make_client, leads):
    client, _ = make_client()
    r = post_all(client, leads)
    assert r["L010"]["duplicate_of"] == "L001"
    assert r["L033"]["duplicate_of"] == "L003"                             # uppercase email, other phone format
    assert r["L026"]["same_company_as"] == ["L001", "L010"]
    assert r["L030"]["qualification"]["draft_reply"] == ""
    assert r["L019"]["action_hint"].startswith("DO NOT CONTACT")
    assert "invalid_email" in r["L012"]["flags"]
    assert r["L006"]["qualification"]["draft_reply"].startswith("Dear Dr. Iyer,")


def test_run_summary_totals(make_client, leads):
    client, _ = make_client()
    res = post_all(client, leads)
    s = client.get("/v1/runs/run-test/summary").json()
    assert s["leads"]["total"] == 40
    assert s["cost"]["llm_calls"] == sum(r["usage"]["llm_calls"] for r in res.values())
    assert abs(s["cost"]["total_cost_usd"] - sum(r["usage"]["cost_usd"] or 0 for r in res.values())) < 1e-9
    assert s["cost"]["total_cost_usd"] > 0 and s["cost"]["wasted_cost_usd"] > 0
    assert s["cost"]["projection"]["tokens_per_month"] > 0


def test_auth_input_and_fatal_errors(make_client, leads):
    client, _ = make_client()
    assert client.post("/v1/leads/qualify", json=leads[0], headers={"x-api-key": "wrong"}).status_code == 401
    assert client.post("/v1/leads/qualify", json={"name": "No id"}).status_code == 422
    empty = client.post("/v1/leads/qualify", json={"lead_id": "E1", "message": ""}).json()
    assert empty["quarantine_reason"] == "empty_message" and empty["usage"]["llm_calls"] == 0

    bad_key, fake = make_client(llm=lambda body: err(401, "No auth credentials found"))
    r = bad_key.post("/v1/leads/qualify", json=leads[0])
    assert r.status_code == 502


def test_run_budget_stops_spending(make_client, leads):
    expensive = ScriptedLLM()
    client, _ = make_client(llm=expensive, LLM_MODELS="m2", RUN_BUDGET_USD="0.001")
    res = post_all(client, leads[:10])
    assert any(r.get("quarantine_reason") == "run_budget_reached" for r in res.values())
    assert client.get("/v1/runs/run-test/summary").json()["cost"]["total_cost_usd"] <= 0.002
