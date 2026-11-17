"""Registry, Manager, multi-channel writer and checker, reviewers."""
import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from orchestra.agents.manager import plan_request
from orchestra.agents.multichannel import parse_multichannel, user_prompt, verify_multichannel, write_multichannel
from orchestra.agents.reviewers import compliance_review, editorial_review
from orchestra.agents.specialists import compose, market_part
from orchestra.data.acquisition import acquire
from orchestra.data.sources.bourse_trader import BourseTraderSource
from orchestra.llm import Gateway, LLMError, Route
from orchestra.registry import OrgPolicy, Registry, RegistryError

from test_content import BRAND, PAGE, FakeProvider

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 11)
ALL = OrgPolicy(("telegram", "bale", "instagram"))


def gw(sessions, tier, *replies):
    prov = FakeProvider(list(replies))
    return Gateway(sessions, {tier: [Route(prov, "m")]}), prov


# ---------------------------------------------------------------- registry

def test_registry_loads_and_resolves_every_entrypoint():
    reg = Registry.load(ROOT / "registry")
    assert {"writer", "publisher", "designer", "human_approval"} <= set(reg.agents)
    assert {"market_report", "stock_report"} <= set(reg.templates)


def test_plan_drops_conditional_steps_and_keeps_gates():
    reg = Registry.load(ROOT / "registry", check_entrypoints=False)
    plan = reg.instantiate("market_report", {"platforms": ["telegram"]}, today=TODAY, policy=ALL)
    ids = [s.id for s in plan.steps]
    assert "design" not in ids and "technical" not in ids and plan.params["day"] == TODAY
    assert ids.index("verify") < ids.index("approve") < ids.index("publish")
    approve = next(s for s in plan.steps if s.id == "approve")
    assert set(approve.depends_on) == {"edit", "comply"}  # the inactive design step is dropped from dependencies
    ig = reg.instantiate("market_report", {"technical_subject": "شاخص کل"}, today=TODAY, policy=ALL)
    assert ig.has("design") and ig.has("technical")


def test_org_policy_and_bad_params_are_refused():
    reg = Registry.load(ROOT / "registry", check_entrypoints=False)
    with pytest.raises(RegistryError, match="سهم"):
        reg.instantiate("stock_report", {"subject": "فولاد"}, today=TODAY, policy=ALL)
    with pytest.raises(RegistryError, match="سهم"):
        reg.instantiate("market_report", {"technical_subject": "فولاد"}, today=TODAY, policy=ALL)
    with pytest.raises(RegistryError, match="مجاز نیست"):
        reg.instantiate("market_report", {"platforms": ["twitter"]}, today=TODAY, policy=ALL)
    with pytest.raises(RegistryError, match="unknown parameters"):
        reg.instantiate("market_report", {"steps": []}, today=TODAY, policy=ALL)
    stock_ok = OrgPolicy(("telegram",), single_stock_content=True)
    plan = reg.instantiate("stock_report", {"subject": "فولاد", "sections": "technical"}, today=TODAY, policy=stock_ok)
    assert plan.has("technical") and not plan.has("fundamental") and not plan.has("design")


def _write_registry(tmp_path, steps):
    for p in (ROOT / "registry" / "agents").glob("*.yaml"):
        (tmp_path / "agents").mkdir(exist_ok=True)
        (tmp_path / "agents" / p.name).write_bytes(p.read_bytes())
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "t.yaml").write_text(yaml.safe_dump(
        {"id": "t", "version": "1.0.0", "name_fa": "x", "description": "x", "steps": steps}, allow_unicode=True), encoding="utf-8")


def test_template_that_skips_a_gate_does_not_load(tmp_path):
    _write_registry(tmp_path, [{"id": "w", "agent": "writer"}, {"id": "v", "agent": "fact_checker", "depends_on": ["w"]},
                               {"id": "p", "agent": "publisher", "depends_on": ["v"]}])
    with pytest.raises(RegistryError, match="compliance_officer"):
        Registry.load(tmp_path, check_entrypoints=False)


