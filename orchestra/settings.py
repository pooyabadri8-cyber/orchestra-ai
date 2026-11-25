"""Runtime settings, read from the environment or a .env file (prefix ORCHESTRA_)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCHESTRA_", env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

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
    llm_max_calls_per_run: int = 40  # manager + writer (retries, a polishing round, revisions) + two reviewers each

    # data sources: a different address for the market page (a mirror, or a local copy for testing)
    bourse_trader_url: str = ""
    tgju_url: str = ""  # a different address for tgju's ajax.json (mirror or local test server)
    tse_old_base: str = "https://old.tsetmc.com"
    tse_cdn_base: str = "https://cdn.tsetmc.com"
    # foreign sources (Telegram channels, foreign media) through the VPN's local proxy, e.g. http://127.0.0.1:10809 or
    # socks5://127.0.0.1:10808. Empty: the proxy Windows advertises (System Proxy), if any.
    foreign_proxy: str = ""

    # publishing
    max_publish_attempts: int = 4
    retry_delays_seconds: list[int] = [30, 120, 600]
    send_timeout_seconds: float = 45.0
    # Instagram fetches images from a public URL: a folder your web server publishes, and that folder's address
    public_asset_dir: Path | None = None
    public_asset_url: str = ""

    # other Bot API addresses (a local Bot API server, a relay, or a test server); empty = the official ones
    telegram_api_base: str = ""
    bale_api_base: str = ""

    # control bot: the owner gives requests and approves from their phone
    control_platform: str = "telegram"  # telegram | bale
    control_bot_token: str = ""
    control_chat_ids: str = ""  # numeric ids of the people allowed to command and approve, comma-separated
    control_poll_seconds: int = 5

    # model tiers: empty means "use llm_model"
    manager_model: str = ""
    reviewer_model: str = ""


def load_settings() -> Settings:
    return Settings()
