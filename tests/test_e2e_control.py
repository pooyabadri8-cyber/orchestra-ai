"""The control bot, end to end: the real `serve` process, SQLite, the real workflow engine and publisher, with the
Bot API (control chat and channels), the model and the market page served by a local HTTP server.

The owner writes a request in the chat, is shown the content with buttons, approves, and the posts go to the channels.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from test_e2e_cli import FakeServices, env_for, project  # noqa: F401  (project is a fixture)

OWNER, STRANGER = "111", "999"
REQUEST = "گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن"


def configure(work, platform: str, owners: str = OWNER) -> None:
    env = (work / ".env").read_text(encoding="utf-8-sig")
    env = env.replace("TELEGRAM_TEST_BOT_TOKEN=", "TELEGRAM_TEST_BOT_TOKEN=TGCHAN").replace("BALE_TEST_BOT_TOKEN=", "BALE_TEST_BOT_TOKEN=BLCHAN")
    env += f"ORCHESTRA_CONTROL_PLATFORM={platform}\nORCHESTRA_CONTROL_BOT_TOKEN=CTRL\nORCHESTRA_CONTROL_CHAT_IDS={owners}\nORCHESTRA_CONTROL_POLL_SECONDS=1\n"
    (work / ".env").write_text(env, encoding="utf-8")


class Service:
    def __init__(self, work):
        self.work, self.proc = work, None

    def start(self):
        self.log = open(self.work / "serve.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "orchestra.cli", "serve"], cwd=self.work, env=env_for(self.work),
                                     stdout=self.log, stderr=subprocess.STDOUT)
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self.log.close()

    def output(self) -> str:
        return (self.work / "serve.log").read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def service(project):  # noqa: F811
    svc = Service(project)
    yield svc
    svc.stop()


_uid = [1000]


def say(text: str, user: str = OWNER) -> None:
    _uid[0] += 1
    with FakeServices.lock:
        FakeServices.updates.append({"update_id": _uid[0], "message": {"text": text, "chat": {"id": int(user)}, "from": {"id": int(user)}}})


def press(data: str, user: str = OWNER) -> None:
    _uid[0] += 1
    with FakeServices.lock:
        FakeServices.updates.append({"update_id": _uid[0], "callback_query": {
            "id": str(_uid[0]), "data": data, "from": {"id": int(user)}, "message": {"chat": {"id": int(user)}}}})


def sent(token: str | None = None, method: str | None = None) -> list[dict]:
    with FakeServices.lock:
        return [m for m in FakeServices.sent if (token is None or m["token"] == token) and (method is None or m["method"] == method)]


def wait_for(what, svc: Service, timeout: float = 120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = what()
        if got:
            return got
        assert svc.proc.poll() is None, "the service stopped:\n" + svc.output()
        time.sleep(0.2)
    raise AssertionError("timed out.\n--- service output ---\n" + svc.output() + "\n--- sent ---\n"
                         + "\n".join(f"{m['token']} {m['method']} {str(m.get('text', ''))[:80]}" for m in sent()))


def control_texts() -> list[str]:
    return [m.get("text", "") for m in sent("CTRL") if m["method"] in ("sendMessage", "editMessageText")]


def question():
    return next((m for m in sent("CTRL", "sendMessage") if "reply_markup" in m), None)


@pytest.mark.parametrize("platform", ["telegram", "bale"])
def test_request_approve_publish_from_the_phone(project, service, platform):  # noqa: F811
    configure(project, platform)
    service.start()
    say("/start")
    wait_for(lambda: any("ربات کنترل" in t for t in control_texts()), service)

    say("سلام، گزارش بده", user=STRANGER)  # not an owner: ignored completely
    say(REQUEST)
    q = wait_for(question, service)
    assert q["chat_id"] == OWNER
    buttons = json.loads(q["reply_markup"])["inline_keyboard"][0]
    assert [b["callback_data"][:3] for b in buttons] == ["ok:", "ed:", "no:"]
    texts = control_texts()
    assert any("دریافت شد" in t for t in texts)
    assert any("شاخص کل" in t and "{{" not in t for t in texts), texts  # the channel text itself was shown
    assert sent("CTRL", "sendMediaGroup"), "the Instagram slides were not shown"
    assert not any(m.get("chat_id") == STRANGER for m in sent()), "a stranger was answered"
    assert not sent("TGCHAN") and not sent("BLCHAN"), "something was published before approval"

    press(buttons[0]["callback_data"], user=STRANGER)  # a stranger pressing the button changes nothing
    press(buttons[0]["callback_data"])
    wait_for(lambda: sent("TGCHAN", "sendMessage") and sent("BLCHAN", "sendMessage"), service)
    wait_for(lambda: sum("✅ منتشر شد" in t for t in control_texts()) >= 2 and any("❌ منتشر نشد" in t for t in control_texts()), service)
    assert "شاخص کل" in sent("TGCHAN", "sendMessage")[0]["text"]
    assert sent("TGCHAN", "sendMessage")[0].get("parse_mode") == "HTML" and "parse_mode" not in sent("BLCHAN", "sendMessage")[0]

    press(buttons[0]["callback_data"])  # pressed again: nothing is sent twice
    wait_for(lambda: any("باز نیست" in t for t in control_texts()), service)
    say("/status")
    wait_for(lambda: any("تأیید شد" in t and "انتشار فعال" in t for t in control_texts()), service)
    assert len(sent("TGCHAN")) == 1 and len(sent("BLCHAN")) == 1

    # restart: old messages are not handled again, nothing is re-sent, and the bot still answers
    service.stop()
    before = len(sent())
    service.start()
    say("/jobs")
    wait_for(lambda: any("ارسال 1 به" in t for t in control_texts()[-3:]), service)
    assert len(sent("TGCHAN")) == 1 and len(sent("BLCHAN")) == 1
    after = [m for m in sent()[before:] if m["method"] != "getMe"]
    assert len(after) == 1, [m.get("text", m["method"])[:60] for m in after]
    assert "Traceback" not in service.output(), service.output()
    assert "CTRL" not in service.output() and "TGCHAN" not in service.output(), "a bot token was printed"
    import os
    if os.environ.get("DUMP_CHAT"):
        open(os.environ["DUMP_CHAT"], "w", encoding="utf-8").write("\n".join(
            f"[{m['token']} {m['method']} -> {m.get('chat_id')}] {m.get('text', '')} {m.get('reply_markup', '')}" for m in sent()))


def test_reject_by_typing_and_a_run_that_waited_before_the_service_started(project, service):  # noqa: F811
    from test_e2e_cli import run_ask, run_id_of
    configure(project, "bale")
    rid = run_id_of(run_ask(project, REQUEST))  # prepared from the computer, before the bot was running
    service.start()
    q = wait_for(question, service)
    pid = json.loads(q["reply_markup"])["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    say(f"رد {pid} عدد شاخص را دوباره ببینید")
    wait_for(lambda: any("رد شد" in t and "عدد شاخص" in t for t in control_texts()), service)
    out = subprocess.run([sys.executable, "-m", "orchestra.cli", "show", rid], cwd=project, env=env_for(project),
                         capture_output=True, text=True, encoding="utf-8").stdout
    assert "rejected" in out
    assert not sent("TGCHAN") and not sent("BLCHAN")


def test_without_allowed_ids_the_bot_only_tells_the_id(project, service):  # noqa: F811
    configure(project, "telegram", owners="")
    service.start()
    say(REQUEST, user="424242")
    wait_for(lambda: any("424242" in t and "ORCHESTRA_CONTROL_CHAT_IDS" in t for t in control_texts()), service)
    time.sleep(2)
    assert FakeServices.calls == [], "a request from an unknown person reached the model"


def test_serve_without_token_explains(project):  # noqa: F811
    p = subprocess.run([sys.executable, "-m", "orchestra.cli", "serve"], cwd=project, env=env_for(project),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode != 0 and "ORCHESTRA_CONTROL_BOT_TOKEN" in p.stdout + p.stderr and "Traceback" not in p.stderr


@pytest.mark.parametrize("seed", range(1, int(__import__("os").environ.get("CHAOS_SEEDS", "3")) + 1))
def test_killing_the_service_at_random_moments_never_publishes_twice(project, service, seed):  # noqa: F811
    """Stop the process again and again while it plans, writes, designs, asks and publishes. After each restart it
    must carry on, and at the end every channel has at most one post; a send that was cut is reported as unclear."""
    import random
    rnd = random.Random(seed)
    configure(project, "bale")
    service.start()
    say(REQUEST)
    restarts, pressed_pid, wait_after = 0, None, 0
    noted = not (rnd.random() < 0.6)  # in most runs the owner first sends it back with a note
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        while service.output().count("ربات کنترل روشن است") <= restarts:  # count from the moment it is working
            time.sleep(0.05)
        time.sleep(rnd.choice([rnd.uniform(0.0, 1.0), rnd.uniform(0.0, 7.0)]))
        texts = control_texts()
        qs = questions()
        latest = json.loads(qs[-1]["reply_markup"])["inline_keyboard"][0][0]["callback_data"].split(":")[1] if qs else None
        if latest and not noted:
            say(f"اصلاح {latest} تیتر کوتاه‌تر شود")
            noted, wait_after = True, len(qs)
        elif latest and noted and latest != pressed_pid and len(qs) > wait_after:
            press(f"ok:{latest}")  # always the newest question; an older button must be dead
            pressed_pid = latest
            time.sleep(rnd.uniform(0.0, 1.5))
        done = sum(("منتشر شد" in t or "نامعلوم" in t or "منتشر نشد" in t) and t.startswith("ارسال") for t in texts)
        if pressed_pid and done >= 3:
            break
        service.stop()
        service.start()
        restarts += 1
    else:
        raise AssertionError("never finished.\n" + service.output() + "\n" + "\n".join(control_texts()))
    time.sleep(3)
    assert len(sent("TGCHAN")) <= 1 and len(sent("BLCHAN")) <= 1, "a channel got the post twice"
    out = subprocess.run([sys.executable, "-m", "orchestra.cli", "jobs"], cwd=project, env=env_for(project),
                         capture_output=True, text=True, encoding="utf-8").stdout
    for dest, token in (("tg_test", "TGCHAN"), ("bale_test", "BLCHAN")):
        line = next(ln for ln in out.splitlines() if dest in ln)
        if "confirmed" in line:
            assert len(sent(token)) == 1, line
        else:
            assert "unknown" in line, line  # cut mid-send: a human decides, never an automatic resend
    assert FakeServices.calls.count("writer") <= 4 and FakeServices.calls.count("manager") <= 2, FakeServices.calls
    print(f"seed {seed}: {restarts} restarts, {len(questions())} questions, model calls {FakeServices.calls}")


def test_a_wrong_token_is_explained_at_start(project):  # noqa: F811
    configure(project, "telegram")
    env = (project / ".env").read_text(encoding="utf-8")
    port = env.split("ORCHESTRA_TELEGRAM_API_BASE=http://127.0.0.1:")[1].split("/")[0]
    (project / ".env").write_text(env.replace(f"ORCHESTRA_TELEGRAM_API_BASE=http://127.0.0.1:{port}/bot",
                                              f"ORCHESTRA_TELEGRAM_API_BASE=http://127.0.0.1:{port}/nobot"), encoding="utf-8")
    p = subprocess.run([sys.executable, "-m", "orchestra.cli", "serve"], cwd=project, env=env_for(project),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode != 0 and "توکن را نپذیرفت" in p.stdout + p.stderr and "Traceback" not in p.stderr


def questions():
    return [m for m in sent("CTRL", "sendMessage") if "reply_markup" in m]


def test_send_back_with_a_note_then_approve_the_new_version(project, service):  # noqa: F811
    configure(project, "bale")
    service.start()
    say(REQUEST)
    q1 = wait_for(lambda: questions(), service)[0]
    row = [b["callback_data"] for b in json.loads(q1["reply_markup"])["inline_keyboard"][0]]
    assert row == ["ok:1", "ed:1", "no:1"]
    press("ed:1")
    wait_for(lambda: any("چه چیزی عوض شود" in t for t in control_texts()), service)
    say("تیتر را کوتاه‌تر کن")
    wait_for(lambda: len(questions()) == 2, service)
    assert any("نسخهٔ 2" in t for t in control_texts())
    writer_prompts = [p for role, p in FakeServices.prompts if role == "writer"]
    assert len(writer_prompts) == 2 and "تیتر را کوتاه‌تر کن" in writer_prompts[1] and "تیتر را کوتاه‌تر کن" not in writer_prompts[0]
    assert FakeServices.calls.count("manager") == 1  # the plan and the data are not redone
    assert not sent("TGCHAN") and not sent("BLCHAN")

    press("ok:1")  # the first version's button is dead
    wait_for(lambda: any("باز نیست" in t for t in control_texts()), service)
    press("ok:2")
    wait_for(lambda: sent("TGCHAN", "sendMessage") and sent("BLCHAN", "sendMessage"), service)
    wait_for(lambda: sum("✅ منتشر شد" in t for t in control_texts()) >= 2, service)
    assert len(sent("TGCHAN")) == 1 and len(sent("BLCHAN")) == 1
    assert "Traceback" not in service.output(), service.output()