def test_cycles_are_refused(tmp_path):
    _write_registry(tmp_path, [{"id": "a", "agent": "writer", "depends_on": ["b"]}, {"id": "b", "agent": "editor", "depends_on": ["a"]}])
    with pytest.raises(RegistryError, match="cycle"):
        Registry.load(tmp_path, check_entrypoints=False)


# ---------------------------------------------------------------- manager

def test_manager_plans_repairs_and_declines(sessions):
    reg = Registry.load(ROOT / "registry", check_entrypoints=False)
    bad = json.dumps({"action": "run", "template": "market_report", "params": {"platforms": ["twitter"]}})
    good = json.dumps({"action": "run", "template": "market_report", "params": {"technical_subject": "شاخص کل"},
                       "summary_fa": "گزارش بازار امروز"}, ensure_ascii=False)
    g, prov = gw(sessions, "manager", bad, good)
    d = asyncio.run(plan_request(g, reg, "گزارش بازار امروز با تکنیکال شاخص", org_id="pouyan", run_id="m1", today=TODAY, policy=ALL))
    assert d.action == "run" and d.plan.has("technical") and len(prov.calls) == 2
    assert "twitter" in prov.calls[1][1] and "decline" in prov.calls[0][0]

    stock = json.dumps({"action": "run", "template": "stock_report", "params": {"subject": "فولاد"}}, ensure_ascii=False)
    g, _ = gw(sessions, "manager", stock)
    d = asyncio.run(plan_request(g, reg, "تحلیل فولاد", org_id="pouyan", run_id="m2", today=TODAY, policy=ALL))
    assert d.action == "decline" and "سهم" in d.message

    g, _ = gw(sessions, "manager", json.dumps({"action": "clarify", "question": "کدام روز؟"}, ensure_ascii=False))
    d = asyncio.run(plan_request(g, reg, "گزارش", org_id="pouyan", run_id="m3", today=TODAY, policy=ALL))
    assert d.action == "clarify" and d.message == "کدام روز؟"


# ---------------------------------------------------------------- multi-channel

def market_report(sessions, assets, clock):
    async def fetch(url):
        return PAGE
    with sessions.begin() as s:
        got = asyncio.run(acquire(s, assets, BourseTraderSource(), fetch, clock()))
        part, excluded = market_part(s, snapshot_id=got.snapshot_id, brand=BRAND, run_date=TODAY, now=clock())
    return compose(got.snapshot_id, [part], excluded)


def good_draft(rep):
    keys = {p.metric: p.key for p in rep.points if p.subject is None}
    idx, pct, trade, net = keys["index.total.value"], keys["index.total.change_pct"], keys["retail.trade_value"], keys["retail.real_money_net"]
    top = next(p.key for p in rep.points if p.metric == "real_money_net.group.top_in")
    return {
        "post": {"hashtag": "#تحلیل_بازار", "headline": f"رشد {{{{{pct}|abs_pct}}}} شاخص کل",
                 "paragraphs": [f"شاخص کل با رشد {{{{{pct}|abs_pct}}}} به {{{{{idx}|int}}}} واحد رسید.",
                                f"ارزش معاملات خرد {{{{{trade}|money}}}} بود و {{{{{net}|abs_money}}}} پول حقیقی وارد شد."],
                 "cta": "نظرتان را بنویسید."},
        "instagram": {"paragraphs": [f"شاخص کل امروز {{{{{pct}|abs_pct}}}} رشد کرد.", "جزئیات در اسلایدها."],
                      "hashtags": ["#بورس", "شاخص کل", "#بورس", "#کارگزاری_پویان"]},
        "slides": [{"title": "بازار امروز", "bullets": ["{{D1|name}}"]},
                   {"title": "شاخص و ارزش معاملات", "bullets": ["پول حقیقی وارد بازار شد."],
                    "stats": [{"label": "شاخص کل", "value": f"{{{{{idx}|int}}}}"}, {"label": "تغییر", "value": f"{{{{{pct}|signed_pct}}}}"}]},
                   {"title": "پول کجا رفت؟", "bullets": [f"گروه {{{{{top}|name}}}} بیشترین ورود را داشت."], "chart": "groups_flow"}],
    }


