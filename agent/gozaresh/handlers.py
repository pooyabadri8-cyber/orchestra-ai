from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from ..sources import user_agent
from .parse import GozareshError, Report, parse_page
from .report import format_report
from .settings import GozareshSettings, load_settings

log = logging.getLogger("content-agent.gozaresh")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MAX_TIMES = 12

HELP = """\
<b>گزارش روزانهٔ بازار</b>
/gozaresh — گزارش همین الان
/gozaresh 10:30 13:00 18:30 — تعیین ساعت‌های ارسال خودکار (به وقت {tz})
/gozaresh times — دیدن ساعت‌های فعلی
/gozaresh off — خاموش کردن ارسال خودکار

گزارش شامل شاخص کل، ارزش معاملات خرد، ورود/خروج پول حقیقی خرد و بیشترین ورود و خروج پول حقیقی (سهم و گروه) است.
گزارش‌های خودکار فقط برای خود شما در همین چت می‌آیند، نه در کانال. روزهای تعطیل ({off}) خودکار ارسال نمی‌شود."""

WEEKDAY_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def normalize_times(args: list[str]) -> list[str]:
    """['۱۰:۳۰', '9:5'] -> ['10:30'] or raises ValueError. Sorted, de-duplicated."""
    out = set()
    for a in args:
        a = a.translate(_DIGITS).replace("٫", ":").replace("：", ":")
        m = TIME_RE.match(a)
        if not m:
            raise ValueError(a)
        out.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(out)


class Gozaresh:
    """State + fetching. Everything lives in data/gozaresh.json and in memory; no other component is touched."""

    def __init__(self, settings: GozareshSettings, tz, owner_ids: set[int]):
        self.s = settings
        self.tz = tz
        self.owner_ids = owner_ids
        self.path = Path(settings.state_file)
        self.subs: dict[str, list[str]] = {}  # user id -> times; [] = explicitly off
        self.last: dict[str, dict[str, str]] = {}  # user id -> {time: date sent}
        self._cache: tuple[float, Report] | None = None
        self._lock = asyncio.Lock()
        self._attempts: dict[tuple, tuple[int, float | None]] = {}
        self._load()

    # ---- persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.subs = {str(k): list(v) for k, v in data.get("subs", {}).items()}
            self.last = {str(k): dict(v) for k, v in data.get("last", {}).items()}
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"subs": self.subs, "last": self.last}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    def times_for(self, uid: int) -> list[str]:
        key = str(uid)
        if key in self.subs:
            return self.subs[key]
        try:
            return normalize_times(self.s.default_times)
        except ValueError:
            return []

    def set_times(self, uid: int, times: list[str]) -> None:
        self.subs[str(uid)] = times
        self.last.pop(str(uid), None)
        self._save()

    # ---- fetching
    async def fetch(self, force: bool = False) -> Report:
        async with self._lock:
            now = time.monotonic()
            if not force and self._cache and now - self._cache[0] < self.s.cache_seconds:
                return self._cache[1]
            last_err = ""
            async with httpx.AsyncClient(
                headers={"User-Agent": user_agent(), "Accept-Language": "fa,en;q=0.8"},
                timeout=httpx.Timeout(40, connect=20), follow_redirects=True,
            ) as http:
                for attempt in range(3):
                    try:
                        r = await http.get(self.s.url)
                    except httpx.HTTPError as e:
                        last_err = f"({type(e).__name__}) {str(e)[:100] or 'سایت جواب نداد'}"
                    else:
                        if r.status_code == 200:
                            report = parse_page(r.text, self.s.top_n)  # GozareshError propagates: retry won't help
                            self._cache = (time.monotonic(), report)
                            return report
                        last_err = f"HTTP {r.status_code}"
                        if r.status_code not in (429, 500, 502, 503, 504):
                            break
                    await asyncio.sleep(3 * (attempt + 1))
            raise GozareshError(f"دریافت صفحه از {self.s.url} ناموفق بود: {last_err}")

    async def build_text(self, force: bool = False) -> str:
        report = await self.fetch(force)
        return format_report(report, datetime.now(self.tz), self.s.persian_digits)


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Gozaresh:
    return context.application.bot_data["gozaresh"]


async def _send(bot, chat_id: int, text: str) -> None:
    for i in range(0, len(text), 4000):  # Telegram's limit is 4096; cut on line breaks
        chunk = text[i : i + 4000]
        if i + 4000 < len(text) and "\n" in chunk:
            chunk = chunk[: chunk.rindex("\n")]
        await bot.send_message(
            chat_id, chunk, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )


