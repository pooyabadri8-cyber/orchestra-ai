"""Configuration loading: secrets from environment/.env, everything else from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import find_dotenv, load_dotenv

from .exclude import Exclusions
from .profile import Profile, load_profile

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass
class Config:
    bot_token: str
    api_key: str
    channel: str
    owner_ids: set[int]
    channel_name: str = ""  # the brand name the model writes as (profile.yaml organization.name unless config.yaml overrides it)
    profile: Profile = field(default_factory=Profile)  # the customer profile (voice, rules, features); see agent/profile.py
    platform: str = "telegram"  # "telegram" | "bale"
    provider: str = "anthropic"  # "anthropic" | "openai_compatible" (Gemini, OpenRouter, Groq, ...)
    base_url: str = ""
    model: str = DEFAULT_MODEL
    # optional second OpenAI-compatible provider, used automatically if the first one fails (quota, outage)
    fallback_api_key: str = ""
    fallback_base_url: str = ""
    fallback_model: str = ""
    models: list[str] = field(default_factory=list)  # all primary models, in order (first == model)
    fallback_models: list[str] = field(default_factory=list)
    timezone: str = "Asia/Tehran"
    post_times: list[str] = field(default_factory=lambda: ["10:30", "18:30"])
    max_chars: int = 1200  # normal posts
    source_max_chars: int = 2500  # posts rewritten from a source may be longer so nothing important is cut
    max_output_tokens: int = 6000  # reasoning models spend part of this on thinking; too small = cut-off posts
    pillars: list[dict] = field(default_factory=list)
    db_path: str = "data/agent.db"
    knowledge_dir: str = "knowledge"
    template_file: str = "post_template.yaml"
    profile_file: str = "profile.yaml"
    sources_file: str = "sources.yaml"
    source_poll_hours: float = 6  # 0 = no automatic polling (only /fetch)
    max_new_per_poll: int = 3  # drafts created per run, to keep review workload (and free-tier quota) small
    max_age_days: int = 3  # ignore source items older than this
    attribution: bool = True  # append "منبع: …" link to posts written from a source
    auto_publish: bool = False  # True = posts from sources are scheduled and published without owner approval
    approval: str = "auto"  # who may publish without the owner: "review" (nobody) | "trusted" (sources marked trusted) | "auto" (all); needs auto_publish
    max_publish_attempts: int = 4  # transient send errors are retried; after this many tries the post goes to the dead-letter queue
    retry_delays: list[int] = field(default_factory=lambda: [30, 120, 600])  # seconds after the 1st, 2nd, 3rd... failure
    auto_max_queue: int = 4  # in auto mode: at most this many scheduled posts waiting (keeps news fresh, saves quota)
    auto_state_file: str = "data/autopilot.json"  # pause/resume flag
    auto_gap_minutes: int = 3  # auto mode: a post goes out the moment it is ready; if another one just went out, at most this much later
    auto_from: str = ""  # optional daily window (empty = around the clock)
    auto_to: str = ""
    auto_max_per_day: int = 0  # source/manual posts per day (calendar posts not counted); 0 = no limit
    # photos: a post written from a source can carry the photo of the original item (caption = the post text)
    images: bool = True
    image_ratio: float = 0.5  # share of eligible posts that get their photo (0 = never, 1 = always)
    image_dir: str = "data/images"
    image_min_width: int = 400  # smaller pictures (icons, logos, thumbnails) are not used
    caption_max_chars: int = 1024  # photo caption limit of Telegram and Bale; longer post text is sent without the photo
    tg_api_id: int = 0  # optional: your own Telegram account, to read groups (see login_telegram.py)
    tg_api_hash: str = ""
    tg_session: str = "data/tg_account"
    # topics the channel never posts about (see agent/exclude.py); empty exclude_words = nothing excluded
    exclude_words: list[str] = field(default_factory=list)
    exclude_symbols: list[str] = field(default_factory=list)
    exclude_ignore: list[str] = field(default_factory=list)

    @property
    def exclusions(self) -> Exclusions:
        return Exclusions(list(self.exclude_words), list(self.exclude_symbols), list(self.exclude_ignore))

    @property
    def tg_account(self):
        from .sources import Account

        if self.tg_api_id and self.tg_api_hash:
            return Account(self.tg_api_id, self.tg_api_hash, self.tg_session)
        return None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: str | Path = "config.yaml") -> Config:
    base = Path(path).resolve().parent
    for name in ("secrets.env", ".env"):  # a customer's secrets live next to its settings (secrets.env; .env also works)
        load_dotenv(base / name)
    load_dotenv(find_dotenv(usecwd=True))
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    env = lambda k: os.environ.get(k, "").strip()  # noqa: E731

    provider = (env("LLM_PROVIDER") or raw.get("provider") or "anthropic").lower()
    if provider not in ("anthropic", "openai_compatible"):
        raise SystemExit("LLM_PROVIDER باید anthropic یا openai_compatible باشد.")

    base_url = env("LLM_BASE_URL") or raw.get("base_url") or ""
    csv = lambda v: [x.strip() for x in v.split(",") if x.strip()]  # noqa: E731
    models = csv(env("LLM_MODEL")) or [raw.get("model") or DEFAULT_MODEL]
    model = models[0]
    fallback_models = csv(env("LLM2_MODEL"))

    platform = (env("PLATFORM") or raw.get("platform") or "telegram").lower()
    if platform not in ("telegram", "bale"):
        raise SystemExit("PLATFORM باید telegram یا bale باشد.")
    # a Telegram token never works on Bale (and vice versa), so each platform has its own variable names
    token = env("BALE_BOT_TOKEN") if platform == "bale" else env("TELEGRAM_BOT_TOKEN")
    channel_raw = env("BALE_CHANNEL") if platform == "bale" else env("TELEGRAM_CHANNEL")
    missing = [k for k, v in (("TELEGRAM_BOT_TOKEN" if platform == "telegram" else "BALE_BOT_TOKEN", token),
                              ("TELEGRAM_CHANNEL" if platform == "telegram" else "BALE_CHANNEL", channel_raw),
                              ("OWNER_IDS", env("OWNER_IDS"))) if not v]
    if provider == "anthropic":
        api_key = env("ANTHROPIC_API_KEY")
        if not api_key:
            missing.append("ANTHROPIC_API_KEY")
    else:
        api_key = env("LLM_API_KEY")
        if not api_key:
            missing.append("LLM_API_KEY")
        if not base_url:
            missing.append("LLM_BASE_URL")
        if model == DEFAULT_MODEL:  # the Claude model name makes no sense for another provider
            missing.append("LLM_MODEL")
    if missing:
        raise SystemExit(f"متغیرهای محیطی تنظیم نشده‌اند: {', '.join(missing)} (فایل secrets.env همین مشتری را ببینید)")
    second = [env("LLM2_API_KEY"), env("LLM2_BASE_URL"), env("LLM2_MODEL")]
    if any(second) and not all(second):
        raise SystemExit("برای ارائه‌دهندهٔ پشتیبان باید هر سه مقدار LLM2_API_KEY, LLM2_BASE_URL, LLM2_MODEL پر شوند.")

    owner_ids = {int(x) for x in env("OWNER_IDS").replace(" ", "").split(",") if x}
    channel = channel_raw
    if not channel.startswith("@") and not channel.lstrip("-").isdigit():
        channel = "@" + channel

    profile_file = str(raw.get("profile_file", "profile.yaml"))
    profile_path = Path(profile_file) if Path(profile_file).is_absolute() else Path(path).parent / profile_file
    if not profile_path.is_file():
        raise SystemExit(f"فایل پروفایل مشتری پیدا نشد: {profile_path} (profile.yaml را از customers/_template کپی و پر کنید).")
    profile = load_profile(profile_path)
    approval = str(raw.get("approval") or ("auto" if raw.get("auto_publish") else "review")).lower()
    if approval not in ("review", "trusted", "auto"):
        raise SystemExit("config.yaml: approval باید review یا trusted یا auto باشد.")
    auto_publish = approval in ("trusted", "auto")  # the older auto_publish flag is still understood when `approval` is absent
    cfg = Config(
        bot_token=token,
        platform=platform,
        api_key=api_key,
        channel=channel,
        owner_ids=owner_ids,
        channel_name=str(raw.get("channel_name") or profile.name),
        profile=profile,
        profile_file=profile_file,
        provider=provider,
        base_url=base_url.rstrip("/"),
        model=model,
        fallback_api_key=env("LLM2_API_KEY"),
        fallback_base_url=env("LLM2_BASE_URL").rstrip("/"),
        fallback_model=fallback_models[0] if fallback_models else "",
        models=models if provider == "openai_compatible" else [model],
        fallback_models=fallback_models,
        timezone=raw.get("timezone", "Asia/Tehran"),
        post_times=[str(t) for t in raw.get("post_times", ["10:30", "18:30"])],
        max_chars=int(raw.get("max_chars", 1200)),
        source_max_chars=int(raw.get("source_max_chars", 2500)),
        max_output_tokens=int(raw.get("max_output_tokens", 6000)),
        pillars=raw.get("pillars", []),
        db_path=raw.get("db_path", "data/agent.db"),
        knowledge_dir=raw.get("knowledge_dir", "knowledge"),
        template_file=raw.get("template_file", "post_template.yaml"),
        sources_file=raw.get("sources_file", "sources.yaml"),
        source_poll_hours=float(raw.get("source_poll_hours", 6)),
        max_new_per_poll=int(raw.get("max_new_per_poll", 3)),
        max_age_days=int(raw.get("max_age_days", 3)),
        attribution=bool(raw.get("attribution", True)),
        auto_publish=auto_publish,
        approval=approval,
        max_publish_attempts=max(1, int(raw.get("max_publish_attempts", 4))),
        retry_delays=[max(1, int(x)) for x in (raw.get("retry_delays") or [30, 120, 600])],
        auto_max_queue=max(1, int(raw.get("auto_max_queue", 4))),
        auto_state_file=raw.get("auto_state_file", "data/autopilot.json"),
        auto_gap_minutes=max(0, int(raw.get("auto_gap_minutes", 3))),
        auto_from=str(raw.get("auto_from") or ""),
        auto_to=str(raw.get("auto_to") or ""),
        auto_max_per_day=max(0, int(raw.get("auto_max_per_day", 0))),
        images=bool(raw.get("images", True)),
        image_ratio=min(1.0, max(0.0, float(raw.get("image_ratio", 0.5)))),
        image_dir=str(raw.get("image_dir", "data/images")),
        image_min_width=max(0, int(raw.get("image_min_width", 400))),
        caption_max_chars=int(raw.get("caption_max_chars", 1024)),
        tg_api_id=int(env("TELEGRAM_API_ID") or 0),
        tg_api_hash=env("TELEGRAM_API_HASH"),
        tg_session=raw.get("tg_session", "data/tg_account"),
        exclude_words=_str_list(raw.get("exclude_words"), []),  # nothing is excluded unless the customer says so
        exclude_symbols=_str_list(raw.get("exclude_symbols"), []),
        exclude_ignore=_str_list(raw.get("exclude_ignore"), []),
    )
    for t in [*cfg.post_times, *[x for x in (cfg.auto_from, cfg.auto_to) if x]]:
        _parse_hhmm(t)  # fail fast on bad times
    return cfg


def _str_list(value, default: list[str]) -> list[str]:
    """Missing key -> default; an explicit empty list stays empty (= switched off)."""
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [value]
    return [str(x).strip() for x in value if str(x).strip()]


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"ساعت نامعتبر: {s}")
    return h, m
