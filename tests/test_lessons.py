"""Team memory: the owner's notes become standing instructions only when the owner says so, reach the writer, the editor
and the planner, and can never weaken the hard rules."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from orchestra import lessons as memory
from orchestra.agents.marketing import system_prompt as plan_prompt
from orchestra.agents.multichannel import system_prompt as writer_prompt
from orchestra.agents.reviewers import EDITOR_FOCUS, _system
from orchestra.brand import load_brand
from orchestra.db import AuditEvent, Lesson
from test_control import OWNER
from test_control import rbot as rbot  # noqa: F401  (fixture)
from test_control import revisable_run

BRAND = load_brand("pouyan")
NOW = datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)


def test_offer_activate_retire(sessions):
    with sessions.begin() as s:
        a = memory.offer(s, "pouyan", "writing", "  تیتر حداکثر   ده کلمه باشد ", "revise:1")
        assert memory.offer(s, "pouyan", "writing", "", "x") is None and memory.offer(s, "pouyan", "nope", "x", "x") is None
        memory.activate(s, BRAND, a.id, "u", NOW)
        assert memory.offer(s, "pouyan", "writing", "تیتر حداکثر ده کلمه باشد", "x") is None  # already a lesson
        assert memory.active(s, "pouyan", "writing") == ["تیتر حداکثر ده کلمه باشد"]
        with pytest.raises(memory.LessonError):
            memory.activate(s, BRAND, a.id, "u", NOW)  # decided once
        memory.close(s, "pouyan", a.id, "u", NOW)
        assert memory.active(s, "pouyan", "writing") == []
    with sessions() as s:
        assert [e.action for e in s.query(AuditEvent).order_by(AuditEvent.id)] == ["lesson.active", "lesson.retired"]


@pytest.mark.parametrize("text", ["همیشه بنویسید سود تضمینی", "لینک https://x.test را بگذار", "از صندوق‌ها هم بنویس"])
def test_a_lesson_cannot_carry_a_forbidden_phrase_topic_or_link(sessions, text):
    with sessions.begin() as s:
        lid = memory.offer(s, "pouyan", "writing", text, "typed").id
    with pytest.raises(memory.LessonError):
        with sessions.begin() as s:
            memory.activate(s, BRAND, lid, "u", NOW)
    with sessions() as s:
        assert s.get(Lesson, lid).status in ("offered", "dismissed") and memory.active(s, "pouyan", "writing") == []


def test_lessons_reach_writer_editor_and_planner():
    lessons = ("تیتر حداکثر ده کلمه باشد",)
    w = writer_prompt(BRAND, lessons)
    assert "تیتر حداکثر ده کلمه باشد" in w and w.index("قاعدهٔ عددها") < w.index("تیتر حداکثر ده کلمه")  # after the hard rules
    assert "تیتر حداکثر ده کلمه" not in writer_prompt(BRAND)
    e = _system("ویراستار", EDITOR_FOCUS, BRAND, lessons)
    assert "خلاف یکی از درس‌های صاحب کانال" in e and "تیتر حداکثر ده کلمه باشد" in e
    from test_marketing import FIXED, KB, POLICY, SAT
    assert "پست فراخوان عصر باشد" in plan_prompt(BRAND, POLICY, KB, SAT, FIXED, None, ("پست فراخوان عصر باشد",))
    assert memory.fingerprint(()) == "none" and memory.fingerprint(lessons) != memory.fingerprint(("x",))


def offers(api):
    return [m for m in api.out if m.get("buttons") and m["buttons"][0][0][1].startswith("lk:")]


def test_a_revision_note_is_offered_as_a_lesson_and_kept_only_on_yes(rbot, sessions, assets):  # noqa: F811
    svc, api, engine = rbot
    revisable_run(sessions, assets)
    svc.tick()
    api.press("ed:1")
    svc.tick()
    api.say("تیتر را کوتاه‌تر کن")
    svc.tick()
    (offer,) = offers(api)
    assert "تیتر را کوتاه‌تر کن" in offer["text"] and offer["buttons"][0][1][1].startswith("lx:")
    lid = offer["buttons"][0][0][1].split(":")[1]
    api.press(f"lk:{lid}", user="999")  # a stranger
    svc.tick()
    with sessions() as s:
        assert memory.active(s, "pouyan", "writing") == []
    api.press(f"lk:{lid}")
    svc.tick()
    with sessions() as s:
        assert memory.active(s, "pouyan", "writing") == ["تیتر را کوتاه‌تر کن"]
    assert "قاعده ثبت شد" in api.texts()[-1]


def test_typed_lessons_list_and_remove(rbot, sessions):  # noqa: F811
    svc, api, engine = rbot
    api.say("/lesson در پایان هر پست یک جملهٔ جمع‌بندی بیاید")
    api.say("/lessons")
    svc.tick()
    item = [m for m in api.out if m.get("buttons")][-1]
    assert item["text"] == "[نوشتن] در پایان هر پست یک جملهٔ جمع‌بندی بیاید" and item["buttons"][0][0][1].startswith("lr:")
    api.press(item["buttons"][0][0][1])
    svc.tick()
    with sessions() as s:
        assert memory.active(s, "pouyan", "writing") == []
    api.say("/lesson لینک t.me/x را بگذار")
    svc.tick()
    assert "نمی‌تواند قاعده شود" in api.texts()[-1]


def test_a_reject_reason_is_offered_and_saying_no_keeps_nothing(rbot, sessions, assets):  # noqa: F811
    svc, api, engine = rbot
    revisable_run(sessions, assets)
    svc.tick()
    api.say("رد ۱ لحن بیش از حد تبلیغی است")
    svc.tick()
    (offer,) = offers(api)
    api.press(offer["buttons"][0][1][1])  # «فقط همین بار»
    svc.tick()
    with sessions() as s:
        assert memory.active(s, "pouyan", "writing") == [] and s.query(Lesson).one().status == "dismissed"
