"""The control bot: the owner works with the team from their phone.

In the control chat (Telegram or Bale) the owner can
  - write a request in plain Persian: the Manager plans it and the team prepares the content;
  - see exactly what would be published (every channel's text, the Instagram caption and slides);
  - approve or reject with one button (or by typing «تأیید ۱۲» / «رد ۱۲ دلیل»), or send it back with a note
    («اصلاح ۱۲ تیتر کوتاه‌تر شود»): the team makes a new version from the same data;
  - be told what happened to every publish job, and act on the unclear ones (/sent, /retry);
  - stop everything (/pause) and continue (/resume);
  - ask the marketing agent for the channel's content calendar (/plan), approve it, send it back with a note, or reject
    it. Each item of an approved plan starts by itself before its time and still comes back for its own approval.
A daily schedule (orgs/<org>/schedule.yaml) can start requests by itself; they still wait for approval.

Safety rules kept here:
  - only the numeric ids in ORCHESTRA_CONTROL_CHAT_IDS are obeyed; anyone else is ignored;
  - an approval is bound to the package hash the owner was shown; a button on older content does nothing;
  - a prompt is decided once: the state moves pending -> deciding in one database statement;
  - updates are acknowledged after they are handled, and every action is safe to repeat, so a restart loses nothing
    and repeats nothing.

`ControlService.tick()` is one round of work; `run_forever()` repeats it. The workflow engine is behind the small
`Engine` interface, so the logic is tested without it.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..assets import AssetStore
from ..agents import marketing as mk
from ..agents import reporting as rp
from .. import lessons as memory
from ..crm import store as crm
from ..db import (
    ContentPlanRow, ControlJobReport, ControlPrompt, ControlRequest, ControlState, OrgControl, PackageRow, PublishJob, Run,
    utcnow,
)
from ..registry import DEFAULT_ROOT, Registry
from .botapi import BotApi, BotApiError, split_text

log = logging.getLogger("orchestra.control")
TEHRAN = ZoneInfo("Asia/Tehran")
WORKING = ("starting", "planning", "running")
JOB_FINAL = ("confirmed", "failed", "unknown", "dead", "blocked", "expired", "cancelled")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # date.weekday() order
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
SCHEDULE_WINDOW = timedelta(hours=4)
PLAN_LATE = mk.PLAN_LATE

HELP = """سلام! من ربات کنترل تیم محتوا هستم.

• هر درخواستی را ساده بنویسید، مثل:
  «گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن»
• وقتی محتوا آماده شد، متن و اسلایدها را همین‌جا می‌فرستم و با دکمه تأیید یا رد می‌کنید.
• اگر تغییری می‌خواهید دکمهٔ «اصلاح» را بزنید و بنویسید چه چیزی عوض شود؛ نسخهٔ جدید می‌آید.

دستورها:
/status — آخرین کارها
/jobs — وضعیت ارسال‌ها
/pause — توقف فوری همهٔ انتشارها
/resume — ادامهٔ انتشار
/sent شماره — «این پست روی کانال هست» (برای ارسال نامعلوم)
/retry شماره — دوباره بفرست
/plan — برنامهٔ محتوای هفتهٔ بعد (/plan فردا: از فردا)
/plans — برنامهٔ تأییدشده و پست‌های پیش رو
/report — گزارش عملکرد هفت روز گذشته (/report امروز، /report 30)
/leads — سرنخ‌های باز تامین مالی
/lead شماره — جزئیات و سابقهٔ یک سرنخ
/note شماره متن — یادداشت روی سرنخ
/lessons — قاعده‌هایی که تیم از بازخوردهای شما به خاطر سپرده است
/lesson متن — یک قاعدهٔ همیشگی تازه برای نویسنده
/cancel — انصراف از نوشتن توضیح اصلاح"""


class Engine(Protocol):
    def start_request(self, org_id: str, text: str, *, new: bool, preset: dict | None = None,
                      publish_at: datetime | None = None) -> str: ...
    def propose_plan(self, org_id: str, *, start: date | None, schedule: list, note: str, revise_of: int | None,
                     actor: str) -> int: ...
    def can_resume(self, run_id: str) -> bool: ...
    def is_alive(self, run_id: str) -> bool: ...
    def decide(self, run_id: str, package_hash: str, decision: str, actor: str, reason: str | None) -> dict: ...
    def publish_due(self) -> dict[int, str]: ...
    def set_paused(self, org_id: str, paused: bool, actor: str) -> None: ...
    def confirm_sent(self, job_id: int, actor: str) -> bool: ...
    def requeue(self, job_id: int, actor: str) -> bool: ...


@dataclass(frozen=True)
class ScheduleEntry:
    name: str
    at: tuple[int, int]
    days: tuple[str, ...]
    request: str


def load_schedule(path: Path) -> list[ScheduleEntry]:
    """Read orgs/<org>/schedule.yaml. A broken file is an error at start-up, never a silently missing report."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for i, e in enumerate(data.get("schedule") or []):
        if not e.get("enabled", True):
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(e.get("time", "")))
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"{path}: مورد {i + 1}: time باید مثل \"12:45\" باشد")
        days = tuple(str(d).lower()[:3] for d in (e.get("days") or DAYS))
        if any(d not in DAYS for d in days):
            raise ValueError(f"{path}: مورد {i + 1}: days فقط از {', '.join(DAYS)}")
        request = str(e.get("request") or "").strip()
        name = str(e.get("name") or "").strip()
        if not request or not re.fullmatch(r"[a-z0-9_]{1,40}", name):
            raise ValueError(f"{path}: مورد {i + 1}: name (حروف کوچک انگلیسی/عدد/_) و request لازم است")
        out.append(ScheduleEntry(name, (int(m.group(1)), int(m.group(2))), days, request))
    if len({e.name for e in out}) != len(out):
        raise ValueError(f"{path}: نام‌های schedule باید یکتا باشند")
    return out


def plain(body: str, parse_mode: str) -> str:
    """Channel text without its formatting marks (HTML tags, Markdown links and bold)."""
    if parse_mode == "HTML":
        return html.unescape(re.sub(r"<[^>]+>", "", body))
    if parse_mode == "Markdown":
        return re.sub(r"\*([^*\n]+)\*", r"\1", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body))
    return body


