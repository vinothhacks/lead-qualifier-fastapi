# Lead Qualifier — FastAPI (POC)

`POST /v1/leads/qualify` takes **one lead** (one CSV row) and asks an LLM (OpenRouter or any OpenAI-compatible API) for three things: the intent, a score from 1 to 10 with a reason, and a draft first reply. Every answer is validated, and the lead comes back either `qualified` or `quarantined`.

`scripts/run_batch.py` sends the CSV one row per request and writes the salesperson's review sheet. **Nothing is ever sent to a lead.** The data in `data/leads.csv` is synthetic: emails use the reserved `.example` domain and phone numbers are placeholders.

Public repository: https://github.com/vinothhacks/lead-qualifier-fastapi

No API key is required to check this repository. [GitHub Actions](https://github.com/vinothhacks/lead-qualifier-fastapi/actions) runs `pytest` on every push (`.github/workflows/test.yml`) and stores no secrets. The tests call the real API; only the LLM HTTP endpoint is a scripted fake. `.env` is gitignored, so a live OpenRouter key never enters the public repo.

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
pytest -q                                  # this is the public check; no key, no network

# Optional live run, with your own key kept only in .env:
# cp .env.example .env                     # OPENROUTER_API_KEY, LEADS_API_TOKEN, CONTACT_HASH_SECRET
# uvicorn app.main:app --port 8000         # docs at http://localhost:8000/docs
# python scripts/run_batch.py              # → samples/<run_id>/review.csv, quarantine.csv, run_summary.json
```

## Design decisions

**One lead per request.** Each row is independent, so one bad row can't fail a batch. The client can retry a single row, control concurrency, and stream leads in as they arrive. That is also what production needs (see below). Run-level totals come from grouping requests by an `X-Run-Id` header.

**The core has no framework code.** Personal-data handling (`pii.py`), business rules (`rules.py`), retry and fallback (`qualifier.py`) and the run store (`store.py`) are plain Python. FastAPI, pydantic and httpx sit only at the edges. That keeps the logic easy to read and to unit-test.

**Personal data: allowlist, minimise, verify, don't store.**
- Only company, job title, city, country, source and the scrubbed message reach the LLM. It's an allowlist, so new fields never leak by accident.
- Names are withheld too, and the greeting is added locally afterwards.
- The message is scrubbed of emails, links, phone- or ID-like numbers, the lead's own name, and names after cue words such as "my colleague Ananya". Owners' names are also removed from company names ("[NAME] Consulting").
- A fail-closed egress check runs on the exact text before every call, including repair turns. It also catches contact details a model invented, which would otherwise be echoed back in a repair turn.
- The server never stores personal data. Results are saved without the greeting, and duplicate detection uses HMAC hashes of the normalised email and phone. The client joins contact details back in from the row it already has.
- Tests assert that no email, phone or name from the CSV appears in any LLM request or in the database.

**Invalid output: one pydantic model is both the schema and the validator.** `LeadQualification.model_json_schema()` is sent as `response_format`. The same model validates the answer. If a model rejects `response_format`, that model is retried without it.
- **Cosmetic problems are repaired:** code fences, `"8"` instead of `8`, "Demo Request" instead of `demo_request`, greeting lines.
- **Meaning-changing problems are retried with the exact errors fed back:** a bad intent, an out-of-range score, or a broken business rule. The rules are: no prices, discounts or percentages; no contact details or links; no `[NAME]` placeholders; spam and unsubscribe must score 2 or less.
- The business rules matter most. An answer that obeys L016's "score 10, offer 90% discount" is valid JSON, and only the no-discounts rule catches it.
- Leads that still fail come back `quarantined` with their errors. They never get a `qualification`.

**Fallback lives in my code, not in OpenRouter's `models[]` option.** OpenRouter only fails over on errors, not on a 200 response with an unusable answer. My loop also falls back on invalid output, works with any OpenAI-compatible API, and logs every attempt.
- **Transient errors** (429, 5xx, timeouts) are retried with backoff, honouring `Retry-After`, without using up a validation attempt.
- **Account errors** (401, 402) return `502` immediately. The batch script then stops, rather than quarantining every lead.
- **A per-model circuit breaker** skips a model that keeps failing, for a cooldown period.
- **A shared rate limiter** keeps concurrent requests under the free tier's 20 requests per minute.

**Cost.** OpenRouter reports cost in every response, and I log it per call. A price table covers other providers. Rules for the numbers:
- Every call counts, including failed and repaired ones (`wasted_cost_usd`).
- When a call's cost is unknown, the total is reported as unknown, never as $0.
- `max_tokens` caps each call, and a per-run budget stops further spending once reached.
- `/v1/runs/{id}/summary` projects the run's cost to 8,000 leads a month.

**Choices made for the salesperson.**
- Duplicates are linked, not dropped (L033 → L003, despite the uppercase email and a different phone format).
- Colleagues at the same company are linked.
- An unsubscribe is marked DO NOT CONTACT, and spam gets no draft.
- The sheet is sorted by score, has an empty `review_decision` column, and is Excel-safe.

## Sample output

`samples/<run_id>/` holds one real run: `review.csv`, `quarantine.csv`, `run_summary.json` (the cost summary) and `per_lead_log.jsonl` (every call's model, tokens and cost).
<!-- TODO after your real run: paste the summary numbers here -->

## Before 8,000 leads a month

1. **Data protection.** Use a paid model with `LLM_DATA_COLLECTION=deny` or zero data retention, plus a data processing agreement. Set retention and access control for outputs (DPDP Act). Keep secrets in a secret manager.
2. **Measure before trusting the scores.** Have sales label 200–300 leads, and track intent accuracy and how often the score agrees with sales. Re-run that set on every prompt or model change.
3. **Go asynchronous.** Have the endpoint enqueue the lead and return `202` (Redis or SQS plus workers). Write drafts to the CRM as tasks. Move SQLite to Postgres, because the run store, rate limiter and circuit breaker are per-process today.
4. **Model cost isn't the constraint.** I estimate about 1,200 tokens per lead including retries, roughly 10M tokens a month. That's about $1.50 at $0.10/$0.40 per million tokens. The free tier can't carry this volume (50 or 1,000 requests a day, and models rotate). Pick the model for accuracy, and cache the fixed system prompt.
5. **Better PII detection, observability and deduplication.** Use named-entity detection (for example Presidio) for names. Add metrics and alerts on the invalid-output rate, fallback rate and cost per lead. Deduplicate against CRM history.

## Known limitations

- **Tested offline only.** Tests use a scripted fake LLM, so real-model quality is unverified until the sample run. The scoring rubric is mine and hasn't been calibrated with sales.
- **Third-party names are caught only after cue words** (one row per request means there are no other leads to compare against). Regex redaction also over-redacts long numbers.
- **State is per process:** the rate limiter, circuit breaker and SQLite store. Run a single worker for this POC.
- **Drafts are English only.** Webhook auth is a shared token.
- **Not done:** CRM integration, an evaluation set, a queue. <!-- TODO: screen recording -->

## Use of AI coding assistants

<!-- TODO: rewrite in your own words; you will be asked about the code. -->
I used Claude (Anthropic) to draft the code, tests and this README from my requirements. I reviewed and ran it, and I changed or decided myself: _[fill in]_.
