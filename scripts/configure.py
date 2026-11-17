"""Interactive set-up of .env and the test channel, for a fresh computer. Run by setup.bat; safe to run again.

Asks only for what is missing (Enter keeps what is already there), checks every key on the spot, and finds the ids
nobody should have to look up by hand:
  - the channel id: add the bot to the channel as admin and post any message there;
  - your own id (for the control bot): send /start to the bot.
Secrets are never printed. Nothing here publishes anything.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
DESTINATIONS = ROOT / "orgs" / "pouyan" / "destinations.yaml"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODELS = "gemini-3.6-flash,gemini-3.5-flash-lite"
WAIT_SECONDS = 180

BASE_SETTINGS = {
    "ORCHESTRA_DATABASE_URL": "sqlite:///data/app.db",
    "ORCHESTRA_SYSTEM_DATABASE_URL": "sqlite:///data/dbos.sqlite",
    "ORCHESTRA_ASSET_DIR": "assets_store",
    "ORCHESTRA_LLM_PROVIDER": "openai_compatible",
    "ORCHESTRA_LLM_BASE_URL": GEMINI_BASE,
}


class Io:
    """Console input and output; replaced in tests."""

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def secret(self, prompt: str) -> str:
        return getpass.getpass(prompt)

    def say(self, text: str = "") -> None:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            print(text.encode("ascii", "replace").decode(), flush=True)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ------------------------------------------------------------------ .env


def read_env(path: Path = ENV) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(values: dict[str, str], path: Path = ENV) -> None:
    """Keep the existing file's lines and comments; replace values in place; add new keys at the end."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else [
        "# تنظیمات ORCHESTRA — این فایل رمزها را دارد؛ آن را جایی نفرستید و در گیت‌هاب نگذارید."]
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and not line.lstrip().startswith("#") and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            seen.add(m.group(1))
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ checks


