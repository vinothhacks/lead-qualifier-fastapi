"""All configuration comes from environment variables (or .env). Secrets are never logged."""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Inbound auth: callers send this in the x-api-key header.
    leads_api_token: SecretStr
    # Keyed hashing of email/phone for duplicate detection. Empty = random per process.
    contact_hash_secret: SecretStr = SecretStr("")

    # Any OpenAI-compatible endpoint. OpenRouter-only fields are sent only to OpenRouter.
    llm_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: SecretStr = SecretStr("")
    llm_api_key: SecretStr = SecretStr("")          # takes precedence if set
    # Primary first, then fallbacks in order.
    llm_models: str = "z-ai/glm-5.3-flash,nex-agi/nex-n2.5-mini"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 700                         # hard cap on output spend per call
    llm_timeout_s: float = 60.0
    llm_structured_output: bool = True                # send response_format=json_schema (auto-off per model if rejected)
    llm_data_collection: str = "allow"                # OpenRouter: "deny" = only providers that don't store prompts
    llm_attempts_per_model: int = 2                   # validation attempts per model (1 + one repair turn)
    llm_max_attempts_per_lead: int = 4                # across all models: bounds worst-case cost per lead
    llm_transport_retries: int = 2                    # for 429/5xx/timeouts, per model
    llm_backoff_s: float = 2.0
    llm_requests_per_minute: int = 18                 # shared by all requests; OpenRouter free tier is 20/min
    llm_max_concurrency: int = 3
    llm_breaker_failures: int = 3
    llm_breaker_cooldown_s: float = 120.0
    llm_prices_json: str = "{}"                       # USD per 1M tokens, used only if the provider reports no cost
    run_budget_usd: float = 1.0                       # stop calling the LLM for a run once it has spent this

    business_name: str = "Bayline Digital"
    business_profile: str = ("an IT services company in Chennai offering cloud (Azure, Microsoft 365), data and analytics "
                             "(Power BI, Microsoft Fabric), AI and workflow automation, and business applications (ERP, CRM).")
    reply_sign_off: str = "Best regards,\n[Your name]\nBayline Digital"
    database_path: str = "data/runs.sqlite3"
    monthly_lead_volume: int = 8000

    @property
    def models(self) -> list[str]:
        return [m.strip() for m in self.llm_models.split(",") if m.strip()]

    @property
    def api_key(self) -> str:
        return (self.llm_api_key.get_secret_value() or self.openrouter_api_key.get_secret_value()).strip()

    @property
    def prices(self) -> dict[str, dict[str, float]]:
        try:
            return json.loads(self.llm_prices_json)
        except json.JSONDecodeError:
            return {}

    @property
    def is_openrouter(self) -> bool:
        return "openrouter.ai" in self.llm_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
