from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.fake_llm import ScriptedLLM

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token"


@pytest.fixture
def leads() -> list[dict]:
    with (ROOT / "data" / "leads.csv").open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Builds the real app with the LLM HTTP calls served by a scripted fake."""
    created = []

    def _make(llm=None, **env):
        base = {"LEADS_API_TOKEN": TOKEN, "OPENROUTER_API_KEY": "sk-test", "LLM_MODELS": "m1,m2,m3",
                "DATABASE_PATH": str(tmp_path / "runs.sqlite3"), "LLM_REQUESTS_PER_MINUTE": "600000",
                "LLM_BACKOFF_S": "0", "LLM_PRICES_JSON": json.dumps({"m3": {"prompt": 0.5, "completion": 1.5}})}
        for k, v in {**base, **env}.items():
            monkeypatch.setenv(k, v)
        from app.config import get_settings
        from app.main import app
        get_settings.cache_clear()
        fake = llm or ScriptedLLM()

        def handler(request: httpx.Request) -> httpx.Response:
            status, headers, body = fake(json.loads(request.content))
            return httpx.Response(status, headers=headers, json=body)

        app.state.llm_transport = httpx.MockTransport(handler)
        client = TestClient(app, headers={"x-api-key": TOKEN})
        client.__enter__()          # runs the lifespan (creates the LLM client with the mock transport)
        created.append(client)
        return client, fake

    yield _make
    for c in created:
        c.__exit__(None, None, None)
