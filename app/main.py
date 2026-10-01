"""FastAPI app. POST one lead (one CSV row) → qualified or quarantined. Nothing is ever sent to the lead."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app.config import get_settings
from app.llm import LLMClient
from app.qualifier import FatalLLMError
from app.schemas import LeadIn, QualifyResponse
from app.service import Deps, process_lead

logging.basicConfig(level=logging.INFO, format="%(message)s")  # one JSON line per lead, no personal data


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    llm = LLMClient(s, transport=getattr(app.state, "llm_transport", None))  # tests set app.state.llm_transport
    app.state.deps = Deps.build(s, llm)
    yield
    await llm.aclose()


app = FastAPI(title="Lead Qualifier API", version="0.1.0", lifespan=lifespan,
              description="Qualifies ONE inbound lead per request with an LLM. Drafts only: nothing is sent to leads.")


def get_deps(request: Request) -> Deps:
    return request.app.state.deps


def require_api_key(x_api_key: str | None = Header(default=None), deps: Deps = Depends(get_deps)) -> None:
    expected = deps.settings.leads_api_token.get_secret_value()
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid x-api-key header.")


@app.post("/v1/leads/qualify", response_model=QualifyResponse, dependencies=[Depends(require_api_key)])
async def qualify_lead(
    lead: LeadIn,
    x_run_id: str | None = Header(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$", description="Groups leads into a run for the cost summary."),
    deps: Deps = Depends(get_deps),
) -> QualifyResponse:
    run_id = x_run_id or f"adhoc-{date.today().isoformat()}"
    try:
        return await process_lead(lead, run_id, deps)
    except FatalLLMError as e:
        # Bad key or no credit: fail loudly instead of quarantining every lead one by one.
        raise HTTPException(status_code=502, detail=f"LLM provider rejected the request: {e}") from e


@app.get("/v1/runs/{run_id}/summary", dependencies=[Depends(require_api_key)])
def run_summary(run_id: str, deps: Deps = Depends(get_deps)) -> dict:
    return deps.store.summary(run_id, deps.settings.monthly_lead_volume)


@app.get("/v1/runs/{run_id}/results", dependencies=[Depends(require_api_key)])
def run_results(run_id: str, deps: Deps = Depends(get_deps)) -> list[dict]:
    """Stored results contain no personal data; join contact details from your own copy of the lead."""
    return deps.store.results(run_id)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
