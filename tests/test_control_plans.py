"""The content calendar in the control chat: /plan, approve / revise / reject with buttons, items starting by themselves
before their time with a preset plan, and the weekly proposal. The database, the plan rules and the marketing agent are
real; the model is scripted and the workflow engine is a stand-in."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from conftest import Clock, add_destinations
from orchestra.agents.marketing import make_plan
from orchestra.control.service import ControlService, ScheduleEntry
from orchestra.db import ContentPlanRow, ControlRequest
from orchestra.llm import Gateway, Route
from orchestra.registry import DEFAULT_ROOT, Registry
from test_control import OWNER, FakeApi, FakeEngine
from test_marketing import GOOD
from wf_support import ScriptedProvider

SCHEDULE = [ScheduleEntry("market_close", (12, 45), ("sat", "sun", "mon", "tue", "wed"), "گزارش پایان بازار")]
THU = datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc)  # Thursday 07:30 in Tehran, before the weekly proposal
FIRST_PREP = datetime(2026, 11, 28, 13, 0, tzinfo=timezone.utc)  # Saturday 16:30 in Tehran: 90 minutes before 18:00


class PlanEngine(FakeEngine):
    def __init__(self, sessions, assets, clock, replies):
        super().__init__(sessions, assets, clock)
        self.clock, self.provider, self.presets = clock, ScriptedProvider(replies), []

    def start_request(self, org_id, text, *, new, preset=None, publish_at=None):
        if preset is not None:  # plan items only (the daily market report also starts by itself)
            self.presets.append((preset, publish_at))
        return super().start_request(org_id, text, new=new)

    def propose_plan(self, org_id, *, start, schedule, note, revise_of, actor):
        gw = Gateway(self.sessions, {"manager": [Route(self.provider, "scripted")]}, max_calls_per_run=10)
        return asyncio.run(make_plan(self.sessions, gw, Registry.load(DEFAULT_ROOT), org_id=org_id, root="orgs", start=start,
                                     schedule=schedule, platforms=("telegram", "bale", "instagram"), now=self.clock(),
                                     actor=actor, note=note, revise_of=revise_of))


def make_bot(sessions, assets, replies, now=THU):
    add_destinations(sessions)
    clock = Clock(now)
    api, engine = FakeApi(), PlanEngine(sessions, assets, clock, replies)
    svc = ControlService(org_id="pouyan", api=api, engine=engine, sessions=sessions, assets=assets, owners=(OWNER,),
                         clock=clock, schedule=SCHEDULE)
    return svc, api, engine, clock


def plans(sessions):
    with sessions() as s:
        return s.query(ContentPlanRow).order_by(ContentPlanRow.id).all()


def buttons(api):
    return [b[1] for m in api.out if m.get("buttons") for b in m["buttons"][0]]


def test_plan_is_shown_with_buttons_and_asked_for_once(sessions, assets):
    svc, api, engine, _ = make_bot(sessions, assets, [GOOD])
    api.say("/plan")
    svc.tick()
    text = "\n".join(api.texts())
    assert "کارمند بازاریابی" in text and "اثبات و کارنامه: کارنامهٔ پویان کراد" in text and "(ثابت)" in text
    assert buttons(api) == ["pok:1", "ped:1", "pno:1"] and plans(sessions)[0].status == "proposed"
    api.say("/plan")
    svc.tick()
    assert "هنوز منتظر تصمیم" in api.texts()[-1] and len(plans(sessions)) == 1


def test_approved_plan_items_start_before_their_time_once(sessions, assets):
    svc, api, engine, clock = make_bot(sessions, assets, [GOOD])
    api.say("/plan")
    svc.tick()
    api.press("pok:1")
    svc.tick()
    assert plans(sessions)[0].status == "approved" and any("برنامهٔ 1 تأیید شد" in t for t in api.texts())
    assert any(m.get("edited") for m in api.out)  # the question lost its buttons
    clock.now = FIRST_PREP - timedelta(minutes=1)
    svc.tick()
    assert engine.presets == []  # 91 minutes before: not yet
    clock.now = FIRST_PREP
    svc.tick()
    svc.tick()
    assert len(engine.presets) == 1  # Saturday's 09:30 news is long past (the bot was "off"): only the 18:00 post starts
    preset, at = engine.presets[0]
    assert preset["template"] == "topic_post" and preset["params"]["topic"] == "crowd_track_record"
    assert at.isoformat() == "2026-11-28T18:00:00+03:30"
    with sessions() as s:
        assert [r.origin for r in s.query(ControlRequest) if r.origin.startswith("plan:")] == ["plan:1:2"]
    assert "▶️ شروع شده" in svc.plans_text()


def test_an_item_whose_time_passed_long_ago_is_not_started(sessions, assets):
    svc, api, engine, clock = make_bot(sessions, assets, [GOOD])
    api.say("/plan")
    svc.tick()
    api.press("pok:1")
    svc.tick()
    clock.now = datetime(2026, 11, 28, 17, 0, tzinfo=timezone.utc)  # 20:30 in Tehran: 18:00 was two and a half hours ago
    svc.tick()
    assert engine.presets == [] and "⏭ گذشت" in svc.plans_text()


def test_revise_makes_a_new_version_and_the_old_buttons_do_nothing(sessions, assets):
    svc, api, engine, _ = make_bot(sessions, assets, [GOOD, GOOD])
    api.say("/plan")
    svc.tick()
    api.press("ped:1")
    svc.tick()
    api.say("پست فراخوان عصر باشد")
    svc.tick()
    first, second = plans(sessions)
    assert (first.status, second.status, second.version, second.note) == ("superseded", "proposed", 2, "پست فراخوان عصر باشد")
    assert "پست فراخوان عصر باشد" in engine.provider.calls[-1] and "pok:2" in buttons(api)
    api.press("pok:1")
    svc.tick()
    assert "منتظر تصمیم نیست" in api.texts()[-1] and plans(sessions)[0].status == "superseded"
    api.press("pno:2")
    svc.tick()
    assert plans(sessions)[1].status == "rejected"


def test_a_stranger_cannot_approve_a_plan(sessions, assets):
    svc, api, engine, _ = make_bot(sessions, assets, [GOOD])
    api.say("/plan")
    svc.tick()
    api.press("pok:1", user="999")
    svc.tick()
    assert plans(sessions)[0].status == "proposed"


def test_a_plan_the_model_cannot_get_right_is_explained(sessions, assets):
    bad = {"items": GOOD["items"][:2]}
    svc, api, engine, _ = make_bot(sessions, assets, [bad, bad])
    api.say("/plan")
    svc.tick()
    assert "برنامه ساخته نشد" in api.texts()[-1] and plans(sessions) == []


def test_next_weeks_plan_is_proposed_by_itself_once(sessions, assets):
    svc, api, engine, clock = make_bot(sessions, assets, [GOOD], now=datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc))
    svc.tick()  # 09:30 on Thursday: before auto_propose (10:00)
    assert plans(sessions) == []
    clock.now = datetime(2026, 11, 26, 7, 0, tzinfo=timezone.utc)  # 10:30
    svc.tick()
    svc.tick()
    assert [p.start for p in plans(sessions)] == ["2026-11-28"] and "pok:1" in buttons(api)


@pytest.mark.parametrize("text", ["/plans"])
def test_plans_without_any(sessions, assets, text):
    svc, api, engine, _ = make_bot(sessions, assets, [])
    api.say(text)
    svc.tick()
    assert "برنامهٔ فعالی نیست" in api.texts()[-1]


def test_a_broken_policy_file_does_not_stop_publishing(sessions, assets, monkeypatch):
    svc, api, engine, clock = make_bot(sessions, assets, [GOOD])
    api.say("/plan")
    svc.tick()
    api.press("pok:1")
    svc.tick()
    import orchestra.agents.marketing as mk

    def broken(*a, **kw):
        raise mk.PlanError("marketing.yaml خراب است")
    monkeypatch.setattr(mk, "load_context", broken)
    called = []
    monkeypatch.setattr(engine, "publish_due", lambda: called.append(1) or {})
    clock.now = FIRST_PREP
    svc.tick()
    assert called == [1] and engine.presets == []


def test_the_morning_news_item_starts_as_a_news_digest(sessions, assets):
    svc, api, engine, clock = make_bot(sessions, assets, [GOOD])
    api.say("/plan")
    svc.tick()
    api.press("pok:1")
    svc.tick()
    clock.now = datetime(2026, 11, 28, 4, 30, tzinfo=timezone.utc)  # Saturday 08:00 in Tehran: 90 minutes before 09:30
    svc.tick()
    assert [(p["template"], p["params"], at.isoformat()) for p, at in engine.presets] == [
        ("news_digest", {}, "2026-11-28T09:30:00+03:30")]
