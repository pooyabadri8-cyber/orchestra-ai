"""Audience figures: view counts from a public channel's page, matched to the posts the publisher confirmed."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import Clock
from orchestra.agents.audience import AudienceCollector, audience, collect_views, parse_count, parse_views
from orchestra.agents.reporting import build_report, report_text
from orchestra.db import Approval, Destination, PackageRow, PostView, PublishJob, Run

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def page(ids_views) -> bytes:
    """A page of t.me/s/<channel> in Telegram's structure (newest last, as on the real page)."""
    posts = "".join(f"<div class='tgme_widget_message' data-post='pouyanbrokerage/{i}'><div class='tgme_widget_message_text'>متن</div>"
                    f"<span class='tgme_widget_message_views'>{v}</span></div>" for i, v in sorted(ids_views.items()))
    return f"<html><body><div class='tgme_channel_info'></div>{posts}</body></html>".encode()


def test_counts_and_pages():
    assert [parse_count(x) for x in ("987", "1.2K", "15K", "3.4M", "1,234", "", "abc")] == [987, 1200, 15000, 3400000, 1234, None, None]
    assert parse_views(page({101: "1.2K", 102: "950"})) == {101: 1200, 102: 950}


def add_post(s, n, *, dest="tg_main", kind="market_report", message_ids, when=NOW - timedelta(days=1), status="confirmed",
             headline="سرخط", summary=""):
    pid = f"pkg_{n}"
    s.add(Run(id=f"run{n}", org_id="pouyan", template=kind, status="approved", detail={"summary": summary} if summary else {}))
    s.add(PackageRow(package_id=pid, org_id="pouyan", package_hash=(pid * 9)[:64].ljust(64, "0"), run_id=f"run{n}", version=1,
                     content_class=kind, content_json=json.dumps({"text": {"canonical_html": f"#x\n📌  <b>{headline}</b>\n..."}}),
                     status="active"))
    s.flush()
    a = Approval(org_id="pouyan", package_id=pid, package_hash=(pid * 9)[:64].ljust(64, "0"), destination_id=dest,
                 decision="approved", approver="u")
    s.add(a)
    s.flush()
    s.add(PublishJob(org_id="pouyan", package_id=pid, package_hash=a.package_hash, destination_id=dest, approval_id=a.id,
                     idempotency_key=f"k{n}{dest}", status=status, message_ids=message_ids, created_at=when, updated_at=when))


@pytest.fixture
def posts(sessions):
    with sessions.begin() as s:
        s.add(Destination(id="tg_main", org_id="pouyan", platform="telegram", chat_id="@pouyanbrokerage", secret_env="T",
                          public_username="pouyanbrokerage"))
        s.add(Destination(id="tg_test", org_id="pouyan", platform="telegram", chat_id="-100", secret_env="T"))
        add_post(s, 1, message_ids=[95], headline="گزارش پایان بازار؛ رشد شاخص")  # older than the first page
        add_post(s, 2, kind="topic_post", message_ids=[103, 104, 105], headline="کارنامهٔ پویان کراد",
                 summary="اثبات و کارنامه: کارنامهٔ پویان کراد")  # an album: three messages, one count
        add_post(s, 3, kind="news_digest", message_ids=[118], headline="خلاصهٔ خبرهای صبح")
        add_post(s, 4, message_ids=[60], when=NOW - timedelta(days=20))  # too old to read again
        add_post(s, 5, dest="tg_test", message_ids=[7])  # a private channel: no public page
    return sessions


def fake_channel(calls):
    full = {i: f"{i * 10}" for i in range(90, 121)}
    full[104] = full[103] = full[105] = "2.5K"

    async def fetch(url, foreign):
        calls.append(url)
        assert foreign  # Telegram is reached like a foreign source (VPN)
        before = int(url.split("before=")[1]) if "before=" in url else 121
        ids = [i for i in sorted(full) if i < before][-20:]
        return page({i: full[i] for i in ids})
    return fetch


