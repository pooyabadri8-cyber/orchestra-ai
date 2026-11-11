"""Phase 0 fixes in V3 (run: python -m pytest tests/test_phase0.py).

G1: an approved (scheduled) post that is edited, or whose picture changes, goes back to draft and needs a new approval.
G7: only a post claimed with begin_send can be marked published.
Timed posts: approving a late daily-calendar post never moves it to a later post_times slot.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.bot as bmod  # noqa: E402
import test_auto as ta  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.store import Store  # noqa: E402
from _profile import P  # noqa: E402


def cfg(auto: bool):
    return Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1}, auto_publish=auto, attribution=False,
                  approval="auto" if auto else "review", post_times=["11:45", "19:00"])


def press(c, s, bot, action, post_id):
    answers, replies = [], []

    class Q:
        data = f"{action}:{post_id}"
        message = SimpleNamespace(reply_text=lambda *a, **k: replies.append(a) or asyncio.sleep(0))

        async def answer(self, *a, **k):
            answers.append(a)

        async def edit_message_reply_markup(self, m):
            pass

    upd = SimpleNamespace(callback_query=Q(), effective_user=SimpleNamespace(id=1))
    asyncio.run(bmod.on_button(upd, ta._ctx(c, s, ta.state(), bot)))
    return answers, replies


# ---------------------------------------------------------------- G1


def test_edit_after_approval_needs_new_approval():
    s = Store(":memory:")
    when = datetime.now(timezone.utc) - timedelta(minutes=1)
    p = s.add_draft("t", None, ta.good(1))
    s.schedule(p.id, when)
    s.update_text(p.id, ta.good(1, "تیتر ویرایش‌شده"))
    after = s.get(p.id)
    assert after.status == "draft" and after.scheduled_at is None
    assert after.proposed_at == when.isoformat(timespec="seconds")  # the time is kept for the next approval
    assert not s.due(datetime.now(timezone.utc))  # the publish loop no longer picks it up


def test_picture_change_after_approval_needs_new_approval():
    s = Store(":memory:")
    p = s.add_draft("t", None, ta.good(1), image_path="a.jpg")
    s.schedule(p.id, datetime.now(timezone.utc) - timedelta(minutes=1))
    s.set_image(p.id, None)
    assert s.get(p.id).status == "draft" and not s.due(datetime.now(timezone.utc))
    kinds = [r[0] for r in s.db.execute("SELECT kind FROM events WHERE post_id=? ORDER BY id", (p.id,))]
    assert kinds[-1] == "edited"


def test_edit_of_a_draft_or_failed_post_keeps_its_status():
    s = Store(":memory:")
    d = s.add_draft("t", None, ta.good(1))
    s.update_text(d.id, ta.good(1, "تیتر دیگر"))
    assert s.get(d.id).status == "draft"
    f = s.add_draft("t", None, ta.good(2))
    s.mark_failed(f.id, "x")
    s.update_text(f.id, ta.good(2, "تیتر سوم"))
    assert s.get(f.id).status == "failed"  # the owner still publishes it again by hand, as before


def test_published_post_cannot_be_edited():
    s = Store(":memory:")
    p = s.add_draft("t", None, ta.good(1))
    assert s.begin_send(p.id) and s.mark_published(p.id, 5)
    s.update_text(p.id, "دیگر")
    assert s.get(p.id).text == ta.good(1) and s.get(p.id).status == "published"


# ---------------------------------------------------------------- G7


def test_only_a_claimed_post_can_become_published():
    s = Store(":memory:")
    p = s.add_draft("t", None, ta.good(1))
    assert s.mark_published(p.id, 9) is False and s.get(p.id).status == "draft"
    s.reject(p.id)
    assert s.mark_published(p.id, 9) is False and s.get(p.id).status == "rejected"
    q = s.add_draft("t", None, ta.good(2))
    assert s.begin_send(q.id) and s.mark_published(q.id, 10)
    assert s.get(q.id).status == "published" and s.get(q.id).message_id == 10
    assert s.mark_published(q.id, 11) is False and s.get(q.id).message_id == 10  # no second "published"


# ---------------------------------------------------------------- timed posts in review mode


def _daily(s, minutes_ago):
    return s.add_draft("گزارش بازار", "daily:report-1030", ta.good(3), datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


def test_late_but_fresh_timed_post_is_scheduled_now_not_at_the_next_slot():
    c, s, bot = cfg(auto=False), Store(":memory:"), ta.Bot()
    p = _daily(s, minutes_ago=5)
    press(c, s, bot, "ok", p.id)
    got = s.get(p.id)
    assert got.status == "scheduled"
    delay = datetime.fromisoformat(got.scheduled_at) - datetime.now(timezone.utc)
    assert delay < timedelta(seconds=5)  # due on the next publish tick, not hours later


def test_timed_post_approved_before_its_time_keeps_its_time():
    c, s, bot = cfg(auto=False), Store(":memory:"), ta.Bot()
    p = s.add_draft("گزارش بازار", "daily:report-1330", ta.good(3), datetime.now(timezone.utc) + timedelta(minutes=20))
    press(c, s, bot, "ok", p.id)
    assert s.get(p.id).scheduled_at == p.proposed_at


def test_stale_timed_post_is_refused_and_stays_a_draft():
    c, s, bot = cfg(auto=False), Store(":memory:"), ta.Bot()
    p = _daily(s, minutes_ago=int(bmod.DAILY_STALE.total_seconds() // 60) + 5)
    answers, _ = press(c, s, bot, "ok", p.id)
    assert s.get(p.id).status == "draft" and answers and "دیر شده" in answers[0][0]


def test_regular_post_still_uses_post_times():
    c, s, bot = cfg(auto=False), Store(":memory:"), ta.Bot()
    p = s.add_draft("t", None, ta.good(4), datetime.now(timezone.utc) - timedelta(minutes=5))
    press(c, s, bot, "ok", p.id)
    local = datetime.fromisoformat(s.get(p.id).scheduled_at).astimezone(c.tz)
    assert local.strftime("%H:%M") in c.post_times
