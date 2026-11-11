"""One-time login of YOUR Telegram account, so the bot can read groups/channels you are a member of.

Run once on the machine that runs the bot:   python login_telegram.py
You will be asked for your phone number, the code Telegram sends you, and your 2FA password if you have one.
The session is saved in data/tg_account.session. That file gives full access to your Telegram account:
never share it, never upload it, never put it in a zip you send to someone.

Needs TELEGRAM_API_ID and TELEGRAM_API_HASH in .env (create them at https://my.telegram.org -> API development tools).
"""
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from telethon import TelegramClient

load_dotenv(find_dotenv(usecwd=True))
api_id = int(os.environ.get("TELEGRAM_API_ID") or 0)
api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
if not api_id or not api_hash:
    raise SystemExit("TELEGRAM_API_ID و TELEGRAM_API_HASH را در فایل .env بگذارید (از my.telegram.org).")

Path("data").mkdir(exist_ok=True)
with TelegramClient("data/tg_account", api_id, api_hash) as client:  # prompts for phone/code in the console
    me = client.loop.run_until_complete(client.get_me())
    print(f"ورود موفق: {me.first_name} (@{me.username})")
    print("حالا می‌توانید ربات را اجرا کنید. فایل data/tg_account.session را به کسی ندهید.")
