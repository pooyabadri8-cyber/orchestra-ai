"""Offline tests of the Bale version: a fake Bale server speaks the Bot API over real HTTP (run: python tests/test_bale.py)."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from telegram import Update  # noqa: E402

import agent.platform as plat  # noqa: E402
from agent.bot import build_app  # noqa: E402
from agent.config import Config, load_config  # noqa: E402
from agent.platform import BaleFormat, html_to_bale  # noqa: E402

from _profile import P  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")
POST = (
    "#کارگزاری_پویان\n#تحلیل_بازار\n\n\n📌  <b>تیتر &amp; نمونه</b>\n\n🔹بند اول 3 * 4\n\n"
    '☝ <a href="https://pouyancrowd.ir/?a=1&amp;b=2">فراخوان</a>\n\n\n➖➖➖\nپاورقی'
)


def test_html_to_bale_markdown_and_options():
    out = html_to_bale(POST)
    assert "📌  *تیتر & نمونه*" in out and "[فراخوان](https://pouyancrowd.ir/?a=1&b=2)" in out
    assert "<" not in out and "&amp;" not in out and "#کارگزاری_پویان" in out and "3 ＊ 4" in out
    plain = html_to_bale(POST, BaleFormat(markdown=False, links="plain"))
    assert "*تیتر" not in plain and "3 * 4" in plain and "[" not in plain and "فراخوان (https://pouyancrowd.ir/?a=1&b=2)" in plain
    assert "#کارگزاری‌پویان" in html_to_bale(POST, BaleFormat(underscore="zwnj"))
    assert "#کارگزاری\\_پویان" in html_to_bale(POST, BaleFormat(underscore="escape"))
    assert "تیتر" in html_to_bale(POST, BaleFormat(bold="")) and "*تیتر" not in html_to_bale(POST, BaleFormat(bold=""))


def test_config_platform_and_bale_env_names():
    keep = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith(("TELEGRAM_", "BALE_")) or k in ("PLATFORM", "OWNER_IDS"):
                del os.environ[k]
        os.environ.update(PLATFORM="bale", BALE_BOT_TOKEN="123:abc", BALE_CHANNEL="mychannel", OWNER_IDS="77",
                          LLM_PROVIDER="openai_compatible", LLM_API_KEY="k", LLM_BASE_URL="http://x", LLM_MODEL="m")
        c = load_config(os.path.join(CUSTOMER, "config.yaml"))
        assert c.platform == "bale" and c.bot_token == "123:abc" and c.channel == "@mychannel" and c.owner_ids == {77}
        del os.environ["BALE_BOT_TOKEN"]
        try:
            load_config(os.path.join(CUSTOMER, "config.yaml"))
            raise AssertionError("should fail without a token")
        except SystemExit as e:
            assert "BALE_BOT_TOKEN" in str(e)
    finally:
        os.environ.clear()
        os.environ.update(keep)


class FakeBale(BaseHTTPRequestHandler):
    calls: list = []
    updates: list = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:  # form-encoded
            from urllib.parse import parse_qs
            data = {k: v[0] for k, v in parse_qs(raw).items()}
        method = self.path.rsplit("/", 1)[-1]
        with self.lock:
            self.calls.append((self.path, method, data))
        result = True
        if method == "getMe":
            result = {"id": 99, "is_bot": True, "first_name": "t", "username": "t_bot"}
        elif method == "getUpdates":
            with self.lock:
                result, FakeBale.updates = list(FakeBale.updates), []
            if not result:
                time.sleep(0.3)
        elif method == "sendMessage":
            result = {"message_id": 5, "date": int(time.time()), "chat": {"id": data.get("chat_id", 1) if str(data.get("chat_id", "1")).lstrip("-").isdigit() else 1, "type": "private"},
                      "text": data.get("text", "")}
        elif method == "getChatMember":
            result = {"status": "member", "user": {"id": 99, "is_bot": True, "first_name": "t"}}
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_bot_runs_against_a_bale_style_server():
    server = HTTPServer(("127.0.0.1", 0), FakeBale)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/bot"
    FakeBale.calls.clear()
    old, old_f = plat.BALE_API, plat.BALE_FILE_API
    plat.BALE_API, plat.BALE_FILE_API = base, base.replace("/bot", "/file/bot")
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(CUSTOMER)
    try:
        cfg = Config(**P, bot_token="123:abc", api_key="x", channel="@mychannel", owner_ids={77}, platform="bale",
                     auto_publish=True, db_path=os.path.join(d, "a.db"), auto_state_file=os.path.join(d, "auto.json"))
        app = build_app(cfg)
        assert isinstance(app.bot, plat.BaleBot) and app.bot.base_url.startswith(base)

        async def scenario():
            async with app:
                # 1) the owner asks for /id and /start; a stranger gets the refusal
                msg = lambda uid, text, i: {"update_id": i, "message": {"message_id": i, "date": int(time.time()), "text": text,  # noqa: E731
                                            "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "is_bot": False, "first_name": "u"},
                                            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}}
                for u in (msg(77, "/id", 1), msg(77, "/start", 2), msg(5, "/start", 3), msg(77, "/balecheck", 4)):
                    update = Update.de_json(u, app.bot)
                    await app.process_update(update)
                # 2) a post goes to the channel as Bale text
                from telegram.constants import ParseMode
                await app.bot.send_message("@mychannel", POST, parse_mode=ParseMode.HTML, link_preview_options=None, disable_notification=True)
                await app.bot.edit_message_reply_markup(chat_id=77, message_id=5)
                from agent.bot import post_init
                await post_init(app)
        asyncio.run(scenario())
    finally:
        plat.BALE_API, plat.BALE_FILE_API = old, old_f
        os.chdir(cwd)
        server.shutdown()

    sent = [(c[2]) for c in FakeBale.calls if c[1] == "sendMessage"]
    assert all("parse_mode" not in s and "link_preview_options" not in s and "disable_notification" not in s for s in sent)
    assert any("شناسهٔ عددی شما: 77" in s["text"] for s in sent)
    assert any("ادمین کانال" in s["text"] and "<b>" not in s["text"] and "<code>" not in s["text"] for s in sent)  # /start help converted
    assert any(s["text"].startswith("این ربات خصوصی است.") and "شناسهٔ عددی شما: 5" in s["text"] for s in sent)  # stranger refused
    ch = [s for s in sent if s["chat_id"] == "@mychannel"]
    assert ch and "📌  *تیتر & نمونه*" in ch[0]["text"] and "[فراخوان](https://pouyancrowd.ir/?a=1&b=2)" in ch[0]["text"]
    assert any("تیتر آزمایشی" in s["text"] and "*تیتر آزمایشی*" in s["text"] for s in sent)  # /balecheck sample
    assert any(c[1] == "getMe" for c in FakeBale.calls) and any(c[1] == "getChatMember" for c in FakeBale.calls)
    assert any("حالت خودکار فعال است" in s["text"] for s in sent)  # startup notice reached the owner
    assert not any("ادمین با دسترسی" in s["text"] for s in sent)  # no false 'not admin' warning on Bale


def test_bale_connection_ignores_the_system_proxy():
    """Bale is reached directly: a dead HTTP(S)_PROXY (VPN left on, Windows proxy) must not break it."""
    class Ok(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    keep = dict(os.environ)
    try:
        for k in ("NO_PROXY", "no_proxy", "BALE_PROXY"):
            os.environ.pop(k, None)
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ[k] = "http://127.0.0.1:9"  # nothing listens there
        req = plat._bale_request(1)

        async def go():
            async with req._client as c:  # type: ignore[attr-defined]
                return (await c.get(f"http://127.0.0.1:{server.server_port}/")).text
        assert asyncio.run(go()) == "ok"
    finally:
        os.environ.clear()
        os.environ.update(keep)
        server.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
