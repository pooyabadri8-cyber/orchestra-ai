"""Phase 4: the generic weekly calendar and the second-customer (non-financial) pilot (run: python tests/test_planner.py)."""
import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.planner as pl  # noqa: E402
from agent.bot import build_app  # noqa: E402
from agent.sources import Item  # noqa: E402
from agent.store import Store  # noqa: E402
from test_product import CUSTOMERS, Env, Fake, writer_for  # noqa: E402
import test_auto as ta  # noqa: E402

TZ = ZoneInfo("Asia/Tehran")
CAFE = os.path.join(CUSTOMERS, "example-cafe")
POST = ("#دم_آوری\n📌  نکتهٔ دم‌آوری در خانه\n\n"
        "🔹برای دم‌آوری بهتر، قهوه را پیش از دم‌آوری آسیاب کنید و از آب با دمای مناسب استفاده کنید تا طعم دانه کامل حفظ شود.\n"
        "این نکته برای همهٔ روش‌های دم‌آوری، از اسپرسو تا کالدبرو، درست است و تفاوت طعم را نشان می‌دهد.\n\n"
        "🔹قهوهٔ آسیاب‌شده را در ظرف دربسته و دور از نور نگه دارید.\n\n"
        "☝ برای نکته‌های بیشتر دربارهٔ قهوه، کانال را دنبال کنید.")


def settings(extra=""):
    p = os.path.join(tempfile.mkdtemp(), "calendar.yaml")
    open(p, "w", encoding="utf-8").write(extra)
    return pl.load_settings(p)


def test_settings_load_validate_and_default_off():
    s = pl.load_settings(os.path.join(CAFE, "calendar.yaml"))
    assert [e.id for e in s.entries] == ["tip", "event", "weekly"] and s.entries[0].days == [5, 0, 2] and s.entries[2].kind == "digest"
    assert not pl.load_settings("/nonexistent.yaml").enabled
    for bad, word in [("schedule: [{id: a, kind: x, time: '10:00', task: t}]", "kind"),
                      ("schedule: [{id: a, time: '25:00', task: t}]", "ساعت"),
                      ("schedule: [{id: a, time: '10:00'}]", "task"),
                      ("schedule: [{id: 'a b', time: '10:00', task: t}]", "id"),
                      ("schedule: [{id: a, time: '10:00', task: t, days: [xyz]}]", "روز"),
                      ("schedule: [{id: a, time: '10:00', task: t}, {id: a, time: '11:00', task: t}]", "تکراری")]:
        try:
            settings(bad)
            raise AssertionError(bad)
        except SystemExit as e:
            assert word in str(e), (bad, str(e))


def make_planner(sched, replies, auto=True, approval="auto", sources=()):
    with Env():
        fake = Fake(replies)
        w, cfg = writer_for("example-cafe", fake)
    cfg.auto_publish, cfg.approval = auto, approval
    cfg.auto_gap_minutes, cfg.auto_from, cfg.auto_to = 0, "", ""
    st = ta.state()
    store = Store(":memory:")
    app = SimpleNamespace(bot_data={"cfg": cfg, "store": store, "writer": w, "auto": st, "sources": list(sources)})
    s = settings(sched)
    s.state_file = os.path.join(tempfile.mkdtemp(), "cal.json")
    return pl.Planner(app, cfg, s), store, fake, ta.Bot()


TIP = "schedule:\n  - {id: tip, kind: topic, days: [sat], time: '11:00', pillar: 'نکتهٔ قهوه', task: 'نکته دربارهٔ {angle}', angles: [A, B]}\n"
SAT = datetime(2026, 11, 14, 11, 0, tzinfo=TZ)  # a Saturday


def test_topic_entry_runs_once_rotates_angles_and_publishes():
    p, store, fake, bot = make_planner(TIP, [POST, POST], approval="auto")
    asyncio.run(p.tick(bot, SAT - timedelta(minutes=10)))  # too early
    assert not fake.calls
    asyncio.run(p.tick(bot, SAT))
    assert len(fake.calls) == 1 and "نکته دربارهٔ A" in fake.calls[0]["messages"][0]["content"]
    post = store.get(1)
    assert post.status == "published" and post.pillar == "daily:tip" and bot.sent  # auto level: published on its own
    asyncio.run(p.tick(bot, SAT + timedelta(minutes=1)))  # the same day again: nothing
    assert len(fake.calls) == 1
    asyncio.run(p.tick(bot, SAT + timedelta(days=7)))  # next Saturday: next angle
    assert len(fake.calls) == 2 and "نکته دربارهٔ B" in fake.calls[1]["messages"][0]["content"]
    p2 = pl.Planner(p.app, p.cfg, p.s)  # restart: state is on disk, no double post
    asyncio.run(p2.tick(bot, SAT + timedelta(hours=1)))
    assert len(fake.calls) == 2