def build_preview(content: dict, control_platform: str) -> list[tuple]:
    """What the owner is shown: ("text", text, parse_mode|None) and ("album", [asset refs]) items, in order.
    Channels that get the same words (Telegram and Bale differ only in formatting marks) are shown once; the text is
    shown formatted when the control chat can render it, otherwise as plain text."""
    assets = {a["asset_id"]: a for a in content.get("assets") or []}
    groups: dict[tuple, dict] = {}
    for d in content["destinations"]:
        step = d["delivery_plan"][0]
        mode = step.get("parse_mode", "HTML")
        asset_ids = tuple(step.get("asset_ids") or ())
        g = groups.setdefault((plain(step["body"], mode), asset_ids), {"dests": [], "platforms": [], "shown": None})
        g["dests"].append(d["destination_id"])
        if d["platform"] not in g["platforms"]:
            g["platforms"].append(d["platform"])
        if d["platform"] == control_platform:
            # Telegram renders HTML when asked; Bale renders its Markdown by itself
            g["shown"] = (step["body"], "HTML") if (control_platform, mode) == ("telegram", "HTML") else \
                         (step["body"], None) if (control_platform, mode) == ("bale", "Markdown") else None
    items: list[tuple] = []
    for (text, asset_ids), g in groups.items():
        items.append(("text", f"📣 {'، '.join(g['platforms'])} — {'، '.join(g['dests'])}", None))
        if asset_ids:
            items.append(("album", [assets[a] for a in asset_ids if a in assets]))
        body, mode = g["shown"] or (text, None)
        items.append(("text", body, mode))
    return items


def _round(run: Run) -> int:
    return int((run.detail or {}).get("approval_round") or 0)


def _num(text: str) -> int | None:
    m = re.search(r"\d+", text.translate(FA_DIGITS))
    return int(m.group()) if m else None


