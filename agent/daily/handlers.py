"""Daily content calendar: writes the timed posts (news digest, reports, analyses) and hands them to the normal
publish pipeline (auto mode: safety check -> scheduled -> published; otherwise: sent to the owners for approval).

Independent of the source-polling flow and of /gozaresh's per-user subscriptions. The only things it shares are the
Writer, the Store and the /gozaresh page reader (read-only).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from ..autopilot import check_post
from ..gozaresh.parse import GozareshError
from ..sources import Item, SourceError, fetch_source, load_sources, new_http_client
from ..writer import LLMError
from . import build
from .settings import DailySettings, Entry, load_settings

log = logging.getLogger("content-agent.daily")

RETRY_EVERY = 150  # seconds between attempts for one post
DAILY_PILLAR = "daily:"


class NotReady(Exception):
    """Data exists but is not usable yet (e.g. the market has not opened: trade value is zero)."""


class Skip(Exception):
    """Nothing worth posting today (not enough data / model said SKIP). Not an error."""


class DailyState:
    """Which post of which day is done. Survives restarts, so a restart never causes a double post."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.done: dict[str, str] = {}
        self.used: list[dict] = []  # [{"date": "...", "name": "..."}] symbols/groups already analysed
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.done, self.used = dict(data.get("done", {})), list(data.get("used", []))
        except (OSError, ValueError, AttributeError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            keep = sorted(self.done)[-300:]
            tmp.write_text(json.dumps({"done": {k: self.done[k] for k in keep}, "used": self.used[-20:]},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    def mark(self, key: str, status: str) -> None:
        self.done[key] = status
        self.save()

    def recent_names(self, n: int = 6) -> list[str]:
        return [u["name"] for u in self.used[-n:]]

    def use_name(self, date: str, name: str) -> None:
        self.used.append({"date": date, "name": name})
        self.save()


class Daily:
    def __init__(self, app: Application, cfg, settings: DailySettings):
        self.app, self.cfg, self.s = app, cfg, settings
        self.state = DailyState(settings.state_file)
        self.news_sources = load_sources(settings.news_sources_file)
        self.capital_sources = load_sources(settings.capital_sources_file)
        self._cache: dict[str, tuple[float, list[Item], list[str]]] = {}
        self._tries: dict[str, tuple[int, float, str]] = {}  # key -> (attempts, last time, last error)
        self._lock = asyncio.Lock()
        store = app.bot_data.get("store")
        if store is not None and settings.enabled:
            store.extra_busy = self.upcoming_times  # source posts keep away from calendar posts that are still to come

    def upcoming_times(self) -> list[datetime]:
        """Today's calendar slots that have not been posted yet (UTC)."""
        if not self.s.enabled:
            return []
        now = datetime.now(self.cfg.tz)
        out = []
        for e in self.s.entries:
            if now.weekday() not in e.days or f"{now.date().isoformat()}:{e.id}" in self.state.done:
                continue
            t = self._target(e, now)
            if t >= now - timedelta(minutes=1):
                out.append(t.astimezone(timezone.utc))
        return out

    # ---------------------------------------------------------- shared handles
    @property
    def store(self):
        return self.app.bot_data["store"]

    @property
    def writer(self):
        return self.app.bot_data["writer"]

    @property
    def market_sources(self):
        return [s for s in self.app.bot_data.get("sources", []) if s.type in ("rss", "telegram")]

    def banned_names(self) -> list[str]:
        return [s.name for s in [*self.news_sources, *self.capital_sources, *self.app.bot_data.get("sources", [])]]

    # ---------------------------------------------------------- gathering material
    async def _gather(self, which: str, window_hours: int) -> tuple[list[Item], list[str]]:
        cached = self._cache.get(which)
        if cached and time.monotonic() - cached[0] < 15 * 60:
            return cached[1], cached[2]
        sources = {"news": self.news_sources, "capital": self.capital_sources}.get(which) or (
            [] if which in ("news", "capital") else self.market_sources)
        errors: list[str] = []
        items: list[Item] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        async def one(http, src):
            try:
                return src, await fetch_source(http, src)
            except SourceError as e:
                return src, e

        async with new_http_client() as http:
            results = await asyncio.gather(*(one(http, s) for s in sources))
        for src, res in results:
            if isinstance(res, Exception):
                errors.append(f"{src.name}: {res}")
                continue
            items += [it for it in res if not it.published or it.published >= cutoff]
        items = self.cfg.exclusions.keep_items(items)  # nothing about funds reaches the model
        items.sort(key=lambda i: i.published or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        self._cache[which] = (time.monotonic(), items, errors)
        return items, errors

    async def _report(self):
        svc = self.app.bot_data["gozaresh"]
        try:
            rep = self.cfg.exclusions.clean_report(await svc.fetch())  # fund rows removed from the lists
            if not self.s.report_stocks:
                rep = replace(rep, top_in_stocks=[], top_out_stocks=[])
            return rep
        except GozareshError as e:
            raise SourceError(str(e)) from e

    # ---------------------------------------------------------- builders: each returns (text, topic)
    async def make(self, entry: Entry, now: datetime) -> tuple[str, str]:
        tpl = self.writer.tpl
        kind = entry.kind
        if kind == "report":
            rep = await self._report()
            if rep.retail_value.is_zero or rep.index.value <= 0:
                raise NotReady("ارزش معاملات صفر است؛ بازار هنوز باز نشده یا امروز تعطیل است.")
            return build.report_post(tpl, rep, now), f"گزارش بازار {build.jalali_date(now)} {now:%H:%M}"

        if kind == "capital":
            items, errors = await self._gather("capital", self.s.capital_window_hours)
            hits = build.match_items(items, build.CAPITAL_WORDS)
            if not hits:
                if errors and not items:  # the sources did not answer: try again until the late window ends
                    raise NotReady("منابع افزایش سرمایه پاسخ ندادند. " + "؛ ".join(errors[:3]))
                raise Skip("خبری دربارهٔ افزایش سرمایه سهم‌ها در منابع نبود.")
            task, tag = build.capital_task(now)
            text = await self.writer.compose(task, build.items_material(hits, 25, 450), tag, max_chars=2200)
            if text is None:
                raise Skip("مدل خبر کافی برای پست افزایش سرمایه ندید.")
            headline = build.capital_headline(now)
            return build.force_headline(text, headline), headline

        if kind == "news":
            items, errors = await self._gather("news", self.s.news_window_hours)
            if len(items) < 3:
                raise NotReady(f"خبر کافی از منابع خبری نیامد ({len(items)} مورد). " + "؛ ".join(errors[:3]))
            headline, task, tag = build.news_task(now)
            text = await self.writer.compose(task, build.items_material(items, 45, 200), tag, max_chars=1600)
            if text is None:
                raise Skip("خبر مهمی برای بازار پیدا نشد.")
            return build.force_headline(text, headline), headline

        market, _ = await self._gather("market", self.s.context_window_hours)
        news, _ = await self._gather("news", self.s.news_window_hours) if self.news_sources else ([], [])

        if kind == "gold":
            gold_cutoff = datetime.now(timezone.utc) - timedelta(hours=self.s.gold_window_hours)
            pool = [i for i in market + news if not i.published or i.published >= gold_cutoff]
            gold = build.match_items(pool, build.GOLD_WORDS)
            if not gold:
                raise Skip("مطلبی دربارهٔ طلا در منابع نبود.")
            task, tag = build.gold_task(now)
            text = await self.writer.compose(task, build.items_material(gold, 20, 400), tag, max_chars=1400)
            if text is None:
                raise Skip("مدل داده را برای تحلیل طلا کافی ندید.")
            return text, f"تحلیل طلا {build.jalali_date(now)}"

        rep = None
        try:
            rep = await self._report()
        except (NotReady, SourceError):
            if kind != "open":  # the opening analysis may be written without numbers; the others need them
                raise
        has_numbers = bool(rep and not rep.retail_value.is_zero and rep.index.value > 0)
        parts = []
        if has_numbers:
            parts.append(build.report_material(rep, now))
        if market:
            parts.append("مطالب و تحلیل‌های اخیر بازار:\n" + build.items_material(market, 15, 350))
        if news:
            parts.append("خبرهای مهم اخیر:\n" + build.items_material(news, 12, 160))

        if kind == "open":
            if not market and not news and not has_numbers:
                raise NotReady("هیچ داده‌ای برای تحلیل آغاز بازار نیست.")
            task, tag = build.open_task(now)
            topic = f"تحلیل آغاز بازار {build.jalali_date(now)}"
        elif kind == "close":
            if not has_numbers:
                raise NotReady("داده‌ٔ پایان بازار آماده نیست.")
            task, tag = build.close_task(now)
            topic = f"تحلیل پایان بازار {build.jalali_date(now)}"
        elif kind in ("symbol", "industry"):
            if not has_numbers:
                raise NotReady("داده‌ٔ ورود پول آماده نیست.")
            cands = build.symbol_candidates(rep, self.state.recent_names(), now.day)[:3]
            if kind == "industry":  # industry of the day: groups only, never a single stock
                cands = build.industry_candidates(rep, self.state.recent_names())[:3]
            if not cands:
                raise NotReady("فهرست بیشترین ورود پول خالی است.")
            # the model answers SKIP for a fund symbol (ETF) that the name filter could not recognise: then try the next one
            for sym_kind, name in cands:
                related = build.match_items(market + news, [name])
                parts = [build.report_material(rep, now)]
                if related:
                    parts.append(f"مطالب مرتبط با «{name}»:\n" + build.items_material(related, 6, 500))
                task, tag = build.symbol_task(now, sym_kind, name)
                text = await self.writer.compose(task, "\n\n".join(parts), tag, max_chars=1400)
                if text is not None:
                    self._pending_name = (build.jalali_date(now), name)
                    return text, f"تحلیل {sym_kind} روز: {name}"
            raise Skip("مدل داده را برای نوشتن این تحلیل کافی ندید.")
        else:  # pragma: no cover
            raise Skip(f"نوع ناشناخته: {kind}")
        text = await self.writer.compose(task, "\n\n".join(parts), tag, max_chars=1400)
        if text is None:
            raise Skip("مدل داده را برای نوشتن این تحلیل کافی ندید.")
        return text, topic

    # ---------------------------------------------------------- delivery
    async def deliver(self, bot, entry: Entry, text: str, topic: str, when: datetime, review_only: bool = False) -> str:
        from .. import bot as botmod  # late import: bot.py imports this package

        cfg, store = self.cfg, self.store
        auto = self.app.bot_data["auto"]
        post = store.add_draft(topic, DAILY_PILLAR + entry.id, text, when)
        chats = sorted(cfg.owner_ids)
        if review_only or not botmod.auto_active(cfg, auto):
            for cid in chats:
                await botmod.send_review(bot, cid, post, cfg)
            return "review"
        if entry.kind == "report":  # numbers straight from the page, no model: only a size check
            reasons = ["طولانی‌تر از حد تلگرام"] if len(text) > botmod.TG_LIMIT else []
        else:
            others = [t for t in store.recent_headlines() if t != post.text]
            reasons = check_post(text, others, self.banned_names(), self.cfg.exclusions, self.cfg.profile.guard_phrases)
        if reasons:
            store.log_event(post.id, "held")
            note = "⏸ <b>این پست خودکار منتشر نشد؛ لطفاً خودتان بررسی کنید:</b>\n" + "\n".join("• " + html.escape(r) for r in reasons)
            for cid in chats:
                await botmod.send_review(bot, cid, post, cfg, note=note)
            return "held"
        store.log_event(post.id, "auto")
        await botmod.release(bot, cfg, store, post, when, notice=False)  # published right now if its time has come
        return "auto"

    async def run_entry(self, bot, entry: Entry, now_local: datetime, target: datetime, review_only: bool = False) -> str:
        self._pending_name = None
        text, topic = await self.make(entry, now_local)
        when = max(target.astimezone(timezone.utc), datetime.now(timezone.utc))
        outcome = await self.deliver(bot, entry, text, topic, when, review_only)
        if self._pending_name and not review_only:
            self.state.use_name(*self._pending_name)
        return outcome

    # ---------------------------------------------------------- the clock
    def _target(self, entry: Entry, now_local: datetime) -> datetime:
        h, m = entry.time.split(":")
        return now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    async def tick(self, bot, now: datetime | None = None) -> None:
        if not self.s.enabled or self._lock.locked():
            return
        async with self._lock:
            auto = self.app.bot_data["auto"]
            if auto.paused:
                return
            now = now or datetime.now(self.cfg.tz)
            day = now.date().isoformat()
            for entry in self.s.entries:
                if now.weekday() not in entry.days:
                    continue
                key = f"{day}:{entry.id}"
                if key in self.state.done:
                    continue
                target = self._target(entry, now)
                lead = self.s.lead_minutes if entry.lead is None else entry.lead
                if entry.kind == "report":
                    lead = 0 if entry.lead is None else entry.lead
                if now < target - timedelta(minutes=lead):
                    continue
                n, last, err = self._tries.get(key, (0, 0.0, ""))
                if now > target + timedelta(minutes=self.s.late_minutes):
                    self.state.mark(key, "missed")
                    log.warning("daily %s: skipped (too late). last error: %s", key, err)
                    if err and not err.startswith("NotReady"):
                        await self._notify(bot, f"⚠️ پست زمان‌دار «{entry.id}» امروز ساخته نشد: {err}")
                    continue
                if n and time.monotonic() - last < RETRY_EVERY:
                    continue
                try:
                    outcome = await self.run_entry(bot, entry, now, target)
                except NotReady as e:
                    self._tries[key] = (n + 1, time.monotonic(), f"NotReady: {e}")
                    log.info("daily %s not ready: %s", key, e)
                except Skip as e:
                    self.state.mark(key, "skipped")
                    log.info("daily %s skipped: %s", key, e)
                except (LLMError, SourceError) as e:
                    self._tries[key] = (n + 1, time.monotonic(), str(e)[:300])
                    log.warning("daily %s failed: %s", key, e)
                except Exception as e:  # never let one bad post stop the others
                    self._tries[key] = (n + 1, time.monotonic(), f"{type(e).__name__}: {e}"[:300])
                    log.exception("daily %s crashed", key)
                else:
                    self.state.mark(key, outcome)
                    self._tries.pop(key, None)

    async def _notify(self, bot, text: str) -> None:
        from .. import bot as botmod

        await botmod.notify_owners(bot, self.cfg, html.escape(text), silent=True)


# ---------------------------------------------------------------- commands


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Daily:
    return context.application.bot_data["daily"]


def _owner(update: Update, context) -> bool:
    u = update.effective_user
    return bool(u and u.id in _svc(context).cfg.owner_ids)


STATUS_ICON = {"auto": "✅", "review": "📝", "held": "⏸", "skipped": "⏭", "missed": "⌛"}


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner(update, context):
        return
    d = _svc(context)
    msg = update.effective_message
    args = [a.lower() for a in context.args]
    if args:
        entry = next((e for e in d.s.entries if e.id.lower() == args[0]), None)
        if not entry:
            await msg.reply_text("شناسه پیدا نشد. فهرست: /daily")
            return
        review_only = len(args) > 1 and args[1] in ("draft", "پیش‌نویس", "review")
        await msg.reply_text("در حال ساخت… (چند دقیقه طول می‌کشد)")
        now = datetime.now(d.cfg.tz)
        try:
            outcome = await d.run_entry(context.bot, entry, now, now, review_only)
        except (NotReady, Skip, LLMError, SourceError) as e:
            await msg.reply_text(f"ساخته نشد: {e}")
            return
        await msg.reply_text({
            "auto": "✅ زمان‌بندی شد و تا چند ثانیه دیگر منتشر می‌شود.",
            "review": "📝 برای بررسی شما فرستاده شد.",
            "held": "⏸ بررسی ایمنی رد کرد؛ بالا برای بررسی شما آمد.",
        }[outcome])
        return
    now = datetime.now(d.cfg.tz)
    day = now.date().isoformat()
    lines = [f"جدول امروز ({build.weekday_fa(now)} {build.jalali_date(now)})"]
    for e in d.s.entries:
        if now.weekday() not in e.days:
            continue
        st = d.state.done.get(f"{day}:{e.id}")
        icon = STATUS_ICON.get(st, "⏳" if d._target(e, now) > now - timedelta(minutes=d.s.late_minutes) else "—")
        lines.append(f"{icon} {build.fa(e.time)} · {e.id} ({e.kind})")
    lines.append("\n✅ منتشر/زمان‌بندی · 📝 برای بررسی · ⏸ مشکوک · ⏭ رد (داده نبود) · ⌛ جا ماند · ⏳ در انتظار")
    lines.append("ساخت دستی: /daily شناسه — فقط برای بررسی خودتان: /daily شناسه draft — بررسی منابع خبر: /dailycheck")
    await msg.reply_text("\n".join(lines))


async def cmd_dailycheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner(update, context):
        return
    d = _svc(context)
    await update.effective_message.reply_text("در حال تست منابع خبری…")
    d._cache.clear()
    lines = []
    async with new_http_client() as http:
        for src in [*d.news_sources, *d.capital_sources]:
            try:
                items = await fetch_source(http, src)
                fresh = [i for i in items if not i.published or i.published >= datetime.now(timezone.utc) - timedelta(hours=d.s.news_window_hours)]
                lines.append(f"✅ {src.name}: {len(items)} مطلب ({len(fresh)} مورد در {d.s.news_window_hours} ساعت اخیر)")
            except SourceError as e:
                lines.append(f"❌ {src.name}: {e}")
    if not lines:
        lines = ["هیچ منبع خبری تعریف نشده (news_sources.yaml و capital_sources.yaml)."]
    await update.effective_message.reply_text("\n".join(lines) + "\n\nمنبع ❌ را در news_sources.yaml پاک یا اصلاح کنید.")


async def _job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.application.bot_data["daily"].tick(context.bot)


def register(app: Application, cfg) -> None:
    """Hook for bot.build_app(). Adds /daily, /dailycheck and the clock."""
    settings = load_settings()
    app.bot_data["daily"] = Daily(app, cfg, settings)
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("daily", cmd_daily, filters=private))
    app.add_handler(CommandHandler("dailycheck", cmd_dailycheck, filters=private))
    if settings.enabled:
        app.job_queue.run_repeating(_job, interval=45, first=30, name="daily")