def test_review_level_sends_to_owner_and_never_publishes():
    p, store, fake, bot = make_planner(TIP, [POST], auto=False, approval="review")
    asyncio.run(p.tick(bot, SAT))
    assert store.get(1).status == "draft" and any(c == 1 for c, *_ in bot.sent)


def test_wrong_day_late_pause_and_skip():
    p, store, fake, bot = make_planner(TIP, [POST])
    asyncio.run(p.tick(bot, SAT + timedelta(days=1)))  # Sunday: not in `days`
    assert not fake.calls
    asyncio.run(p.tick(bot, SAT + timedelta(minutes=45)))  # bot was off: too late, skipped and not written
    assert not fake.calls and p.state.done[f"{SAT.date().isoformat()}:tip"] == "missed"
    p, store, fake, bot = make_planner(TIP, [POST])
    p.app.bot_data["auto"].set_paused(True)
    asyncio.run(p.tick(bot, SAT))
    assert not fake.calls  # /pause stops the calendar too


def test_model_failure_is_retried_then_reported_when_missed():
    from agent.writer import LLMError

    class Boom(Fake):
        async def _c(self, **kw):
            raise LLMError("quota")
    p, store, fake, bot = make_planner(TIP, [POST])
    p.writer.client = Boom()
    p.writer.cfg.provider = "anthropic"
    asyncio.run(p.tick(bot, SAT))
    assert p._tries[f"{SAT.date().isoformat()}:tip"][0] == 1 and not store.queue()
    asyncio.run(p.tick(bot, SAT + timedelta(minutes=40)))
    assert any("ساخته نشد" in str(t) for _, t, _ in bot.sent)  # the owner is told once it is too late


def test_digest_uses_recent_items_and_skips_when_there_are_none():
    import agent.planner as mod
    sched = "schedule:\n  - {id: weekly, kind: digest, days: [sat], time: '11:00', window_hours: 48, task: 'خلاصهٔ هفته'}\n"
    now = datetime.now(timezone.utc)
    items = [Item("S", "k1", "خبر تازه یک", "متن خبر یک دربارهٔ قهوه", "https://s.test/1", now - timedelta(hours=3)),
             Item("S", "k2", "خبر کهنه", "متن قدیمی", "https://s.test/2", now - timedelta(days=9))]

    async def fake_fetch(http, src, **kw):
        return items

    class NoHttp:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    real = mod.fetch_source, mod.new_http_client
    mod.fetch_source, mod.new_http_client = fake_fetch, lambda: NoHttp()
    try:
        p, store, fake, bot = make_planner(sched, [POST], sources=[SimpleNamespace(name="S")])
        asyncio.run(p.tick(bot, SAT))
        material = fake.calls[0]["messages"][0]["content"]
        assert "خبر تازه یک" in material and "خبر کهنه" not in material and "https://s.test" not in material
        assert re.search(r"\bS\b", material) is None  # the source name never reaches the model
        items.clear()
        p, store, fake, bot = make_planner(sched, [POST], sources=[SimpleNamespace(name="S")])
        asyncio.run(p.tick(bot, SAT))
        assert not fake.calls and p.state.done[f"{SAT.date().isoformat()}:weekly"] == "skipped"
    finally:
        mod.fetch_source, mod.new_http_client = real


def test_cafe_pilot_registers_calendar_not_finance():
    with Env():
        os.chdir(CAFE)
        from agent.config import load_config
        d = tempfile.mkdtemp()
        cfg = load_config("config.yaml")
        cfg.db_path, cfg.auto_state_file = os.path.join(d, "a.db"), os.path.join(d, "a.json")
        app = build_app(cfg)
        cmds = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
        assert "planner" in app.bot_data and "calendar" in cmds and "gozaresh" not in cmds and "daily" not in cmds
        assert cfg.approval == "review"  # the pilot starts with the owner approving every post
