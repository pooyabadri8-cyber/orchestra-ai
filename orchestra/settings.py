"""Runtime settings, read from the environment or a .env file (prefix ORCHESTRA_)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCHESTRA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://orchestra:orchestra@localhost:5432/orchestra"
    system_database_url: str = "postgresql://orchestra:orchestra@localhost:5432/orchestra_dbos"  # DBOS workflow state
    asset_dir: Path = Path("assets_store")
    orgs_dir: Path = Path("orgs")

    # language model (writer tier)
    llm_provider: str = "anthropic"  # anthropic | openai_compatible
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_fallback_model: str = ""  # optional second model on the same provider
    llm_max_calls_per_run: int = 20  # manager + writer (with retries) + two reviewers

    # publishing
    max_publish_attempts: int = 4
    retry_delays_seconds: list[int] = [30, 120, 600]
    send_timeout_seconds: float = 45.0
    # Instagram fetches images from a public URL: a folder your web server publishes, and that folder's address
    public_asset_dir: Path | None = None
    public_asset_url: str = ""

    # model tiers: empty means "use llm_model"
    manager_model: str = ""
    reviewer_model: str = ""


def load_settings() -> Settings:
    return Settings()