def _off_days_text(s: GozareshSettings) -> str:
    return "، ".join(WEEKDAY_FA[d] for d in s.off_weekdays) or "هیچ‌روز"


def _next_text(svc: Gozaresh, uid: int) -> str:
    times = svc.times_for(uid)
    if not times:
        return "ارسال خودکار خاموش است."
    return "ارسال خودکار هر روز (به‌جز " + _off_days_text(svc.s) + ") ساعت: " + "، ".join(times)


async def cmd_gozaresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    user, msg = update.effective_user, update.effective_message
    if not user or user.id not in svc.owner_ids:
        return
    args = context.args or []
    word = args[0].lower() if args else ""

    if word in ("help", "راهنما"):
        await msg.reply_text(HELP.format(tz=svc.tz.key, off=_off_days_text(svc.s)), parse_mode=ParseMode.HTML)
        return
    if word in ("times", "time", "ساعت"):
        rest = args[1:]
        if not rest:
            await msg.reply_text(_next_text(svc, user.id))
            return
        args, word = rest, rest[0].lower()
    if word in ("off", "stop", "خاموش"):
        svc.set_times(user.id, [])
        await msg.reply_text("ارسال خودکار گزارش خاموش شد. (گزارش دستی همچنان با /gozaresh)")
        return
    if args:  # anything else must be a list of times
        try:
            times = normalize_times(args)
        except ValueError as e:
            await msg.reply_text(
                f"ساعت «{e}» را نفهمیدم. مثال: /gozaresh 10:30 13:00 18:30\nراهنما: /gozaresh help"
            )
            return
        if len(times) > MAX_TIMES:
            await msg.reply_text(f"حداکثر {MAX_TIMES} ساعت مجاز است.")
            return
        svc.set_times(user.id, times)
        await msg.reply_text("✅ ذخیره شد.\n" + _next_text(svc, user.id) + f"\n(به وقت {svc.tz.key})")
        return

    # no arguments: report right now
    wait = await msg.reply_text("⏳ در حال گرفتن گزارش…")
    try:
        text = await svc.build_text(force=True)
    except GozareshError as e:
        await wait.edit_text(f"⚠️ گزارش ساخته نشد: {e}")
        return
    except Exception:  # noqa: BLE001 - never let this feature crash the bot
        log.exception("gozaresh failed")
        await wait.edit_text("⚠️ خطای غیرمنتظره در ساخت گزارش. جزئیات در لاگ ربات است.")
        return
    await _send(context.bot, msg.chat_id, text)
    try:
        await wait.delete()
    except TelegramError:
        pass


async def send_due(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every 30 s: sends each owner's report at the times they chose."""
    svc = _svc(context)
    now = datetime.now(svc.tz)
    if now.weekday() in svc.s.off_weekdays:
        return
    today = now.strftime("%Y-%m-%d")
    for uid in svc.owner_ids:
        for t in svc.times_for(uid):
            hh, mm = map(int, t.split(":"))
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if not (at <= now < at + timedelta(minutes=svc.s.late_minutes)):
                continue
            if svc.last.get(str(uid), {}).get(t) == today:
                continue
            key = (uid, t, today)
            tries, last_try = svc._attempts.get(key, (0, None))
            if tries >= 3 or (last_try is not None and time.monotonic() - last_try < 120):
                continue
            svc._attempts[key] = (tries + 1, time.monotonic())
            try:
                text = await svc.build_text()
                await _send(context.bot, uid, text)
            except GozareshError as e:
                log.warning("scheduled report failed for %s at %s: %s", uid, t, e)
                if tries + 1 >= 3:  # tell the person once, after the last try
                    await _safe_notify(context.bot, uid, f"⚠️ گزارش ساعت {t} ساخته نشد: {html.escape(str(e))}")
                continue
            except TelegramError as e:
                log.warning("could not deliver the report to %s: %s", uid, e)
                continue
            except Exception:  # noqa: BLE001
                log.exception("scheduled report crashed")
                continue
            svc.last.setdefault(str(uid), {})[t] = today
            svc._save()
    # forget old attempt counters
    for k in [k for k in svc._attempts if k[2] != today]:
        svc._attempts.pop(k, None)


async def _safe_notify(bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


def register(app: Application, cfg) -> None:
    """Hook for bot.build_app(): adds /gozaresh and its scheduler. Touches nothing else."""
    settings = load_settings()
    app.bot_data["gozaresh"] = Gozaresh(settings, cfg.tz, set(cfg.owner_ids))
    app.add_handler(CommandHandler("gozaresh", cmd_gozaresh, filters=filters.ChatType.PRIVATE))
    app.job_queue.run_repeating(send_due, interval=30, first=20, name="gozaresh")