def test_market_part_adds_flow_intensity_and_chart(sessions, assets, clock):
    rep = market_report(sessions, assets, clock)
    assert any(p.metric == "market.flow_intensity_pct" for p in rep.points)
    assert any("شدت جابه‌جایی پول حقیقی" in f for f in rep.facts)
    chart = rep.charts[0]
    assert chart.id == "groups_flow" and all("صندوق" not in b.label for b in chart.bars)
    assert chart.bars[0].value > 0 > chart.bars[-1].value


def test_multichannel_writes_all_channels_from_one_call(sessions, assets, clock):
    rep = market_report(sessions, assets, clock)
    g, prov = gw(sessions, "writer", json.dumps(good_draft(rep), ensure_ascii=False))
    draft = asyncio.run(write_multichannel(g, BRAND, rep, org_id="pouyan", run_id="w1", date_text="چهارشنبه",
                                           platforms=("telegram", "instagram"), focus="جریان پول"))
    user = prov.calls[0][1]
    assert "groups_flow" in user and "جریان پول" in user and '"slides"' in user
    vc = verify_multichannel(draft, rep, BRAND, ("telegram", "instagram"))
    assert not vc.blocked, vc.feedback()
    assert vc.caption.hashtags == ("#بورس", "#شاخص_کل")  # cleaned, deduplicated, brand tag added by the template
    assert [s.kind for s in vc.slides] == ["cover", "content", "content"] and vc.slides[2].chart.id == "groups_flow"
    assert vc.slides[1].stats[0][1] == next(c.rendered for c in vc.claims if c.fmt == "int")
    assert "telegram" not in user_prompt(rep, date_text="x", platforms=("telegram",)) or '"slides"' not in user_prompt(rep, date_text="x", platforms=("telegram",))


@pytest.mark.parametrize("mutate,rule", [
    (lambda d: d["instagram"]["paragraphs"].__setitem__(0, "شاخص ۲ درصد رشد کرد."), "V-UNBOUND"),
    (lambda d: d["slides"][1]["stats"][0].__setitem__("value", "حدود {{P1|int}}"), "R-STAT"),
    (lambda d: d["slides"][2].__setitem__("chart", "unknown"), "R-CHART"),
    (lambda d: d["slides"].__setitem__(slice(1, None), []), "R-SHAPE"),
    (lambda d: d["slides"][1]["bullets"].__setitem__(0, "این سهم را بخرید."), "R-GUARD"),
    (lambda d: d["instagram"]["paragraphs"].append("صندوق‌های طلا هم رشد کردند."), "R-EXCLUDED"),
    (lambda d: d["slides"][1].__setitem__("title", "تیتر " * 20), "R-LENGTH"),
    (lambda d: d.pop("instagram"), "R-SHAPE"),
])
def test_multichannel_checks_block(sessions, assets, clock, mutate, rule):
    rep = market_report(sessions, assets, clock)
    d = good_draft(rep)
    mutate(d)
    vc = verify_multichannel(parse_multichannel(json.dumps(d, ensure_ascii=False)), rep, BRAND, ("telegram", "instagram"))
    assert vc.blocked and any(f.rule_id == rule for f in vc.findings), [f.rule_id for f in vc.findings]


# ---------------------------------------------------------------- reviewers

def _verified(sessions, assets, clock):
    rep = market_report(sessions, assets, clock)
    vc = verify_multichannel(parse_multichannel(json.dumps(good_draft(rep), ensure_ascii=False)), rep, BRAND, ("telegram", "instagram"))
    return rep, vc


