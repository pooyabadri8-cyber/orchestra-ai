"""The research agent: news collected as evidence, chosen by the model only from what it was shown, and every figure in a
news post bound to the sentence and the bytes it came from.

Feed and channel samples here are synthetic, in the standard RSS/Atom shape and Telegram's public page structure (the
same structure V3's parser read live). Real captures come from scripts/capture_sources.py."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orchestra.agents.multichannel import MultiChannelDraft, verify_multichannel
from orchestra.agents.research import ResearchConfig, ResearchError, candidates, collect, load_research, news_part, pick
from orchestra.agents.specialists import compose
from orchestra.brand import load_brand
from orchestra.content.numbers import format_point
from orchestra.data.acquisition import AcquisitionError
from orchestra.data.sources.base import ParseError
from orchestra.data.sources.news import figures, parse_feed, parse_telegram
from orchestra.db import DataPointRow, DataSnapshot, EvidenceItem
from orchestra.llm import Gateway, Route
from wf_support import ScriptedProvider

NOW = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)
BRAND = load_brand("pouyan")


def rss(*items) -> bytes:
    body = "".join(f"<item><title>{t}</title><link>https://n.test/{i}</link><guid>g{i}</guid><description><![CDATA[<p>{d}</p>]]>"
                   f"</description><pubDate>{p}</pubDate></item>" for i, (t, d, p) in enumerate(items))
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


OIL = ("قیمت نفت برنت به 80 دلار رسید", "قیمت نفت برنت امروز با ۲٫۵ درصد افزایش به 80 دلار رسید. تولید اوپک ثابت ماند.",
       "Wed, 11 Nov 2026 10:00:00 GMT")
FUND = ("ورود پول به صندوق‌های طلا", "صندوق‌های طلا امروز ۳ همت ورودی داشتند.", "Wed, 11 Nov 2026 10:05:00 GMT")
OLD = ("خبر قدیمی", "این خبر دو روز پیش است.", "Mon, 09 Nov 2026 08:00:00 GMT")
RATE = ("بانک مرکزی نرخ سود را ثابت نگه داشت", "بانک مرکزی اعلام کرد نرخ سود سپرده ۲۳ درصد باقی می‌ماند.",
        "Wed, 11 Nov 2026 11:00:00 GMT")
TG = ("<div class='tgme_channel_info'></div>"
      "<div class='tgme_widget_message' data-post='betasahm1/77'><div class='tgme_widget_message_text'>"
      "سازمان بورس دامنهٔ نوسان را از شنبه به ۵ درصد افزایش داد.<br/>این تصمیم برای همهٔ نمادها اجرا می‌شود.</div>"
      "<time datetime='2026-11-11T09:30:00+00:00'></time></div>"
      "<div class='tgme_widget_message' data-post='betasahm1/78'><div class='tgme_widget_message_text'>کوتاه</div></div>").encode()

CFG = ResearchConfig.model_validate({"org_id": "pouyan", "max_items": 3, "sources": [
    {"id": "isna", "name": "ایسنا", "type": "rss", "url": "https://isna.test/rss"},
    {"id": "beta", "name": "بتا سهم", "type": "telegram", "channel": "betasahm1", "foreign": True},
    {"id": "down", "name": "منبع خراب", "type": "rss", "url": "https://down.test/rss"},
]})


def fetcher(calls):
    async def fetch(url, foreign):
        calls.append((url, foreign))
        if "down" in url:
            raise AcquisitionError("HTTP 404", retryable=False)
        return TG if "t.me" in url else rss(OIL, FUND, OLD, RATE, OIL)
    return fetch


def test_parsers():
    items = parse_feed(rss(OIL))
    assert items[0].title == OIL[0] and "افزایش" in items[0].text and items[0].url == "https://n.test/0"
    assert items[0].published == datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><id>x1</id><link href="https://a.test/1"/>' \
           b'<summary>S</summary><updated>2026-11-11T10:00:00Z</updated></entry></feed>'
    assert parse_feed(atom)[0].url == "https://a.test/1"
    tg = parse_telegram(TG, min_chars=20)
    assert len(tg) == 1 and tg[0].url == "https://t.me/betasahm1/77" and "\n" in tg[0].text
    with pytest.raises(ParseError):
        parse_feed(b"<html><body>not a feed</body></html>")
    with pytest.raises(ParseError):
        parse_telegram(b"<html><body>private</body></html>")


def test_figures_keep_their_unit_and_sentence():
    got = [(f.text, f.value) for f in figures("قیمت با 2.5 درصد افزایش به 1,250 دلار رسید و 3 میلیون بشکه تولید شد. سال ۱۴۰۵")]
    assert got == [("۲٫۵ درصد", 2.5), ("۱٬۲۵۰ دلار", 1250.0), ("۳ میلیون بشکه", 3.0), ("۱۴۰۵", 1405.0)]
    assert figures("نرخ 35% شد.")[0].text == "۳۵٪" and figures("رشد 2.5 درصدی بود.")[0].sentence == "رشد 2.5 درصدی بود"


def test_collect_stores_evidence_once_and_reports_a_failing_source(sessions, assets):
    calls = []
    status = asyncio.run(collect(sessions, assets, fetcher(calls), CFG, NOW))
    assert status["isna"] == "5 تازه از 5" and status["beta"] == "1 تازه از 1" and status["down"].startswith("خطا")
    assert ("https://t.me/s/betasahm1", True) in calls and ("https://isna.test/rss", False) in calls
    again = asyncio.run(collect(sessions, assets, fetcher([]), CFG, NOW + timedelta(minutes=30)))
    assert again["isna"] == "0 تازه از 5"
    with sessions() as s:
        assert s.query(EvidenceItem).count() == 6
        snap = s.query(DataSnapshot).filter(DataSnapshot.source_id == "news:isna").first()
        assert assets.get(snap.raw_sha256) == rss(OIL, FUND, OLD, RATE, OIL)


def test_candidates_are_recent_and_never_about_funds(sessions, assets):
    asyncio.run(collect(sessions, assets, fetcher([]), CFG, NOW))
    with sessions() as s:
        titles = [c.title for c in candidates(s, CFG, BRAND, NOW)]
    assert titles == [RATE[0], OIL[0], "سازمان بورس دامنهٔ نوسان را از شنبه به ۵ درصد افزایش داد."]  # newest first


def gateway(sessions, replies):
    prov = ScriptedProvider(replies)
    return Gateway(sessions, {"reviewer": [Route(prov, "scripted")]}, max_calls_per_run=10), prov


def test_the_model_can_only_pick_what_it_was_shown(sessions, assets):
    asyncio.run(collect(sessions, assets, fetcher([]), CFG, NOW))
    with sessions() as s:
        items = candidates(s, CFG, BRAND, NOW)
        gw, prov = gateway(sessions, ["نه", {"picks": [{"n": 2, "why": "نفت"}, {"n": 2}, {"n": 99}, {"n": 1, "why": "نرخ سود"}]}])
        got = asyncio.run(pick(gw, BRAND, CFG, items, org_id="pouyan", run_id="r"))
        assert got == [(items[1].id, "نفت"), (items[0].id, "نرخ سود")]
        gw, _ = gateway(sessions, ["x", "y"])
        with pytest.raises(ResearchError):
            asyncio.run(pick(gw, BRAND, CFG, items, org_id="pouyan", run_id="r2"))
        assert asyncio.run(pick(gw, BRAND, CFG, [], org_id="pouyan", run_id="r3")) == []


def test_news_figures_are_bound_and_typed_ones_are_blocked(sessions, assets):
    asyncio.run(collect(sessions, assets, fetcher([]), CFG, NOW))
    with sessions() as s:
        oil = next(c for c in candidates(s, CFG, BRAND, NOW) if c.title == OIL[0])
    with sessions.begin() as s:
        part = news_part(s, [oil.id], {oil.id: "اثر بر بازار انرژی"}, now=NOW)
    assert [p.text for p in part.points] == ["۸۰ دلار", "۲٫۵ درصد", "۸۰ دلار"]
    assert "80" not in part.facts[0] and "[[F:" in part.facts[0] and "اثر بر بازار انرژی" in part.facts[0]
    with sessions() as s:
        row = s.get(DataPointRow, part.points[1].point_id)
        assert row.source_id == "news:isna" and row.source_text == "۲٫۵ درصد" and s.get(DataSnapshot, row.snapshot_id)
    rep = compose("news", [part])
    assert "{{P2|as_is}}" in rep.facts[0] and format_point(rep.points[1], "as_is") == "۲٫۵ درصد"
    post = {"hashtag": "#خبر_بازار", "headline": "نفت برنت گران شد",
            "paragraphs": ["به گزارش ایسنا، نفت برنت با {{P2|as_is}} افزایش به {{P3|as_is}} رسید.", "تولید اوپک ثابت ماند."],
            "cta": "نظرتان را بنویسید."}
    ok = verify_multichannel(MultiChannelDraft(post=post), rep, BRAND, ("telegram",))
    assert not ok.blocked and "۸۰ دلار" in ok.post.paragraphs[0]
    typed = {**post, "paragraphs": ["نفت برنت به ۸۱ دلار رسید.", "تولید اوپک ثابت ماند."]}
    assert verify_multichannel(MultiChannelDraft(post=typed), rep, BRAND, ("telegram",)).blocked


def test_pouyan_research_config():
    cfg = load_research("pouyan")
    assert {s.id for s in cfg.sources} >= {"isna", "bbc_persian", "betasahm"}
    assert next(s for s in cfg.sources if s.id == "betasahm").address == "https://t.me/s/betasahm1"


# ------------------------------------------------------------------ real feeds (captured 2026-11-25 with capture_sources)

from pathlib import Path  # noqa: E402

REAL = {n: (Path(__file__).parent / "fixtures" / f"news_{n}_real.xml").read_bytes() for n in ("sena", "isna", "mehr", "irna")}
REAL_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
REAL_CFG = ResearchConfig.model_validate({"org_id": "pouyan", "max_candidates": 40, "sources": [
    {"id": n, "name": n, "type": "rss", "url": f"https://{n}.test/rss"} for n in REAL]})


def test_real_feeds_parse():
    for name, raw in REAL.items():
        items = parse_feed(raw)
        assert len(items) == 30 and all(i.published and i.url.startswith("https://") and i.title for i in items), name


def test_a_figure_partly_in_words_is_kept_whole():
    wool = next(i for i in parse_feed(REAL["isna"]) if "پشم" in i.title)
    texts = [f.text for f in figures(wool.title + "\n" + wool.text)]
    assert "یک‌هزار و ۴۵ تن" in texts and "۴۵ تن" not in texts  # «۴۵ تن» alone would be a wrong figure (1045 t)
    fee = next(i for i in parse_feed(REAL["mehr"]) if "اعتکاف" in i.title)
    assert [f.text for f in figures(fee.text)] == ["یک میلیون و ۵۰۰ هزار تومانی"]


def test_every_real_sentence_can_be_quoted_without_an_unbound_figure(sessions, assets):
    """The writer may copy any evidence sentence as it is: no figure in it may be left outside a token."""
    from orchestra.content.numbers import render, scan

    async def fetch(url, foreign):
        return REAL[url.split("//")[1].split(".")[0]]
    asyncio.run(collect(sessions, assets, fetch, REAL_CFG, REAL_NOW))
    with sessions() as s:
        ids = [e.id for e in s.query(EvidenceItem)]
    assert len(ids) == 120
    for start in range(0, len(ids), 10):
        with sessions.begin() as s:
            part = news_part(s, ids[start:start + 10], now=REAL_NOW)
        rep = compose("news", [part])
        points = rep.by_key()
        for fact in rep.facts:
            out, _, spans = render(fact, points)
            assert scan(out, spans) == [], (fact[:200], scan(out, spans))


def test_real_candidates_keep_a_share_for_every_source(sessions, assets):
    async def fetch(url, foreign):
        return REAL[url.split("//")[1].split(".")[0]]
    asyncio.run(collect(sessions, assets, fetch, REAL_CFG, REAL_NOW))
    with sessions() as s:
        got = candidates(s, REAL_CFG, BRAND, REAL_NOW)
    by = {}
    for c in got:
        by[c.source_id] = by.get(c.source_id, 0) + 1
    assert len(got) == 40 and by["sena"] >= 10  # Sena covers two days; the agencies only their last half hour


def test_news_is_collected_on_an_interval(sessions, assets, tmp_path):
    from orchestra.agents.research import NewsCollector
    (tmp_path / "pouyan").mkdir()
    (tmp_path / "pouyan" / "research.yaml").write_text(
        "org_id: pouyan\ncollect_every_minutes: 20\nsources:\n  - {id: sena, name: سنا, type: rss, url: 'https://sena.test/rss'}\n",
        encoding="utf-8")
    calls = []

    async def fetch(url, foreign):
        calls.append(url)
        return REAL["sena"]
    from conftest import Clock
    clock = Clock(REAL_NOW)
    c = NewsCollector("pouyan", tmp_path, sessions, assets, fetch, clock)
    c.tick()
    clock.advance(10 * 60)
    c.tick()
    clock.advance(11 * 60)
    c.tick()
    assert len(calls) == 2 and c.last == {"sena": "0 تازه از 30"}