def test_views_are_read_page_by_page_and_matched_to_their_posts(posts):
    calls = []
    status = asyncio.run(collect_views(posts, fake_channel(calls), NOW))
    assert status == {"pouyanbrokerage": "5 از 5 پست"} and len(calls) == 2 and "before=" in calls[1]
    with posts() as s:
        got = {(v.job_id, v.message_id): v.views for v in s.query(PostView)}
    assert got[(1, 95)] == 950 and got[(2, 104)] == 2500 and got[(3, 118)] == 1180
    assert not any(j == 4 for j, _ in got) and not any(j == 5 for j, _ in got)


def test_figures_in_the_report(posts):
    asyncio.run(collect_views(posts, fake_channel([]), NOW))
    with posts() as s:
        a = audience(s, "pouyan", NOW - timedelta(days=7), NOW)
        rep = build_report(s, "pouyan", end_day=NOW.date(), days=7, now=NOW)
    assert a.public and a.posts == 3 and a.total == 950 + 2500 + 1180
    assert a.top[0] == (2500, "کارنامهٔ پویان کراد") and a.by_topic == {"اثبات و کارنامه: کارنامهٔ پویان کراد": [2500]}
    text = report_text(rep)
    assert "👁 مخاطب" in text and "پست موضوعی ۲٬۵۰۰" in text and "«کارنامهٔ پویان کراد»: ۲٬۵۰۰ بازدید" in text


def test_no_section_without_a_public_channel(sessions):
    with sessions.begin() as s:
        s.add(Destination(id="tg_test", org_id="pouyan", platform="telegram", chat_id="-100", secret_env="T"))
        add_post(s, 1, dest="tg_test", message_ids=[7])
    with sessions() as s:
        assert not audience(s, "pouyan", NOW - timedelta(days=7), NOW).public
        assert "مخاطب" not in report_text(build_report(s, "pouyan", end_day=NOW.date(), days=7, now=NOW))


def test_a_public_channel_not_read_yet_says_so(posts):
    with posts() as s:
        text = report_text(build_report(s, "pouyan", end_day=NOW.date(), days=7, now=NOW))
    assert "هنوز بازدیدی خوانده نشده" in text


def test_collected_every_six_hours_and_a_failure_is_kept_quiet(posts):
    calls = []
    clock = Clock(NOW)
    c = AudienceCollector(posts, fake_channel(calls), clock)
    c.tick()
    clock.advance(3600)
    c.tick()
    assert len(calls) == 2  # one collection (two pages)

    async def down(url, foreign):
        raise ConnectionError("blocked")
    c.fetch = down
    clock.advance(6 * 3600)
    c.tick()
    assert c.last["pouyanbrokerage"].startswith("خطا")


def test_public_username_from_destinations_yaml(tmp_path, monkeypatch):
    from orchestra.cli import _load_destinations
    from orchestra.db import Base, make_engine, make_sessionmaker
    from orchestra.settings import Settings
    (tmp_path / "pouyan").mkdir()
    (tmp_path / "pouyan" / "destinations.yaml").write_text(
        "destinations:\n"
        "  - {id: a, platform: telegram, chat_id: '@pouyanbrokerage', secret_env: T}\n"
        "  - {id: b, platform: telegram, chat_id: '-100123', secret_env: T, public_username: '@my_channel'}\n"
        "  - {id: c, platform: telegram, chat_id: '-100456', secret_env: T}\n", encoding="utf-8")
    engine = make_engine(f"sqlite:///{tmp_path / 'x.db'}")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    with sessions.begin() as s:
        _load_destinations(Settings(orgs_dir=tmp_path), "pouyan", s)
    with sessions() as s:
        assert {d.id: d.public_username for d in s.query(Destination)} == {"a": "pouyanbrokerage", "b": "my_channel", "c": None}
