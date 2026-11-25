"""The marketing agent: a content calendar the model proposes and code holds to the organization's rules."""
import asyncio
from datetime import date, datetime, timezone

import pytest

from orchestra.agents.marketing import (
    ContentPlan, PlanError, PlanItem, decide_plan, fixed_items, load_context, load_plan, make_plan, next_start,
    plan_text, preset_for, propose_plan, publish_at, request_text, store_plan, validate_plan,
)
from orchestra.control.service import ScheduleEntry
from orchestra.db import AuditEvent, ContentPlanRow
from orchestra.llm import Gateway, Route
from orchestra.registry import DEFAULT_ROOT, Registry
from wf_support import ScriptedProvider

REG = Registry.load(DEFAULT_ROOT)
POLICY, KB, _, BRAND = load_context("pouyan", "orgs", REG)
SAT = date(2026, 11, 28)
NOW = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)  # Thursday
SCHEDULE = [ScheduleEntry("market_close", (12, 45), ("sat", "sun", "mon", "tue", "wed"), "گزارش پایان بازار")]
FIXED = fixed_items(SCHEDULE, SAT, 7)
PLATFORMS = ("telegram", "bale", "instagram")


def item(day, t, pillar, topic, angle="زاویه‌ای تازه برای مدیران شرکت‌ها"):
    return {"day": day, "time": t, "pillar": pillar, "topic": topic, "angle": angle}


NEWS = [{"day": d, "time": "09:30", "pillar": "news", "topic": "", "angle": ""}
        for d in ("2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03")]
GOOD = {"summary_fa": "هر صبح خلاصهٔ خبر و هر روز یک محور، دور از گزارش ظهر.", "items": NEWS + [
    item("2026-11-28", "18:00", "proof", "crowd_track_record"),
    item("2026-11-29", "15:00", "concerns", "crowd_concerns"),
    item("2026-11-30", "18:00", "path", "crowd_path"),
    item("2026-12-01", "15:00", "identity", "crowd_intro"),
    item("2026-12-02", "18:30", "call", "crowd_cta"),
]}


def check(items):
    return validate_plan([PlanItem.model_validate(i) for i in items], policy=POLICY, start=SAT, fixed=FIXED, brand=BRAND,
                         registry=REG, platforms=PLATFORMS, earliest_start=NOW)


def test_policy_fixed_posts_and_next_week():
    assert [p.id for p in POLICY.pillars] == ["news", "proof", "concerns", "path", "identity", "call"]
    assert next_start(date(2026, 11, 26), "sat") == SAT and next_start(SAT, "sat") == date(2026, 12, 5)
    assert len(FIXED) == 5 and {f.time for f in FIXED} == {"12:45"}  # Saturday to Wednesday


def test_a_plan_that_keeps_every_rule_passes():
    assert check(GOOD["items"]) == []


@pytest.mark.parametrize("change, problem", [
    (lambda it: it.__setitem__(0, item("2026-11-28", "13:30", "proof", "crowd_track_record")), "فاصلهٔ"),  # 45 min after the noon report
    (lambda it: it.__setitem__(0, item("2026-11-28", "22:00", "proof", "crowd_track_record")), "بین 09:00 و 21:00"),
    (lambda it: it.__setitem__(0, item("2026-12-04", "18:00", "proof", "crowd_track_record")), "روز انتشار نیست"),  # Friday
    (lambda it: it.__setitem__(0, item("2026-12-08", "18:00", "proof", "crowd_track_record")), "خارج از دوره"),
    (lambda it: it.__setitem__(0, item("2026-11-28", "18:00", "proof", "crowd_path")), "از موضوع‌های محور"),
    (lambda it: it.__setitem__(0, item("2026-11-28", "18:00", "proof", "crowd_track_record", "سه طرح در ۲ سال")), "عدد"),
    (lambda it: it.__setitem__(0, item("2026-11-28", "18:00", "proof", "crowd_track_record", "سود تضمینی برای همه")), "عبارت ممنوع"),
    (lambda it: it.__setitem__(0, item("2026-11-28", "18:00", "nope", "crowd_track_record")), "وجود ندارد"),
    (lambda it: it.pop(), "دقیقاً 1"),
    (lambda it: it.pop(0), "دقیقاً 6"),
    (lambda it: it.append(item("2026-11-28", "15:00", "call", "crowd_cta")), "حداکثر 3"),  # four with news and the noon report
    (lambda it: it.append(item("2026-12-02", "15:00", "call", "crowd_cta")), "حداکثر یک پست در روز"),
    (lambda it: it.__setitem__(0, {**NEWS[0], "time": "11:30"}), "باید بین 09:00 و 10:30"),
    (lambda it: it.__setitem__(0, {**NEWS[0], "angle": "فقط انرژی"}), "زاویه نمی‌گیرد"),
    (lambda it: it.__setitem__(0, {**NEWS[0], "topic": "crowd_path"}), "موضوع نمی‌گیرد"),
])
def test_every_broken_rule_is_named(change, problem):
    items = [dict(i) for i in GOOD["items"]]
    change(items)
    assert any(problem in p for p in check(items)), check(items)


