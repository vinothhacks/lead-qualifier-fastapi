"""The LLM output contract. ONE pydantic model is both the JSON Schema sent to the model
(response_format) and the validator applied to its answer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.rules import INTENTS, business_rule_errors, clean_reason, extract_json_object, normalise_intent, strip_greeting_and_sign_off


class LeadQualification(BaseModel):
    model_config = ConfigDict(extra="ignore")   # extra keys are dropped, not a reason to retry

    intent: Literal[INTENTS]  # type: ignore[valid-type]
    lead_score: int = Field(ge=1, le=10)        # "8" is coerced to 8 (lax mode) – cosmetic
    score_reason: str = Field(min_length=10, max_length=200)
    draft_reply: str = Field(max_length=1200)

    # Cosmetic repairs run before type checks; meaning-changing problems are rejected below.
    @field_validator("intent", mode="before")
    @classmethod
    def _normalise_intent(cls, v: Any) -> Any:
        return normalise_intent(v)

    @field_validator("score_reason", mode="before")
    @classmethod
    def _one_line_reason(cls, v: Any) -> Any:
        return clean_reason(v)

    @field_validator("draft_reply", mode="before")
    @classmethod
    def _strip_greeting(cls, v: Any) -> Any:
        return strip_greeting_and_sign_off(v)

    @model_validator(mode="after")
    def _business_rules(self) -> "LeadQualification":
        if self.intent == "spam":
            self.draft_reply = ""                    # never draft replies to spam
        errors = business_rule_errors(self.intent, self.lead_score, self.draft_reply)
        if errors:
            raise ValueError("; ".join(errors))
        return self


@dataclass
class Validation:
    ok: bool
    value: LeadQualification | None = None
    errors: list[str] = field(default_factory=list)


def validate_llm_output(text: str | None) -> Validation:
    try:
        data: Any = extract_json_object(text)
    except ValueError as e:
        return Validation(False, errors=[str(e)])
    try:
        return Validation(True, value=LeadQualification.model_validate(data))
    except ValidationError as e:
        # err["msg"] never contains the input value, so nothing from the lead is echoed back
        return Validation(False, errors=[f"{'.'.join(map(str, err['loc'])) or 'output'}: {err['msg']}" for err in e.errors()])


def output_schema() -> dict:
    schema = LeadQualification.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def response_format() -> dict:
    return {"type": "json_schema", "json_schema": {"name": "lead_qualification", "strict": True, "schema": output_schema()}}
