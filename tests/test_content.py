"""Analysis, writer, verification, template and package for the market-close post, on the real Bourse Trader page."""
import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from orchestra.agents.analysis import InsufficientData, analyze_market_close
from orchestra.agents.verification import verify
from orchestra.agents.writer import Draft, parse_draft, write_market_close, WriterError
from orchestra.brand import load_brand
from orchestra.content.package_builder import build_text_package
from orchestra.content.template import bale_markdown, telegram_html
from orchestra.data.acquisition import acquire
from orchestra.data.sources.bourse_trader import BourseTraderSource
from orchestra.db import LLMCall
from orchestra.llm import BudgetExceeded, Completion, Gateway, LLMError, Route
from orchestra.package import DataPointRef

ROOT = Path(__file__).resolve().parent.parent
PAGE = (Path(__file__).parent / "fixtures" / "bourse_trader_sample.html").read_bytes()
BRAND = load_brand("pouyan", ROOT / "orgs")

GOOD = {
    "skip": False, "hashtag": "#تحلیل_بازار",
    "headline": "رشد {{P3|abs_pct}} شاخص کل در پایان معاملات",
    "paragraphs": [
        "شاخص کل بورس امروز با رشد {{P3|abs_pct}} به {{P1|int}} واحد رسید و ارزش معاملات خرد {{P4}} بود.",
        "در معاملات خرد {{P5|abs_money}} پول حقیقی وارد بازار شد. گروه {{P6|name}} با {{P6|abs_money}} بیشترین ورود پول را داشت و گروه {{P9|name}} بیشترین خروج را.",
    ],
    "cta": "نظرتان را دربارهٔ بازار امروز بنویسید.",
}


class FakeProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, model, system, user, max_tokens):
        self.calls.append((system, user, max_tokens))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, Completion) else Completion(r, 900, 300, False)


def gateway(sessions, *providers, max_calls=12):
    return Gateway(sessions, {"writer": [Route(p, f"model-{i}") for i, p in enumerate(providers)]}, max_calls_per_run=max_calls)


def report_for(sessions, assets, clock):
    async def fetch(url):
        return PAGE
    with sessions.begin() as s:
        got = asyncio.run(acquire(s, assets, BourseTraderSource(), fetch, clock()))
        return analyze_market_close(s, got.snapshot_id, BRAND), got


# ---------------------------------------------------------------- analysis


def test_analysis_removes_funds_and_states_directions(sessions, assets, clock):
    rep, _ = report_for(sessions, assets, clock)
    names = [p.subject for p in rep.points if p.subject]
    assert names == ["فلزات", "دارویی", "سیمان", "کاشی و سرامیک"]  # every fund group removed, no single stocks
    assert set(rep.excluded) == {"صندوق سهامی", "صندوق اهرمی", "صندوق درآمد ثابت", "صندوق مختلط"}
    assert "شاخص کل امروز رشد کرد." in rep.facts and "در معاملات خرد، پول حقیقی وارد بازار شد." in rep.facts
    assert [p.key for p in rep.points][:5] == ["P1", "P2", "P3", "P4", "P5"]


def test_closed_market_is_not_analysed(sessions, assets, clock):
    page = PAGE.replace(b">18.6T<", b">0<")

    async def fetch(url):
        return page
    with sessions.begin() as s:
        got = asyncio.run(acquire(s, assets, BourseTraderSource(), fetch, clock()))
        with pytest.raises(InsufficientData):
            analyze_market_close(s, got.snapshot_id, BRAND)


# ---------------------------------------------------------------- writer and gateway


def test_writer_gets_tokens_not_numbers_to_copy(sessions, assets, clock):
    rep, _ = report_for(sessions, assets, clock)
    prov = FakeProvider([json.dumps(GOOD, ensure_ascii=False)])
    draft = asyncio.run(write_market_close(gateway(sessions, prov), BRAND, rep, org_id="pouyan", run_id="r1", date_text="سه‌شنبه"))
    system, user, _ = prov.calls[0]
    assert "{{P1|int}} → ۷٬۹۷۶٬۴۰۰" in user and "صندوق" not in user.split("جدول داده")[1]
    assert "هیچ عدد" in system and "کارگزاری پویان" in system
    assert draft.headline == GOOD["headline"]
    with sessions() as s:
        call = s.execute(select(LLMCall)).scalar_one()
        assert call.ok and call.input_tokens == 900 and call.prompt == user and call.response


def test_unreadable_json_gets_one_repair(sessions, assets, clock):
    rep, _ = report_for(sessions, assets, clock)
    prov = FakeProvider(["متن آزاد بدون JSON", json.dumps(GOOD, ensure_ascii=False)])
    asyncio.run(write_market_close(gateway(sessions, prov), BRAND, rep, org_id="pouyan", run_id="r2", date_text="x"))
    assert len(prov.calls) == 2
    with pytest.raises(WriterError):
        parse_draft('{"headline": 5, "unknown_field": 1}')


