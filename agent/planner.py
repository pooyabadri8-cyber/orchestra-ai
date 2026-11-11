"""Generic weekly content calendar (any industry): fixed days and times, each entry writes one post.

calendar.yaml (per customer):

    enabled: true
    lead_minutes: 2          # start writing this long before the time
    late_minutes: 30         # too late (bot was off): skip today's post and tell the owners
    state_file: data/calendar.json
    schedule:
      - id: tip
        kind: topic          # a post about a theme, written from the knowledge base only
        days: [sat, mon, wed]
        time: "11:00"
        pillar: "نکتهٔ قهوه"             # optional: a pillar of config.yaml
        task: "یک نکتهٔ کوتاه دربارهٔ {angle}"   # {angle} = the next item of `angles`, rotating
        angles: ["دانه", "آسیاب", "دم‌آوری"]
      - id: weekly
        kind: digest         # a summary of what the customer's sources published recently
        days: [fri]
        time: "17:00"
        window_hours: 96
        task: "خلاصهٔ مهم‌ترین خبرهای این هفته"

The finished post goes through the same pipeline as every other post: safety check, the customer's approval level
(review / trusted / auto), retry, dead-letter queue. Nothing in this file is industry-specific.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from .sources import Item, SourceError, fetch_source, new_http_client
from .writer import LLMError

log = logging.getLogger("content-agent.planner")

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
KINDS = ("topic", "digest")
PILLAR_PREFIX = "daily:"  # marks calendar posts: they do not count against the per-day cap of source posts, and are dropped if very late
RETRY_EVERY = 150  # seconds between attempts for one post


@dataclass
class Entry:
    id: str
    kind: str
    time: str
    days: list[int]
    task: str
    pillar: str | None = None
    angles: list[str] = field(default_factory=list)
    window_hours: int = 72
    max_items: int = 25


@dataclass
class PlannerSettings:
    enabled: bool = True
    lead_minutes: int = 2
    late_minutes: int = 30
    state_file: str = "data/calendar.json"
    entries: list[Entry] = field(default_factory=list)


def _days(value, where: str) -> list[int]:
    if value in (None, "all"):
        return list(range(7))
    out = []
    for x in value:
        k = str(x).lower()[:3]
        if k not in WEEKDAYS:
            raise SystemExit(f"calendar.yaml، {where}: نام روز نامعتبر «{x}»؛ از mon,tue,wed,thu,fri,sat,sun یا all استفاده کنید.")
        out.append(WEEKDAYS[k])
    return out


def load_settings(path: str | Path = "calendar.yaml") -> PlannerSettings:
    p = Path(path)
    if not p.is_file():
        return PlannerSettings(enabled=False)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    s = PlannerSettings(
        enabled=bool(raw.get("enabled", True)), lead_minutes=int(raw.get("lead_minutes", 2)),
        late_minutes=int(raw.get("late_minutes", 30)), state_file=str(raw.get("state_file", "data/calendar.json")),
    )
    seen: set[str] = set()
    for i, e in enumerate(raw.get("schedule") or [], 1):
        where = f"مورد {i}"
        eid, kind = str(e.get("id", "")).strip(), str(e.get("kind", "topic")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", eid):
            raise SystemExit(f"calendar.yaml، {where}: id باید حروف انگلیسی، عدد، _ یا - باشد.")
        if eid in seen:
            raise SystemExit(f"calendar.yaml: شناسهٔ تکراری «{eid}».")
        seen.add(eid)
        if kind not in KINDS:
            raise SystemExit(f"calendar.yaml، {where}: kind باید یکی از {', '.join(KINDS)} باشد.")
        t = str(e.get("time", ""))
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise SystemExit(f"calendar.yaml، {where}: ساعت «{t}» نامعتبر است (قالب HH:MM).")
        task = str(e.get("task", "")).strip()
        if not task:
            raise SystemExit(f"calendar.yaml، {where}: task (دستور نوشتن) لازم است.")
        s.entries.append(Entry(
            id=eid, kind=kind, time=t, days=_days(e.get("days"), where), task=task, pillar=e.get("pillar"),
            angles=[str(a) for a in e.get("angles") or []], window_hours=int(e.get("window_hours", 72)),
            max_items=int(e.get("max_items", 25)),
        ))
    return s


class Skip(Exception):
    """Nothing worth posting this time (no recent items / the model said SKIP). Not an error."""


class PlannerState:
    """Which post of which day is done, and which angle comes next. Survives restarts: a restart never causes a double post."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.done: dict[str, str] = {}
        self.rot: dict[str, int] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.done, self.rot = dict(data.get("done", {})), dict(data.get("rot", {}))
        except (OSError, ValueError, AttributeError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            keep = sorted(self.done)[-300:]
            tmp.write_text(json.dumps({"done": {k: self.done[k] for k in keep}, "rot": self.rot}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    def mark(self, key: str, status: str) -> None:
        self.done[key] = status
        self.save()


def digest_material(items: list[Item], limit: int, snippet: int = 260) -> str:
    """Numbered items; the model never sees which site or channel an item came from."""
    out = []
    for i, it in enumerate(items[:limit], 1):
        body = re.sub(r"\s+", " ", it.text or "").strip()
        title = re.sub(r"\s+", " ", it.title or "").strip()
        if body.startswith(title[:40]):
            body = body[len(title):].strip()
        out.append(f"[{i}] {title}" + (f"\n    {body[:snippet]}" if body else ""))
    return "\n".join(out)


class Planner:
    def __init__(self, app: Application, cfg, settings: PlannerSettings):
        self.app, self.cfg, self.s = app, cfg, settings
        self.state = PlannerState(settings.state_file)
        self._tries: dict[str, tuple[int, float, str]] = {}
        self._lock = asyncio.Lock()

    @property
    def store(self):
        return self.app.bot_data["store"]

    @property
    def writer(self):
        return self.app.bot_data["writer"]

    def _target(self, entry: Entry, now_local: datetime) -> datetime:
        h, m = entry.time.split(":")
        return now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    def next_angle(self, entry: Entry, advance: bool) -> str:
        if not entry.angles:
            return ""
        n = self.state.rot.get(entry.id, 0)
        if advance:
            self.state.rot[entry.id] = n + 1
            self.state.save()
        return entry.angles[n % len(entry.angles)]

    async def make(self, entry: Entry, advance: bool = True) -> tuple[str, str]:
        """(post text, topic). Raises Skip / LLMError / SourceError."""
        if entry.kind == "topic":
            angle = self.next_angle(entry, advance)
            topic = entry.task.replace("{angle}", angle).strip()
            text = await self.writer.draft(topic, entry.pillar, avoid=self.store.recent_topics())
            return text, topic
        items: list[Item] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=entry.window_hours)
        async with new_http_client() as http:
            for src in self.app.bot_data.get("sources", []):
                try:
                    got = await fetch_source(http, src)
                except SourceError as e:
                    log.info("calendar %s: source %s: %s", entry.id, src.name, e)
                    continue
                items += [it for it in got if not it.published or it.published >= cutoff]
        items = self.cfg.exclusions.keep_items(items)
        if not items:
            raise Skip("خبری در بازهٔ مورد نظر از منابع نیامد.")
        items.sort(key=lambda i: i.published or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        text = await self.writer.compose(entry.task, digest_material(items, entry.max_items), "", max_chars=self.cfg.source_max_chars)
        if text is None:
            raise Skip("مدل خبر کافی برای نوشتن این پست ندید.")
        return text, entry.task

    async def run_entry(self, bot, entry: Entry, review_only: bool = False) -> str:
        from . import bot as botmod  # late import: bot.py imports this module

        text, topic = await self.make(entry, advance=not review_only)
        post = self.store.add_draft(topic, PILLAR_PREFIX + entry.id, text, datetime.now(timezone.utc))
        chats = sorted(self.cfg.owner_ids)
        if review_only:
            for cid in chats:
                await botmod.send_review(bot, cid, post, self.cfg)
            return "review"
        banned = [s.name for s in self.app.bot_data.get("sources", [])]
        return await botmod.dispatch_post(bot, self.cfg, self.store, self.app.bot_data["auto"], post, chats, banned, trusted=True)

    async def tick(self, bot, now: datetime | None = None) -> None:
        if not self.s.enabled or self._lock.locked():
            return
        async with self._lock:
            if self.app.bot_data["auto"].paused:
                return
            now = now or datetime.now(self.cfg.tz)
            day = now.date().isoformat()
            for entry in self.s.entries:
                key = f"{day}:{entry.id}"
                if now.weekday() not in entry.days or key in self.state.done:
                    continue
                target = self._target(entry, now)
                if now < target - timedelta(minutes=self.s.lead_minutes):
                    continue
                n, last, err = self._tries.get(key, (0, 0.0, ""))
                if now > target + timedelta(minutes=self.s.late_minutes):
                    self.state.mark(key, "missed")
                    if err:
                        await self._notify(bot, f"⚠️ پست برنامه‌ای «{entry.id}» امروز ساخته نشد: {err}")
                    continue
                if n and time.monotonic() - last < RETRY_EVERY:
                    continue
                try:
                    outcome = await self.run_entry(bot, entry)
                except Skip as e:
                    self.state.mark(key, "skipped")
                    log.info("calendar %s skipped: %s", key, e)
                except (LLMError, SourceError) as e:
                    self._tries[key] = (n + 1, time.monotonic(), str(e)[:300])
                    log.warning("calendar %s failed: %s", key, e)
                except Exception as e:  # never let one bad post stop the others
                    self._tries[key] = (n + 1, time.monotonic(), f"{type(e).__name__}: {e}"[:300])
                    log.exception("calendar %s crashed", key)
                else:
                    self.state.mark(key, outcome)
                    self._tries.pop(key, None)

    async def _notify(self, bot, text: str) -> None:
        from . import bot as botmod

        await botmod.notify_owners(bot, self.cfg, html.escape(text), silent=True)


# ---------------------------------------------------------------- commands

DAY_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # python weekday() order
ICON = {"auto": "✅", "review": "📝", "held": "⏸", "skipped": "⏭", "missed": "⌛"}


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Planner:
    return context.application.bot_data["planner"]


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = _svc(context)
    u = update.effective_user
    if not (u and u.id in p.cfg.owner_ids):
        return
    msg = update.effective_message
    args = [a.lower() for a in context.args]
    if args:
        entry = next((e for e in p.s.entries if e.id.lower() == args[0]), None)
        if not entry:
            await msg.reply_text("شناسه پیدا نشد. فهرست: /calendar")
            return
        await msg.reply_text("در حال ساخت… (چند دقیقه طول می‌کشد)")
        try:
            outcome = await p.run_entry(context.bot, entry, review_only=len(args) > 1 and args[1] in ("draft", "review", "پیش‌نویس"))
        except (Skip, LLMError, SourceError) as e:
            await msg.reply_text(f"ساخته نشد: {e}")
            return
        await msg.reply_text({"auto": "✅ زمان‌بندی شد و تا چند ثانیه دیگر منتشر می‌شود.", "review": "📝 برای بررسی شما فرستاده شد.",
                              "held": "⏸ بررسی ایمنی رد کرد؛ بالا برای بررسی شما آمد."}[outcome])
        return
    now = datetime.now(p.cfg.tz)
    if not p.s.entries:
        await msg.reply_text("برنامه‌ای در calendar.yaml تعریف نشده.")
        return
    lines = ["برنامهٔ هفتگی:"]
    for d in range(7):
        day = (now.weekday() + d) % 7
        for e in sorted((x for x in p.s.entries if day in x.days), key=lambda x: x.time):
            date = (now + timedelta(days=d)).date().isoformat()
            st = p.state.done.get(f"{date}:{e.id}")
            lines.append(f"{ICON.get(st, '⏳' if d else '•')} {DAY_FA[day]} {e.time} · {e.id} ({e.kind})")
    lines.append("\n✅ منتشر/زمان‌بندی · 📝 برای بررسی · ⏸ مشکوک · ⏭ رد · ⌛ جا ماند · ⏳ در انتظار")
    lines.append("ساخت دستی: /calendar شناسه — فقط برای بررسی: /calendar شناسه draft")
    await msg.reply_text("\n".join(lines))


async def _job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _svc(context).tick(context.bot)


def register(app: Application, cfg) -> None:
    settings = load_settings()
    app.bot_data["planner"] = Planner(app, cfg, settings)
    app.add_handler(CommandHandler("calendar", cmd_calendar, filters=filters.ChatType.PRIVATE))
    if settings.enabled:
        app.job_queue.run_repeating(_job, interval=45, first=40, name="planner")
