"""Offline tests of the "calmer channel" changes: 15-minute spacing incl. calendar slots, daily cap, footer links,
no single-stock posts, news digest priorities (run: python tests/test_calm.py)."""
import asyncio
import os
import tempfile
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.bot as bmod  # noqa: E402
import agent.daily.handlers as H  # noqa: E402
from agent.autopilot import AutoState  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.daily import build  # noqa: E402
from agent.daily.settings import load_settings  # noqa: E402
from agent.sources import Item, load_sources  # noqa: E402
from agent.store import Store  # noqa: E402
import test_auto as ta  # noqa: E402
import test_daily as td  # noqa: E402

from _profile import P  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")


def test_footer_has_site_telegram_and_hides_linkedin_until_it_is_set():
    tpl = td.TPL
    text = tpl.assemble(["#بازار_سرمایه"], "تیتر", ["بند اول."], "")
    foot = text.split("➖➖➖")[1]
    assert 'href="https://pouyanbroker.ir"' in foot and 'href="https://t.me/pouyanbrokerage"' in foot
    assert "instagram.com/pouyanbrokerage" in foot and "ble.ir/pouyanbrokerage" in foot
    assert "لینکدین" not in foot  # no URL yet -> the line is not shown
    for item in tpl.footer_links:
        if "لینکدین" in item["text"]:
            item["url"] = "https://www.linkedin.com/company/example"
    try:
        assert 'href="https://www.linkedin.com/company/example"' in tpl.assemble(["#بازار_سرمایه"], "تیتر", ["بند."], "")
    finally:
        for item in tpl.footer_links:
            if "لینکدین" in item["text"]:
                item["url"] = ""


def test_gap_is_15_minutes_and_calendar_slots_are_kept_free():
    cfg = Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1}, auto_publish=True, auto_gap_minutes=15)
    store = Store(":memory:")
    now = datetime.now(timezone.utc)
    store.extra_busy = lambda: [now + timedelta(minutes=5)]  # a calendar post is due in 5 minutes
    p = store.add_draft("t", None, ta.good(1))

    class B:
        async def send_message(self, *a, **k):
            return type("M", (), {"message_id": 1})()

    when = asyncio.run(bmod.release(B(), cfg, store, p, notice=False))
    assert when >= now + timedelta(minutes=20) - timedelta(seconds=2)  # 5 + 15 minutes
    p2 = store.add_draft("t2", None, ta.good(2, "تیتر دیگر"))
    when2 = asyncio.run(bmod.release(B(), cfg, store, p2, notice=False))
    assert when2 >= when + timedelta(minutes=15) - timedelta(seconds=2)  # two source posts are 15 minutes apart (times are stored in whole seconds)


def test_upcoming_calendar_slots_come_from_the_clock_and_skip_done_ones():
    d, store, _, _ = td.make()
    assert store.extra_busy == d.upcoming_times

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 8, 50, tzinfo=td.TZ)  # Saturday 08:50

    d.state.mark("2026-10-10:report-0900", "auto")
    real = H.datetime
    H.datetime = FakeDT
    try:
        slots = d.upcoming_times()
    finally:
        H.datetime = real
    local = sorted(s.astimezone(td.TZ).strftime("%H:%M") for s in slots)
    assert local == ["09:20", "10:30", "11:15", "12:00", "13:30", "14:15", "15:00", "16:30", "17:00"]  # 08:30 passed, 09:00 done


def test_daily_cap_for_source_posts():
    cfg = Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1}, auto_publish=True, auto_max_per_day=2)
    store = Store(":memory:")
    state = AutoState(os.path.join(tempfile.mkdtemp(), "calm-test.json"))
    for i in range(2):
        p = store.add_draft(f"s{i}", None, ta.good(i, f"تیتر {i}"), source_url=f"https://x/{i}")
        store.schedule(p.id, datetime.now(timezone.utc))
    daily_post = store.add_draft("d", "daily:news", ta.good(9, "روزانه"))
    store.schedule(daily_post.id, datetime.now(timezone.utc))  # calendar posts do not count
    assert store.count_regular_since(datetime.now(cfg.tz).replace(hour=0, minute=0, second=0)) == 2
    out = asyncio.run(bmod.run_fetch(None, cfg, store, None, [], [1], 2, state))
    assert "سقف امروز پر است" in out


