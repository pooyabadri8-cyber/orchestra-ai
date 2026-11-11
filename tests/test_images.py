"""Offline tests of photo posts (run: python tests/test_images.py)."""
import asyncio
import io
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from telegram.error import BadRequest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.bot as bmod  # noqa: E402
import agent.images as pics  # noqa: E402
import agent.platform as plat  # noqa: E402
import test_auto as ta  # noqa: E402
from agent.config import Config, load_config  # noqa: E402
from agent.sources import Item, Source, clean_image_url, load_sources, parse_article, parse_feed, parse_telegram  # noqa: E402
from agent.store import Store  # noqa: E402

from _profile import P  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")


def jpeg(w=800, h=500) -> bytes:
    from PIL import Image
    import random
    rnd = random.Random(1)
    im = Image.frombytes("RGB", (w, h), bytes(rnd.randrange(256) for _ in range(w * h * 3)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


IMG = jpeg()


def cfg(**kw):
    d = tempfile.mkdtemp()
    base = dict(bot_token="x", api_key="x", channel="@c", owner_ids={1}, auto_publish=True, attribution=False,
                image_dir=os.path.join(d, "img"), image_ratio=1.0)
    base.update(kw)
    return Config(**{**P, **base})


def http_with(routes):
    def handler(request):
        r = routes.get(str(request.url))
        return httpx.Response(200, content=r, headers={"content-type": "image/jpeg"}) if r is not None else httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- extraction


def test_extraction_from_feed_article_and_telegram():
    src = Source("S", "rss", "u")
    rss = """<rss><channel><item><title>t</title><link>https://s.test/a</link><description>%s</description>
    <enclosure url="https://s.test/p.jpg" type="image/jpeg"/><pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>""" % ("متن " * 60)
    items = parse_feed(rss, src)
    assert items and items[0].image == "https://s.test/p.jpg"
    art = "<html><head><meta property='og:title' content='T'><meta property='og:image' content='/img/x.jpg'></head><body><article>%s</article></body></html>" % "".join(
        f"<p>{'جمله بلند برای آزمایش متن مقاله ' * 3}</p>" for _ in range(8))
    assert parse_article(art, "https://s.test/news/1", "S").image == "https://s.test/img/x.jpg"
    tg = ("<div class='tgme_widget_message' data-post='ch/5'><a class='tgme_widget_message_photo_wrap' style=\"background-image:url('https://cdn.t.me/a.jpg')\"></a>"
          "<div class='tgme_widget_message_text'>%s</div><time datetime='2026-11-09T08:00:00+00:00'></time></div>" % ("متن پست تلگرام " * 8))
    assert parse_telegram(tg, Source("T", "telegram", "ch", min_chars=10))[0].image == "https://cdn.t.me/a.jpg"
    assert clean_image_url("https://s.test/logo.svg") == "" and clean_image_url("data:image/png;base64,xx") == "" and clean_image_url("") == ""


# ---------------------------------------------------------------- download / choice


def test_download_accepts_real_photo_and_refuses_the_rest():
    d = tempfile.mkdtemp()

    async def run():
        async with http_with({"https://s.test/ok.jpg": IMG, "https://s.test/tiny.jpg": b"\xff\xd8\xff" + b"0" * 100,
                              "https://s.test/html.jpg": b"<html>" + b"x" * 20000, "https://s.test/small.jpg": jpeg(200, 150)}) as h:
            ok = await pics.download(h, "https://s.test/ok.jpg", d, 400)
            assert ok and os.path.exists(ok) and open(ok, "rb").read() == IMG
            for bad in ("tiny", "html", "small", "missing"):
                assert await pics.download(h, f"https://s.test/{bad}.jpg", d, 400) is None, bad
            assert await pics.download(h, "", d, 400) is None
    asyncio.run(run())


def test_pick_ratio_is_stable_and_roughly_right():
    keys = [f"k{i}" for i in range(400)]
    assert all(pics.pick(k, 1) for k in keys) and not any(pics.pick(k, 0) for k in keys)
    n = sum(pics.pick(k, 0.5) for k in keys)
    assert 140 < n < 260 and [pics.pick(k, 0.5) for k in keys] == [pics.pick(k, 0.5) for k in keys]


# ---------------------------------------------------------------- drafting


def _draft(c, item, reply, imgs):
    store = Store(":memory:")
    w = ta.Writer(c, "<kb>", client=ta.FakeClient([reply]))
    real = bmod.new_http_client
    bmod.new_http_client = lambda: http_with(imgs)
    try:
        return asyncio.run(bmod.make_draft_from_item(c, store, w, item, None)), w
    finally:
        bmod.new_http_client = real


def _item(img="https://s.test/ok.jpg"):
    return Item("S", "kk", "عنوان", "متن", "https://s.test/1", datetime.now(timezone.utc), image=img)


def test_draft_keeps_photo_when_caption_fits_and_drops_it_otherwise():
    c = cfg()
    post, w = _draft(c, _item(), ta.good(1), {"https://s.test/ok.jpg": IMG})
    assert post.image_path and os.path.exists(post.image_path)
    assert "تا " in w.client.calls[0]["messages"][0]["content"] if hasattr(w, "client") else True
    long_text = ta.good(2).replace("ورود پول حقیقی", "ورود پول حقیقی " + "و ادامهٔ روند " * 120)
    post, _ = _draft(c, _item(), long_text, {"https://s.test/ok.jpg": IMG})
    assert post is not None and post.image_path is None  # too long for a caption -> text-only
    post, _ = _draft(c, _item(), ta.good(3), {})  # picture missing
    assert post is not None and post.image_path is None
    post, _ = _draft(cfg(image_ratio=0), _item(), ta.good(4), {"https://s.test/ok.jpg": IMG})  # not chosen
    assert post.image_path is None
    post, _ = _draft(c, _item(img=""), ta.good(5), {})  # item without a picture
    assert post.image_path is None
    post, _ = _draft(cfg(images=False), _item(), ta.good(6), {"https://s.test/ok.jpg": IMG})
    assert post.image_path is None


# ---------------------------------------------------------------- publishing


class PhotoBot(ta.Bot):
    def __init__(self, photo_error=None):
        super().__init__()
        self.photos = []
        self.photo_error = photo_error

    async def send_photo(self, chat, photo, caption=None, **kw):
        if self.photo_error:
            err, self.photo_error = self.photo_error, (self.photo_error if kw.get("parse_mode") is None and err_is_always(err) else None)
            raise err
        self.photos.append((chat, photo.read(), caption, kw))
        return SimpleNamespace(message_id=500 + len(self.photos))


def err_is_always(e):
    return "wrong file" in str(e)


def _photo_post(c, text=None):
    store = Store(":memory:")
    os.makedirs(c.image_dir, exist_ok=True)
    path = os.path.join(c.image_dir, "p.jpg")
    open(path, "wb").write(IMG)
    return store, store.add_draft("t", None, text or ta.good(1), None, image_path=path)


def test_publish_with_photo_caption_and_fallbacks():
    c = cfg()
    store, p = _photo_post(c)
    bot = PhotoBot()
    ok, msg = asyncio.run(bmod.publish_post(bot, c, store, p))
    assert ok and len(bot.photos) == 1 and not bot.sent and bot.photos[0][2] == p.text and bot.photos[0][3]["parse_mode"] == "HTML"
    assert store.get(p.id).status == "published"
    # caption HTML refused -> same photo with a plain caption
    store, p = _photo_post(c)
    bot = PhotoBot(BadRequest("Can't parse entities"))
    ok, _ = asyncio.run(bmod.publish_post(bot, c, store, p))
    assert ok and len(bot.photos) == 1 and "<b>" not in bot.photos[0][2] and not bot.sent
    # picture refused -> the text still goes out
    store, p = _photo_post(c)
    bot = PhotoBot(BadRequest("wrong file identifier"))
    ok, _ = asyncio.run(bmod.publish_post(bot, c, store, p))
    assert ok and not bot.photos and len(bot.sent) == 1
    # file gone from disk -> text only
    store, p = _photo_post(c)
    os.remove(p.image_path)
    bot = PhotoBot()
    ok, _ = asyncio.run(bmod.publish_post(bot, c, store, p))
    assert ok and not bot.photos and len(bot.sent) == 1
    # caption too long -> text only, photo untouched
    store, p = _photo_post(c, ta.good(1).replace("ادامه داشته", "ادامه داشته " + "و روند ادامه دارد " * 80))
    bot = PhotoBot()
    ok, _ = asyncio.run(bmod.publish_post(bot, c, store, p))
    assert ok and not bot.photos and len(bot.sent) == 1


def test_review_shows_photo_and_remove_button():
    c = cfg()
    store, p = _photo_post(c)
    bot = PhotoBot()
    asyncio.run(bmod.send_review(bot, 1, p, c))
    assert len(bot.photos) == 1 and bot.sent
    data = [b.callback_data for row in bmod.keyboard(p).inline_keyboard for b in row]
    assert f"noimg:{p.id}" in data
    store.set_image(p.id, None)
    assert f"noimg:{p.id}" not in [b.callback_data for row in bmod.keyboard(store.get(p.id)).inline_keyboard for b in row]


def test_auto_release_publishes_photo_and_old_db_migrates():
    c = cfg(auto_gap_minutes=0, auto_from="00:00", auto_to="23:59")
    store, p = _photo_post(c)
    bot = PhotoBot()
    asyncio.run(bmod.release(bot, c, store, p, when=datetime.now(timezone.utc)))
    assert len(bot.photos) == 1
    d = tempfile.mkdtemp()
    old = os.path.join(d, "old.db")
    db = sqlite3.connect(old)
    db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, pillar TEXT, text TEXT, status TEXT NOT NULL DEFAULT 'draft', "
               "proposed_at TEXT, scheduled_at TEXT, created_at TEXT NOT NULL DEFAULT '', published_at TEXT, message_id INTEGER, error TEXT, source_url TEXT)")
    db.execute("INSERT INTO posts (topic, text) VALUES ('a','b')")
    db.commit(); db.close()
    s = Store(old)
    assert s.get(1).image_path is None
    s.set_image(1, "x.jpg")
    assert s.get(1).image_path == "x.jpg"


def test_bale_caption_is_converted_and_photo_goes_as_multipart():
    sent = {}

    async def fake_post(self, method, data=None, *a, **kw):  # intercepts the Bale API call
        sent["method"], sent["data"] = method, dict(data or {})
        return {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "channel"}}

    bot = plat.BaleBot("1:a", plat.load_bale_format())
    bot._post = fake_post.__get__(bot)
    asyncio.run(bot.send_photo("@c", IMG, caption="📌 <b>تیتر</b>", parse_mode="HTML"))
    assert sent["method"] == "sendPhoto" and "<b>" not in str(sent["data"].get("caption")) and "*تیتر*" in str(sent["data"].get("caption"))
    assert sent["data"].get("parse_mode") in (None, "") or getattr(sent["data"].get("parse_mode"), "value", 1) is None  # PTB DefaultValue(None) = no parse mode


def test_real_config_images_and_beta_sahm_off():
    os.environ.update(PLATFORM="telegram", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHANNEL="c", OWNER_IDS="1", LLM_PROVIDER="openai_compatible",
                      LLM_API_KEY="k", LLM_BASE_URL="http://x", LLM_MODEL="m")
    real = load_config(os.path.join(CUSTOMER, "config.yaml"))
    assert real.images is True and real.image_ratio == 0.5 and real.caption_max_chars == 1024 and real.image_min_width == 400
    by = {s.name: s for s in load_sources(os.path.join(CUSTOMER, "sources.yaml"))}
    assert by["بتا سهم"].images is False and all(s.images for n, s in by.items() if n != "بتا سهم")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
