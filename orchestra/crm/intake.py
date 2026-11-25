"""The public intake bot: anyone may talk to it; it asks the questions of crm.yaml in order and records a lead.

It obeys nobody and decides nothing: it only collects answers. Answers are plain text, never formatted and never given
to a model. Only private chats are answered (a group gets nothing). The state of each person's form is in the
database, so a restart neither loses a half-filled form nor answers an update twice (the offset is saved after each).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable

from ..control.botapi import BotApiError
from ..db import ControlState, IntakeChat, utcnow
from .store import CrmConfig, clean_answer, leads_today, submit_lead

log = logging.getLogger("orchestra.crm.intake")
GO, OK, REDO = "in:go", "in:ok", "in:redo"


class IntakeBot:
    def __init__(self, cfg: CrmConfig, api, sessions, *, notify: Callable[[int, bool], None] | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.cfg, self.api, self.sessions, self.notify, self.clock = cfg, api, sessions, notify, clock
        self.questions = cfg.intake.questions

    # ------------------------------------------------------------------ loop

    def _offset_key(self) -> str:
        return f"intake:{self.cfg.org_id}:offset"

    def tick(self) -> None:
        with self.sessions() as s:
            row = s.get(ControlState, self._offset_key())
            offset = int(row.value) if row else 0
        for u in self.api.get_updates(offset, 0):  # no long wait: the control bot shares this loop
            try:
                self.handle(u)
            except BotApiError:
                raise  # not reachable now: this update is handled again next round
            except Exception:  # noqa: BLE001
                log.exception("intake update %s failed", u.get("update_id"))
            with self.sessions.begin() as s:
                s.merge(ControlState(key=self._offset_key(), value=str(int(u["update_id"]) + 1)))

    # ------------------------------------------------------------------ one update

    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            if (msg.get("chat") or {}).get("type", "private") != "private":
                return
            user = (cq.get("from") or {})
            self.api.answer_button(str(cq.get("id")))
            self.on_button(str(user.get("id", "")), user.get("username"), str(cq.get("data") or ""))
            return
        msg = u.get("message") or {}
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        if chat.get("type", "private") != "private" or not user.get("id"):
            return  # groups and channels are not answered
        text = msg.get("text") or ""
        if not text.strip():
            self._say(str(user["id"]), "لطفاً پاسخ را به‌صورت متن بنویسید.")
            return
        self.on_text(str(user["id"]), user.get("username"), text)

    def _key(self, user: str) -> str:
        return f"{self.cfg.org_id}:{self.cfg.intake.platform}:{user}"

    def _chat(self, s, user: str) -> IntakeChat:
        row = s.get(IntakeChat, self._key(user))
        if row is None:
            row = IntakeChat(key=self._key(user), step=-1, answers={})
            s.add(row)
        return row

    def _say(self, user: str, text: str, buttons=None) -> None:
        self.api.send_text(user, text, buttons=buttons)

    def _welcome(self, user: str) -> None:
        it = self.cfg.intake
        self._say(user, f"{it.welcome}\n\n{it.consent}", buttons=[[("✅ موافقم، شروع", GO)]])

    def _ask(self, user: str, step: int) -> None:
        q = self.questions[step]
        self._say(user, f"({step + 1} از {len(self.questions)}) {q.ask}\n(انصراف: /cancel)")

    def _summary(self, user: str, answers: dict) -> None:
        lines = ["لطفاً بررسی کنید:"] + [f"• {q.label}: {answers.get(q.id, '')}" for q in self.questions]
        self._say(user, "\n".join(lines), buttons=[[("✅ ثبت درخواست", OK), ("↩️ از اول", REDO)]])

    def _set(self, user: str, step: int, answers: dict | None = None, ref: str | None | bool = False) -> None:
        with self.sessions.begin() as s:
            row = self._chat(s, user)
            row.step, row.updated_at = step, self.clock()
            if answers is not None:
                row.answers = dict(answers)
            if ref is not False:
                row.ref = ref

    def on_text(self, user: str, username: str | None, text: str) -> None:
        word = text.split()[0].lower()
        if word.startswith("/start"):
            payload = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""
            ref = re.sub(r"[^A-Za-z0-9_-]", "", payload)[:64] or None
            self._set(user, -1, {}, ref)
            self._welcome(user)
            return
        if word == "/cancel":
            self._set(user, -1, {})
            self._say(user, "لغو شد. برای شروع دوباره /start را بزنید.")
            return
        with self.sessions() as s:
            row = s.get(IntakeChat, self._key(user))
            step, answers = (row.step, dict(row.answers or {})) if row else (-1, {})
        if step < 0:
            self._welcome(user)
            return
        if step >= len(self.questions):
            self._say(user, "لطفاً یکی از دکمه‌های «ثبت درخواست» یا «از اول» را بزنید.")
            return
        value, problem = clean_answer(self.questions[step], text)
        if value is None:
            self._say(user, problem)
            return
        answers[self.questions[step].id] = value
        self._set(user, step + 1, answers)
        if step + 1 < len(self.questions):
            self._ask(user, step + 1)
        else:
            self._summary(user, answers)

    def on_button(self, user: str, username: str | None, data: str) -> None:
        if not user:
            return
        if data in (GO, REDO):
            self._set(user, 0, {})
            self._ask(user, 0)
            return
        if data != OK:
            return
        now = self.clock()
        with self.sessions.begin() as s:
            row = s.get(IntakeChat, self._key(user))
            if row is None or row.step != len(self.questions):
                created = None  # an old or repeated press: nothing to submit
            elif leads_today(s, self.cfg.org_id, self.cfg.intake.platform, user, now) >= self.cfg.max_leads_per_day:
                created = "limit"
            else:
                lead, created = submit_lead(s, self.cfg, channel=self.cfg.intake.platform, user_id=user, username=username,
                                            ref=row.ref, fields=dict(row.answers), now=now, actor=f"intake:{self.cfg.intake.platform}:{user}")
                lead_id = lead.id
                row.step, row.answers = -1, {}
        if created is None:
            self._say(user, "چیزی برای ثبت نیست. برای شروع /start را بزنید.")
        elif created == "limit":
            self._say(user, "درخواست شما پیش‌تر ثبت شده است؛ کارشناس پذیرش با شما تماس می‌گیرد.")
        else:
            self._say(user, self.cfg.intake.done)
            if self.notify:
                self.notify(lead_id, created)
