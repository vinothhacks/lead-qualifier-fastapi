#!/usr/bin/env python3
"""Send a leads CSV to the API ONE ROW PER REQUEST, then write the salesperson's files.

    python scripts/run_batch.py --csv data/leads.csv
    → samples/<run_id>/review.csv, quarantine.csv, run_summary.json, per_lead_log.jsonl

Contact details never round-trip through the server's storage: the server returns the result,
and this script joins it with the row it already holds.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REVIEW_COLS = ["review_decision", "lead_id", "lead_score", "intent", "score_reason", "action_hint", "name", "email", "phone",
               "company", "job_title", "city", "source", "message", "draft_reply", "flags", "duplicate_of", "same_company_as",
               "model_used", "llm_calls", "tokens", "cost_usd"]
QUARANTINE_COLS = ["lead_id", "name", "company", "reason", "errors", "llm_calls", "cost_usd", "flags", "message"]


def excel_safe(v: object) -> str:
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not s.replace("+", "").replace(" ", "").replace("-", "").isdigit():
        s = "'" + s   # a lead's message must never run as a spreadsheet formula
    return s


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel shows ₹ and Tamil correctly
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: excel_safe(r.get(k)) for k in cols})


async def post_row(client: httpx.AsyncClient, row: dict, run_id: str, stop: asyncio.Event) -> dict:
    for attempt in range(4):
        if stop.is_set():
            return {"lead_id": row.get("lead_id"), "status": "not_sent", "quarantine_reason": "run_stopped"}
        try:
            r = await client.post("/v1/leads/qualify", json=row, headers={"X-Run-Id": run_id})
        except httpx.HTTPError as e:
            err = f"network: {e.__class__.__name__}"
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 422):
                return {"lead_id": row.get("lead_id"), "status": "rejected", "quarantine_reason": f"http_{r.status_code}", "errors": [r.text[:300]]}
            if r.status_code == 502:   # provider account problem (bad key, no credit): stop the whole run
                stop.set()
                return {"lead_id": row.get("lead_id"), "status": "not_sent", "quarantine_reason": "provider_rejected", "errors": [r.text[:300]]}
            err = f"http_{r.status_code}"
        await asyncio.sleep(2 ** attempt)
    return {"lead_id": row.get("lead_id"), "status": "not_sent", "quarantine_reason": "api_unreachable", "errors": [err]}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "data" / "leads.csv"))
    ap.add_argument("--url", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--out", default=str(ROOT / "samples"))
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()
    token = os.environ.get("LEADS_API_TOKEN")
    if not token:
        print("Set LEADS_API_TOKEN (same value as the server).", file=sys.stderr)
        return 2

    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"{run_id}: {len(rows)} leads → {args.url} (one row per request, {args.concurrency} at a time)")

    sem, stop = asyncio.Semaphore(args.concurrency), asyncio.Event()
    async with httpx.AsyncClient(base_url=args.url, headers={"x-api-key": token}, timeout=300) as client:
        async def one(row: dict) -> dict:
            async with sem:
                res = await post_row(client, row, run_id, stop)
                print(f"  {row.get('lead_id')}: {res.get('status')} {res.get('quarantine_reason') or ''}")
                return res
        results = await asyncio.gather(*(one(r) for r in rows))
        summary = (await client.get(f"/v1/runs/{run_id}/summary")).json()

    review, quarantine = [], []
    for row, res in zip(rows, results):
        usage = res.get("usage") or {}
        common = {**row, "flags": "; ".join(res.get("flags", [])), "llm_calls": usage.get("llm_calls", 0),
                  "cost_usd": usage.get("cost_usd"), "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)}
        if res.get("status") == "qualified":
            q = res["qualification"]
            review.append({**common, **q, "action_hint": res.get("action_hint"), "duplicate_of": res.get("duplicate_of") or "",
                           "same_company_as": "; ".join(res.get("same_company_as", [])), "model_used": res.get("model_used")})
        else:
            quarantine.append({**common, "reason": res.get("quarantine_reason"), "errors": " | ".join(res.get("errors", []))})
    review.sort(key=lambda r: (-int(r["lead_score"]), r["lead_id"]))

    out = Path(args.out) / run_id
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "review.csv", REVIEW_COLS, review)
    write_csv(out / "quarantine.csv", QUARANTINE_COLS, quarantine)
    (out / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (out / "per_lead_log.jsonl").open("w", encoding="utf-8") as f:   # no contact details in the log
        for res in results:
            f.write(json.dumps({k: res.get(k) for k in ("lead_id", "status", "quarantine_reason", "errors", "model_used", "flags", "usage")}) + "\n")

    c = summary.get("cost", {})
    print(f"\n{len(review)} ready for review, {len(quarantine)} quarantined → {out}/")
    print(f"{c.get('llm_calls')} LLM calls, {c.get('total_tokens')} tokens, ${c.get('total_cost_usd')} "
          f"(${c.get('wasted_cost_usd')} on unused answers); {c.get('calls_with_unknown_cost')} calls with unknown cost")
    for m, v in (c.get("by_model") or {}).items():
        print(f"  {m:50s} {v['calls']:3d} calls {v['valid']:3d} valid {v['invalid_output']:3d} invalid {v['errors']:3d} errors  ${v['cost_usd']}")
    p = c.get("projection", {})
    print(f"projection: {p.get('leads_per_month')} leads/month ≈ {p.get('tokens_per_month')} tokens ≈ ${p.get('cost_per_month_usd_at_this_runs_rates')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
