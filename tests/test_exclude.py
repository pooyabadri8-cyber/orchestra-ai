"""Offline tests of the "no posts about funds" filter (run: python tests/test_exclude.py)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from agent.autopilot import check_post  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.daily import build  # noqa: E402
from agent.exclude import Exclusions, from_raw  # noqa: E402
from agent.sources import Item, load_sources  # noqa: E402
import test_daily as td  # noqa: E402

from _profile import P  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")
EX = Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1}).exclusions


def test_defaults_on_and_can_be_switched_off():
    assert EX.enabled
    off = Config(**{**P, "exclude_words": []}, bot_token="x", api_key="x", channel="@c", owner_ids={1}).exclusions
    assert not off.enabled and off.count("صندوق سهامی") == 0 and not off.about("صندوق", "صندوق " * 9)
    assert off.reason("صندوق", "صندوق صندوق") is None


def test_counts_funds_but_not_imf_or_crowdfunding():
    assert EX.count("ورود پول به صندوق‌های درآمد ثابت") == 1
    assert EX.count("صندوق های سهامی و صندوق طلا") == 2  # space instead of ZWNJ, Arabic letters fine
    assert EX.count("ETF inflows and gold funds rose") == 2
    assert EX.count("صندوق بین‌المللی پول رشد جهانی را کاهش داد") == 0
    assert EX.count("The International Monetary Fund cut its forecast; Norway's sovereign wealth fund sold shares") == 0
    assert EX.count("صندوق توسعه ملی و صندوق‌های بازنشستگی") == 0
    assert EX.count("crowdfunding and funding rounds") == 0
    assert EX.count("تامین مالی جمعی در فرابورس") == 0


def test_incoming_item_dropped_when_title_or_three_mentions():
    def it(title, text):
        return Item("S", title, title, text, "u", datetime.now(timezone.utc))
    assert EX.about("بازدهی صندوق‌های طلا در هفتهٔ گذشته", "متن")
    assert EX.about("گزارش بازار", "صندوق " * 3)
    assert not EX.about("گزارش بازار", "ورود پول به صندوق‌ها ادامه داشت و شاخص رشد کرد.")  # one passing mention
    kept = EX.keep_items([it("خبر مهم", "متن"), it("ETF جدید عرضه شد", "متن")])
    assert [i.title for i in kept] == ["خبر مهم"]


def test_fund_rows_removed_from_report_without_touching_the_original():
    rep = td.REP
    clean = EX.clean_report(rep)
    assert [r.name for r in clean.top_in_groups] == ["فلزات", "دارویی"]
    assert [r.name for r in clean.top_out_groups] == ["سیمان", "کاشی و سرامیک"]
    assert [r.name for r in clean.top_in_stocks] == ["فولاد", "دوایکس", "فملی", "تاصیکو"]
    assert len(rep.top_in_groups) == 4  # cached original unchanged
    ex2 = from_raw(["صندوق"], ["دوایکس"])  # a fund symbol without the word «صندوق» in its name
    c2 = ex2.clean_report(rep)
    assert "دوایکس" not in [r.name for r in c2.top_in_stocks]
    post = build.report_post(td.TPL, clean, td.at(9, 0))
    assert "صندوق" not in post and "فلزات" in post
    assert "صندوق" not in build.report_material(clean, td.at(9, 0))


def test_check_post_blocks_fund_headline_or_repeated_mentions():
    body = [
        "شاخص کل در معاملات امروز با رشد همراه بود و ارزش معاملات نسبت به روز گذشته بیشتر شد و فعالان بازار آن را دنبال کردند.",
        "گروه‌های پیشرو در ورود پول حقیقی ادامه دادند و حجم معاملات در بیشتر نمادها از میانگین ماه گذشته بالاتر ماند.",
    ]
    ok = td.TPL.assemble(["#تحلیل_بازار"], "تحلیل بازار", body, "")
    assert check_post(ok, [], [], EX) == []
    one = td.TPL.assemble(["#تحلیل_بازار"], "تحلیل بازار", [body[0] + " صندوق‌ها هم فعال بودند.", body[1]], "")
    assert check_post(one, [], [], EX) == []  # one passing mention is allowed
    head = td.TPL.assemble(["#صندوق"], "بازدهی صندوق‌های درآمد ثابت", body, "")
    assert any("صندوق" in r for r in check_post(head, [], [], EX))
    twice = td.TPL.assemble(["#تحلیل_بازار"], "تحلیل بازار", [body[0] + " ورود پول به صندوق‌ها ادامه داشت.", body[1] + " صندوق‌های اهرمی بالاتر رفتند."], "")
    assert any("چندبار" in r for r in check_post(twice, [], [], EX))
    assert check_post(head, [], [], None) == []  # without the argument nothing changes


def test_real_sources_have_no_fund_channels_and_writer_is_told():
    names = [s.name for s in load_sources(os.path.join(CUSTOMER, "sources.yaml"))]
    assert "ترید فاند" not in names and "فاندوستینگ" not in names and len(names) >= 3
    d, store, w, _ = td.make(replies=[td.good("x")])
    assert "موضوع ممنوع" in w._system[0]["text"]


def test_industry_of_day_skips_refused_group_and_tries_next_candidate():
    items = {"market": td.mk_items(2), "news": []}
    d, store, w, _ = td.make(replies=["SKIP", td.good("تحلیل صنعت")], items=items)
    for e in d.s.entries:
        if e.id != "industry":
            d.state.mark(f"2026-10-10:{e.id}", "x")
    asyncio.run(d.tick(td.Bot(), td.at(11, 12)))  # industry at 11:15
    prompts = [c["messages"][0]["content"] for c in w.client.calls]
    assert len(prompts) == 2 and "صندوق" not in prompts[0].split("<material>")[1]  # no fund row reaches the model
    assert "«فلزات»" in prompts[0] and "«دارویی»" in prompts[1]  # fund groups were removed, the next group was tried
    p = td.posts(store)[0]
    assert p.pillar == "daily:industry" and p.status == "published" and p.topic == "تحلیل گروه روز: دارویی"
    assert "ETF" in prompts[0]  # the model may refuse a fund symbol itself


def test_daily_fetch_drops_fund_items(monkeypatch_free=True):
    import agent.daily.handlers as H

    now = datetime.now(timezone.utc)
    fund = Item("S", "k1", "بازدهی صندوق‌های سهامی", "متن", "u", now)
    fine = Item("S", "k2", "تحلیل طلا", "متن", "u", now)
    d, *_ = td.make(items={"news": []})

    async def fake_fetch(http, src, *a, **k):
        return [fund, fine]

    original = H.fetch_source
    H.fetch_source = fake_fetch
    try:
        d2 = H.Daily(d.app, d.cfg, d.s)
        d2.news_sources = [type("S", (), {"name": "n"})()]
        items, _ = asyncio.run(d2._gather("news", 20))
    finally:
        H.fetch_source = original
    assert [i.title for i in items] == ["تحلیل طلا"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
