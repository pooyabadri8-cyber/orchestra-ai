"""The control bot: the owner works with the team from their phone.

In the control chat (Telegram or Bale) the owner can
  - write a request in plain Persian: the Manager plans it and the team prepares the content;
  - see exactly what would be published (every channel's text, the Instagram caption and slides);
  - approve or reject with one button (or by typing «تأیید ۱۲» / «رد ۱۲ دلیل»), or send it back with a note
    («اصلاح ۱۲ تیتر کوتاه‌تر شود»): the team makes a new version from the same data;
  - be told what happened to every publish job, and act on the unclear ones (/sent, /retry);
  - stop everything (/pause) and continue (/resume).
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..assets import AssetStore
from ..db import (
    ControlJobReport, ControlPrompt, ControlRequest, ControlState, OrgControl, PackageRow, PublishJob, Run, utcnow,
)
from .botapi import BotApi, BotApiError, split_text

log = logging.getLogger("orchestra.control")
TEHRAN = ZoneInfo("Asia/Tehran")
WORKING = ("starting", "planning", "running")
JOB_FINAL = ("confirmed", "failed", "unknown", "dead", "blocked", "expired", "cancelled")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # date.weekday() order
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
SCHEDULE_WINDOW = timedelta(hours=4)

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
/cancel — انصراف از نوشتن توضیح اصلاح"""


class Engine(Protocol):
    def start_request(self, org_id: str, text: str, *, new: bool) -> str: ...
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
                log.warning("به ربات کنترل وصل نشد (%s)؛ چند ثانیهٔ دیگر دوباره تلاش می‌کنم.", e)
                time.sleep(10)
            except Exception:  # noqa: BLE001  one bad round must not stop the service
                log.exception("control round failed")
                time.sleep(5)

    def tick(self) -> None:
        self.read_updates()
        self.run_schedule()
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

    def start_request(self, text: str, chat: str | None, origin: str, *, new: bool) -> None:
        with self.sessions() as s:
            if s.execute(select(ControlRequest.id).where(ControlRequest.origin == origin)).first():
                return  # this message was already taken (a replay after a restart)
        run_id = self.engine.start_request(self.org_id, text, new=new)
        try:
            with self.sessions.begin() as s:
                s.add(ControlRequest(org_id=self.org_id, origin=origin, chat_id=chat, run_id=run_id, text=text))
        except IntegrityError:
            return
        self._say(chat, "دریافت شد ✅ تیم مشغول آماده‌سازی است؛ نتیجه را همین‌جا می‌فرستم.")

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
        elif status == "rejected":
            self._finish_prompt(prompt, "rejected", f"❌ درخواست {pid} رد شد." + (f"\nدلیل: {reason}" if reason else ""))
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
        for r in runs:
            what = (r.detail or {}).get("request") or r.template
            lines.append(f"• {STATUS_FA.get(r.status, r.status)} — {str(what)[:70]}")
        return "\n".join(lines) if runs else lines[0] + "\nهنوز کاری انجام نشده است."

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
