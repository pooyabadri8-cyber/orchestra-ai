"""The set-up conversation for a fresh computer (scripts/configure.py), with the model service and the Bot API faked."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("configure", ROOT / "scripts" / "configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class FakeIo(configure.Io):
    def __init__(self, answers):
        self.answers, self.out = list(answers), []

    def ask(self, prompt):
        self.out.append(prompt)
        return self.answers.pop(0)

    secret = ask

    def say(self, text=""):
        self.out.append(text)

    def sleep(self, s):
        pass


class World:
    """Gemini's /models and a Bot API whose updates appear after the bot starts waiting."""

    def __init__(self, good_key="GOOD", token="123:TOK"):
        self.good_key, self.token = good_key, token
        self.updates, self.pending, self.calls = [], [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            ok = req.headers.get("authorization") == f"Bearer {self.good_key}"
            return httpx.Response(200 if ok else 401, json={"data": []})
        token, method = req.url.path.split("/bot", 1)[1].split("/", 1)
        if token != self.token:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        form = dict(x.split("=", 1) for x in req.content.decode().split("&") if "=" in x)
        self.calls.append((method, form))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "TestBot"}})
        offset = int(form.get("offset", 0))
        if offset == -1:
            result = self.updates[-1:]
        else:
            if self.pending and int(form.get("timeout", 0)) > 0:
                self.updates.append(self.pending.pop(0))  # the user acts while we wait
            result = [u for u in self.updates if u["update_id"] >= offset]
        return httpx.Response(200, json={"ok": True, "result": result})


def files(tmp_path):
    env, dest = tmp_path / ".env", tmp_path / "destinations.yaml"
    dest.write_text(yaml.safe_dump({"destinations": [
        {"id": "tg_test", "platform": "telegram", "chat_id": "-100old", "secret_env": "TELEGRAM_TEST_BOT_TOKEN"},
        {"id": "tg_test_slides", "platform": "telegram", "content": "carousel", "chat_id": "-100old", "secret_env": "TELEGRAM_TEST_BOT_TOKEN"},
        {"id": "bale_test", "platform": "bale", "chat_id": "@b", "secret_env": "BALE_TEST_BOT_TOKEN"}]}), encoding="utf-8")
    return env, dest


def run(world, answers, env, dest):
    io = FakeIo(answers)
    http = httpx.Client(transport=httpx.MockTransport(world.handler))
    assert configure.configure(io, http, telegram_base="https://tg.example/bot", env_path=env, destinations_path=dest) == 0
    return io


def test_fresh_computer_full_setup(tmp_path):
    env, dest = files(tmp_path)
    w = World()
    w.updates = [{"update_id": 5, "channel_post": {"chat": {"id": -100999, "type": "channel", "title": "old"}}}]  # stale
    w.pending = [{"update_id": 6, "channel_post": {"chat": {"id": -100777, "type": "channel", "title": "تست"}}},
                 {"update_id": 7, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/start"}}]
    io = run(w, ["BAD", "", "GOOD", "", "123:TOK", "", ""], env, dest)
    values = configure.read_env(env)
    assert values["ORCHESTRA_LLM_API_KEY"] == "GOOD" and values["ORCHESTRA_LLM_MODEL"] == configure.DEFAULT_MODELS
    assert values["ORCHESTRA_DATABASE_URL"] == "sqlite:///data/app.db"
    assert values["TELEGRAM_TEST_BOT_TOKEN"] == values["ORCHESTRA_CONTROL_BOT_TOKEN"] == "123:TOK"
    assert values["ORCHESTRA_CONTROL_CHAT_IDS"] == "42" and values["ORCHESTRA_CONTROL_PLATFORM"] == "telegram"
    d = {x["id"]: x["chat_id"] for x in yaml.safe_load(dest.read_text(encoding="utf-8"))["destinations"]}
    assert d == {"tg_test": "-100777", "tg_test_slides": "-100777", "bale_test": "@b"}  # the stale channel was skipped
    shown = "\n".join(io.out)
    assert "GOOD" not in shown and "TOK" not in shown  # secrets are never printed
    assert "کلید پذیرفته نشد" in shown and "@TestBot" in shown


def test_running_again_keeps_everything_with_enter(tmp_path):
    env, dest = files(tmp_path)
    env.write_text("# my comment\nORCHESTRA_LLM_API_KEY=GOOD\nORCHESTRA_LLM_MODEL=m1,m2\nTELEGRAM_TEST_BOT_TOKEN=123:TOK\n"
                   "ORCHESTRA_CONTROL_CHAT_IDS=42\nEXTRA=keep\n", encoding="utf-8")
    run(World(), ["", "", "", "n", "", ""], env, dest)
    text = env.read_text(encoding="utf-8")
    values = configure.read_env(env)
    assert "# my comment" in text and values["EXTRA"] == "keep" and values["ORCHESTRA_LLM_MODEL"] == "m1,m2"
    assert values["ORCHESTRA_CONTROL_CHAT_IDS"] == "42" and text.count("ORCHESTRA_LLM_API_KEY=") == 1


def test_without_a_bot_nothing_breaks(tmp_path):
    env, dest = files(tmp_path)
    run(World(), ["GOOD", "", ""], env, dest)
    values = configure.read_env(env)
    assert "TELEGRAM_TEST_BOT_TOKEN" not in values and "ORCHESTRA_CONTROL_BOT_TOKEN" not in values
    assert "-100old" in dest.read_text(encoding="utf-8")


def test_a_wrong_token_is_explained_and_can_be_skipped(tmp_path):
    env, dest = files(tmp_path)
    io = run(World(), ["GOOD", "", "999:WRONG", "n"], env, dest)
    assert any("ربات جواب نداد" in t for t in io.out)
    assert "TELEGRAM_TEST_BOT_TOKEN" not in configure.read_env(env)


def test_unreachable_model_service_says_vpn(tmp_path):
    def down(req):
        raise httpx.ConnectError("no route")
    why = configure.check_model_key("k", configure.GEMINI_BASE, httpx.Client(transport=httpx.MockTransport(down)))
    assert "VPN" in why


def test_env_written_by_configure_is_read_by_the_app(tmp_path, monkeypatch):
    env, dest = files(tmp_path)
    configure.write_env({**configure.BASE_SETTINGS, "ORCHESTRA_LLM_API_KEY": "k", "ORCHESTRA_LLM_MODEL": "a,b"}, env)
    monkeypatch.chdir(tmp_path)
    for k in list(json.loads(json.dumps(dict(**__import__("os").environ)))):
        if k.startswith("ORCHESTRA_"):
            monkeypatch.delenv(k)
    from orchestra.settings import load_settings
    cfg = load_settings()
    assert cfg.llm_api_key == "k" and cfg.llm_model == "a,b" and cfg.database_url == "sqlite:///data/app.db"