def test_too_late_to_prepare_is_refused():
    late = datetime(2026, 11, 28, 14, 0, tzinfo=timezone.utc)  # 17:30 in Tehran, 30 minutes before the first item
    problems = validate_plan([PlanItem.model_validate(i) for i in GOOD["items"]], policy=POLICY, start=SAT, fixed=FIXED,
                             brand=BRAND, registry=REG, platforms=PLATFORMS, earliest_start=late)
    assert any("آماده شود" in p for p in problems)


def gateway(sessions, replies):
    prov = ScriptedProvider(replies)
    return Gateway(sessions, {"manager": [Route(prov, "scripted")]}, max_calls_per_run=10), prov


def propose(gw, **kw):
    return asyncio.run(propose_plan(gw, brand=BRAND, policy=POLICY, kb=KB, registry=REG, start=SAT, fixed=FIXED,
                                    platforms=PLATFORMS, now=NOW, **kw))


def test_a_broken_plan_goes_back_once_with_the_reasons(sessions):
    bad = {"items": GOOD["items"][:4]}
    gw, prov = gateway(sessions, [bad, GOOD])
    plan = propose(gw)
    assert len(plan.items) == 11 and plan.summary_fa and plan.policy.startswith("marketing#")
    assert "دقیقاً" in prov.calls[1]
    gw, _ = gateway(sessions, ["not json", bad])
    with pytest.raises(PlanError):
        propose(gw)


def test_items_become_preset_requests_at_their_time():
    plan = ContentPlan(org_id="pouyan", start=SAT, days=7, items=tuple(PlanItem.model_validate(i) for i in GOOD["items"]))
    news = plan.items[0]
    assert preset_for(news, POLICY, KB) == {"template": "news_digest", "params": {}, "summary_fa": "خلاصهٔ خبرهای موثر بر بازار"}
    it = plan.items[6]
    assert preset_for(it, POLICY, KB) == {"template": "topic_post", "summary_fa": "اثبات و کارنامه: کارنامهٔ پویان کراد",
                                          "params": {"topic": "crowd_track_record", "focus": it.angle}}
    assert publish_at(it).isoformat() == "2026-11-28T18:00:00+03:30"
    assert "کارنامهٔ پویان کراد" in request_text(it, POLICY, KB)
    text = plan_text(plan, POLICY, KB, FIXED)
    assert "۱۸:۰۰ — اثبات و کارنامه: کارنامهٔ پویان کراد" in text and "(ثابت)" in text and "۱۲:۴۵" in text


def test_approval_is_bound_to_the_hash_and_given_once(sessions):
    plan = ContentPlan(org_id="pouyan", start=SAT, days=7, items=tuple(PlanItem.model_validate(i) for i in GOOD["items"]))
    with sessions.begin() as s:
        row = store_plan(s, plan, actor="test")
        pid, h = row.id, row.plan_hash
    with sessions.begin() as s, pytest.raises(PlanError):
        decide_plan(s, pid, "0" * 64, "approved", "u", date(2026, 11, 26))
    with sessions.begin() as s:
        assert decide_plan(s, pid, h, "approved", "u", date(2026, 11, 26)).status == "approved"
    with sessions.begin() as s, pytest.raises(PlanError):
        decide_plan(s, pid, h, "rejected", "u", date(2026, 11, 26))
    # a newer approved plan for the same days retires the old one
    newer = plan.model_copy(update={"summary_fa": "نسخهٔ دیگر"})
    with sessions.begin() as s:
        row2 = store_plan(s, newer, actor="test")
        decide_plan(s, row2.id, row2.plan_hash, "approved", "u", date(2026, 11, 26))
    with sessions() as s:
        assert s.get(ContentPlanRow, pid).status == "superseded"
        assert load_plan(s.get(ContentPlanRow, row2.id)) == newer
        actions = [e.action for e in s.query(AuditEvent).order_by(AuditEvent.id)]
    assert actions == ["plan.proposed", "plan.approved", "plan.proposed", "plan.approved"]


def test_a_revision_is_a_new_version_with_the_note(sessions):
    gw, prov = gateway(sessions, [GOOD, GOOD])
    first = asyncio.run(make_plan(sessions, gw, REG, org_id="pouyan", root="orgs", start=None, schedule=SCHEDULE,
                                  platforms=PLATFORMS, now=NOW, actor="test"))
    second = asyncio.run(make_plan(sessions, gw, REG, org_id="pouyan", root="orgs", start=None, schedule=SCHEDULE,
                                   platforms=PLATFORMS, now=NOW, actor="test", note="پست فراخوان عصر باشد", revise_of=first))
    with sessions() as s:
        a, b = s.get(ContentPlanRow, first), s.get(ContentPlanRow, second)
        assert (a.status, b.status, b.version, b.supersedes, b.start) == ("superseded", "proposed", 2, first, "2026-11-28")
    assert "پست فراخوان عصر باشد" in prov.calls[1]
    with pytest.raises(PlanError):  # the superseded one cannot be revised again
        asyncio.run(make_plan(sessions, gw, REG, org_id="pouyan", root="orgs", start=None, schedule=SCHEDULE,
                              platforms=PLATFORMS, now=NOW, actor="test", note="x", revise_of=first))