@dataclass
class ControlService:
    org_id: str
    api: BotApi
    engine: Engine
    sessions: Callable
    assets: AssetStore
    owners: tuple[str, ...]
    schedule: list[ScheduleEntry] = field(default_factory=list)
    poll_seconds: int = 5
    clock: Callable[[], datetime] = utcnow
    orgs_root: Path = Path("orgs")
    registry_root: Path = DEFAULT_ROOT
    crm_cfg: "crm.CrmConfig | None" = None
    extra_ticks: list = field(default_factory=list)  # e.g. the public intake bot: its failures never stop this one
    _failures: dict[str, tuple[int, float]] = field(default_factory=dict)

    # ------------------------------------------------------------------ loop

    def recover(self) -> None:
        """Once at start-up: tidy what a stopped process left behind. A prompt that was never fully shown is voided
        (the content is shown again under a new number); a decision that was being recorded is answerable again unless the run has moved on."""
        with self.sessions.begin() as s:
            s.execute(update(ControlPrompt).where(ControlPrompt.org_id == self.org_id, ControlPrompt.state == "sending")
                      .values(state="void"))
            s.execute(update(ControlPrompt).where(ControlPrompt.org_id == self.org_id, ControlPrompt.state == "deciding")
                      .values(state="pending"))

    def run_forever(self) -> None:
        self.recover()
        while True:
            try:
                self.tick()
            except BotApiError as e:
                if e.code == 409 or "Conflict" in str(e):
                    log.warning("یک برنامهٔ دیگر هم با همین ربات پیام می‌گیرد (روی کامپیوتر دیگر، یا پنجرهٔ دیگری که "
                                "کوچک شده است). فقط یکی را روشن نگه دارید؛ تا آن موقع پیام‌ها ممکن است به این یکی نرسند.")
                else:
                    log.warning("به ربات کنترل وصل نشد (%s)؛ چند ثانیهٔ دیگر دوباره تلاش می‌کنم.", e)
                time.sleep(10)
            except Exception:  # noqa: BLE001  one bad round must not stop the service
                log.exception("control round failed")
                time.sleep(5)

    def tick(self) -> None:
        self.read_updates()
        self.run_schedule()
        for t in self.extra_ticks:
            try:
                t()
            except Exception as e:  # noqa: BLE001  a background task failing must never stop the control bot
                log.warning("background task (intake bot or news collection): %s", e)
        for step in (self.auto_propose_plan, self.run_plans, self.auto_report, self.remind_leads):  # never stops approvals or publishing
            try:
                step()
            except BotApiError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("content plan step failed")
        self.ask_for_approvals()
        self.close_stale_prompts()
        self.report_requests()
        self.engine.publish_due()
        self.report_jobs()

    # ------------------------------------------------------------------ small helpers

    def _state(self, key: str, default: str = "") -> str:
        with self.sessions() as s:
            row = s.get(ControlState, key)
            return row.value if row else default

    def _set_state(self, key: str, value: str) -> None:
        with self.sessions.begin() as s:
            s.merge(ControlState(key=key, value=value))

    def _say(self, chat_id: str | None, text: str, **kw) -> None:
        """Send to one chat, or to every owner when chat_id is None. A chat that cannot be reached is logged."""
        for chat in ([chat_id] if chat_id else self.owners):
            for part in split_text(text):
                try:
                    self.api.send_text(chat, part, **kw)
                except BotApiError as e:
                    log.warning("could not write to %s: %s", chat, e)
                    if e.retryable:
                        raise

    # ------------------------------------------------------------------ incoming

    def read_updates(self) -> None:
        offset = int(self._state("offset", "0"))
        for u in self.api.get_updates(offset, self.poll_seconds):
            try:
                self.handle_update(u)
            except BotApiError:
                raise  # the chat is unreachable: keep the update for the next round
            except Exception:  # noqa: BLE001
                log.exception("update %s failed", u.get("update_id"))
            self._set_state("offset", str(int(u["update_id"]) + 1))

    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            user = str((cq.get("from") or {}).get("id", ""))
            chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", "") or user)
            if user not in self.owners:
                return
            action, _, pid = str(cq.get("data") or "").partition(":")
            if action in ("ok", "no") and pid.isdigit():
                self.api.answer_button(str(cq.get("id")), "در حال ثبت…")
                self.decide(int(pid), "approved" if action == "ok" else "rejected", user, chat, None)
            elif action == "ed" and pid.isdigit():
                self.api.answer_button(str(cq.get("id")))
                self.ask_note(int(pid), chat)
            elif action in ("pok", "pno") and pid.isdigit():
                self.api.answer_button(str(cq.get("id")), "در حال ثبت…")
                self.decide_plan(int(pid), "approved" if action == "pok" else "rejected", user, chat)
            elif action == "ped" and pid.isdigit():
                self.api.answer_button(str(cq.get("id")))
                self.ask_plan_note(int(pid), chat)
            elif action in ("lk", "lx", "lr") and pid.isdigit():
                self.api.answer_button(str(cq.get("id")), "ثبت شد")
                self.decide_lesson(int(pid), action, f"{self.api.platform}:{user}", chat)
            elif action == "ld" and self.crm_cfg is not None:
                lead_id, _, stage = pid.partition(":")
                if lead_id.isdigit():
                    self.api.answer_button(str(cq.get("id")), "ثبت شد")
                    self.move_lead(int(lead_id), stage, f"{self.api.platform}:{user}", chat)
            return
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id", ""))
        user = str((msg.get("from") or {}).get("id", "") or chat)
        if not text or not chat:
            return
        if not self.owners:
            self.api.send_text(chat, f"شناسهٔ شما: {user}\nاین عدد را در فایل .env جلوی ORCHESTRA_CONTROL_CHAT_IDS بنویسید و برنامه را دوباره اجرا کنید.")
            return
        if user not in self.owners or chat not in self.owners:
            log.warning("ignored a message from %s in chat %s", user, chat)
            return
        self.handle_text(text, user, chat, origin=f"update:{u['update_id']}")

    def handle_text(self, text: str, user: str, chat: str, origin: str) -> None:
        actor = f"{self.api.platform}:{user}"
        word = text.split()[0].lower().split("@")[0]
        rest = text[len(text.split()[0]):].strip()
        pending_note = self._state(f"note:{chat}")
        if pending_note and not word.startswith("/"):  # the owner pressed «اصلاح»: this message is the note
            self._set_state(f"note:{chat}", "")
            if pending_note.startswith("plan:"):
                self.make_plan(chat, None, note=text, revise_of=int(pending_note[5:]), actor=actor)
            else:
                self.decide(int(pending_note), "revise", user, chat, text)
        elif word == "/cancel":
            self._set_state(f"note:{chat}", "")
            self._say(chat, "انصراف ثبت شد." if pending_note else "چیزی برای انصراف نیست.")
        elif word in ("/start", "/help"):
            self._say(chat, HELP)
        elif word == "/status":
            self._say(chat, self.status_text())
        elif word == "/jobs":
            self._say(chat, self.jobs_text())
        elif word == "/plan":
            today = self.clock().astimezone(TEHRAN).date()
            self.make_plan(chat, today + timedelta(days=1) if rest in ("فردا", "tomorrow") else None, note="", revise_of=None,
                           actor=actor)
        elif word == "/plans":
            self._say(chat, self.plans_text())
        elif word == "/lessons":
            self.show_lessons(chat)
        elif word == "/lesson":
            if not rest:
                self._say(chat, "متن قاعده را بنویسید، مثل: /lesson تیتر حداکثر ده کلمه باشد")
            else:  # typed by the owner: that is the decision, no second question
                with self.sessions.begin() as s:
                    row = memory.offer(s, self.org_id, "writing", rest, "typed")
                    lid = row.id if row is not None else None
                if lid is None:
                    self._say(chat, f"این متن قاعده نمی‌شود (خالی، بیش از {memory.MAX_LEN} نویسه، یا از قبل هست).")
                else:
                    self.decide_lesson(lid, "lk", actor, chat)
        elif word in ("/leads", "/lead", "/note"):
            if self.crm_cfg is None:
                self._say(chat, "CRM تنظیم نشده است (orgs/<org>/crm.yaml).")
            elif word == "/leads":
                self._say(chat, self.leads_text())
            else:
                n = _num(rest)
                if n is None:
                    self._say(chat, f"شمارهٔ سرنخ را بنویسید، مثل: {word} 12" + (" متن یادداشت" if word == "/note" else ""))
                elif word == "/lead":
                    self.show_lead(n, chat)
                else:
                    note = re.sub(r"^\s*[\d۰-۹٠-٩]+\s*", "", rest)
                    try:
                        with self.sessions.begin() as s:
                            crm.add_note(s, self.crm_cfg, n, note, actor, self.clock())
                        self._say(chat, f"یادداشت روی سرنخ {n} ثبت شد.")
                    except crm.CrmError as e:
                        self._say(chat, str(e))
        elif word == "/report":
            days = 1 if rest in ("امروز", "today") else (_num(rest) or 7)
            self._say(chat, self.report_text(min(days, rp.MAX_DAYS)))
        elif word in ("/pause", "/resume"):
            self.engine.set_paused(self.org_id, word == "/pause", actor)
            self._say(chat, "⏸ همهٔ انتشارها متوقف شد." if word == "/pause" else "▶️ انتشار ادامه پیدا کرد.")
        elif word in ("/sent", "/retry"):
            n = _num(rest)
            if n is None:
                self._say(chat, f"شمارهٔ ارسال را بنویسید، مثل: {word} 12")
            elif word == "/sent":
                self._say(chat, "ثبت شد." if self.engine.confirm_sent(n, actor) else "این ارسال در وضعیت «نامعلوم» نیست.")
            else:
                self._say(chat, "دوباره در صف قرار گرفت." if self.engine.requeue(n, actor) else "این ارسال قابل فرستادن دوباره نیست.")
        elif word in ("تأیید", "تایید", "رد", "اصلاح"):
            n = _num(rest)
            if n is None:
                self._say(chat, f"شمارهٔ درخواست تأیید را هم بنویسید، مثل: {word} 12")
                return
            reason = re.sub(r"^\s*[\d۰-۹٠-٩]+\s*", "", rest).strip() or None
            if word == "اصلاح" and not reason:
                self.ask_note(n, chat)
                return
            self.decide(n, {"رد": "rejected", "اصلاح": "revise"}.get(word, "approved"), user, chat, reason)
        elif word.startswith("/"):
            self._say(chat, "این دستور را نمی‌شناسم. /help را بزنید.")
        else:
            self.start_request(text, chat, origin, new=True)

    # ------------------------------------------------------------------ requests

    def start_request(self, text: str, chat: str | None, origin: str, *, new: bool, preset: dict | None = None,
                      publish_at: datetime | None = None, ack: str | None = None) -> None:
        with self.sessions() as s:
            if s.execute(select(ControlRequest.id).where(ControlRequest.origin == origin)).first():
                return  # this message was already taken (a replay after a restart)
        if preset is None:
            run_id = self.engine.start_request(self.org_id, text, new=new)
        else:
            run_id = self.engine.start_request(self.org_id, text, new=new, preset=preset, publish_at=publish_at)
        try:
            with self.sessions.begin() as s:
                s.add(ControlRequest(org_id=self.org_id, origin=origin, chat_id=chat, run_id=run_id, text=text))
        except IntegrityError:
            return
        self._say(chat, ack or "دریافت شد ✅ تیم مشغول آماده‌سازی است؛ نتیجه را همین‌جا می‌فرستم.")

    def run_schedule(self) -> None:
        now = self.clock().astimezone(TEHRAN)
        for e in self.schedule:
            start = now.replace(hour=e.at[0], minute=e.at[1], second=0, microsecond=0)
            if DAYS[now.weekday()] not in e.days or not (start <= now < start + SCHEDULE_WINDOW):
                continue
            self.start_request(e.request, None, f"schedule:{e.name}:{now.date().isoformat()}", new=False)

    def report_requests(self) -> None:
        with self.sessions() as s:
            rows = s.execute(select(ControlRequest).where(ControlRequest.org_id == self.org_id,
                                                          ControlRequest.reported.is_(False))).scalars().all()
            runs = {r.id: s.get(Run, r.run_id) for r in rows}
        for r in rows:
            run = runs[r.id]
            if run is not None and run.status in WORKING and not self.engine.is_alive(r.run_id):
                run = self._mark_stopped(r.run_id)  # the engine gave up on it: say so instead of waiting forever
            if run is None or run.status in WORKING:
                continue
            if run.status not in ("awaiting_approval", "approved", "rejected"):  # those are told by the approval prompt
                self._say(r.chat_id, f"درخواست: «{r.text[:120]}»\n{describe_run(run)}")
            with self.sessions.begin() as s:
                s.execute(update(ControlRequest).where(ControlRequest.id == r.id).values(reported=True))

    def _mark_stopped(self, run_id: str) -> Run | None:
        with self.sessions.begin() as s:
            run = s.get(Run, run_id)  # read again: the last step may have finished it in the meantime
            if run is not None and run.status in WORKING:
                run.status = "failed"
                run.detail = {**(run.detail or {}), "error": "اجرا متوقف شد و ادامه پیدا نکرد؛ درخواست را دوباره بفرستید."}
            return run

    # ------------------------------------------------------------------ approvals

    def ask_for_approvals(self) -> None:
        with self.sessions() as s:
            runs = s.execute(select(Run).where(Run.org_id == self.org_id, Run.status == "awaiting_approval",
                                               Run.package_id.is_not(None))).scalars().all()
            todo = []
            for run in runs:
                pkg = s.get(PackageRow, run.package_id)
                if pkg is None or pkg.status != "active":
                    continue
                # asked already in this round? (a void or closed question does not count: the owner must not be
                # left with content that waits and nothing to press)
                if s.execute(select(ControlPrompt.id).where(ControlPrompt.run_id == run.id, ControlPrompt.round == _round(run),
                                                            ControlPrompt.state.notin_(("void", "closed")))).first():
                    continue
                todo.append((run, pkg))
        for run, pkg in todo:
            fails, wait_until = self._failures.get(run.id, (0, 0.0))
            if time.monotonic() < wait_until or not self.owners or not self.engine.can_resume(run.id):
                continue
            try:
                self.send_prompt(run, pkg)
                self._failures.pop(run.id, None)
            except BotApiError as e:  # tried again later, less and less often
                log.warning("could not show %s for approval: %s", run.id, e)
                self._failures[run.id] = (fails + 1, time.monotonic() + min(600, 30 * 2 ** fails))

    def send_prompt(self, run: Run, pkg: PackageRow) -> None:
        content = json.loads(pkg.content_json)
        with self.sessions.begin() as s:  # the prompt exists before anything is sent: its number goes on the buttons
            prompt = ControlPrompt(org_id=self.org_id, run_id=run.id, package_id=pkg.package_id,
                                   package_hash=pkg.package_hash, round=_round(run), state="sending")
            s.add(prompt)
            s.flush()
            pid = prompt.id
        try:
            messages = []
            request = (run.detail or {}).get("request")
            detail = run.detail or {}
            head = "📋 محتوای آماده برای تأیید" + (f" — نسخهٔ {pkg.version}" if pkg.version > 1 else "")
            head += f"\nدرخواست: «{request[:200]}»" if request else ""
            if detail.get("revision_error"):
                head = (f"⚠️ نسخهٔ جدید ساخته نشد: {str(detail['revision_error'])[:400]}\n"
                        f"نسخهٔ قبلی همچنان منتظر تصمیم شماست.\n\n{head}")
            can_revise = run.template != "market_close" and bool(detail.get("can_revise"))
            notes = [f"⚠️ {f['message']}" for f in run.findings or [] if f.get("severity") != "block"]
            for chat in self.owners:
                self.api.send_text(chat, "\n".join([head, *notes[:8]]))
                for item in build_preview(content, self.api.platform):
                    if item[0] == "album":
                        images = [(f"{a['order']:02d}.jpg", self.assets.get(a["sha256"]), a["mime"]) for a in item[1]]
                        if images:
                            self.api.send_album(chat, images)
                    else:
                        for part in split_text(item[1]):
                            self.api.send_text(chat, part, parse_mode=item[2])
                dests = "، ".join(d["destination_id"] for d in content["destinations"])
                typed = f"«تأیید {pid}» / «رد {pid} دلیل»" + (f" / «اصلاح {pid} توضیح»" if can_revise else "")
                question = (f"درخواست تأیید شمارهٔ {pid}\nمقصدها: {dests}\nشناسهٔ بسته: {pkg.package_hash[:12]}\n\n"
                            f"منتشر شود؟ (یا بنویسید: {typed})")
                row = [("✅ تأیید و انتشار", f"ok:{pid}"), *([("✏️ اصلاح", f"ed:{pid}")] if can_revise else []), ("❌ رد", f"no:{pid}")]
                mid = self.api.send_text(chat, question, buttons=[row])
                messages.append([chat, mid])
        except Exception:
            with self.sessions.begin() as s:  # nothing half-shown stays answerable; its number is never used again
                s.execute(update(ControlPrompt).where(ControlPrompt.id == pid).values(state="void"))
            raise
        with self.sessions.begin() as s:
            s.execute(update(ControlPrompt).where(ControlPrompt.id == pid).values(state="pending", messages=messages))

    def decide(self, pid: int, decision: str, user: str, chat: str, reason: str | None) -> None:
        actor = f"{self.api.platform}:{user}"
        with self.sessions.begin() as s:
            claimed = s.execute(update(ControlPrompt).where(ControlPrompt.id == pid, ControlPrompt.org_id == self.org_id,
                                                            ControlPrompt.state == "pending")
                                .values(state="deciding", decided_by=actor)).rowcount
        if claimed != 1:
            self._say(chat, f"درخواست تأیید {pid} باز نیست (قبلاً تصمیم گرفته شده یا منقضی شده است).")
            return
        with self.sessions() as s:
            prompt = s.get(ControlPrompt, pid)
            run = s.get(Run, prompt.run_id)
        if run is None or run.status != "awaiting_approval" or run.package_id != prompt.package_id or _round(run) != prompt.round:
            self._finish_prompt(prompt, "closed", "⌛️ این محتوا دیگر منتظر تأیید نیست.")
            return
        if decision == "revise":
            reason = " ".join((reason or "").split())[:500]
            if not reason or not (run.detail or {}).get("can_revise") or run.template == "market_close":
                with self.sessions.begin() as s:  # still answerable: approve or reject
                    s.execute(update(ControlPrompt).where(ControlPrompt.id == pid).values(state="pending", decided_by=None))
                self._say(chat, "برای این محتوا دیگر اصلاح ممکن نیست؛ تأیید یا رد کنید." if reason else "توضیح اصلاح خالی است.")
                return
        try:
            result = self.engine.decide(prompt.run_id, prompt.package_hash, decision, actor, reason)
        except Exception as e:  # noqa: BLE001
            log.exception("decision for %s failed", prompt.run_id)
            # The decision may have reached the run. Asking again could collect a second, different answer while
            # the first is still on its way, so this question stays closed to buttons ("unclear") and is not repeated.
            self._finish_prompt(prompt, "unclear", f"⚠️ ثبت تصمیم کامل نشد ({type(e).__name__}). با /status وضعیت را ببینید.")
            return
        status = result.get("status")
        if status == "approved":
            self._finish_prompt(prompt, "approved", f"✅ درخواست {pid} تأیید شد؛ در صف انتشار است.")
        elif status == "revising":
            self._finish_prompt(prompt, "revising", f"✏️ درخواست {pid}: اصلاح ثبت شد؛ نسخهٔ جدید را همین‌جا می‌فرستم.\nتوضیح شما: {reason}")
            self.offer_lesson(reason or "", "writing", f"revise:{pid}", chat=chat)
        elif status == "rejected":
            self._finish_prompt(prompt, "rejected", f"❌ درخواست {pid} رد شد." + (f"\nدلیل: {reason}" if reason else ""))
            if reason:
                self.offer_lesson(reason, "writing", f"reject:{pid}", chat=chat)
        else:
            self._finish_prompt(prompt, "closed", f"⚠️ درخواست {pid}: {result.get('error') or status}")

    def ask_note(self, pid: int, chat: str) -> None:
        with self.sessions() as s:
            p = s.get(ControlPrompt, pid)
            run = s.get(Run, p.run_id) if p is not None and p.org_id == self.org_id else None
        if p is None or run is None or p.state != "pending":
            self._say(chat, f"درخواست تأیید {pid} باز نیست.")
        elif not (run.detail or {}).get("can_revise") or run.template == "market_close":
            self._say(chat, "برای این محتوا دیگر اصلاح ممکن نیست؛ تأیید یا رد کنید.")
        else:
            self._set_state(f"note:{chat}", str(pid))
            self._say(chat, f"بنویسید در درخواست {pid} چه چیزی عوض شود. پیام بعدی شما به نویسنده داده می‌شود. (انصراف: /cancel)")

    def _finish_prompt(self, prompt: ControlPrompt, state: str, text: str) -> None:
        with self.sessions.begin() as s:
            s.execute(update(ControlPrompt).where(ControlPrompt.id == prompt.id).values(state=state))
        for chat, mid in prompt.messages or []:
            if mid:
                self.api.replace_text(chat, mid, text)
        self._say(None, text)

    def close_stale_prompts(self) -> None:
        with self.sessions() as s:
            prompts = s.execute(select(ControlPrompt).where(ControlPrompt.org_id == self.org_id,
                                                            ControlPrompt.state == "pending")).scalars().all()
            stale = []
            for p in prompts:
                run = s.get(Run, p.run_id)
                if run is None or run.status != "awaiting_approval" or run.package_id != p.package_id or _round(run) != p.round:
                    stale.append((p, run.status if run else "حذف‌شده"))
        for p, status in stale:
            with self.sessions.begin() as s:
                n = s.execute(update(ControlPrompt).where(ControlPrompt.id == p.id, ControlPrompt.state == "pending")
                              .values(state="closed")).rowcount
            if n:
                why = "نسخهٔ تازه‌تری آمده است" if status == "awaiting_approval" else STATUS_FA.get(status, status)
                text = f"⌛️ درخواست تأیید {p.id} بسته شد ({why})."
                for chat, mid in p.messages or []:
                    if mid:
                        self.api.replace_text(chat, mid, text)
                self._say(None, text)

    # ------------------------------------------------------------------ content plans (marketing agent)

    def _registry(self) -> Registry:
        return Registry.load(self.registry_root, check_entrypoints=False)

    def _context(self):
        return mk.load_context(self.org_id, self.orgs_root, self._registry())

    def make_plan(self, chat: str | None, start: date | None, *, note: str, revise_of: int | None, actor: str) -> None:
        """Ask the marketing agent for a plan (or a new version of one) and show it for approval."""
        if revise_of is None and start is None:
            with self.sessions() as s:
                waiting = s.execute(select(ContentPlanRow.id).where(ContentPlanRow.org_id == self.org_id,
                                                                    ContentPlanRow.status == "proposed")).scalars().first()
            if waiting is not None:
                self._say(chat, f"برنامهٔ {waiting} هنوز منتظر تصمیم شماست؛ تأیید، اصلاح یا ردش کنید.")
                return
        self._say(chat, "🗓 کارمند بازاریابی در حال چیدن برنامه است…" if revise_of is None else
                  f"✏️ برنامهٔ {revise_of}: اصلاح ثبت شد؛ نسخهٔ جدید را می‌فرستم.\nتوضیح شما: {note}")
        try:
            plan_id = self.engine.propose_plan(self.org_id, start=start, schedule=self.schedule, note=note,
                                               revise_of=revise_of, actor=actor)
        except BotApiError:
            raise
        except Exception as e:  # noqa: BLE001  a model or rule failure: say why; a revised plan's old version still waits
            log.warning("plan failed: %s", e)
            msg = f"⚠️ برنامه ساخته نشد: {str(e)[:500]}"
            if revise_of is not None:
                msg += f"\nبرنامهٔ {revise_of} همچنان منتظر تصمیم شماست."
            self._say(chat, msg)
            return
        if revise_of is not None:
            self._close_plan_question(revise_of, f"⌛️ برنامهٔ {revise_of} با نسخهٔ جدید (شمارهٔ {plan_id}) جایگزین شد.", tell=False)
        self.show_plan(plan_id)
        if revise_of is not None and note:
            self.offer_lesson(note, "planning", f"plan:{revise_of}", chat=chat)

    def show_plan(self, plan_id: int) -> None:
        with self.sessions() as s:
            row = s.get(ContentPlanRow, plan_id)
        plan = mk.load_plan(row)
        policy, kb, _, _ = self._context()
        fixed = mk.fixed_items(self.schedule, plan.start, plan.days)
        can_revise = row.version <= mk.MAX_REVISIONS
        messages = []
        for chat in self.owners:
            for part in split_text(mk.plan_text(plan, policy, kb, fixed)):
                self.api.send_text(chat, part)
            question = (f"درخواست تأیید برنامهٔ شمارهٔ {plan_id}\nشناسهٔ برنامه: {row.plan_hash[:12]}\n\n"
                        "این برنامه اجرا شود؟ (هر پست جدا هم از شما تأیید می‌گیرد)")
            buttons = [("✅ تأیید برنامه", f"pok:{plan_id}"), *([("✏️ اصلاح", f"ped:{plan_id}")] if can_revise else []),
                       ("❌ رد", f"pno:{plan_id}")]
            messages.append([chat, self.api.send_text(chat, question, buttons=[buttons])])
        with self.sessions.begin() as s:
            s.execute(update(ContentPlanRow).where(ContentPlanRow.id == plan_id).values(messages=messages))

    def ask_plan_note(self, plan_id: int, chat: str) -> None:
        with self.sessions() as s:
            row = s.get(ContentPlanRow, plan_id)
        if row is None or row.org_id != self.org_id or row.status != "proposed":
            self._say(chat, f"برنامهٔ {plan_id} منتظر تصمیم نیست.")
        elif row.version > mk.MAX_REVISIONS:
            self._say(chat, "برای این برنامه دیگر اصلاح ممکن نیست؛ تأیید یا رد کنید.")
        else:
            self._set_state(f"note:{chat}", f"plan:{plan_id}")
            self._say(chat, f"بنویسید در برنامهٔ {plan_id} چه چیزی عوض شود (مثلاً «پست فراخوان عصر سه‌شنبه باشد»). (انصراف: /cancel)")

    def decide_plan(self, plan_id: int, decision: str, user: str, chat: str) -> None:
        actor = f"{self.api.platform}:{user}"
        today = self.clock().astimezone(TEHRAN).date()
        with self.sessions() as s:
            row = s.get(ContentPlanRow, plan_id)
        if row is None or row.org_id != self.org_id:
            self._say(chat, f"برنامهٔ {plan_id} پیدا نشد.")
            return
        try:
            with self.sessions.begin() as s:
                mk.decide_plan(s, plan_id, row.plan_hash, decision, actor, today)
        except mk.PlanError as e:
            self._say(chat, f"برنامهٔ {plan_id}: {e}")
            return
        if decision == "approved":
            plan = mk.load_plan(row)
            text = (f"✅ برنامهٔ {plan_id} تأیید شد: {len(plan.items)} پست تا {mk.long_date(plan.end)}. "
                    "هر پست پیش از ساعتش آماده می‌شود و برای تأیید می‌آید. (/plans)")
        else:
            text = f"❌ برنامهٔ {plan_id} رد شد. برای برنامهٔ تازه /plan را بزنید."
        self._close_plan_question(plan_id, text)

    def _close_plan_question(self, plan_id: int, text: str, tell: bool = True) -> None:
        with self.sessions() as s:
            row = s.get(ContentPlanRow, plan_id)
        for chat, mid in (row.messages or []) if row else []:
            if mid:
                self.api.replace_text(chat, mid, text)
        if tell:
            self._say(None, text)

    def run_plans(self) -> None:
        """Start every item of an approved plan whose preparation time has come (once: the origin is unique)."""
        now = self.clock()
        today = now.astimezone(TEHRAN).date()
        with self.sessions() as s:
            rows = s.execute(select(ContentPlanRow).where(ContentPlanRow.org_id == self.org_id,
                                                          ContentPlanRow.status == "approved")).scalars().all()
        due = []
        for row in rows:
            plan = mk.load_plan(row)
            if plan.end < today:
                continue
            for n, it in enumerate(plan.items, 1):
                at = mk.publish_at(it)
                due.append((row.id, n, it, at))
        if not due:
            return
        policy, kb, _, _ = self._context()
        for plan_id, n, it, at in due:
            if at - timedelta(minutes=policy.prepare_minutes) <= now < at + PLAN_LATE:
                preset = mk.preset_for(it, policy, kb)
                self.start_request(mk.request_text(it, policy, kb), None, f"plan:{plan_id}:{n}", new=False, preset=preset,
                                   publish_at=at, ack=f"🗓 طبق برنامهٔ {plan_id}: آماده‌سازی «{preset['summary_fa']}» برای ساعت "
                                                     f"{it.time.translate(mk.FA)} شروع شد.")

    def auto_propose_plan(self) -> None:
        """Once a week (marketing.yaml auto_propose), propose next period's plan by itself. Tried once per period."""
        if not self.owners:
            return
        now = self.clock().astimezone(TEHRAN)
        try:
            policy = mk.load_policy(self.org_id, self.orgs_root)
        except mk.PlanError:
            return  # reported when someone asks for a plan
        auto = policy.auto_propose if policy else None
        if auto is None or DAYS[now.weekday()] != auto.day:
            return
        h, m = map(int, auto.time.split(":"))
        begin = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if not begin <= now < begin + SCHEDULE_WINDOW:
            return
        start = mk.next_start(now.date(), policy.week_start)
        key = f"autoplan:{start.isoformat()}"
        with self.sessions() as s:
            exists = s.execute(select(ContentPlanRow.id).where(ContentPlanRow.org_id == self.org_id,
                                                               ContentPlanRow.start == start.isoformat())).first()
        if exists or self._state(key):
            return
        self._set_state(key, "tried")
        self.make_plan(None, start, note="", revise_of=None, actor="system:schedule")

    def plans_text(self) -> str:
        now = self.clock()
        today = now.astimezone(TEHRAN).date()
        with self.sessions() as s:
            rows = s.execute(select(ContentPlanRow).where(ContentPlanRow.org_id == self.org_id,
                                                          ContentPlanRow.status.in_(("approved", "proposed")))
                             .order_by(ContentPlanRow.id)).scalars().all()
            started = {r.origin for r in s.execute(select(ControlRequest).where(ControlRequest.origin.like("plan:%"))).scalars()}
        lines = []
        for row in rows:
            plan = mk.load_plan(row)
            if plan.end < today:
                continue
            state = "تأییدشده" if row.status == "approved" else "منتظر تصمیم شما"
            lines.append(f"🗓 برنامهٔ {row.id} ({state}): {mk.long_date(plan.start)} تا {mk.long_date(plan.end)}")
            for n, it in enumerate(plan.items, 1):
                at = mk.publish_at(it)
                mark = "▶️ شروع شده" if f"plan:{row.id}:{n}" in started else ("⏭ گذشت" if at + PLAN_LATE <= now else "⏳")
                lines.append(f"  {mark} {mk.long_date(it.day)} {it.time.translate(mk.FA)} — {it.pillar}/{it.topic}")
        return "\n".join(lines) or "برنامهٔ فعالی نیست. /plan را بزنید."

    # ------------------------------------------------------------------ performance report (reporting agent)

    def report_text(self, days: int) -> str:
        now = self.clock()
        with self.sessions() as s:
            rep = rp.build_report(s, self.org_id, end_day=now.astimezone(TEHRAN).date(), days=days, now=now, root=self.orgs_root)
        return rp.report_text(rep)

    def auto_report(self) -> None:
        """The weekly report (orgs/<org>/reporting.yaml), once per day it is due."""
        if not self.owners:
            return
        weekly = rp.load_weekly(self.org_id, self.orgs_root)
        now = self.clock().astimezone(TEHRAN)
        if weekly is None or DAYS[now.weekday()] != weekly.day:
            return
        begin = now.replace(hour=weekly.at[0], minute=weekly.at[1], second=0, microsecond=0)
        key = f"report:{now.date().isoformat()}"
        if not begin <= now < begin + SCHEDULE_WINDOW or self._state(key):
            return
        self._set_state(key, "sent")
        self._say(None, self.report_text(weekly.days))

    # ------------------------------------------------------------------ team memory (lessons from the owner's notes)

    def offer_lesson(self, text: str, scope: str, origin: str, *, chat: str | None) -> None:
        """Ask whether a note should become a standing instruction. Nothing is kept unless the owner says so."""
        with self.sessions.begin() as s:
            row = memory.offer(s, self.org_id, scope, text, origin)
            lid = row.id if row is not None else None
        if lid is None:
            if origin == "typed":
                self._say(chat, f"این متن قاعده نمی‌شود (خالی، بیش از {memory.MAX_LEN} نویسه، یا از قبل هست).")
            return
        who = "برنامه‌ریزی تقویم محتوا" if scope == "planning" else "نوشتن همهٔ محتوای بعدی"
        question = f"📌 این را قاعدهٔ همیشگی {who} کنم؟\n«{text.strip()[:memory.MAX_LEN]}»"
        for c in ([chat] if chat else self.owners):
            self.api.send_text(c, question, buttons=[[("📌 بله، همیشه", f"lk:{lid}"), ("فقط همین بار", f"lx:{lid}")]])

    def decide_lesson(self, lesson_id: int, action: str, actor: str, chat: str) -> None:
        from ..brand import load_brand
        now = self.clock()
        try:
            with self.sessions.begin() as s:
                if action == "lk":
                    row = memory.activate(s, load_brand(self.org_id, self.orgs_root), lesson_id, actor, now)
                    text = f"📌 قاعده ثبت شد و از این به بعد رعایت می‌شود: «{row.text}» (فهرست: /lessons)"
                else:
                    row = memory.close(s, self.org_id, lesson_id, actor, now)
                    text = "قاعده برداشته شد." if action == "lr" else "باشد، فقط برای همین بار."
        except memory.LessonError as e:
            text = f"⚠️ {e}"
        self._say(chat, text)

    def show_lessons(self, chat: str) -> None:
        with self.sessions() as s:
            rows = memory.active_rows(s, self.org_id)
        if not rows:
            self._say(chat, "هنوز قاعده‌ای ثبت نشده است. وقتی محتوا را با توضیح اصلاح یا رد کنید، می‌پرسم که قاعدهٔ همیشگی شود؛ "
                            "یا بنویسید: /lesson متن قاعده")
            return
        self._say(chat, f"📌 {len(rows)} قاعدهٔ همیشگی (هر کدام را می‌توانید بردارید):")
        for r in rows:
            where = "برنامه‌ریزی" if r.scope == "planning" else "نوشتن"
            self.api.send_text(chat, f"[{where}] {r.text}", buttons=[[("🗑 برداشتن", f"lr:{r.id}")]])

    # ------------------------------------------------------------------ CRM (leads from the public intake bot)

    def lead_card(self, lead, head: str) -> str:
        cfg = self.crm_cfg
        lines = [head]
        lines += [f"{q.label}: {lead.fields.get(q.id, '—')}" for q in cfg.intake.questions]
        who = f"@{lead.username}" if lead.username else (f"شناسهٔ {lead.user_id}" if lead.user_id else "")
        lines.append(f"مرحله: {cfg.stage_fa(lead.stage)}" + (f" | از: {lead.ref}" if lead.ref else "") + (f" | {lead.channel} {who}" if who else ""))
        return "\n".join(lines)

    def _lead_buttons(self, lead) -> list:
        moves = [(s.name_fa, f"ld:{lead.id}:{s.id}") for s in self.crm_cfg.stages if s.id != lead.stage]
        return [moves[i:i + 3] for i in range(0, len(moves), 3)]

    def notify_lead(self, lead_id: int, created: bool) -> None:
        """Called by the intake bot when someone submits the form."""
        from ..db import Lead
        with self.sessions() as s:
            lead = s.get(Lead, lead_id)
        head = f"🆕 سرنخ تازهٔ تامین مالی — شمارهٔ {lead.id}" if created else f"✏️ سرنخ {lead.id} پاسخ‌هایش را به‌روز کرد"
        for chat in self.owners:
            self.api.send_text(chat, self.lead_card(lead, head), buttons=self._lead_buttons(lead))

    def move_lead(self, lead_id: int, stage: str, actor: str, chat: str) -> None:
        try:
            with self.sessions.begin() as s:
                lead = crm.set_stage(s, self.crm_cfg, lead_id, stage, actor, self.clock())
        except crm.CrmError as e:
            self._say(chat, str(e))
            return
        self.api.send_text(chat, self.lead_card(lead, f"سرنخ {lead.id} → {self.crm_cfg.stage_fa(stage)}"),
                           buttons=None if self.crm_cfg.stage(stage).final else self._lead_buttons(lead))

    def show_lead(self, lead_id: int, chat: str) -> None:
        from ..db import Lead
        with self.sessions() as s:
            lead = s.get(Lead, lead_id)
            history = crm.events(s, lead_id) if lead is not None and lead.org_id == self.org_id else []
        if not history:
            self._say(chat, f"سرنخ {lead_id} پیدا نشد.")
            return
        lines = []
        for e in history:
            when = e.ts.astimezone(TEHRAN).strftime("%m/%d %H:%M")
            what = {"created": "ثبت شد", "updated": "پاسخ‌ها به‌روز شد", "reminded": "یادآوری پیگیری",
                    "stage": f"{self.crm_cfg.stage_fa(e.data.get('from', ''))} ← {self.crm_cfg.stage_fa(e.data.get('to', ''))}",
                    "note": f"یادداشت: {e.data.get('text', '')}"}.get(e.kind, e.kind)
            lines.append(f"• {when} — {what}")
        self.api.send_text(chat, self.lead_card(lead, f"📇 سرنخ {lead.id}") + "\n\nسابقه:\n" + "\n".join(lines[-15:]),
                           buttons=self._lead_buttons(lead))

    def leads_text(self) -> str:
        with self.sessions() as s:
            leads = crm.open_leads(s, self.crm_cfg)
        if not leads:
            return "سرنخ بازی نیست."
        now = self.clock()
        first = self.crm_cfg.intake.questions[0].id
        lines = [f"📇 {len(leads)} سرنخ باز:"]
        for st in self.crm_cfg.stages:
            group = [x for x in leads if x.stage == st.id]
            if group:
                lines.append(f"\n{st.name_fa}:")
                lines += [f"  {x.id} — {x.fields.get(first, '—')} ({int((now - x.created_at).total_seconds() // 3600)} ساعت پیش)"
                          for x in group]
        return "\n".join(lines) + "\n\nجزئیات: /lead شماره"

    def remind_leads(self) -> None:
        """A lead still in the first stage after follow_up_hours: the owners are reminded once."""
        if self.crm_cfg is None or not self.owners:
            return
        from ..db import Lead, LeadEvent
        now = self.clock()
        with self.sessions() as s:
            due = [x for x in crm.overdue(s, self.crm_cfg, now)
                   if s.execute(select(LeadEvent.id).where(LeadEvent.lead_id == x.id, LeadEvent.kind == "reminded")).first() is None]
        for lead in due:
            for chat in self.owners:
                self.api.send_text(chat, self.lead_card(lead, f"⏰ سرنخ {lead.id} بیش از {self.crm_cfg.follow_up_hours} ساعت است "
                                                               "منتظر تماس است"), buttons=self._lead_buttons(lead))
            with self.sessions.begin() as s:
                s.add(LeadEvent(lead_id=lead.id, actor="system:reminder", kind="reminded", data={}, ts=now))

    # ------------------------------------------------------------------ publish reports

    def report_jobs(self) -> None:
        with self.sessions() as s:
            packages = select(ControlPrompt.package_id).where(ControlPrompt.org_id == self.org_id)
            jobs = s.execute(select(PublishJob).where(PublishJob.package_id.in_(packages),
                                                      PublishJob.status.in_(JOB_FINAL)).order_by(PublishJob.id)).scalars().all()
            told = {r.job_id: r.status for r in s.execute(select(ControlJobReport)).scalars()}
        for j in jobs:
            if told.get(j.id) == j.status:
                continue
            self._say(None, describe_job(j))
            with self.sessions.begin() as s:
                s.merge(ControlJobReport(job_id=j.id, status=j.status))

    # ------------------------------------------------------------------ texts

    def status_text(self) -> str:
        with self.sessions() as s:
            runs = s.execute(select(Run).where(Run.org_id == self.org_id).order_by(Run.created_at.desc()).limit(6)).scalars().all()
            paused = (s.get(OrgControl, self.org_id) or OrgControl(paused=False)).paused
        lines = ["⏸ انتشار متوقف است (/resume)." if paused else "▶️ انتشار فعال است."]
        last = self._state("backup:last")
        if last:
            when = datetime.fromisoformat(last).astimezone(TEHRAN)
            lines.append(f"💾 آخرین پشتیبان: {mk.long_date(when.date())} ساعت {when.strftime('%H:%M').translate(mk.FA)}")
        for r in runs:
            what = (r.detail or {}).get("request") or r.template
            lines.append(f"• {STATUS_FA.get(r.status, r.status)} — {str(what)[:70]}")
        return "\n".join(lines) if runs else "\n".join(lines) + "\nهنوز کاری انجام نشده است."

    def jobs_text(self) -> str:
        with self.sessions() as s:
            jobs = s.execute(select(PublishJob).where(PublishJob.org_id == self.org_id).order_by(PublishJob.id.desc()).limit(12)).scalars().all()
        return "\n".join(describe_job(j) for j in jobs) or "هنوز ارسالی ثبت نشده است."


