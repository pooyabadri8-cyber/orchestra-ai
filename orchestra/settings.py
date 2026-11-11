"""Runtime settings, read from the environment or a .env file (prefix ORCHESTRA_)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCHESTRA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://orchestra:orchestra@localhost:5432/orchestra"
    asset_dir: Path = Path("assets_store")

    # publishing
    max_publish_attempts: int = 4
    retry_delays_seconds: list[int] = [30, 120, 600]
    send_timeout_seconds: float = 45.0


def load_settings() -> Settings:
    return Settings()
