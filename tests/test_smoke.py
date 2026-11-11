"""Offline smoke tests: no network, no real API keys."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.config import Config  # noqa: E402
from agent.knowledge import load_knowledge  # noqa: E402
from agent.slots import next_slots, parse_local  # noqa: E402
from agent.store import Store, iso  # noqa: E402
from agent.writer import LLMError, Writer, has_placeholder, parse_plan  # noqa: E402

TZ = ZoneInfo("Asia/Tehran")
from _profile import P  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(__file__))
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")


def make_cfg():
    return Config(
        **P, bot_token="x", api_key="x", channel="@c", owner_ids={1},
        pillars=[{"name": "رفع ابهام", "brief": "b"}],
    )


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        text, stop = r if isinstance(r, tuple) else (r, "end_turn")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop)


def test_knowledge_loads():
    k = load_knowledge(os.path.join(CUSTOMER, "knowledge"))
    assert "پویان‌چوب" in k and "هنوز نامشخص" in k


def test_placeholder_detection():
    assert has_placeholder("سقف [[نیاز به تکمیل: سقف]] است")
    assert not has_placeholder("متن عادی")


def test_parse_plan_tolerates_wrapping():
    raw = 'بفرمایید:\n```json\n[{"pillar":"a","topic":"t1"},{"topic":"t2"},{"pillar":"x"}]\n```'
    out = parse_plan(raw, 5)
    assert [o["topic"] for o in out] == ["t1", "t2"]


def test_slots_skip_taken_and_past():
    now = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)  # 10:30 Tehran
    first = next_slots(now, TZ, ["10:30", "18:30"], set(), 1)[0]
    assert first.astimezone(TZ).strftime("%H:%M") == "18:30"  # 10:30 is within lead time/past
    seq = next_slots(now, TZ, ["10:30", "18:30"], {iso(first)}, 3)
    assert len(seq) == 3 and iso(first) not in {iso(s) for s in seq}
    assert seq == sorted(seq)


def test_parse_local():
    d = parse_local("2026-10-12 10:30", TZ)
    assert d.hour == 7 and d.minute == 0  # Tehran is UTC+3:30


def test_store_flow():
    s = Store(":memory:")
    p = s.add_draft("t", "p", "text")
    assert p.status == "draft"
    when = datetime.now(timezone.utc) - timedelta(minutes=1)
    s.schedule(p.id, when)
    assert [x.id for x in s.due(datetime.now(timezone.utc))] == [p.id]
    s.mark_published(p.id, 77)
    assert s.get(p.id).status == "published" and not s.due(datetime.now(timezone.utc))
    s.reject(p.id)  # no-op for published
    assert s.get(p.id).status == "published"


def test_writer_prompt_and_calls():
    cfg = make_cfg()
    fake = FakeClient(["پست نمونه", "پست کوتاه‌تر", '[{"pillar":"رفع ابهام","topic":"وثیقه"}]'])
    w = Writer(cfg, "<kb>", client=fake)
    assert "پست نمونه" in asyncio.run(w.draft("موضوع", "رفع ابهام"))
    assert "پست کوتاه‌تر" in asyncio.run(w.revise("پست نمونه", "کوتاه"))
    assert asyncio.run(w.plan(3))[0]["topic"] == "وثیقه"
    sys_blocks = fake.calls[0]["system"]
    assert sys_blocks[1]["cache_control"] == {"type": "ephemeral"} and "<kb>" in sys_blocks[1]["text"]
    assert "نیاز به تکمیل" in sys_blocks[0]["text"]


def test_openai_compatible_provider(monkeypatch=None):
    import httpx

    import agent.writer as wmod

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        n = seen.get("n", 0)
        seen["n"] = n + 1
        if n == 0:  # first call rate-limited -> must retry
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>پست رایگان"}}]})

    real = httpx.AsyncClient
    wmod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    real_sleep = wmod.asyncio.sleep

    async def no_sleep(_):
        return None

    wmod.asyncio.sleep = no_sleep
    try:
        cfg = make_cfg()
        cfg.provider, cfg.base_url, cfg.model, cfg.api_key = "openai_compatible", "https://x.test/v1", "m", "KEY"
        w = Writer(cfg, "<kb>")
        assert "پست رایگان" in asyncio.run(w.draft("موضوع"))
        assert seen["url"] == "https://x.test/v1/chat/completions" and seen["auth"] == "Bearer KEY"
        assert "<kb>" in seen["body"] and seen["n"] == 2

        # non-retryable error surfaces as LLMError
        def bad(request):
            return httpx.Response(401, text="bad key")

        wmod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(bad), **kw)
        try:
            asyncio.run(w.draft("x"))
            raise AssertionError("expected LLMError")
        except LLMError as e:
            assert "401" in str(e)
    finally:
        wmod.httpx.AsyncClient = real
        wmod.asyncio.sleep = real_sleep


def test_fallback_provider():
    import httpx

    import agent.writer as wmod

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "primary" in str(request.url):
            return httpx.Response(403, text="quota")
        return httpx.Response(200, json={"choices": [{"message": {"content": "از پشتیبان"}}]})

    real = httpx.AsyncClient
    wmod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        cfg = make_cfg()
        cfg.provider, cfg.base_url, cfg.model, cfg.api_key = "openai_compatible", "https://primary.test/v1", "m", "K1"
        cfg.fallback_api_key, cfg.fallback_base_url, cfg.fallback_model = "K2", "https://backup.test/v1", "m2"
        w = Writer(cfg, "<kb>")
        assert "از پشتیبان" in asyncio.run(w.draft("x"))
        assert calls == ["https://primary.test/v1/chat/completions", "https://backup.test/v1/chat/completions"]
        # both failing -> combined error
        cfg.fallback_base_url = "https://primary2.test/v1"
        try:
            asyncio.run(w.draft("x"))
            raise AssertionError("expected LLMError")
        except LLMError as e:
            assert "m2:" in str(e)  # "m" is on cooldown after its 403
    finally:
        wmod.httpx.AsyncClient = real


def test_model_chain_skips_exhausted_models():
    import httpx

    import agent.writer as wmod

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        model = _j.loads(request.content)["model"]
        calls.append(model)
        if model in ("g1", "g2"):
            return httpx.Response(429, text="You exceeded your current quota")
        return httpx.Response(200, json={"choices": [{"message": {"content": "از مدل سوم"}}]})

    real, real_sleep = httpx.AsyncClient, wmod.asyncio.sleep

    async def no_sleep(_):
        return None

    wmod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    wmod.asyncio.sleep = no_sleep
    try:
        cfg = make_cfg()
        cfg.provider, cfg.base_url, cfg.api_key = "openai_compatible", "https://g.test/v1", "K"
        cfg.model, cfg.models = "g1", ["g1", "g2", "g3"]
        w = Writer(cfg, "<kb>")
        assert "از مدل سوم" in asyncio.run(w.draft("x"))
        assert set(calls) == {"g1", "g2", "g3"}
        calls.clear()
        # exhausted models are on cooldown now: the next post goes straight to g3
        assert "از مدل سوم" in asyncio.run(w.draft("y"))
        assert calls == ["g3"]
    finally:
        wmod.httpx.AsyncClient = real
        wmod.asyncio.sleep = real_sleep


def test_config_provider_rules(tmp_path=None):
    import tempfile

    from agent.config import load_config

    keys = ["PLATFORM", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL", "OWNER_IDS", "ANTHROPIC_API_KEY",
            "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    cwd = os.getcwd()
    os.chdir(tempfile.mkdtemp())  # no .env here
    cfgfile = os.path.join(CUSTOMER, "config.yaml")
    try:
        os.environ.update(PLATFORM="telegram", TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHANNEL="ch", OWNER_IDS="1", LLM_PROVIDER="openai_compatible", LLM_API_KEY="k")
        try:
            load_config(cfgfile)
            raise AssertionError("should require base url and model")
        except SystemExit as e:
            assert "LLM_BASE_URL" in str(e) and "LLM_MODEL" in str(e)
        os.environ.update(LLM_BASE_URL="https://g.test/v1/", LLM_MODEL="gemini-x, gemini-y")
        c = load_config(cfgfile)
        assert c.models == ["gemini-x", "gemini-y"]
        os.environ.update(LLM_MODEL="gemini-x")
        c = load_config(cfgfile)
        assert c.provider == "openai_compatible" and c.base_url == "https://g.test/v1" and c.model == "gemini-x"
        assert c.channel == "@ch"
    finally:
        os.chdir(cwd)
        for k in keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>x</title>
<item><title>عنوان یک</title><link>https://s.test/1</link><guid>g1</guid>
<pubDate>{now}</pubDate><content:encoded><![CDATA[<p>متن <b>کامل</b> مقاله</p>]]></content:encoded></item>
<item><title>قدیمی</title><link>https://s.test/2</link><guid>g2</guid><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
<description>خیلی قدیمی</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>اتم</title>
<link rel="alternate" href="https://s.test/a"/><id>urn:1</id><updated>2026-10-04T10:00:00Z</updated>
<summary>خلاصهٔ اتم</summary></entry></feed>"""

