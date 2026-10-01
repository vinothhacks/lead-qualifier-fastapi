"""Run store on SQLite (standard library). Holds NO names, emails or phone numbers:
results keep the draft body (no greeting), and duplicates are found via keyed hashes."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
  run_id TEXT, lead_id TEXT, status TEXT, reason TEXT, intent TEXT, lead_score INTEGER,
  final_model TEXT, payload TEXT, updated_at TEXT, PRIMARY KEY (run_id, lead_id));
CREATE TABLE IF NOT EXISTS calls (
  run_id TEXT, lead_id TEXT, attempt INTEGER, model_requested TEXT, model_used TEXT, outcome TEXT,
  http_status INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, cost_source TEXT,
  latency_ms INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS contacts (run_id TEXT, key TEXT, lead_id TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS companies (run_id TEXT, company_key TEXT, lead_id TEXT, root_id TEXT, PRIMARY KEY (run_id, lead_id));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()   # one writer at a time; calls are fast, so we don't need a pool

    def link_lead(self, run_id: str, lead_id: str, contact_keys: list[str], company_key: str | None) -> tuple[str | None, list[str]]:
        """Atomically: is this the same person as an earlier lead (duplicate_of), and which
        other people from the same company are in this run (same_company_as)?"""
        with self._lock:
            duplicate_of = None
            if contact_keys:
                q = f"SELECT lead_id FROM contacts WHERE run_id=? AND lead_id<>? AND key IN ({','.join('?' * len(contact_keys))}) ORDER BY rowid LIMIT 1"
                row = self._db.execute(q, (run_id, lead_id, *contact_keys)).fetchone()
                duplicate_of = row[0] if row else None
                self._db.executemany("INSERT OR IGNORE INTO contacts VALUES (?,?,?)", [(run_id, k, lead_id) for k in contact_keys])
            same_company: list[str] = []
            if company_key:
                root = duplicate_of or lead_id
                self._db.execute("INSERT OR REPLACE INTO companies VALUES (?,?,?,?)", (run_id, company_key, lead_id, root))
                same_company = [r[0] for r in self._db.execute(
                    "SELECT lead_id FROM companies WHERE run_id=? AND company_key=? AND root_id<>? ORDER BY rowid", (run_id, company_key, root))]
            return duplicate_of, same_company

    def spent_usd(self, run_id: str) -> float:
        row = self._db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE run_id=?", (run_id,)).fetchone()
        return float(row[0])

    def record(self, run_id: str, lead_id: str, public: dict, calls: list[dict]) -> None:
        """`public` must not contain personal data (enforced by the caller's response model split)."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._write(run_id, lead_id, public, calls)
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _write(self, run_id: str, lead_id: str, public: dict, calls: list[dict]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, lead_id, public["status"], public.get("reason"), public.get("intent"), public.get("lead_score"),
             public.get("final_model"), json.dumps(public), _now()))
        # Calls are appended, never replaced: re-submitting a lead still costs money.
        self._db.executemany(
            "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, lead_id, c.get("attempt"), c.get("model_requested"), c.get("model_used"), c.get("outcome"),
              c.get("http_status"), c.get("prompt_tokens", 0), c.get("completion_tokens", 0), c.get("cost_usd"),
              c.get("cost_source"), c.get("latency_ms"), _now())
             for c in calls if c.get("outcome") != "skipped_circuit_open"])

    def results(self, run_id: str) -> list[dict]:
        rows = self._db.execute("SELECT payload FROM results WHERE run_id=? ORDER BY lead_score DESC, lead_id", (run_id,))
        return [json.loads(r[0]) for r in rows]

    def summary(self, run_id: str, monthly_volume: int) -> dict:
        db = self._db
        leads = dict(db.execute("SELECT status, COUNT(*) FROM results WHERE run_id=? GROUP BY status", (run_id,)).fetchall())
        reasons = dict(db.execute("SELECT reason, COUNT(*) FROM results WHERE run_id=? AND status<>'qualified' GROUP BY reason", (run_id,)).fetchall())
        intents = dict(db.execute("SELECT intent, COUNT(*) FROM results WHERE run_id=? AND status='qualified' GROUP BY intent", (run_id,)).fetchall())
        t = db.execute("""SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cost_usd),0),
                          COALESCE(SUM(CASE WHEN outcome<>'valid' THEN cost_usd ELSE 0 END),0),
                          SUM(CASE WHEN cost_source='unknown' THEN 1 ELSE 0 END) FROM calls WHERE run_id=?""", (run_id,)).fetchone()
        by_model = {
            m: {"calls": n, "valid": v, "invalid_output": i, "errors": n - v - i, "prompt_tokens": pt, "completion_tokens": ct, "cost_usd": round(c, 6)}
            for m, n, v, i, pt, ct, c in db.execute(
                """SELECT COALESCE(model_used, model_requested), COUNT(*), SUM(outcome='valid'), SUM(outcome='invalid_output'),
                          SUM(prompt_tokens), SUM(completion_tokens), COALESCE(SUM(cost_usd),0)
                   FROM calls WHERE run_id=? GROUP BY 1 ORDER BY 2 DESC""", (run_id,))}
        sent = db.execute("SELECT COUNT(DISTINCT lead_id) FROM calls WHERE run_id=?", (run_id,)).fetchone()[0] or 0
        calls, pt, ct, cost, wasted, unknown = t
        per = lambda x: (x / sent) if sent else 0.0  # noqa: E731
        return {
            "run_id": run_id,
            "leads": {"total": sum(leads.values()), **leads, "not_qualified_by_reason": reasons, "qualified_by_intent": intents},
            "cost": {
                "llm_calls": calls, "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                "total_cost_usd": round(cost, 6), "wasted_cost_usd": round(wasted, 6),
                "calls_with_unknown_cost": unknown or 0,
                "avg_cost_per_lead_usd": round(per(cost), 6), "avg_calls_per_lead": round(per(calls), 2),
                "avg_tokens_per_lead": round(per(pt + ct)),
                "by_model": by_model,
                "projection": {
                    "leads_per_month": monthly_volume,
                    "calls_per_month": round(per(calls) * monthly_volume),
                    "tokens_per_month": round(per(pt + ct) * monthly_volume),
                    "cost_per_month_usd_at_this_runs_rates": round(per(cost) * monthly_volume, 2),
                },
            },
        }
