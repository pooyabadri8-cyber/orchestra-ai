"""Offline tests of the daily content calendar (run: python tests/test_daily.py)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.bot as bmod  # noqa: E402
from agent.autopilot import AutoState  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.daily import build  # noqa: E402
from agent.daily.handlers import Daily, DailyState  # noqa: E402
from agent.daily.settings import load_settings  # noqa: E402
from agent.gozaresh.parse import parse_page  # noqa: E402
from agent.sources import Item, Source, load_sources  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.template import SEP, load_template  # noqa: E402
from agent.writer import Writer  # noqa: E402

from _profile import P  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")
TZ = ZoneInfo("Asia/Tehran")
TPL = load_template(os.path.join(CUSTOMER, "post_template.yaml"))
REP = parse_page(open(os.path.join(ROOT, "tests", "fixtures", "bourse_trader_sample.html"), encoding="utf-8").read())
SAT = datetime(2026, 10, 10, tzinfo=TZ)  # a Saturday (1405/07/18)


def at(h, m, base=SAT):
    return base.replace(hour=h, minute=m)


def good(head="تیتر نمونه", n=1):
    return TPL.assemble(["#تحلیل_بازار"], head, [
        f"شاخص کل در معاملات امروز با رشد همراه بود و ارزش معاملات نسبت به روز گذشته بیشتر شد؛ نمونهٔ {n}.",
        "ورود پول حقیقی به صندوق‌ها و گروه‌های پیشرو ادامه داشته و فعالان بازار آن را زیر نظر دارند."], "")


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)], stop_reason="end_turn")


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text, kw))
        return SimpleNamespace(message_id=500 + len(self.sent))


def make(replies=(), items=None, rep=REP, auto=True, sources=()):
    import tempfile

    cfg = Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1}, auto_publish=auto)
    store = Store(":memory:")
    writer = Writer(cfg, "<kb>", client=FakeClient(replies))
    d = tempfile.mkdtemp()

    class Goz:
        async def fetch(self_inner):
            if isinstance(rep, Exception):
                raise rep
            return rep

    app = SimpleNamespace(bot_data={
        "cfg": cfg, "store": store, "writer": writer, "sources": list(sources),
        "auto": AutoState(os.path.join(d, "a.json")), "gozaresh": Goz(),
    })
    s = load_settings(os.path.join(CUSTOMER, "daily.yaml"))
    s.state_file = os.path.join(d, "daily.json")
    s.news_sources_file = os.path.join(d, "none.yaml")
    daily = Daily(app, cfg, s)
    daily.news_sources = [Source("n", "rss", "u")] if items is not None else []

    async def fake_gather(which, hours):
        return (items or {}).get(which, []), []

    daily._gather = fake_gather
    return daily, store, writer, app


def posts(store):
    return [p for p in (store.get(i) for i in range(1, 60)) if p]


def mk_items(n, prefix="خبر", text="متن خبر"):
    now = datetime.now(timezone.utc)
    return [Item("S", f"k{i}", f"{prefix} {i}", text, f"https://s.test/{i}", now - timedelta(hours=i)) for i in range(n)]


# ---------------------------------------------------------------- tests


def test_real_daily_yaml():
    s = load_settings(os.path.join(CUSTOMER, "daily.yaml"))
    ids = [e.id for e in s.entries]
    assert len(ids) == 11 and ids[0] == "news" and ids[-1] == "capital" and "close" in ids and "industry" in ids and "symbol" not in ids
    assert [e.time for e in s.entries if e.kind == "report"] == ["09:00", "10:30", "12:00", "13:30", "15:00"]
    news = next(e for e in s.entries if e.kind == "news")
    gold = next(e for e in s.entries if e.kind == "gold")
    assert news.days == list(range(7)) and gold.days == [5, 6, 0, 1, 2]  # news daily, rest Sat..Wed
    assert load_sources(os.path.join(CUSTOMER, "news_sources.yaml"))


def test_report_post_layout_has_no_source_or_draft_label():
    text = build.report_post(TPL, REP, at(10, 30))
    assert text.startswith("#کارگزاری_پویان\n#گزارش_بازار\n\n\n📌  <b>گزارش بازار سرمایه | ۱۴۰۵/۰۷/۱۸ — ساعت ۱۰:۳۰</b>")
    assert "شاخص کل" in text and "فولاد" in text and SEP in text and "کارگزاری پویان" in text
    assert "bourse-trader" not in text and "منبع" not in text and "پیش‌نویس" not in text and "⚠️" not in text
    assert len(text) < 4096


def test_material_pick_symbol_headline_and_items():
    m = build.report_material(REP, at(9, 20))
    assert "۱۴۰۵/۰۷/۱۸" in m and "شنبه" in m and "فولاد" in m
    kind, name = build.pick_symbol(REP, [], 10)
    assert kind == "سهم" and name == REP.top_in_stocks[0].name
    kind2, name2 = build.pick_symbol(REP, [], 11)
    assert kind2 == "گروه" and name2 == REP.top_in_groups[0].name
    assert build.pick_symbol(REP, [REP.top_in_stocks[0].name], 10)[1] == REP.top_in_stocks[1].name
    t = "#a\n📌  <b>تیتر مدل</b>\n\n🔹x"
    assert "<b>تیتر ثابت</b>" in build.force_headline(t, "تیتر ثابت") and "مدل" not in build.force_headline(t, "تیتر ثابت")
    mat = build.items_material(mk_items(2, text="خلاصه"))
    assert "[۱]" in mat and "s.test" not in mat and "S" not in mat.replace("خبر", "")
    gold = build.match_items(mk_items(2, "Gold price"), build.GOLD_WORDS)
    assert len(gold) == 2 and not build.match_items(mk_items(2), build.GOLD_WORDS)


def test_tick_report_scheduled_once_and_late_ones_missed():
    d, store, _, app = make()
    bot = Bot()
    asyncio.run(d.tick(bot, at(9, 1)))  # news (08:30) too late -> missed; report 09:00 due; open starts 09:08
    got = posts(store)
    assert [p.pillar for p in got] == ["daily:report-0900"] and got[0].status == "published"
    assert d.state.done["2026-10-10:news"] == "missed" and d.state.done["2026-10-10:report-0900"] == "auto"
    asyncio.run(d.tick(bot, at(9, 2)))
    assert len(posts(store)) == 1  # never twice
    assert not [b for b in bot.sent if "ساخته نشد" in b[1]]  # missed ones don't spam the owners
    d2 = Daily(app, d.cfg, d.s)  # a restart keeps the state
    assert d2.state.done["2026-10-10:report-0900"] == "auto"


def test_tick_analysis_uses_data_and_is_checked():
    items = {"market": mk_items(3, "تحلیل بازار"), "news": mk_items(3)}
    d, store, w, _ = make(replies=[good("تحلیل آغاز بازار")], items=items)
    d.state.mark("2026-10-10:news", "x"); d.state.mark("2026-10-10:report-0900", "x")
    asyncio.run(d.tick(Bot(), at(9, 17)))  # open: target 09:20, lead 12 -> starts 09:08
    p = posts(store)[0]
    assert p.pillar == "daily:open" and p.status == "published"
    prompt = w.client.calls[0]["messages"][0]["content"]
    assert "فولاد" in prompt and "تحلیل بازار 0" in prompt and "<material>" in prompt and "SKIP" in prompt

    d, store, _, _ = make(replies=[good("x")], items=items)  # before the lead window -> nothing yet
    d.state.mark("2026-10-10:news", "x"); d.state.mark("2026-10-10:report-0900", "x")
    asyncio.run(d.tick(Bot(), at(9, 5)))
    assert not posts(store)


def test_suspicious_analysis_goes_to_review_not_channel():
    bad = good("تحلیل").replace("ادامه داشته", "ادامه داشته؛ این سهم را بخرید.")
    d, store, _, _ = make(replies=[bad], items={"market": mk_items(2), "news": []})
    d.state.mark("2026-10-10:news", "x"); d.state.mark("2026-10-10:report-0900", "x")
    bot = Bot()
    asyncio.run(d.tick(bot, at(9, 17)))
    p = posts(store)[0]
    assert p.status == "draft" and any("منتشر نشد" in b[1] for b in bot.sent)
    assert d.state.done["2026-10-10:open"] == "held"


def test_news_digest_fixed_headline_and_not_enough_news():
    model = good("تیتر دلخواه مدل")
    d, store, _, _ = make(replies=[model], items={"news": mk_items(6)})
    asyncio.run(d.tick(Bot(), at(8, 27)))  # news at 08:30, lead 12
    p = posts(store)[0]
    assert "<b>خبرهای موثر بر بازار شنبه ۱۴۰۵/۰۷/۱۸</b>" in p.text and "دلخواه" not in p.text

    d, store, _, _ = make(items={"news": mk_items(1)})
    asyncio.run(d.tick(Bot(), at(8, 27)))
    assert not posts(store) and "2026-10-10:news" not in d.state.done  # retried later, nothing invented


def test_report_not_ready_zero_value_and_site_error():
    zero = parse_page(open(os.path.join(ROOT, "tests", "fixtures", "bourse_trader_sample.html"), encoding="utf-8").read())
    zero.retail_value.value = 0
    d, store, _, _ = make(rep=zero)
    d.state.mark("2026-10-10:news", "x")
    bot = Bot()
    asyncio.run(d.tick(bot, at(9, 1)))
    assert not posts(store) and "2026-10-10:report-0900" not in d.state.done
    asyncio.run(d.tick(bot, at(9, 25)))  # past the late window: skipped silently (market closed / holiday)
    assert d.state.done["2026-10-10:report-0900"] == "missed" and not bot.sent

    from agent.gozaresh.parse import GozareshError

    d, store, _, _ = make(rep=GozareshError("ساختار عوض شد"))
    d.state.mark("2026-10-10:news", "x")
    bot = Bot()
    asyncio.run(d.tick(bot, at(9, 1)))
    asyncio.run(d.tick(bot, at(9, 25)))
    assert any("ساخته نشد" in b[1] and "ساختار عوض شد" in b[1] for b in bot.sent)  # real failures are reported


def test_gold_needs_gold_material_and_weekdays():
    d, store, _, _ = make(items={"market": mk_items(3), "news": []})  # nothing about gold
    for e in d.s.entries:
        if e.id != "gold":
            d.state.mark(f"2026-10-10:{e.id}", "x")
    asyncio.run(d.tick(Bot(), at(14, 12)))
    assert not posts(store) and d.state.done["2026-10-10:gold"] == "skipped"

    d, store, _, _ = make(replies=[good("طلا")], items={"market": mk_items(3, "Gold rises", "gold price forecast"), "news": []})
    for e in d.s.entries:
        if e.id != "gold":
            d.state.mark(f"2026-10-10:{e.id}", "x")
    asyncio.run(d.tick(Bot(), at(14, 12)))
    assert posts(store)[0].pillar == "daily:gold"

    thu = datetime(2026, 10, 15, tzinfo=TZ)  # Thursday: only the news digest runs
    d, store, _, _ = make(replies=[good("n")], items={"news": mk_items(5), "market": mk_items(3, "Gold", "gold")})
    asyncio.run(d.tick(Bot(), at(9, 0, thu)))  # report would be due; thursday -> nothing
    assert not posts(store)


def test_industry_of_day_is_a_group_never_a_stock_and_pause_blocks():
    d, store, _, app = make(replies=[good("صنعت")], items={"market": mk_items(2), "news": []})
    for e in d.s.entries:
        if e.id != "industry":
            d.state.mark(f"2026-10-10:{e.id}", "x")
    app.bot_data["auto"].set_paused(True)
    asyncio.run(d.tick(Bot(), at(11, 12)))
    assert not posts(store)  # paused: nothing is produced
    app.bot_data["auto"].set_paused(False)
    asyncio.run(d.tick(Bot(), at(11, 12)))
    assert posts(store)[0].topic.startswith("تحلیل گروه روز: ") and d.state.recent_names() == ["فلزات"]  # a group; funds are gone


def test_manual_mode_sends_for_review():
    d, store, _, _ = make(auto=False)
    d.state.mark("2026-10-10:news", "x")
    bot = Bot()
    asyncio.run(d.tick(bot, at(9, 1)))
    assert posts(store)[0].status == "draft" and bot.sent and bot.sent[0][2]["reply_markup"]


def test_stale_daily_post_is_dropped_and_excluded_from_queue_cap():
    d, store, _, app = make()
    now = datetime.now(timezone.utc)
    p = store.add_draft("t", "daily:report-0900", good(), now)
    store.schedule(p.id, now - timedelta(minutes=50))
    regular = store.add_draft("r", None, good("r", 2), now)
    store.schedule(regular.id, now + timedelta(hours=3))
    assert store.count_scheduled() == 1  # daily posts don't use up the auto queue
    bot = Bot()
    ctx = SimpleNamespace(application=app, bot=bot)
    asyncio.run(bmod.publish_due(ctx))
    assert store.get(p.id).status == "rejected" and not [b for b in bot.sent if b[0] == "@c"]
    assert "t" not in store.recent_topics()  # daily topics stay out of the writer's avoid-list


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