TG = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="chan/12">
<div class="tgme_widget_message_text">اولین خط پیام<br/>""" + "متن طولانی تلگرام " * 10 + """</div>
<a class="tgme_widget_message_date"><time datetime="2026-10-04T08:00:00+00:00"></time></a></div></div>
<div class="tgme_widget_message" data-post="chan/13"><div class="tgme_widget_message_text">کوتاه</div></div>"""


def test_parse_feed_rss_and_atom():
    from agent.sources import Source, parse_feed

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = parse_feed(RSS.format(now=now).encode(), Source("S", "rss", "u"))
    assert [i.title for i in items] == ["عنوان یک", "قدیمی"]
    assert items[0].text == "متن کامل مقاله" and items[0].url == "https://s.test/1" and items[0].published
    assert items[0].key != items[1].key
    atom = parse_feed(ATOM, Source("A", "rss", "u"))
    assert atom[0].url == "https://s.test/a" and atom[0].text == "خلاصهٔ اتم" and atom[0].published.year == 2026


def test_parse_telegram_skips_short():
    from agent.sources import Source, parse_telegram

    items = parse_telegram(TG, Source("T", "telegram", channel="chan"))
    assert len(items) == 1 and items[0].url == "https://t.me/chan/12" and items[0].title == "اولین خط پیام"


def test_parse_article_and_too_short():
    from agent.sources import SourceError, parse_article

    para = "این یک پاراگراف نمونه با طول کافی برای استخراج است. " * 4
    html_doc = f"<html><head><title>T</title><meta property='og:title' content='عنوان OG'></head><body><nav><p>{para}</p></nav><article><h1>سرتیتر بلند برای تست</h1><p>{para}</p></article></body></html>"
    it = parse_article(html_doc, "https://s.test/a", "S")
    assert it.title == "عنوان OG" and "پاراگراف نمونه" in it.text and it.text.count("پاراگراف نمونه") == 4  # nav removed
    try:
        parse_article("<html><body><p>کوتاه</p></body></html>", "https://s.test/b", "S")
        raise AssertionError("expected SourceError")
    except SourceError:
        pass


def test_load_sources(tmp_path=None):
    import tempfile

    from agent.sources import load_sources

    d = tempfile.mkdtemp()
    f = os.path.join(d, "s.yaml")
    open(f, "w", encoding="utf-8").write(
        "sources:\n  - {name: A, type: rss, url: 'https://x/feed'}\n  - {type: telegram, url: 'https://t.me/s/mychan'}\n"
    )
    src = load_sources(f)
    assert src[0].type == "rss" and src[1].channel == "mychan" and src[1].name == "mychan"
    assert load_sources(os.path.join(d, "missing.yaml")) == []


def test_store_seen_and_reserved_slots():
    s = Store(":memory:")
    assert not s.is_seen("k")
    s.mark_seen("k", "S", "t", "u", "drafted")
    assert s.is_seen("k") and s.seen_counts() == {"S": 1}
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    d = s.add_draft("t", None, "x", when, source_url="https://s.test/1")
    assert d.source_url == "https://s.test/1" and iso(when) in s.reserved_slots()
    assert iso(when) not in s.taken_slots()  # drafts only reserve, they are not scheduled


def test_rewrite_skip_attribution_and_untrusted_wrapping():
    from agent.writer import add_attribution

    fake = FakeClient(["SKIP", "پست بازنویسی‌شده"])
    w = Writer(make_cfg(), "<kb>", client=fake)
    assert asyncio.run(w.rewrite("t", "txt", "https://s.test/1", "S")) is None
    out = asyncio.run(w.rewrite("عنوان", "متن منبع", "https://s.test/1", "S", pillar="رفع ابهام"))
    assert "پست بازنویسی‌شده" in out and "➖➖➖" in out
    prompt = fake.calls[1]["messages"][0]["content"]
    assert "<source_material" in prompt and "متن منبع" in prompt and "رفع ابهام" in prompt
    a = add_attribution("متن", 'سایت "x"', 'https://s.test/?a=1&b="2"')
    assert a.endswith("</a>") and "&amp;" in a and "&quot;" in a


def test_classify_topics_priority_and_word_boundaries():
    from agent.sources import Item, Source, Topic, classify

    src = Source("GS", "links", "u", topics=[
        Topic("طلا", ["gold", "bullion"]), Topic("نقره", ["silver"]), Topic("مس", ["copper"]),
    ])
    it = lambda title, text="": Item("GS", "k", title, text, "u")  # noqa: E731
    assert classify(src, it("Gold prices could hit $5,000")) == ("طلا", 0)
    assert classify(src, it("Why silver is rallying")) == ("نقره", 1)
    assert classify(src, it("Copper demand outlook")) == ("مس", 2)
    # "Goldman" must not count as "gold"; unrelated article is dropped
    assert classify(src, it("Goldman Sachs on AI growth", "Goldman analysts say AI spending rises.")) is None
    # keyword only in body needs 3 hits; priority order wins when two topics qualify
    body = "copper copper copper and gold gold gold"
    assert classify(src, it("Metals outlook", body)) == ("طلا", 0)
    assert classify(src, it("Metals outlook", "a mention of gold and copper")) is None
    assert classify(Source("S", "rss", "u"), it("anything")) == (None, 0)  # no topics = accept all
    fa = Source("F", "rss", "u", topics=[Topic("طلا", ["طلا"])])
    assert classify(fa, it("قیمت طلای جهانی امروز")) == ("طلا", 0)  # suffix ی still matches


def test_parse_links_pattern_and_heuristic():
    from agent.sources import parse_links

    html_doc = """<a href="/insights/articles/gold-outlook-2026">Gold outlook</a>
    <a href="/insights/talks-at-gs/x">Talk</a><a href="/about">About us</a>
    <a href="https://other.test/insights/articles/z">offsite</a>
    <a href="/insights/articles/gold-outlook-2026#top">dup</a>
    <a href="/news/12345-big-headline">اطلاعیهٔ مهم سازمان بورس درباره عرضه اولیه امروز</a>
    <a href="/menu">منو</a>"""
    got = parse_links(html_doc, "https://www.gs.test/insights", r"/insights/(articles|talks-at-gs)/[^/]+")
    assert [u for u, _ in got] == ["https://www.gs.test/insights/articles/gold-outlook-2026", "https://www.gs.test/insights/talks-at-gs/x"]
    auto = parse_links(html_doc, "https://gs.test/", "")  # no pattern: long headline text wins
    assert [u for u, _ in auto] == ["https://gs.test/news/12345-big-headline"]


def test_robots_blocks_links_source():
    import httpx

    import agent.sources as smod

    smod._robots.clear()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text="<html><body>" + "<p>" + "متن کافی برای مقاله " * 20 + "</p></body></html>")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ok = await smod.fetch_article(http, "https://r.test/public/a")
            try:
                await smod.fetch_article(http, "https://r.test/private/a")
                return ok, False
            except smod.SourceError:
                return ok, True

    ok, blocked = asyncio.run(go())
    assert ok.url == "https://r.test/public/a" and blocked


def test_links_source_skips_seen_and_probe():
    import httpx

    import agent.sources as smod
    from agent.sources import Source

    smod._robots.clear()
    smod.POLITE_DELAY = 0
    art = "<html><head><title>T</title></head><body><article><p>" + "متن کافی برای مقاله " * 20 + "</p></article></body></html>"

    def handler(request):
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/news":
            return httpx.Response(200, text='<link rel="alternate" type="application/rss+xml" href="/rss.xml">'
                                  '<a href="/news/a1">خبر مهم شمارهٔ یک از بازار سرمایه امروز</a>'
                                  '<a href="/news/a2">خبر مهم شمارهٔ دو از بازار سرمایه امروز</a>')
        if p == "/rss.xml":
            return httpx.Response(200, text="<?xml version='1.0'?><rss></rss>")
        return httpx.Response(200, text=art)

    seen_key = smod._key("web", "https://n.test/news/a1")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = Source("N", "links", "https://n.test/news")
            items = await smod.fetch_source(http, src, is_seen=lambda k: k == seen_key)
            report = await smod.probe(http, "https://n.test/news")
            return items, report

    items, report = asyncio.run(go())
    assert [i.url for i in items] == ["https://n.test/news/a2"]  # a1 already seen -> not even downloaded
    assert "https://n.test/rss.xml" in report and "a2" in report


def test_message_to_item_telethon_like():
    from agent.sources import Source, message_to_item

    class M:
        def __init__(self, id, message, date):
            self.id, self.message, self.date = id, message, date

    src = Source("Grp", "telegram_account", chat="mygroup", link=False)
    long = "خبر مهم بازار " * 10
    now = datetime.now(timezone.utc)
    assert message_to_item(M(1, "کوتاه", now), src, "mygroup") is None
    assert message_to_item(M(2, None, now), src, "mygroup") is None  # media-only / service
    it = message_to_item(M(3, long, now), src, "mygroup")
    assert it.url == "" and it.published == now  # link disabled for private-style groups
    src.link = True
    assert message_to_item(M(3, long, now), src, "mygroup").url == "https://t.me/mygroup/3"
    assert message_to_item(M(3, long, now), src, None).url == ""  # private group has no public link


def test_attribution_without_url_and_priority_order():
    from agent.writer import add_attribution

    assert add_attribution("متن", "گروه <x>", "") == "متن\n\nمنبع: گروه &lt;x&gt;"


def test_truncated_output_is_retried_never_published_half(monkeypatch=None):
    import httpx

    import agent.writer as wmod

    cfg = make_cfg()
    cfg.max_output_tokens = 100
    # --- anthropic path: max_tokens stop -> retry with double budget
    fake = FakeClient([("نصفه و نیمه", "max_tokens"), ("پست کامل", "end_turn")])
    w = Writer(cfg, "<kb>", client=fake)
    assert "پست کامل" in asyncio.run(w.draft("x"))
    assert [c["max_tokens"] for c in fake.calls] == [100, 200]
    # --- still truncated after retry -> error, no half post
    fake = FakeClient([("ن", "max_tokens"), ("ن", "max_tokens")])
    try:
        asyncio.run(Writer(cfg, "<kb>", client=fake).draft("x"))
        raise AssertionError("expected LLMError")
    except LLMError as e:
        assert "ناقص" in str(e)

    # --- openai-compatible path: finish_reason == length (even with EMPTY content: tokens spent on thinking)
    budgets = []

    def handler(request):
        import json as _j

        budgets.append(_j.loads(request.read())["max_tokens"])
        if len(budgets) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "کامل"}}]})

    real = httpx.AsyncClient
    wmod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        c2 = make_cfg()
        c2.provider, c2.base_url, c2.model, c2.api_key, c2.max_output_tokens = "openai_compatible", "https://x.test/v1", "m", "K", 100
        assert "کامل" in asyncio.run(Writer(c2, "<kb>").draft("x"))
        assert budgets == [100, 200]
    finally:
        wmod.httpx.AsyncClient = real


def test_clip_min_chars_and_completeness_prompt():
    from agent.sources import MAX_TEXT, Source, _clip, parse_telegram

    short, cut = _clip("متن کوتاه")
    assert short == "متن کوتاه" and not cut
    long = ("این یک بند کامل است. " * 30 + "\n\n") * 40
    clipped, cut = _clip(long)
    assert cut and len(clipped) <= MAX_TEXT and clipped.endswith("است.")  # cut at a paragraph break, not mid-word

    msg = lambda i, txt: (f'<div class="tgme_widget_message" data-post="c/{i}"><div class="tgme_widget_message_text">{txt}</div></div>')  # noqa: E731
    html_doc = msg(1, "گزارش بازار " * 6) + msg(2, "x" * 100)
    assert len(parse_telegram(html_doc, Source("C", "telegram", channel="c", min_chars=60))) == 2
    assert len(parse_telegram(html_doc, Source("C", "telegram", channel="c", min_chars=90))) == 1

    cfg = make_cfg()
    cfg.source_max_chars = 2500
    fake = FakeClient(["پست"])
    asyncio.run(Writer(cfg, "<kb>", client=fake).rewrite("t", "x", "u", "S", truncated=True))
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "2500" in prompt and "نیمه‌تمام" in prompt and "بریده شده" in prompt and "حذف محتوا" in prompt


def test_make_draft_from_item_and_run_fetch():
    from agent.bot import make_draft_from_item, run_fetch
    from agent.sources import Item, Source

    cfg = make_cfg()
    s = Store(":memory:")
    fake = FakeClient(["پست یک", "SKIP", "پست سه"])
    w = Writer(cfg, "<kb>", client=fake)
    it1 = Item("S", "k1", "عنوان ۱", "متن", "https://s.test/1")
    post = asyncio.run(make_draft_from_item(cfg, s, w, it1, "رفع ابهام", "فقط خبر مهم"))
    assert post.status == "draft" and "پست یک" in post.text and "منبع: S" in post.text
    assert post.source_url == "https://s.test/1" and s.is_seen("k1")
    assert post.proposed_at in s.reserved_slots()

    # run_fetch with the network layer stubbed out
    import agent.bot as bmod

    now = datetime.now(timezone.utc)
    items = [Item("S", f"n{i}", f"t{i}", "متن", f"https://s.test/n{i}", now - timedelta(hours=i)) for i in range(3)]
    items.append(it1)  # already seen -> must be ignored

    async def fake_fetch_source(http, src, is_seen=None, account=None):
        return items

    class NoHttp:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    real_f, real_c = bmod.fetch_source, bmod.new_http_client
    bmod.fetch_source, bmod.new_http_client = fake_fetch_source, lambda: NoHttp()
    sent = []

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append(chat)
            return SimpleNamespace(message_id=1)

    try:
        summary = asyncio.run(run_fetch(Bot(), cfg, s, w, [Source("S", "rss", "u")], [1], limit=1))
    finally:
        bmod.fetch_source, bmod.new_http_client = real_f, real_c
    # n0 -> SKIP (off-topic), n1 -> drafted, limit reached so n2 is left for the next run
    assert "1 پیش‌نویس" in summary and "1 مطلب بی‌اهمیت یا نامرتبط" in summary and "1 مطلب جدید دیگر" in summary
    assert not s.is_seen("n2")

    # ---- priority + topic filter + old items, in one run
    from agent.sources import Topic

    s2 = Store(":memory:")
    fake2 = FakeClient(["پست مس", "پست طلا", "پست نقره"])
    w2 = Writer(cfg, "<kb>", client=fake2)
    t = lambda title, body="": title  # noqa: E731
    mk = lambda key, title, hours: Item("G", key, title, "text", f"https://g.test/{key}", now - timedelta(hours=hours))  # noqa: E731
    pool = [mk("c", "Copper demand", 1), mk("s", "Silver rally", 2), mk("g", "Gold at record", 3),
            mk("x", "Goldman on AI", 1), mk("old", "Gold old news", 24 * 10)]

    async def fake_fetch2(http, src, is_seen=None, account=None):
        return pool

    bmod.fetch_source, bmod.new_http_client = fake_fetch2, lambda: NoHttp()
    try:
        gsrc = Source("G", "links", "u", priority=1, topics=[Topic("طلا", ["gold"]), Topic("نقره", ["silver"]), Topic("مس", ["copper"])])
        summary2 = asyncio.run(run_fetch(Bot(), cfg, s2, w2, [gsrc], [1], limit=3))
    finally:
        bmod.fetch_source, bmod.new_http_client = real_f, real_c
    assert "3 پیش‌نویس" in summary2 and "خارج از موضوع" in summary2
    # model was called for gold first, then silver, then copper (priority order), despite newest-first feed order
    order = [c["messages"][0]["content"] for c in fake2.calls]
    assert "Gold at record" in order[0] and "Silver rally" in order[1] and "Copper demand" in order[2]
    assert "موضوع تشخیص‌داده‌شدهٔ این مطلب: طلا" in order[0]
    assert s2.is_seen("x") and s2.is_seen("old")  # filtered / too old are remembered, never retried
    assert sent and s.is_seen("n0") and s.is_seen("n1")


def test_publish_blocks_placeholder_and_publishes_clean():
    from agent.bot import publish_post

    cfg = make_cfg()
    s = Store(":memory:")
    sent = []

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append((chat, text))
            return SimpleNamespace(message_id=42)

    bad = s.add_draft("t", None, "متن [[نیاز به تکمیل: عدد]]")
    ok, _ = asyncio.run(publish_post(Bot(), cfg, s, bad))
    assert not ok and not sent and s.get(bad.id).status == "failed"

    good = s.add_draft("t", None, "<b>سلام</b>")
    ok, msg = asyncio.run(publish_post(Bot(), cfg, s, good))
    assert ok and sent == [("@c", "<b>سلام</b>")] and s.get(good.id).message_id == 42
    assert "t.me/c/42" in msg


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
