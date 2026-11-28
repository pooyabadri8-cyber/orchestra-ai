"""Audience figures: how many people saw each published post, read from a public Telegram channel's web page.

t.me/s/<username> lists a public channel's recent posts with their view counts, with no login. The publisher already
records the message ids of every confirmed send, so each count is matched to the exact post (and package) it belongs
to. Private channels (like a test channel) have no public page: they simply get no figures. Nothing here is posted
or sent anywhere; only the channel's public page is read, through the same route as foreign news sources (VPN).
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import ControlState, Destination, PackageRow, PostView, PublishJob, Run

PAGE = "https://t.me/s/{name}"
MAX_PAGES = 8  # 20 posts a page: enough for a week of a busy channel
_COUNT = re.compile(r"^([\d.,]+)\s*([KkMm]?)$")


def parse_count(text: str) -> int | None:
    """'987' -> 987, '1.2K' -> 1200, '3.4M' -> 3400000, '1,234' -> 1234; None for anything else."""
    m = _COUNT.match((text or "").strip())
    if not m:
        return None
    num, unit = m.group(1).replace(",", ""), m.group(2).upper()
    try:
        value = float(num)
    except ValueError:
        return None
    return int(round(value * {"K": 1e3, "M": 1e6, "": 1}[unit]))


def parse_views(raw: bytes) -> dict[int, int]:
    """{message id: views} of the posts on one page of t.me/s/<channel>."""
    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
    out = {}
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            mid = int(str(msg["data-post"]).rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        span = msg.select_one(".tgme_widget_message_views")
        count = parse_count(span.get_text()) if span else None
        if count is not None:
            out[mid] = count
    return out


def page_url(name: str, before: int | None = None) -> str:
    return PAGE.format(name=name) + (f"?before={before}" if before else "")


async def collect_views(sessions, fetch, now: datetime, *, days: int = 7) -> dict[str, str]:
    """Read the view counts of every post confirmed in the last `days` on a public channel. Returns a status per
    channel; a channel that cannot be read is reported, never a reason to stop."""
    since = now - timedelta(days=days)
    with sessions() as s:
        rows = s.execute(select(PublishJob, Destination.public_username)
                         .join(Destination, Destination.id == PublishJob.destination_id)
                         .where(PublishJob.status == "confirmed", Destination.public_username.is_not(None),
                                PublishJob.updated_at >= since)).all()
    wanted: dict[str, dict[int, int]] = {}  # channel -> message id -> job id
    for job, name in rows:
        for mid in job.message_ids or []:
            wanted.setdefault(name, {})[int(mid)] = job.id
    status = {}
    for name, ids in wanted.items():
        found: dict[int, int] = {}
        before = None
        try:
            for _ in range(MAX_PAGES):
                page = parse_views(await fetch(page_url(name, before), True))
                found.update({k: v for k, v in page.items() if k in ids})
                if not page or set(ids) <= set(found) or min(page) <= min(ids):
                    break
                before = min(page)
        except Exception as e:  # noqa: BLE001  the page could not be read: say so, keep what was read
            status[name] = f"خطا: {str(e)[:150]}"
        with sessions.begin() as s:
            for mid, views in found.items():
                s.merge(PostView(job_id=ids[mid], message_id=mid, views=views, fetched_at=now))
        status.setdefault(name, f"{len(found)} از {len(ids)} پست")
    return status


class AudienceCollector:
    """Run by the control bot's loop: view counts every `every_hours`."""

    def __init__(self, sessions, fetch, clock, every_hours: int = 6):
        self.sessions, self.fetch, self.clock, self.every = sessions, fetch, clock, timedelta(hours=every_hours)
        self.last: dict[str, str] = {}

    def tick(self) -> None:
        now = self.clock()
        with self.sessions() as s:
            row = s.get(ControlState, "audience:last")
        if row is not None and now - datetime.fromisoformat(row.value) < self.every:
            return
        with self.sessions.begin() as s:
            s.merge(ControlState(key="audience:last", value=now.isoformat()))
        self.last = asyncio.run(collect_views(self.sessions, self.fetch, now))
        failed = {k: v for k, v in self.last.items() if v.startswith("خطا")}
        if failed:
            logging.getLogger("orchestra.audience").warning("public channel pages not read: %s", failed)


# ------------------------------------------------------------------ figures for the report


@dataclass
class Audience:
    public: bool = False  # the organization publishes to at least one public channel
    posts: int = 0
    total: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)  # content type -> views of each post
    by_topic: dict[str, list[int]] = field(default_factory=dict)  # a calendar post's pillar and topic -> views
    top: list[tuple[int, str]] = field(default_factory=list)  # (views, headline), most seen first

    def average(self, values: list[int]) -> int:
        return int(statistics.mean(values)) if values else 0


def _headline(post_html: str) -> str:
    """The post's headline: the first bold line of its Telegram text."""
    m = re.search(r"<b>(.*?)</b>", post_html)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:80]
    return ""


def audience(session: Session, org_id: str, lo: datetime, hi: datetime) -> Audience:
    """Views of the posts confirmed in [lo, hi) on public channels: one figure per post (an album's messages share
    one count, so the largest is taken), by content type and by calendar topic, and the most seen."""
    import json
    aud = Audience(public=session.execute(select(Destination.id).where(Destination.org_id == org_id,
                                                                       Destination.public_username.is_not(None))).first() is not None)
    if not aud.public:
        return aud
    rows = session.execute(select(PublishJob, PackageRow).join(PackageRow, PackageRow.package_id == PublishJob.package_id)
                           .where(PublishJob.org_id == org_id, PublishJob.status == "confirmed",
                                  PublishJob.updated_at >= lo, PublishJob.updated_at < hi)).all()
    for job, pkg in rows:
        views = session.execute(select(PostView.views).where(PostView.job_id == job.id)).scalars().all()
        if not views:
            continue
        n = max(views)
        aud.posts += 1
        aud.total += n
        aud.by_kind.setdefault(pkg.content_class, []).append(n)
        run = session.get(Run, pkg.run_id)
        summary = (run.detail or {}).get("summary", "") if run is not None and pkg.content_class == "topic_post" else ""
        if summary:
            aud.by_topic.setdefault(summary[:60], []).append(n)
        headline = _headline(json.loads(pkg.content_json).get("text", {}).get("canonical_html", ""))
        aud.top.append((n, headline or pkg.content_class))
    aud.top.sort(key=lambda x: -x[0])
    aud.top = aud.top[:3]
    return aud