def check_model_key(key: str, base: str, http: httpx.Client) -> str | None:
    """None if the key works; otherwise why not."""
    try:
        r = http.get(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except httpx.HTTPError as e:
        return f"به سرویس مدل وصل نشد ({type(e).__name__}). اگر از ایران بسته است، VPN را روشن کنید."
    if r.status_code in (401, 403):
        return "کلید پذیرفته نشد."
    if r.status_code >= 400:
        return f"پاسخ نامعمول از سرویس مدل (HTTP {r.status_code})."
    return None


class Bot:
    def __init__(self, token: str, base: str, http: httpx.Client):
        self.url = f"{base}{token}/"
        self.http = http

    def call(self, method: str, **data):
        try:
            r = self.http.post(self.url + method, data=data, timeout=40)
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise RuntimeError(f"به سرور ربات وصل نشد ({type(e).__name__})") from e
        if not body.get("ok"):
            raise RuntimeError(str(body.get("description") or f"HTTP {r.status_code}"))
        return body["result"]

    def wait_for(self, want, io: Io, timeout: int = WAIT_SECONDS):
        """Read new updates until want(update) returns something; old updates are skipped first."""
        offset = 0
        kinds = '["message", "callback_query", "channel_post", "my_chat_member"]'  # never leave buttons switched off
        for u in self.call("getUpdates", offset=-1, timeout=0, allowed_updates=kinds) or []:
            offset = u["update_id"] + 1
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for u in self.call("getUpdates", offset=offset, timeout=5, allowed_updates=kinds) or []:
                offset = u["update_id"] + 1
                got = want(u)
                if got:
                    self.call("getUpdates", offset=offset, timeout=0, allowed_updates=kinds)  # mark as read
                    return got
            io.sleep(1)
        return None


def channel_of(update: dict):
    post = update.get("channel_post") or update.get("my_chat_member") or {}
    chat = post.get("chat") or {}
    return (str(chat["id"]), chat.get("title") or "") if chat.get("type") == "channel" else None


def person_of(update: dict):
    msg = update.get("message") or {}
    chat, who = msg.get("chat") or {}, msg.get("from") or {}
    return str(who["id"]) if chat.get("type") == "private" and who.get("id") else None


def set_channel(chat_id: str, platform: str, path: Path = DESTINATIONS) -> int:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    n = 0
    for d in data.get("destinations", []):
        if d.get("platform") == platform:
            d["chat_id"] = chat_id
            n += 1
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return n


# ------------------------------------------------------------------ the conversation


def configure(io: Io, http: httpx.Client, *, telegram_base: str = "https://api.telegram.org/bot",
              env_path: Path = ENV, destinations_path: Path = DESTINATIONS) -> int:
    env = read_env(env_path)
    new: dict[str, str] = {k: v for k, v in BASE_SETTINGS.items() if not env.get(k)}
    io.say("\n=== تنظیمات ORCHESTRA ===  (هر جا مقداری از قبل هست، با Enter همان می‌ماند)\n")

    # 1. the model
    base = env.get("ORCHESTRA_LLM_BASE_URL") or GEMINI_BASE
    while True:
        have = bool(env.get("ORCHESTRA_LLM_API_KEY"))
        key = io.secret("کلید Gemini (از aistudio.google.com) " + ("[Enter = همان قبلی]: " if have else ": ")).strip()
        key = key or env.get("ORCHESTRA_LLM_API_KEY", "")
        if not key:
            io.say("بدون کلید مدل، تیم نمی‌تواند بنویسد.")
            continue
        why = check_model_key(key, base, http)
        if why is None:
            io.say("✅ کلید مدل کار می‌کند.")
            new["ORCHESTRA_LLM_API_KEY"] = key
            break
        io.say(f"❌ {why}")
        if io.ask("دوباره امتحان کنم؟ (Enter = بله، n = بعداً): ").strip().lower() == "n":
            new["ORCHESTRA_LLM_API_KEY"] = key
            break
    models = io.ask(f"نام مدل‌ها با کاما [Enter = {env.get('ORCHESTRA_LLM_MODEL') or DEFAULT_MODELS}]: ").strip()
    new["ORCHESTRA_LLM_MODEL"] = models or env.get("ORCHESTRA_LLM_MODEL") or DEFAULT_MODELS

    # 2. the Telegram bot and the test channel
    token = None
    while True:
        have = env.get("TELEGRAM_TEST_BOT_TOKEN")
        t = io.secret("توکن ربات تلگرام (از @BotFather) " + ("[Enter = همان قبلی]: " if have else "[Enter = فعلاً نه]: ")).strip() or have
        if not t:
            io.say("بدون ربات، چیزی منتشر نمی‌شود؛ بعداً می‌توانید همین را دوباره اجرا کنید.")
            break
        bot = Bot(t, telegram_base, http)
        try:
            me = bot.call("getMe")
        except RuntimeError as e:
            io.say(f"❌ ربات جواب نداد: {e}. (تلگرام از ایران بدون VPN باز نمی‌شود.)")
            if io.ask("دوباره امتحان کنم؟ (Enter = بله، n = بعداً): ").strip().lower() == "n":
                break
            continue
        token = t
        new["TELEGRAM_TEST_BOT_TOKEN"] = t
        io.say(f"✅ ربات @{me.get('username', '?')} پیدا شد.")
        break

    if token:
        bot = Bot(token, telegram_base, http)
        if io.ask("کانال تست را تنظیم کنم؟ (Enter = بله، n = نه): ").strip().lower() != "n":
            io.say("ربات را در کانال تست «ادمین» کنید و یک پیام دلخواه در کانال بفرستید. منتظرم…")
            found = bot.wait_for(channel_of, io)
            if found:
                n = set_channel(found[0], "telegram", destinations_path)
                io.say(f"✅ کانال «{found[1]}» پیدا شد و برای {n} مقصد تلگرام ثبت شد.")
            else:
                io.say("پیامی در کانال دیده نشد؛ کانال قبلی در تنظیمات می‌ماند.")

        # 3. the control bot: the same bot, the owner's id found from a /start
        if io.ask("همین ربات برای کنترل از روی گوشی هم باشد؟ (Enter = بله، n = نه): ").strip().lower() != "n":
            ids = env.get("ORCHESTRA_CONTROL_CHAT_IDS", "")
            if ids and io.ask(f"شناسهٔ مجاز فعلی: {ids}. همین بماند؟ (Enter = بله، n = نه): ").strip().lower() != "n":
                owner = ids
            else:
                io.say("از تلگرام خودتان به این ربات پیام /start بفرستید. منتظرم…")
                owner = bot.wait_for(person_of, io)
                io.say(f"✅ شناسهٔ شما: {owner}" if owner else "پیامی نیامد؛ بعداً همین را دوباره اجرا کنید.")
            if owner:
                new.update({"ORCHESTRA_CONTROL_PLATFORM": "telegram", "ORCHESTRA_CONTROL_BOT_TOKEN": token,
                            "ORCHESTRA_CONTROL_CHAT_IDS": owner})

    write_env(new, env_path)
    io.say("\nتنظیمات ذخیره شد (.env). هیچ رمزی اینجا نمایش داده نشد.")
    return 0


def main() -> int:
    os.chdir(ROOT)
    with httpx.Client(timeout=40, trust_env=True) as http:
        return configure(Io(), http)


if __name__ == "__main__":
    sys.exit(main())