def test_news_digest_and_prompts_follow_the_new_priorities():
    _, task, tag = build.news_task(td.at(8, 30))
    assert "اولین پست روز" in task and "ایران و آمریکا" in task and "دلار" in task and tag == "#اخبار_موثر_بر_بازار"
    assert "نام هیچ سهم" in build.close_task(td.at(16, 30))[0] or "نه تک‌تک سهم‌ها" in build.close_task(td.at(16, 30))[0]
    assert "نام هیچ سهم" in build.open_task(td.at(9, 20))[0]
    d, store, w, _ = td.make(replies=[td.good("x")])
    assert "یک سهم یا نماد مشخص" in w._system[0]["text"]  # the stock rule is always on
    fake = ta.FakeClient(["SKIP"])
    w2 = ta.Writer(ta.cfg(), "<kb>", client=fake)
    asyncio.run(w2.rewrite("عنوان", "متن", "https://x.test", "منبع", None))
    assert "فقط مطلب «مهم»" in fake.calls[0]["messages"][0]["content"]


def test_report_can_drop_single_stocks_and_industry_candidates_are_groups_only():
    d, *_ = td.make()
    clean = d.cfg.exclusions.clean_report(td.REP)
    assert [n for _, n in build.industry_candidates(clean, [])] == ["فلزات", "دارویی"]
    assert [n for _, n in build.industry_candidates(clean, ["فلزات"])] == ["دارویی", "فلزات"]
    d.s.report_stocks = False
    rep = asyncio.run(d._report())
    assert rep.top_in_stocks == [] and rep.top_out_stocks == [] and [r.name for r in rep.top_in_groups] == ["فلزات", "دارویی"]
    post = build.report_post(td.TPL, rep, td.at(9, 0))
    assert "فولاد" not in post and "فلزات" in post


def test_capital_increase_post_at_17_uses_only_matching_items_and_skips_when_none():
    from agent.daily.settings import Entry
    e = Entry("capital", "capital", "17:00", list(range(7)))
    now = td.at(16, 58)
    cap = [
        Item("S", "c1", "افزایش سرمایه ۱۰۰ درصدی نماد الف", "هیئت‌مدیره افزایش سرمایه از محل سود انباشته را اعلام کرد.", "u", datetime.now(timezone.utc)),
        Item("S", "c2", "مجمع نماد ب", "افزایش سرمایه ۵۰ درصد از محل آورده نقدی تصویب شد.", "u", datetime.now(timezone.utc)),
    ]
    other = Item("S", "o1", "شاخص بورس", "بازار امروز مثبت بود.", "u", datetime.now(timezone.utc))
    d, store, w, _ = td.make(replies=[td.good("هر چیزی")], items={"capital": cap + [other]})
    text, topic = asyncio.run(d.make(e, now))
    prompt = w.client.calls[0]["messages"][0]["content"]
    assert "نماد الف" in prompt and "نماد ب" in prompt and "شاخص بورس" not in prompt
    assert "تنها استثنای قاعده" in prompt and "توصیهٔ خرید و فروش" in prompt
    assert topic == "سهم‌های دارای افزایش سرمایه | شنبه ۱۴۰۵/۰۷/۱۸"
    assert "<b>سهم‌های دارای افزایش سرمایه | شنبه ۱۴۰۵/۰۷/۱۸</b>" in text and "#افزایش_سرمایه" in prompt
    # nothing about capital increases: no post, no invented content
    d, *_ = td.make(items={"capital": [other]})
    try:
        asyncio.run(d.make(e, now))
        raise AssertionError("should skip")
    except H.Skip:
        pass
    # sources down (no items and errors): try again later instead of skipping the day
    d, *_ = td.make(items={"capital": []})

    async def down(which, hours):
        return [], ["کدال ۷۲۴: ConnectTimeout"]

    d._gather = down
    try:
        asyncio.run(d.make(e, now))
        raise AssertionError("should be NotReady")
    except H.NotReady:
        pass


def test_real_calendar_has_capital_post_every_day_and_sources_file():
    s = load_settings(os.path.join(CUSTOMER, "daily.yaml"))
    cap = next(x for x in s.entries if x.kind == "capital")
    assert cap.time == "17:00" and cap.days == list(range(7))
    assert [x.name for x in load_sources(os.path.join(CUSTOMER, "capital_sources.yaml"))]
    real = Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1})
    assert real.auto_gap_minutes == 3  # dataclass default; the shipped config.yaml sets 30 (checked in test_auto)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