def test_gateway_fails_over_retries_truncation_and_enforces_budget(sessions):
    down = FakeProvider([LLMError("HTTP 529 overloaded")])
    cut = FakeProvider([Completion("نیمه", 10, 10, True), Completion("کامل", 10, 20, False)])
    gw = gateway(sessions, down, cut)
    assert asyncio.run(gw.complete(org_id="o", run_id="r", step_key="s", tier="writer", system="s", user="u", max_tokens=100)) == "کامل"
    assert cut.calls[0][2] == 100 and cut.calls[1][2] == 200  # doubled after a cut-off answer
    with sessions() as s:
        assert [c.ok for c in s.execute(select(LLMCall).order_by(LLMCall.id)).scalars()] == [False, False, True]
    tight = gateway(sessions, FakeProvider(["a", "b"]), max_calls=1)
    asyncio.run(tight.complete(org_id="o", run_id="r9", step_key="s", tier="writer", system="s", user="u"))
    with pytest.raises(BudgetExceeded):
        asyncio.run(tight.complete(org_id="o", run_id="r9", step_key="s", tier="writer", system="s", user="u"))


# ---------------------------------------------------------------- verification


def test_good_draft_renders_with_claims(sessions, assets, clock):
    rep, _ = report_for(sessions, assets, clock)
    v = verify(Draft.model_validate(GOOD), rep, BRAND)
    assert not v.blocked, v.findings
    assert v.parts.headline == "رشد ۲٫۳۹٪ شاخص کل در پایان معاملات"
    assert "۷٬۹۷۶٬۴۰۰ واحد" in v.parts.paragraphs[0] and "۱۸٫۶ همت" in v.parts.paragraphs[0]
    assert "فلزات با ۱٫۱ همت" in v.parts.paragraphs[1]
    assert len(v.claims) == 8


@pytest.mark.parametrize("field,text,rule", [
    ("headline", "رشد ۲ درصدی شاخص کل", "V-UNBOUND"),
    ("headline", "شاخص {{P99|int}}", "V-BINDING"),
    ("cta", "همین حالا بخرید", "R-GUARD"),
    ("cta", "صندوق‌های درآمد ثابت را ببینید", "R-EXCLUDED"),
    ("cta", "کانال ما: t.me/abc", "R-LINK"),
])
def test_bad_drafts_are_blocked(sessions, assets, clock, field, text, rule):
    rep, _ = report_for(sessions, assets, clock)
    v = verify(Draft.model_validate({**GOOD, field: text}), rep, BRAND)
    assert v.blocked and rule in {f.rule_id for f in v.findings}


def test_skip_is_respected(sessions, assets, clock):
    rep, _ = report_for(sessions, assets, clock)
    v = verify(Draft(skip=True, skip_reason="داده کافی نیست"), rep, BRAND)
    assert v.blocked and v.findings[0].rule_id == "W-SKIP"


# ---------------------------------------------------------------- template and package


def test_layout_escapes_model_text_and_builds_both_platforms():
    from orchestra.content.template import PostParts
    parts = PostParts(hashtag="#تحلیل_بازار", headline="رشد ۲٫۳۹٪ شاخص <i>کل</i>",
                      paragraphs=("بند اول.", "بند *دوم* [x](y)."), cta="نظر <b>شما</b> مهم است")
    tg, bale = telegram_html(BRAND.template, parts), bale_markdown(BRAND.template, parts)
    assert tg.startswith("#کارگزاری_پویان\n#تحلیل_بازار") and "📌  <b>رشد ۲٫۳۹٪ شاخص &lt;i&gt;کل&lt;/i&gt;</b>" in tg
    assert "&lt;b&gt;شما" in tg and '<a href="https://pouyanbroker.ir">' in tg and "➖➖➖" in tg
    assert "📌  *رشد ۲٫۳۹٪" in bale and "＊دوم＊ ［x］(y)" in bale  # model text can never become Markdown
    assert "[🌐 وب‌سایت کارگزاری پویان](https://pouyanbroker.ir)" in bale


def test_model_markup_is_blocked(sessions, assets, clock):
    rep, _ = report_for(sessions, assets, clock)
    v = verify(Draft.model_validate({**GOOD, "cta": "نظر <b>شما</b> مهم است"}), rep, BRAND)
    assert v.blocked and "R-MARKUP" in {f.rule_id for f in v.findings}


def test_package_carries_both_texts_sources_and_versions(sessions, assets, clock):
    rep, got = report_for(sessions, assets, clock)
    v = verify(Draft.model_validate(GOOD), rep, BRAND)
    refs = [DataPointRef(id=p.point_id, metric=p.metric, subject=p.subject, value=p.value, unit=p.unit,
                         as_of=clock(), source_id="bourse_trader", raw_sha256="c" * 64) for p in rep.points]
    pkg = build_text_package(brand=BRAND, run_id="r1", content_class="market_close", parts=v.parts, claims=v.claims,
                             data_points=refs, destinations=[("tg_test", "telegram"), ("bale_test", "bale")],
                             versions={"writer_prompt": "writer.market_close@1"})
    tg = pkg.content.destination("tg_test").delivery_plan[0]
    bale = pkg.content.destination("bale_test").delivery_plan[0]
    assert tg.parse_mode == "HTML" and bale.parse_mode == "Markdown" and tg.body != bale.body
    assert {d.id for d in pkg.content.sources.data_points} == {c.point_id for c in v.claims}  # only what was used
    assert pkg.content.versions["brand"].startswith("brand@2#") and pkg.verify()