STATUS_FA = {
    "planning": "در حال برنامه‌ریزی", "running": "در حال انجام", "awaiting_approval": "منتظر تأیید شما",
    "approved": "تأیید شد", "rejected": "رد شد", "blocked": "متوقف در کنترل کیفیت", "expired": "مهلت تأیید گذشت",
    "skipped": "انجام نشد", "failed": "خطا", "needs_input": "نیاز به توضیح شما", "needs_data": "داده کم است",
    "declined": "قابل انجام نیست",
}
JOB_FA = {
    "pending": "در صف", "sending": "در حال ارسال", "retry_wait": "تلاش دوباره به‌زودی", "confirmed": "✅ منتشر شد",
    "failed": "❌ منتشر نشد", "dead": "❌ پس از چند تلاش منتشر نشد", "unknown": "❓ نامعلوم", "blocked": "⛔️ جلویش گرفته شد",
    "expired": "⌛️ مهلتش گذشت", "cancelled": "لغو شد",
}


def describe_run(run: Run) -> str:
    d = run.detail or {}
    text = f"وضعیت: {STATUS_FA.get(run.status, run.status)}"
    for key in ("message", "reason", "error"):
        if d.get(key):
            text += f"\n{d[key]}"
    blocking = [f["message"] for f in run.findings or [] if f.get("severity") == "block"]
    if blocking:
        text += "\n" + "\n".join(f"• {m}" for m in blocking[:8])
    return text


def describe_job(j: PublishJob) -> str:
    text = f"ارسال {j.id} به {j.destination_id}: {JOB_FA.get(j.status, j.status)}"
    if j.status == "unknown":
        text += f"\nکانال را ببینید: اگر پست هست «/sent {j.id}»، اگر نیست «/retry {j.id}»."
    elif j.status in ("failed", "dead", "blocked") and j.error:
        text += f"\n{j.error[:300]}"
    return text
