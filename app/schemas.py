"""API request/response models. The request is exactly ONE lead (one CSV row)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LeadIn(BaseModel):
    """One CSV row. Unknown columns are ignored (and never reach the LLM)."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, json_schema_extra={"examples": [{
        "lead_id": "L001", "name": "Ananya Raghavan", "email": "ananya.r@brightpath-logistics.example",
        "phone": "+91 90000 20001", "company": "BrightPath Logistics", "job_title": "Head of Operations",
        "area": "Ambattur", "city": "Chennai", "state": "Tamil Nadu", "country": "India", "source": "Website form",
        "message": "We're looking to automate our invoice processing. Can we schedule a demo next week?"}]})

    lead_id: str = Field(min_length=1, max_length=64)
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    job_title: str = ""
    area: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    source: str = ""
    message: str = Field(default="", max_length=5000)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_not_null(cls, v: Any) -> Any:
        if v is None:
            return ""
        return str(v) if isinstance(v, (int, float)) else v


class CallLog(BaseModel):
    attempt: int
    model_requested: str
    model_used: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    outcome: str | None = None
    http_status: int | None = None
    structured_output: bool | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    cost_source: str | None = None
    latency_ms: int | None = None
    errors: list[str] = []
    error: str | None = None


class Usage(BaseModel):
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None          # None = at least one call had unknown cost; never pretend it was $0
    wasted_cost_usd: float          # spent on calls whose answer was not used
    calls: list[CallLog]


class Qualification(BaseModel):
    intent: str
    lead_score: int
    score_reason: str
    draft_reply: str                # greeting + body + sign-off, ready for the salesperson to edit


class QualifyResponse(BaseModel):
    run_id: str
    lead_id: str
    status: Literal["qualified", "quarantined"]
    qualification: Qualification | None = None
    quarantine_reason: str | None = None
    errors: list[str] = []
    action_hint: str | None = None
    flags: list[str] = []
    duplicate_of: str | None = None
    same_company_as: list[str] = []
    model_used: str | None = None
    usage: Usage
    privacy: dict[str, Any]
