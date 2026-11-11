"""Phase 3: reliable publishing (claim, retry, dead-letter, unconfirmed) and approval levels (run: python tests/test_reliability.py)."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.bot as bmod  # noqa: E402
import test_auto as ta  # noqa: E402
from agent.autopilot import AutoState  # noqa: E402
from agent.config import Config, load_config  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.sources import load_sources  # noqa: E402
from _profile import P  # noqa: E402


def cfg(**kw):
    return Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1}, auto_publish=True, attribution=False, **kw)


class FlakyBot(ta.Bot):
    """Fails with the given errors one after another, then works."""

    def __init__(self, errors=(), delay=0):
        super().__init__()
        self.errors, self.delay, self.attempts = list(errors), delay, 0

    async def send_message(self, chat, text, **kw):
        if chat == "@c":
            self.attempts += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.errors:
                raise self.errors.pop(0)
        return await super().send_message(chat, text, **kw)


def post(store):
    return store.add_draft("t", None, ta.good(1), None)


def test_claim_prevents_double_publish():
    c, s = cfg(), Store(":memory:")
    p = post(s)
    assert s.begin_send(p.id) and not s.begin_send(p.id)  # the second claim loses
    s.mark_retry(p.id, "x", datetime.now(timezone.utc))
    bot = FlakyBot(delay=0.05)

    async def both():
        return await asyncio.gather(bmod.publish_post(bot, c, s, s.get(p.id)), bmod.publish_post(bot, c, s, s.get(p.id)))
    r = asyncio.run(both())
    assert sorted(ok for ok, _ in r) == [False, True] and bot.attempts == 1 and s.get(p.id).status == "published"


def test_transient_errors_retry_with_backoff_then_dead_letter_then_manual_retry():
    c, s = cfg(max_publish_attempts=3, retry_delays=[30, 120]), Store(":memory:")
    p = post(s)
    bot = FlakyBot([NetworkError("connection reset")] * 3)
    ok, msg = asyncio.run(bmod.publish_post(bot, c, s, s.get(p.id)))
    got = s.get(p.id)
    assert not ok and "🔁" in msg and got.status == "scheduled" and got.attempts == 1
    wait = (datetime.fromisoformat(got.scheduled_at) - datetime.now(timezone.utc)).total_seconds()
    assert 25 < wait <= 31  # 30 s after the first failure
    asyncio.run(bmod.publish_post(bot, c, s, s.get(p.id)))
    wait = (datetime.fromisoformat(s.get(p.id).scheduled_at) - datetime.now(timezone.utc)).total_seconds()
    assert 115 < wait <= 121  # 120 s after the second
    ok, msg = asyncio.run(bmod.publish_post(bot, c, s, s.get(p.id)))
    assert not ok and "💀" in msg and s.get(p.id).status == "dead" and s.get(p.id).attempts == 3
    assert [x.id for x in s.parked()] == [p.id] and s.due(datetime.now(timezone.utc) + timedelta(days=1)) == []  # never retried on its own
    assert s.requeue(p.id, datetime.now(timezone.utc)) and s.get(p.id).attempts == 0
    ok, _ = asyncio.run(bmod.publish_post(bot, c, s, s.get(p.id)))
    assert ok and s.get(p.id).status == "published" and bot.attempts == 4


def test_flood_wait_is_respected_and_permanent_errors_are_not_retried():
    c, s = cfg(retry_delays=[5]), Store(":memory:")
    p = post(s)
    asyncio.run(bmod.publish_post(FlakyBot([RetryAfter(90)]), c, s, s.get(p.id)))
    wait = (datetime.fromisoformat(s.get(p.id).scheduled_at) - datetime.now(timezone.utc)).total_seconds()
    assert s.get(p.id).status == "scheduled" and wait > 85  # Telegram said 90 s: not our own 5 s
    q = post(s)
    ok, msg = asyncio.run(bmod.publish_post(FlakyBot([BadRequest("chat not found")]), c, s, s.get(q.id)))
    assert not ok and s.get(q.id).status == "failed" and "chat not found" in msg and s.get(q.id).attempts == 1


def test_timeout_is_unconfirmed_never_auto_resent():
    c, s = cfg(), Store(":memory:")
    p = post(s)
    ok, msg = asyncio.run(bmod.publish_post(FlakyBot([TimedOut("Timed out")]), c, s, s.get(p.id)))
    assert not ok and s.get(p.id).status == "unconfirmed" and f"/sent {p.id}" in msg and f"/retry {p.id}" in msg
    assert s.due(datetime.now(timezone.utc) + timedelta(days=1)) == []
    assert s.confirm_sent(p.id) and s.get(p.id).status == "published" and not s.confirm_sent(p.id)
    q = post(s)
    asyncio.run(bmod.publish_post(FlakyBot([TimedOut("x")]), c, s, s.get(q.id)))
    assert s.requeue(q.id, datetime.now(timezone.utc))  # "it is not there": send it again


def test_restart_mid_send_becomes_unconfirmed():
    d = os.path.join(tempfile.mkdtemp(), "a.db")
    s = Store(d)
    p = post(s)
    assert s.begin_send(p.id)  # ...the bot dies here
    s2 = Store(d)
    assert s2.recover_sending() == [p.id] and s2.get(p.id).status == "unconfirmed"
    assert s2.recover_sending() == []


def test_due_loop_waits_for_the_retry_time():
    c, s = cfg(), Store(":memory:")
    p = post(s)
    bot = FlakyBot([NetworkError("x")])
    asyncio.run(bmod.publish_post(bot, c, s, s.get(p.id)))
    app = SimpleNamespace(bot_data={"cfg": c, "store": s, "writer": None, "auto": ta.state()})
    ctx = SimpleNamespace(application=app, bot=bot)
    asyncio.run(bmod.publish_due(ctx))
    assert s.get(p.id).status == "scheduled" and bot.attempts == 1  # not due yet (30 s)
    s.schedule(p.id, datetime.now(timezone.utc) - timedelta(seconds=1))
    asyncio.run(bmod.publish_due(ctx))
    assert s.get(p.id).status == "published" and bot.attempts == 2


def test_owner_commands_dead_retry_sent():
    c, s = cfg(), Store(":memory:")
    p, q = post(s), post(s)
    s.begin_send(p.id); s.mark_dead(p.id, "boom")
    s.begin_send(q.id); s.mark_unconfirmed(q.id, "timeout")
    replies = []

    def update(args, uid=1):
        reply = lambda text, **kw: replies.append(text) or asyncio.sleep(0)  # noqa: E731
        return (SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=SimpleNamespace(reply_text=reply)),
                SimpleNamespace(args=args, application=SimpleNamespace(bot_data={"cfg": c, "store": s, "writer": None})))
    run = lambda fn, args, uid=1: asyncio.run(fn(*update(args, uid)))  # noqa: E731
    run(bmod.cmd_dead, [])
    assert f"#{p.id}" in replies[-1] and f"#{q.id}" in replies[-1] and "boom" in replies[-1]
    run(bmod.cmd_dead, [], uid=99)  # a stranger gets nothing
    assert len(replies) == 1
    run(bmod.cmd_sent, [str(q.id)])
    assert s.get(q.id).status == "published"
    run(bmod.cmd_retry, [str(p.id)])
    assert s.get(p.id).status == "scheduled"
    run(bmod.cmd_retry, ["999"])
    assert "پیدا نشد" in replies[-1]


# ---------------------------------------------------------------- approval levels


def _dispatch(c, trusted):
    s, st = Store(":memory:"), ta.state()
    p = post(s)
    bot = ta.Bot()
    out = asyncio.run(bmod.dispatch_post(bot, c, s, st, p, [1], trusted=trusted))
    return out, s.get(p.id).status, bot


def test_approval_levels():
    assert _dispatch(cfg(approval="auto"), False)[0] == "auto"
    out, status, bot = _dispatch(cfg(approval="trusted"), False)
    assert out == "review" and status == "draft" and bot.sent  # untrusted source: owner reviews
    assert _dispatch(cfg(approval="trusted"), True)[0] == "auto"
    c = cfg(approval="review")
    c.auto_publish = False
    assert _dispatch(c, True)[0] == "review"


def test_approval_config_and_trusted_source_flag():
    base = os.path.join(os.path.dirname(__file__), "..", "customers", "pouyan")
    keep = dict(os.environ)
    try:
        os.environ.update(PLATFORM="telegram", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHANNEL="c", OWNER_IDS="1", LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
        c = load_config(os.path.join(base, "config.yaml"))
        assert c.approval == "auto" and c.auto_publish and c.max_publish_attempts == 4 and c.retry_delays == [30, 120, 600]
        d = tempfile.mkdtemp()
        import shutil
        for f in ("profile.yaml", "post_template.yaml"):
            shutil.copy(os.path.join(base, f), d)
        raw = open(os.path.join(base, "config.yaml"), encoding="utf-8").read()
        open(os.path.join(d, "config.yaml"), "w", encoding="utf-8").write(raw.replace("approval: auto", "approval: trusted"))
        assert load_config(os.path.join(d, "config.yaml")).approval == "trusted"
        open(os.path.join(d, "config.yaml"), "w", encoding="utf-8").write(raw.replace("approval: auto", "approval: review"))
        r = load_config(os.path.join(d, "config.yaml"))
        assert r.approval == "review" and not r.auto_publish  # review level means nothing is published on its own
        open(os.path.join(d, "config.yaml"), "w", encoding="utf-8").write(raw.replace("approval: auto\n", ""))
        assert load_config(os.path.join(d, "config.yaml")).approval == "auto"  # old files with only auto_publish: true
        open(os.path.join(d, "config.yaml"), "w", encoding="utf-8").write(raw.replace("approval: auto", "approval: maybe"))
        try:
            load_config(os.path.join(d, "config.yaml"))
            raise AssertionError
        except SystemExit as e:
            assert "approval" in str(e)
    finally:
        os.environ.clear(); os.environ.update(keep)
    f = os.path.join(tempfile.mkdtemp(), "s.yaml")
    open(f, "w", encoding="utf-8").write("sources:\n  - {name: A, type: rss, url: 'https://a.test/f', trusted: true}\n  - {name: B, type: rss, url: 'https://b.test/f'}\n")
    a, b = load_sources(f)
    assert a.trusted and not b.trusted


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