def test_reviewer_findings_must_quote_the_content(sessions, assets, clock):
    rep, vc = _verified(sessions, assets, clock)
    reply = json.dumps({"findings": [
        {"severity": "block", "quote": "پول حقیقی وارد بازار شد", "problem": "تناقض"},
        {"severity": "block", "quote": "این جمله در متن نیست", "problem": "خیالی"},
    ]}, ensure_ascii=False)
    g, prov = gw(sessions, "reviewer", reply)
    found = asyncio.run(editorial_review(g, BRAND, vc, rep, org_id="pouyan", run_id="e1"))
    assert [f.rule_id for f in found] == ["E-EDITOR", "E-EDITOR-UNGROUNDED"] and found[0].severity == "block"
    assert "واقعیت‌های محاسبه‌شده" in prov.calls[0][1] and "اسلاید ۳" in prov.calls[0][1]


def test_compliance_fails_closed_editor_fails_open(sessions, assets, clock):
    rep, vc = _verified(sessions, assets, clock)
    g, _ = gw(sessions, "reviewer", LLMError("down", retryable=False))
    c = asyncio.run(compliance_review(g, BRAND, vc, rep, org_id="pouyan", run_id="c1"))
    assert c[0].severity == "block"
    g, _ = gw(sessions, "reviewer", "not json")
    e = asyncio.run(editorial_review(g, BRAND, vc, rep, org_id="pouyan", run_id="c2"))
    assert e[0].severity == "warn"


def test_a_reviewer_cannot_block_numbers_that_code_traced_to_a_source(sessions, assets, clock):
    """Real block on 2026-11-17: compliance called the support/resistance levels 'made up'."""
    rep, vc = _verified(sessions, assets, clock)
    tile = next(f"{a}: {b}" for s in vc.slides for a, b in s.stats)  # a verified number as it appears on a slide
    reply = json.dumps({"findings": [
        {"severity": "block", "quote": tile, "problem": "این اعداد در داده‌های مرجع نیستند و ساختگی‌اند"},
        {"severity": "block", "quote": tile, "problem": "سطح به‌عنوان هدف قیمتی آمده است"},  # a real compliance point
    ]}, ensure_ascii=False)
    g, prov = gw(sessions, "reviewer", reply)
    found = asyncio.run(compliance_review(g, BRAND, vc, rep, org_id="pouyan", run_id="c3"))
    assert [(f.rule_id, f.severity) for f in found] == [("C-COMPLIANCE", "block"), ("C-COMPLIANCE-UNGROUNDED", "warn")]
    assert "هدف قیمتی" in found[0].message
    user = prov.calls[0][1]
    assert "عددهای متن" in user and tile.split(": ")[1] in user  # the reviewer is shown every verified number


def test_doubt_about_a_number_that_is_not_verified_still_counts(sessions, assets, clock):
    from orchestra.agents.reviewers import ReviewItem, doubts_verified_numbers
    rep, vc = _verified(sessions, assets, clock)
    assert not doubts_verified_numbers(ReviewItem(severity="block", quote="رشد ۹۹٬۹۹۹ واحدی", problem="عدد ساختگی"), vc)
    assert not doubts_verified_numbers(ReviewItem(severity="block", quote="پول حقیقی وارد بازار شد", problem="داده نمی‌گوید"), vc)


def test_a_stray_letter_is_a_typo_the_writer_must_fix():
    """Real typo on 2026-11-17: «ص صنایع پیشرو و مقاومتی» as a slide title."""
    from orchestra.agents.verification import text_rules
    found = text_rules("ص صنایع پیشرو و مقاومتی", BRAND, "اسلاید ۴")
    assert [(f.rule_id, f.severity) for f in found] == [("R-TYPO", "block")] and "«ص»" in found[0].message
    assert text_rules("شاخص کل و صنایع پیشرو؛ ق.م و ه.ش", BRAND) == []
