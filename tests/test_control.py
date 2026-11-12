"""The control bot's rules, with the real database, approvals and publisher; only the chat and the workflow engine are
stand-ins. (tests/test_e2e_control.py runs the same thing as a real process.)"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from conftest import add_destinations, make_package
from orchestra.approvals import decide as record_decision
from orchestra.approvals import store_package
from orchestra.control.botapi import BotApiError, split_text
from orchestra.control.service import ControlService, ScheduleEntry, build_preview, load_schedule, plain
from orchestra.db import ControlPrompt, ControlRequest, PublishJob, Run
from orchestra.publishing.adapters import Delivered, Unknown
from orchestra.publishing.publisher import Publisher

OWNER = "111"


class FakeApi:
    platform = "telegram"

    def __init__(self):
        self.out: list[dict] = []
        self.updates: list[dict] = []
        self.fail_on: str | None = None  # a substring: sending text that contains it fails
        self._uid = 0

    def get_updates(self, offset, wait):
        return [u for u in self.updates if u["update_id"] >= offset]

    def say(self, text, user=OWNER, chat=None):
        self._uid += 1
        self.updates.append({"update_id": self._uid, "message": {"text": text, "chat": {"id": int(chat or user)}, "from": {"id": int(user)}}})

    def press(self, data, user=OWNER):
        self._uid += 1
        self.updates.append({"update_id": self._uid, "callback_query": {"id": "c", "data": data, "from": {"id": int(user)},
                                                                         "message": {"chat": {"id": int(user)}}}})

    def send_text(self, chat_id, text, *, parse_mode=None, buttons=None):
        if self.fail_on and self.fail_on in text:
            raise BotApiError("down", retryable=True)
        self.out.append({"chat": chat_id, "text": text, "buttons": buttons, "parse_mode": parse_mode})
        return len(self.out)

    def send_album(self, chat_id, images):
        self.out.append({"chat": chat_id, "album": len(images), "text": ""})

    def answer_button(self, cid, text=""):
        pass

    def replace_text(self, chat_id, message_id, text):
        self.out.append({"chat": chat_id, "text": text, "edited": message_id})

    def texts(self, chat=None):
        return [m["text"] for m in self.out if chat is None or m["chat"] == chat]


class FakeEngine:
    """Decisions and publishing are the real ones; starting a request just records a run."""

    def __init__(self, sessions, assets, clock, script=None):
        self.sessions, self.started, self.script, self.log = sessions, [], list(script or []), []
        self.resumable = True
        self.alive = True
        self.decide_error: Exception | None = None
        outer = self

        class Adapter:
            async def send(self, step, media):
                outer.log.append(step.mode)
                return outer.script.pop(0) if outer.script else Delivered([len(outer.log)])
        self.pub = Publisher(sessions, assets, adapter_factory=lambda d: Adapter(), max_attempts=3, retry_delays=[30, 120], clock=clock)

    def start_request(self, org_id, text, *, new):
        rid = f"{org_id}:req:{len(self.started) + 1}"
        self.started.append((text, new))
        with self.sessions.begin() as s:
            s.add(Run(id=rid, org_id=org_id, template="request", status="planning", detail={"request": text}))
        return rid

    def can_resume(self, run_id):
        return self.resumable

    def is_alive(self, run_id):
        return self.alive

    def decide(self, run_id, package_hash, decision, actor, reason):
        if self.decide_error:
            raise self.decide_error
        with self.sessions.begin() as s:
            run = s.get(Run, run_id)
            jobs = []
            for dest in ("tg_test", "bale_test"):
                record_decision(s, run.package_id, package_hash, dest, decision, actor, reason=reason)
                if decision == "approved":
                    jobs.append(self.pub.enqueue(s, run.package_id, dest, actor=actor).id)
            run.status = decision
        return {"status": decision, "jobs": jobs}

    def publish_due(self):
        return asyncio.run(self.pub.run_due())

    def set_paused(self, org_id, paused, actor):
        self.pub.set_paused(org_id, paused, actor)

    def confirm_sent(self, job_id, actor):
        return self.pub.confirm_sent(job_id, actor)

    def requeue(self, job_id, actor):
        return self.pub.requeue(job_id, actor)


@pytest.fixture
def bot(sessions, assets, clock):
    add_destinations(sessions)
    api = FakeApi()
    engine = FakeEngine(sessions, assets, clock)
    svc = ControlService(org_id="pouyan", api=api, engine=engine, sessions=sessions, assets=assets, owners=(OWNER,), clock=clock)
    return svc, api, engine


def waiting_run(sessions, assets, run_id="pouyan:req:x", **kw):
    pkg = make_package(assets, **kw)
    with sessions.begin() as s:
        store_package(s, pkg)
        s.add(Run(id=run_id, org_id="pouyan", template="request", status="awaiting_approval", package_id=pkg.package_id,
                  detail={"request": "گزارش بازار"}, findings=[{"severity": "warn", "rule_id": "E", "message": "جمله طولانی است"}]))
    return pkg


def prompt(sessions, pid=1) -> ControlPrompt:
    with sessions() as s:
        return s.get(ControlPrompt, pid)


def jobs(sessions):
    with sessions() as s:
        return s.query(PublishJob).order_by(PublishJob.id).all()


# ------------------------------------------------------------------ who is obeyed


def test_only_owners_are_obeyed(bot):
    svc, api, engine = bot
    api.say("گزارش بده", user="999")
    api.say("/pause", user="999")
    api.say("گزارش بده", user=OWNER, chat="-100500")  # an owner, but in a group that is not allowed
    svc.tick()
    assert engine.started == [] and api.out == []


def test_a_stranger_cannot_press_the_button(bot, sessions, assets):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    svc.tick()
    api.press("ok:1", user="999")
    svc.tick()
    assert prompt(sessions).state == "pending" and jobs(sessions) == []


def test_without_owners_it_only_tells_the_id(sessions, assets, clock):
    api, engine = FakeApi(), FakeEngine(sessions, assets, clock)
    svc = ControlService(org_id="pouyan", api=api, engine=engine, sessions=sessions, assets=assets, owners=(), clock=clock)
    waiting_run(sessions, assets)
    api.say("سلام", user="424242")
    svc.tick()
    assert engine.started == [] and len(api.out) == 1 and "424242" in api.out[0]["text"]


# ------------------------------------------------------------------ requests


def test_a_message_becomes_one_request_even_if_replayed(bot, sessions):
    svc, api, engine = bot
    api.say("گزارش پایان بازار")
    svc.tick()
    svc._set_state("offset", "0")  # as if the process died before saving the offset
    svc.tick()
    assert engine.started == [("گزارش پایان بازار", True)]
    assert sum("دریافت شد" in t for t in api.texts()) == 1


def test_a_request_that_ends_without_content_is_explained(bot, sessions):
    svc, api, engine = bot
    api.say("تحلیل تکنیکال فولاد")
    svc.tick()
    with sessions.begin() as s:
        run = s.get(Run, "pouyan:req:1")
        run.status, run.detail = "declined", {**run.detail, "message": "محتوای تک‌سهم برای این سازمان مجاز نیست."}
    svc.tick()
    svc.tick()
    told = [t for t in api.texts() if "تک‌سهم" in t]
    assert len(told) == 1 and "قابل انجام نیست" in told[0]


def test_a_run_the_engine_gave_up_on_is_reported_not_awaited_forever(bot, sessions):
    svc, api, engine = bot
    api.say("گزارش پایان بازار")
    svc.tick()
    svc.tick()
    assert not any("خطا" in t for t in api.texts())
    engine.alive = False
    svc.tick()
    svc.tick()
    assert sum("اجرا متوقف شد" in t for t in api.texts()) == 1
    with sessions() as s:
        assert s.get(Run, "pouyan:req:1").status == "failed"


def test_unknown_command(bot):
    svc, api, engine = bot
    api.say("/whatever")
    svc.tick()
    assert engine.started == [] and "نمی‌شناسم" in api.texts()[-1]


# ------------------------------------------------------------------ approval


def test_content_is_shown_once_with_buttons_then_approved_and_published(bot, sessions, assets):
    svc, api, engine = bot
    pkg = waiting_run(sessions, assets)
    svc.tick()
    svc.tick()
    questions = [m for m in api.out if m.get("buttons")]
    assert len(questions) == 1 and questions[0]["buttons"][0][0][1] == "ok:1"
    assert pkg.package_hash[:12] in questions[0]["text"]
    assert any(m.get("album") == 3 for m in api.out) and any("جمله طولانی است" in t for t in api.texts())
    assert jobs(sessions) == []

    api.press("ok:1")
    svc.tick()
    assert prompt(sessions).state == "approved" and prompt(sessions).decided_by == "telegram:111"
    assert [j.status for j in jobs(sessions)] == ["confirmed", "confirmed"]
    assert sum("✅ منتشر شد" in t for t in api.texts()) == 2
    api.press("ok:1")
    svc.tick()
    svc.tick()
    assert len(jobs(sessions)) == 2 and len(engine.log) == 2  # nothing sent twice
    assert sum("✅ منتشر شد" in t for t in api.texts()) == 2 and "باز نیست" in api.texts()[-1]


def test_reject_by_typing_with_persian_digits_and_a_reason(bot, sessions, assets):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    svc.tick()
    api.say("رد ۱ تیتر را عوض کنید")
    svc.tick()
    assert prompt(sessions).state == "rejected" and jobs(sessions) == []
    assert any("رد شد" in t and "تیتر را عوض کنید" in t for t in api.texts())


def test_a_button_for_content_that_was_replaced_does_nothing(bot, sessions, assets):
    svc, api, engine = bot
    old = waiting_run(sessions, assets)
    svc.tick()
    new = make_package(assets, version=2, supersedes=old.package_id, body="متن اصلاح‌شده")
    with sessions.begin() as s:
        store_package(s, new)
        s.get(Run, "pouyan:req:x").package_id = new.package_id
    api.press("ok:1")  # the old question
    svc.tick()
    assert prompt(sessions, 1).state == "closed" and jobs(sessions) == []
    svc.tick()
    assert prompt(sessions, 2).state == "pending" and prompt(sessions, 2).package_hash == new.package_hash


def test_an_expired_run_closes_its_question(bot, sessions, assets):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    svc.tick()
    with sessions.begin() as s:
        s.get(Run, "pouyan:req:x").status = "expired"
    svc.tick()
    assert prompt(sessions).state == "closed" and "مهلت تأیید گذشت" in api.texts()[-1]
    api.press("ok:1")
    svc.tick()
    assert jobs(sessions) == []


def test_a_run_the_engine_cannot_resume_is_not_offered(bot, sessions, assets):
    svc, api, engine = bot
    engine.resumable = False
    waiting_run(sessions, assets)
    svc.tick()
    assert api.out == [] and prompt(sessions) is None


def test_if_showing_fails_midway_nothing_is_answerable_and_it_is_shown_again_later(bot, sessions, assets, monkeypatch):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    api.fail_on = "منتشر شود؟"
    svc.tick()
    assert prompt(sessions).state == "void" and not any(m.get("buttons") for m in api.out)
    api.press("ok:1")  # a number from a half-shown question is dead
    svc.tick()
    assert prompt(sessions).state == "void" and jobs(sessions) == []
    api.fail_on = None
    svc.tick()
    assert not any(m.get("buttons") for m in api.out)  # waits before trying again
    svc._failures.clear()
    svc.tick()
    assert [m["buttons"][0][0][1] for m in api.out if m.get("buttons")] == ["ok:2"]


def test_a_failed_decision_is_reported_and_never_published(bot, sessions, assets):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    svc.tick()
    engine.decide_error = TimeoutError("slow")
    api.press("ok:1")
    svc.tick()
    assert prompt(sessions).state == "unclear" and jobs(sessions) == [] and "کامل نشد" in api.texts()[-1]
    svc.tick()
    assert len([m for m in api.out if m.get("buttons")]) == 1  # not asked again: the first answer may still arrive


def test_restart_makes_an_interrupted_decision_answerable_again(bot, sessions, assets):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    svc.tick()
    with sessions.begin() as s:
        s.get(ControlPrompt, 1).state = "deciding"
        s.add(ControlPrompt(org_id="pouyan", run_id="r2", package_id="p", package_hash="h", state="sending"))
    svc.recover()
    assert prompt(sessions, 1).state == "pending" and prompt(sessions, 2).state == "void"


# ------------------------------------------------------------------ publish reports and controls


def test_an_unclear_send_is_reported_with_what_to_do_and_can_be_confirmed(bot, sessions, assets):
    svc, api, engine = bot
    engine.script[:] = [Unknown("no answer")]
    waiting_run(sessions, assets)
    svc.tick()
    api.press("ok:1")
    svc.tick()
    assert any("نامعلوم" in t and "/sent 1" in t and "/retry 1" in t for t in api.texts())
    api.say("/sent ۱")
    svc.tick()
    assert jobs(sessions)[0].status == "confirmed" and "ثبت شد." in api.texts()
    assert any("ارسال 1" in t and "✅ منتشر شد" in t for t in api.texts())


def test_pause_stops_publishing_and_resume_continues(bot, sessions, assets):
    svc, api, engine = bot
    waiting_run(sessions, assets)
    api.say("/pause")
    svc.tick()
    api.press("ok:1")
    svc.tick()
    svc.tick()
    assert engine.log == [] and all(j.status != "confirmed" for j in jobs(sessions))
    api.say("/status")
    svc.tick()
    assert "انتشار متوقف است" in api.texts()[-1]
    api.say("/resume")
    svc.tick()
    svc.tick()
    assert [j.status for j in jobs(sessions)] == ["confirmed", "confirmed"]


# ------------------------------------------------------------------ schedule


def test_schedule_starts_once_a_day_inside_its_window(bot, sessions, clock):
    svc, api, engine = bot
    svc.schedule = [ScheduleEntry("market_close", (12, 45), ("sat", "sun", "mon", "tue", "wed"), "گزارش پایان بازار")]
    clock.now = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)  # Wednesday 12:30 Tehran: too early
    svc.tick()
    assert engine.started == []
    clock.now = datetime(2026, 11, 11, 9, 20, tzinfo=timezone.utc)  # 12:50
    svc.tick()
    svc.tick()
    assert engine.started == [("گزارش پایان بازار", False)]
    with sessions() as s:
        assert s.query(ControlRequest).one().chat_id is None
    clock.now = datetime(2026, 11, 12, 9, 20, tzinfo=timezone.utc)  # Thursday: not a trading day
    svc.tick()
    clock.now = datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc)  # Saturday 17:30: the window has passed
    svc.tick()
    assert len(engine.started) == 1
    clock.now = datetime(2026, 11, 14, 9, 30, tzinfo=timezone.utc)  # Saturday 13:00
    svc.tick()
    assert len(engine.started) == 2


def test_schedule_file(tmp_path):
    f = tmp_path / "schedule.yaml"
    assert load_schedule(f) == []
    f.write_text('schedule:\n  - {name: close, time: "12:45", days: [sat, Sun], request: "گزارش"}\n  - {name: off, time: "9:00", request: x, enabled: false}\n', encoding="utf-8")
    assert load_schedule(f) == [ScheduleEntry("close", (12, 45), ("sat", "sun"), "گزارش")]
    for bad in ('- {name: a, time: "25:00", request: x}', '- {name: a, time: "12:00", days: [funday], request: x}',
                '- {name: "بد", time: "12:00", request: x}', '- {name: a, time: "12:00"}',
                '- {name: a, time: "12:00", request: x}\n  - {name: a, time: "13:00", request: y}'):
        f.write_text("schedule:\n  " + bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_schedule(f)


# ------------------------------------------------------------------ preview and text helpers


def test_preview_shows_same_words_once_and_plain_when_it_cannot_be_rendered(assets):
    content = make_package(assets, slides=0).content.model_dump(mode="json")
    tg = build_preview(content, "telegram")
    assert [i[0] for i in tg] == ["text", "text"]
    assert "telegram، bale" in tg[0][1] and tg[1][1:] == ("<b>متن پست</b>", "HTML")
    assert build_preview(content, "bale")[1][1:] == ("*متن پست*", None)
    content["destinations"][1]["platform"] = "instagram"
    assert build_preview(content, "bale")[1][1:] == ("متن پست", None)
    with_slides = build_preview(make_package(assets).content.model_dump(mode="json"), "telegram")
    assert [i[0] for i in with_slides] == ["text", "album", "text", "text", "text"] and len(with_slides[1][1]) == 3


def test_plain_and_split():
    assert plain('<b>الف</b> &amp; <a href="https://x.ir">ب</a>', "HTML") == "الف & ب"
    assert plain("*الف* [ب](https://x.ir)", "Markdown") == "الف ب"
    parts = split_text("\n".join(["خط"] * 3000))
    assert all(len(p) <= 4096 for p in parts) and "\n".join(parts) == "\n".join(["خط"] * 3000)
    assert [len(p) for p in split_text("x" * 5000)] == [4096, 904]


# ------------------------------------------------------------------ the Bot API client


def _api(handler, platform="telegram"):
    import httpx

    from orchestra.control.botapi import BotApi
    return BotApi(platform, "T", base_url="http://x/bot", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_album_falls_back_to_single_photos_and_text_to_plain():
    import httpx
    seen = []

    def handler(req: httpx.Request):
        method = req.url.path.rsplit("/", 1)[1]
        body = req.content.decode("utf-8", errors="replace")
        seen.append((method, "parse_mode" in body))
        if method == "sendMediaGroup" or (method == "sendMessage" and "parse_mode" in body):
            return httpx.Response(400, json={"ok": False, "description": "Bad Request"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    api = _api(handler)
    api.send_album("1", [("a.jpg", b"x", "image/jpeg"), ("b.jpg", b"y", "image/jpeg")])
    assert [m for m, _ in seen] == ["sendMediaGroup", "sendPhoto", "sendPhoto"]
    seen.clear()
    assert api.send_text("1", "<b>سلام</b>", parse_mode="HTML") == 7
    assert seen == [("sendMessage", True), ("sendMessage", False)]


def test_api_errors_say_whether_to_retry():
    import httpx
    answers = [httpx.Response(429, json={"ok": False, "description": "Too Many Requests"}),
               httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
               httpx.Response(502, text="bad gateway")]
    api = _api(lambda req: answers.pop(0))
    for retryable in (True, False, True):
        with pytest.raises(BotApiError) as e:
            api.call("getMe")
        assert e.value.retryable is retryable

    def down(req):
        raise httpx.ConnectError("no route")
    with pytest.raises(BotApiError) as e:
        _api(down).get_updates(0, 1)
    assert e.value.retryable


def test_bale_gets_no_parse_mode():
    import httpx
    bodies = []

    def handler(req):
        bodies.append(req.content.decode())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    _api(handler, "bale").send_text("1", "*x*", parse_mode="HTML", buttons=[[("بله", "ok:1")]])
    assert "parse_mode" not in bodies[0] and "link_preview" not in bodies[0] and "inline_keyboard" in bodies[0]


# ------------------------------------------------------------------ sending content back with a note


def revisable_run(sessions, assets, run_id="pouyan:req:x", round_no=1, can_revise=True, **kw):
    pkg = waiting_run(sessions, assets, run_id=run_id, **kw)
    with sessions.begin() as s:
        run = s.get(Run, run_id)
        run.detail = {**run.detail, "approval_round": round_no, "can_revise": can_revise}
    return pkg


class RevisingEngine(FakeEngine):
    """Like the real engine: a revise decision is only handed over; the workflow makes the next round later."""

    def decide(self, run_id, package_hash, decision, actor, reason):
        if decision == "revise":
            self.notes = [*getattr(self, "notes", []), (run_id, package_hash, reason)]
            return {"status": "revising"}
        return super().decide(run_id, package_hash, decision, actor, reason)


@pytest.fixture
def rbot(sessions, assets, clock):
    add_destinations(sessions)
    api, engine = FakeApi(), RevisingEngine(sessions, assets, clock)
    return ControlService(org_id="pouyan", api=api, engine=engine, sessions=sessions, assets=assets, owners=(OWNER,), clock=clock), api, engine


def test_revise_button_then_note_then_new_version_gets_a_new_question(rbot, sessions, assets):
    svc, api, engine = rbot
    v1 = revisable_run(sessions, assets)
    svc.tick()
    q = [m for m in api.out if m.get("buttons")][-1]
    assert [cb for _, cb in q["buttons"][0]] == ["ok:1", "ed:1", "no:1"]
    api.press("ed:1")
    svc.tick()
    assert "چه چیزی عوض شود" in api.texts()[-1]
    api.say("تیتر را کوتاه‌تر کن")  # this is the note, not a new request
    svc.tick()
    assert engine.started == [] and engine.notes == [("pouyan:req:x", v1.package_hash, "تیتر را کوتاه‌تر کن")]
    assert prompt(sessions).state == "revising" and any("اصلاح ثبت شد" in t for t in api.texts())
    svc.tick()
    svc.tick()
    assert len([m for m in api.out if m.get("buttons")]) == 1  # the old content is not offered again meanwhile
    api.press("ok:1")
    svc.tick()
    assert jobs(sessions) == []  # and its button is dead

    v2 = make_package(assets, version=2, supersedes=v1.package_id, body="متن نسخهٔ دوم")
    with sessions.begin() as s:  # what the workflow does when the new version is ready
        store_package(s, v2)
        run = s.get(Run, "pouyan:req:x")
        run.package_id, run.detail = v2.package_id, {**run.detail, "approval_round": 2}
    svc.tick()
    q2 = [m for m in api.out if m.get("buttons")][-1]
    assert q2["buttons"][0][0][1] == "ok:2" and any("نسخهٔ 2" in t for t in api.texts())
    api.press("ok:2")
    svc.tick()
    assert {j.package_id for j in jobs(sessions)} == {v2.package_id} and prompt(sessions, 2).state == "approved"


def test_revise_by_typing_and_cancel(rbot, sessions, assets):
    svc, api, engine = rbot
    revisable_run(sessions, assets)
    svc.tick()
    api.say("اصلاح ۱")  # no note yet: the bot asks for it
    svc.tick()
    api.say("/cancel")
    svc.tick()
    assert "انصراف ثبت شد." in api.texts()
    api.say("گزارش هفتگی بنویس")  # after cancelling, a message is a request again
    svc.tick()
    assert engine.started == [("گزارش هفتگی بنویس", True)] and prompt(sessions).state == "pending"
    api.say("اصلاح 1 عدد شاخص را در تیتر بیاور")
    svc.tick()
    assert engine.notes[0][2] == "عدد شاخص را در تیتر بیاور" and prompt(sessions).state == "revising"


def test_when_the_new_version_fails_the_old_one_is_offered_again_with_the_reason(rbot, sessions, assets):
    svc, api, engine = rbot
    v1 = revisable_run(sessions, assets)
    svc.tick()
    api.say("اصلاح 1 لحن هیجانی")
    svc.tick()
    with sessions.begin() as s:  # the workflow could not make a new version: same package, next round
        run = s.get(Run, "pouyan:req:x")
        run.detail = {**run.detail, "approval_round": 2, "revision_error": "کنترل کیفیت نسخهٔ جدید را نپذیرفت"}
    svc.tick()
    assert any("نسخهٔ جدید ساخته نشد" in t and "کنترل کیفیت" in t for t in api.texts())
    api.press("ok:2")
    svc.tick()
    assert {j.package_id for j in jobs(sessions)} == {v1.package_id}


def test_no_revise_button_when_none_are_left_or_for_the_simple_workflow(rbot, sessions, assets):
    svc, api, engine = rbot
    revisable_run(sessions, assets, can_revise=False)
    svc.tick()
    assert [cb for _, cb in [m for m in api.out if m.get("buttons")][-1]["buttons"][0]] == ["ok:1", "no:1"]
    api.say("اصلاح 1 چیزی")
    svc.tick()
    assert "دیگر اصلاح ممکن نیست" in api.texts()[-1] and prompt(sessions).state == "pending" and not getattr(engine, "notes", [])
    api.press("ok:1")
    svc.tick()
    assert prompt(sessions).state == "approved"
