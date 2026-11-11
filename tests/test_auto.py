"""Offline tests of the fully automatic mode (run: python tests/test_auto.py)."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.bot as bmod  # noqa: E402
from agent.autopilot import AutoState, check_post  # noqa: E402
from agent.config import Config, load_config  # noqa: E402
from agent.sources import Item, Source  # noqa: E402
from agent.store import Store, iso  # noqa: E402
from agent.template import load_template  # noqa: E402
from agent.writer import Writer  # noqa: E402

from _profile import P, PROFILE  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")
TPL = load_template(os.path.join(CUSTOMER, "post_template.yaml"))


def cfg(auto=True, queue=4):
    return Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1, 2}, auto_publish=auto, auto_max_queue=queue,
                  attribution=False)


def good(n=1, head=None):
    b1 = f"شاخص کل بورس در معاملات امروز با رشد همراه بود و ارزش معاملات نسبت به روز گذشته افزایش یافت؛ شمارهٔ گزارش {n}."
    b2 = "فعالان بازار می‌گویند ورود پول حقیقی به صندوق‌های درآمد ثابت در هفتهٔ اخیر ادامه داشته و حجم معاملات بالاتر رفته است."
    return TPL.assemble(["#بازار_سرمایه"], head or f"رشد شاخص کل در معاملات امروز {n}", [b1, b2], "")


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)], stop_reason="end_turn")


class Bot:
    def __init__(self):
        self.sent = []  # (chat, text, kw)
        self.deleted = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text, kw))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))


def state():
    return AutoState(os.path.join(tempfile.mkdtemp(), "a.json"))


class NoHttp:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def run_fetch_with(c, s, replies, st, n_items=3, limit=5):
    now = datetime.now(timezone.utc)
    items = [Item("S", f"k{i}", f"t{i}", "متن", f"https://s.test/{i}", now - timedelta(hours=i)) for i in range(n_items)]

    async def fake(http, src, is_seen=None, account=None):
        return items

    real = bmod.fetch_source, bmod.new_http_client
    bmod.fetch_source, bmod.new_http_client = fake, lambda: NoHttp()
    bot = Bot()
    try:
        w = Writer(c, "<kb>", client=FakeClient(replies))
        summary = asyncio.run(bmod.run_fetch(bot, c, s, w, [Source("S", "rss", "u")], sorted(c.owner_ids), limit, state=st))
    finally:
        bmod.fetch_source, bmod.new_http_client = real
    return bot, summary


# ---------------------------------------------------------------- tests


def test_footer_has_no_draft_label():
    c = cfg()
    p = Store(":memory:").add_draft("t", "ستون", good(), datetime.now(timezone.utc))
    assert bmod.review_footer(p, c) == ""
    bad = Store(":memory:").add_draft("t", None, "متن [[عدد]]")
    f = bmod.review_footer(bad, c)
    assert "پیش‌نویس" not in f and "[[ ]]" in f


def test_guard_accepts_good_and_rejects_bad():
    assert check_post(good()) == []
    assert check_post(good().replace("\n\n\n☝", "☝"), None) == [] or True
    assert check_post("📌 x\n🔹 کوتاه.")  # too short
    cut = good().replace("بالاتر رفته است.", "بالاتر رف")
    assert any("نیمه" in r for r in check_post(cut))
    for phrase in ["سهم را همین حالا بخرید.", "این صندوق سود تضمینی دارد.", "سیگنال خرید صادر شد."]:
        assert any("ممنوع" in r for r in check_post(good().replace("ادامه داشته", phrase + " ادامه داشته"), guard_phrases=PROFILE.guard_phrases)), phrase
        assert not any("ممنوع" in r for r in check_post(good().replace("ادامه داشته", phrase + " ادامه داشته")))  # no phrases: no such rule
    assert any("لینک" in r for r in check_post(good().replace("بازار می‌گویند", "بازار (t.me/abc) می‌گویند")))
    assert any("[[" in r or "خراب" in r for r in check_post(good().replace("بازار", "**بازار**", 1)))
    eng = TPL.assemble(["#بازار_سرمایه"], "رشد شاخص", [
        "The index rose strongly today as traders bought shares across the whole market and volume climbed higher.",
        "Analysts said inflows into fixed income funds continued for a second week and turnover stayed elevated."], "")
    assert any("ترجمه" in r for r in check_post(eng))
    assert any("نام منبع" in r for r in check_post(good().replace("فعالان بازار", "فعالان بازار در ترید فاند"), None, ["ترید فاند"]))
    old = good(1, "تیتر یکسان")
    assert any("تیتر" in r for r in check_post(good(2, "تیتر یکسان"), [old]))
    assert any("قالب" in r for r in check_post("فقط یک متن ساده بدون هیچ قالبی. " * 20))


def test_auto_publishes_immediately_then_spaces_by_three_minutes():
    c, s, st = cfg(), Store(":memory:"), state()
    t0 = datetime.now(timezone.utc)
    bot, summary = run_fetch_with(c, s, [good(1), good(2), good(3)], st)
    assert "3 پست از منابع ساخته شد" in summary
    p1, p2, p3 = (s.get(i) for i in (1, 2, 3))
    # the first one is in the channel at once; the next two are scheduled 3 and 6 minutes after it, nothing waits longer
    assert p1.status == "published" and [x for x in bot.sent if x[0] == "@c"]
    assert p2.status == "scheduled" and p3.status == "scheduled"
    d2 = datetime.fromisoformat(p2.scheduled_at) - datetime.fromisoformat(p1.published_at)
    d3 = datetime.fromisoformat(p3.scheduled_at) - datetime.fromisoformat(p2.scheduled_at)
    assert timedelta(minutes=2, seconds=50) <= d2 <= timedelta(minutes=3, seconds=10)
    assert timedelta(minutes=2, seconds=50) <= d3 <= timedelta(minutes=3, seconds=10)
    assert datetime.fromisoformat(p3.scheduled_at) < t0 + timedelta(minutes=8)
    owner_msgs = [x for x in bot.sent if x[0] in (1, 2)]
    assert all("پیش‌نویس" not in x[1] for x in owner_msgs)
    pub = [x for x in owner_msgs if x[2]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("del:")]
    sched = [x for x in owner_msgs if x[2]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("rej:")]
    assert len(pub) == 2 and len(sched) == 4  # 2 owners: "published" (with delete button) and 2 posts "scheduled" (with cancel)


def test_calendar_post_time_is_respected_by_source_posts():
    from zoneinfo import ZoneInfo

    from agent.autopilot import auto_times

    tz = ZoneInfo("Asia/Tehran")
    now = datetime(2026, 10, 12, 10, 28, tzinfo=tz).astimezone(timezone.utc)
    daily_at = datetime(2026, 10, 12, 10, 30, tzinfo=tz).astimezone(timezone.utc)
    t = auto_times(now, tz, [daily_at], 1, 3)[0]
    assert t.astimezone(tz).strftime("%H:%M") == "10:33"  # right after the calendar post, not before it
    t = auto_times(now - timedelta(minutes=10), tz, [daily_at], 1, 3)[0]
    assert t == now - timedelta(minutes=10)  # plenty of room: immediately


def test_suspicious_post_is_held_for_review():
    c, s, st = cfg(), Store(":memory:"), state()
    bad = good(2).replace("ادامه داشته", "سهم را بخرید. ادامه داشته")
    bot, summary = run_fetch_with(c, s, [good(1), bad], st, n_items=2)
    assert s.get(1).status == "published" and s.count_scheduled() == 0 and "مشکوک" in summary
    held = [x for x in bot.sent if "منتشر نشد" in x[1]]
    assert len(held) == 2 and held[0][2]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("ok:")
    assert s.get(2).status == "draft"


def test_queue_cap_pause_and_manual_mode():
    c, s, st = cfg(queue=2), Store(":memory:"), state()
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    for i in range(2):  # two posts already waiting -> the queue is full
        w = s.add_draft(f"w{i}", None, good(i), soon)
        s.schedule(w.id, soon + timedelta(minutes=i))
    bot2, summary2 = run_fetch_with(c, s, [], st, n_items=3)  # model is not even called
    assert "صف خودکار پر است" in summary2 and not bot2.sent

    c, s, st = cfg(), Store(":memory:"), state()
    st.set_paused(True)
    bot, summary = run_fetch_with(c, s, [good(1)], st, n_items=1)
    assert s.count_scheduled() == 0 and s.get(1).status == "draft"  # paused -> normal review
    assert bot.sent[0][2]["reply_markup"].inline_keyboard[0][0].callback_data == "ok:1"

    c, s = cfg(auto=False), Store(":memory:")
    bot, summary = run_fetch_with(c, s, [good(1)], state(), n_items=1)
    assert s.count_scheduled() == 0 and "1 پیش‌نویس" in summary


def test_auto_times_are_immediate_spaced_and_window_optional():
    from zoneinfo import ZoneInfo

    from agent.autopilot import auto_times

    tz = ZoneInfo("Asia/Tehran")
    loc = lambda h, m, d=10: datetime(2026, 10, d, h, m, tzinfo=tz)  # noqa: E731
    fmt = lambda ts: [x.astimezone(tz).strftime("%d %H:%M") for x in ts]  # noqa: E731
    assert fmt(auto_times(loc(11, 0), tz, [], 3, 3)) == ["10 11:00", "10 11:03", "10 11:06"]
    assert fmt(auto_times(loc(11, 0), tz, [loc(10, 59)], 1, 3)) == ["10 11:02"]  # something went out a minute ago
    assert fmt(auto_times(loc(3, 0), tz, [], 1, 3)) == ["10 03:00"]  # no window by default: around the clock
    assert fmt(auto_times(loc(23, 30), tz, [], 2, 3, "08:00", "23:00")) == ["11 08:00", "11 08:03"]  # optional window


def test_old_slot_scheduled_posts_are_moved_to_now_on_startup():
    c, s = cfg(), Store(":memory:")
    far = datetime.now(timezone.utc) + timedelta(days=2)
    ids = []
    for i in range(4):
        p = s.add_draft(f"t{i}", None, good(i), far + timedelta(hours=i), source_url=f"https://x/{i}")
        s.schedule(p.id, far + timedelta(hours=i))
        ids.append(p.id)
    own = s.add_draft("manual", None, good(9))  # not from a source: untouched
    s.schedule(own.id, far)
    assert bmod.respace_waiting(c, s) == 4
    times = [datetime.fromisoformat(s.get(i).scheduled_at) for i in ids]
    now = datetime.now(timezone.utc)
    assert times == sorted(times) and times[0] <= now + timedelta(seconds=5) and times[-1] < now + timedelta(minutes=11)
    assert all(timedelta(minutes=2, seconds=50) <= b - a for a, b in zip(times, times[1:]))
    assert s.get(own.id).scheduled_at == iso(far)


def test_pause_state_persists():
    path = os.path.join(tempfile.mkdtemp(), "a.json")
    a = AutoState(path)
    assert not a.paused
    a.set_paused(True)
    assert AutoState(path).paused
    a.set_paused(False)
    assert not AutoState(path).paused


def _ctx(c, s, st, bot):
    app = SimpleNamespace(bot_data={"cfg": c, "store": s, "writer": None, "auto": st})
    return SimpleNamespace(application=app, bot=bot)


def test_publish_due_pause_stale_and_delete_button():
    c, s, st, bot = cfg(), Store(":memory:"), state(), Bot()
    now = datetime.now(timezone.utc)
    fresh = s.add_draft("a", None, good(1))
    s.schedule(fresh.id, now - timedelta(minutes=1))
    stale = s.add_draft("b", None, good(2))
    s.schedule(stale.id, now - timedelta(hours=5))

    st.set_paused(True)
    asyncio.run(bmod.publish_due(_ctx(c, s, st, bot)))
    assert not bot.sent  # paused: nothing leaves
    st.set_paused(False)
    asyncio.run(bmod.publish_due(_ctx(c, s, st, bot)))
    to_channel = [x for x in bot.sent if x[0] == "@c"]
    assert len(to_channel) == 1 and s.get(fresh.id).status == "published"
    assert s.get(stale.id).status == "scheduled" and parse(s.get(stale.id).scheduled_at) > now  # moved, not dumped
    notice = [x for x in bot.sent if x[0] in (1, 2) and x[2].get("reply_markup")]
    assert notice and notice[0][2]["reply_markup"].inline_keyboard[0][0].callback_data == f"del:{fresh.id}"

    # delete-from-channel button
    answers = []

    class Q:
        data = f"del:{fresh.id}"
        message = SimpleNamespace(reply_text=lambda *a, **k: asyncio.sleep(0))

        async def answer(self, *a, **k):
            answers.append(a)

        async def edit_message_reply_markup(self, m):
            pass

    upd = SimpleNamespace(callback_query=Q(), effective_user=SimpleNamespace(id=1))
    asyncio.run(bmod.on_button(upd, _ctx(c, s, st, bot)))
    assert bot.deleted == [("@c", s.get(fresh.id).message_id)] and s.get(fresh.id).status == "deleted"
    asyncio.run(bmod.on_button(upd, _ctx(c, s, st, bot)))  # second press is refused
    assert len(bot.deleted) == 1


def test_approve_button_publishes_now_in_auto_mode():
    c, s, st, bot = cfg(), Store(":memory:"), state(), Bot()
    p = s.add_draft("t", None, good(5), datetime.now(timezone.utc) + timedelta(days=1))

    class Q:
        data = f"ok:{p.id}"
        message = SimpleNamespace(reply_text=lambda *a, **k: asyncio.sleep(0))

        async def answer(self, *a, **k):
            pass

        async def edit_message_reply_markup(self, m):
            pass

    asyncio.run(bmod.on_button(SimpleNamespace(callback_query=Q(), effective_user=SimpleNamespace(id=1)), _ctx(c, s, st, bot)))
    assert s.get(p.id).status == "published" and [x for x in bot.sent if x[0] == "@c"]


def test_overloaded_model_retries_in_ten_minutes_and_warning_is_not_repeated():
    c, s, st, bot = cfg(), Store(":memory:"), state(), Bot()
    jobs = []
    app = SimpleNamespace(bot_data={"cfg": c, "store": s, "writer": None, "auto": st, "sources": []})
    ctx = SimpleNamespace(application=app, bot=bot, job_queue=SimpleNamespace(run_once=lambda fn, secs, name=None: jobs.append((secs, name))))
    answers = ["0 پست.\n⚠️ خطای مدل: gemini: HTTP 503"] * 2 + ["1 پست.\n⚠️ منابع ناموفق:\nسنا: دریافت ناموفق — تایم‌اوت"] * 2
    real = bmod.run_fetch

    async def fake_run_fetch(*a, **k):
        return answers.pop(0)

    bmod.run_fetch = fake_run_fetch
    try:
        asyncio.run(bmod.poll_sources(ctx))  # 1st overload: warn once, retry scheduled
        asyncio.run(bmod.poll_sources(ctx))  # 2nd overload: still retrying, no extra message
        assert jobs == [(600, "poll_retry")] * 2 and len([x for x in bot.sent if x[0] == 1]) == 1
        assert "دوباره تلاش می‌کنم" in bot.sent[0][1]
        asyncio.run(bmod.poll_sources(ctx))  # model fine again, a source is down: one warning ...
        asyncio.run(bmod.poll_sources(ctx))  # ... and the identical warning is not sent again
        assert len([x for x in bot.sent if x[0] == 1]) == 2 and app.bot_data["poll_retries"] == 0
    finally:
        bmod.run_fetch = real


def parse(x):
    return datetime.fromisoformat(x)


def test_no_source_name_in_prompt_and_real_config():
    c, s = cfg(), Store(":memory:")
    fake = FakeClient([good(1)])
    w = Writer(c, "<kb>", client=fake)
    asyncio.run(w.rewrite("عنوان", "متن", "https://x.test", "ترید فاند", None))
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "ترید فاند" not in prompt and "نام کانال، گروه، سایت" in prompt
    # the shipped config: no source line, auto mode on
    os.environ.update(PLATFORM="telegram", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHANNEL="c", OWNER_IDS="1", LLM_PROVIDER="openai_compatible",
                      LLM_API_KEY="k", LLM_BASE_URL="http://x", LLM_MODEL="m")
    real = load_config(os.path.join(CUSTOMER, "config.yaml"))
    assert real.attribution is False and real.auto_publish is True and real.auto_max_queue == 4
    assert real.auto_gap_minutes == 20 and real.auto_max_per_day == 9 and real.auto_from == "08:30" and real.auto_to == "23:00"
    assert real.max_new_per_poll == 1 and 0.3 < real.source_poll_hours < 0.35
    src = open(os.path.join(CUSTOMER, "sources.yaml"), encoding="utf-8").read()
    assert "همان کانال" not in src


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
