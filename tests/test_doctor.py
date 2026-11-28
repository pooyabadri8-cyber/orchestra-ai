"""The health check: each problem found and explained, with what to do, and nothing sent or stored."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestra.control.botapi import BotApiError
from orchestra.data.acquisition import AcquisitionError
from orchestra.data.sources.base import ParseResult, ParsedPoint
from orchestra.db import ControlState
from orchestra.doctor import FAIL, OK, WARN, Doctor, report
from orchestra.llm import LLMError
from orchestra.settings import Settings

NOW = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)


class FakeBot:
    def __init__(self, platform, token):
        self.token = token

    def call(self, method, data=None):
        if self.token == "bad":
            raise BotApiError("getMe: Unauthorized", retryable=False, code=401)
        if method == "getMe":
            return {"id": 7, "username": f"bot_{self.token}"}
        if method == "getChat":
            if self.token == "nochat":
                raise BotApiError("getChat: Bad Request: chat not found", retryable=False, code=400)
            return {"type": "channel", "title": "کانال تست"}
        if method == "getChatMember":
            return {"status": "member"} if self.token == "member" else {"status": "administrator", "can_post_messages": True}
        raise AssertionError(method)


class FakeGateway:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    async def complete(self, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return "سلام"


class Page:
    source_id, parser_version, url = "bourse_trader", "x@1", "https://page.test/"

    def parse(self, raw):
        return ParseResult([ParsedPoint("index.total.value", 1.0, "point", 0, "1")])


def make(sessions, tmp_path, monkeypatch, *, gateway=None, tokens=None, page_down=False, news_down=False, browser_ok=True,
         destinations=None, rtm=True):
    for k, v in (tokens or {}).items():
        monkeypatch.setenv(k, v)
    cfg = Settings(control_bot_token="good", control_chat_ids="111", control_platform="bale", llm_model="model-x",
                   orgs_dir=Path("orgs"), backup_dir=tmp_path / "bk")

    async def fetch(url):
        if page_down:
            raise AcquisitionError("دریافت ناموفق (ConnectError)", retryable=True)
        return b"page"

    async def news_fetch(url, foreign):
        if news_down and foreign:
            raise AcquisitionError("دریافت ناموفق (ConnectError)", retryable=True)
        from test_research import REAL, TG
        return TG if "t.me" in url else REAL["sena"]
    runtime = SimpleNamespace(gateway=gateway or FakeGateway(), sources={"bourse_trader": Page()}, fetcher=fetch,
                              news_fetcher=news_fetch) if rtm else None

    async def browser():
        if not browser_ok:
            raise RuntimeError("هیچ مرورگری پیدا نشد")
        return "chromium 130"
    dests = destinations if destinations is not None else [
        SimpleNamespace(id="tg_test", platform="telegram", chat_id="-100", secret_env="TG_TOKEN", enabled=True)]
    return Doctor(cfg, "pouyan", sessions=sessions, rtm=runtime, destinations=dests, bot_factory=FakeBot,
                  browser_probe=browser, project=tmp_path, now=NOW,
                  runtime_error="" if rtm else "ORCHESTRA_LLM_API_KEY و ORCHESTRA_LLM_MODEL را در فایل .env پر کنید.")


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_healthy_setup(sessions, tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_INTAKE_BOT_TOKEN", raising=False)
    with sessions.begin() as s:
        s.merge(ControlState(key="backup:last", value=(NOW - timedelta(hours=10)).isoformat()))
    doc = make(sessions, tmp_path, monkeypatch, tokens={"TG_TOKEN": "good"})
    checks = by_name(asyncio.run(doc.run()))
    assert checks["پاسخ مدل (model-x)"].status == OK and checks["ربات کنترل (bale)"].status == OK
    assert checks["tg_test (telegram)"].status == OK and "کانال تست" in checks["tg_test (telegram)"].detail
    assert checks["bourse_trader"].status == OK and checks["مرورگر برای ساخت اسلاید"].status == OK
    assert checks["آخرین پشتیبان"].status == OK and checks["برند (brand.yaml)"].status == OK
    assert checks["ربات پذیرش (CRM)"].status == WARN  # no intake token: said, not a failure
    assert all(c.status == OK for c in checks.values() if c.area in ("تنظیمات", "منبع‌های خبری"))


def test_each_problem_is_explained_with_what_to_do(sessions, tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_INTAKE_BOT_TOKEN", raising=False)
    dests = [SimpleNamespace(id="a", platform="telegram", chat_id="-1", secret_env="T_MEMBER", enabled=True),
             SimpleNamespace(id="b", platform="telegram", chat_id="-2", secret_env="T_NOCHAT", enabled=True),
             SimpleNamespace(id="c", platform="bale", chat_id="@c", secret_env="T_MISSING", enabled=True),
             SimpleNamespace(id="d", platform="instagram", chat_id="1784", secret_env="T_IG", enabled=True)]
    monkeypatch.delenv("T_MISSING", raising=False)
    doc = make(sessions, tmp_path, monkeypatch, gateway=FakeGateway(LLMError("HTTP 429: quota")), page_down=True,
               news_down=True, browser_ok=False, destinations=dests,
               tokens={"T_MEMBER": "member", "T_NOCHAT": "nochat", "T_IG": "x"})
    checks = by_name(asyncio.run(doc.run()))
    assert checks["پاسخ مدل (model-x)"].status == FAIL and "سهمیه" in checks["پاسخ مدل (model-x)"].fix
    assert checks["a (telegram)"].status == FAIL and "Post messages" in checks["a (telegram)"].fix
    assert checks["b (telegram)"].status == FAIL and "ادمین" in checks["b (telegram)"].fix
    assert checks["c (bale)"].status == WARN and "T_MISSING" in checks["c (bale)"].fix
    assert checks["d (instagram)"].status == FAIL and "ORCHESTRA_PUBLIC_ASSET_DIR" in checks["d (instagram)"].fix
    assert checks["bourse_trader"].status == FAIL and checks["مرورگر برای ساخت اسلاید"].status == FAIL
    assert checks["بی‌بی‌سی فارسی"].status == WARN and "ORCHESTRA_FOREIGN_PROXY" in checks["بی‌بی‌سی فارسی"].fix
    assert checks["آخرین پشتیبان"].status == WARN and checks["جای پشتیبان"].status == WARN  # same disk in the test
    text = report(list(checks.values()))
    first_ok = text.index("درست‌ها:")
    assert text.startswith("🩺 بررسی سلامت:") and text.index("❌") < first_ok and "← " in text


def test_a_bad_token_and_a_broken_env(sessions, tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_INTAKE_BOT_TOKEN", raising=False)
    doc = make(sessions, tmp_path, monkeypatch, rtm=False, tokens={"TG_TOKEN": "bad"})
    doc.cfg = doc.cfg.model_copy(update={"control_bot_token": "bad"})
    checks = by_name(asyncio.run(doc.run()))
    assert checks["ربات کنترل (bale)"].status == FAIL and "BotFather" in checks["ربات کنترل (bale)"].fix
    assert checks["فایل .env"].status == FAIL and checks["پاسخ مدل"].status == FAIL
    assert checks["tg_test (telegram)"].status == FAIL


def test_without_model_settings_the_sources_are_still_checked(sessions, tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_INTAKE_BOT_TOKEN", raising=False)
    doc = make(sessions, tmp_path, monkeypatch, tokens={"TG_TOKEN": "good"})
    doc.rtm.gateway = None  # what `check` passes when the model is not set up
    checks = by_name(asyncio.run(doc.run()))
    assert checks["پاسخ مدل"].status == FAIL and checks["bourse_trader"].status == OK and checks["سنا (پایگاه خبری بازار سرمایه)"].status == OK


def test_check_in_the_control_chat(sessions, assets, clock):
    from conftest import add_destinations
    from orchestra.control.service import ControlService
    from test_control import OWNER, FakeApi, FakeEngine
    add_destinations(sessions)
    api, engine = FakeApi(), FakeEngine(sessions, assets, clock)
    svc = ControlService(org_id="pouyan", api=api, engine=engine, sessions=sessions, assets=assets, owners=(OWNER,), clock=clock)
    api.say("/check")
    svc.tick()
    assert "فقط وقتی برنامه کامل روشن است" in api.texts()[-1]
    engine.run_checks = lambda org: "🩺 بررسی سلامت: ✅ 3 درست"
    api.say("/check")
    svc.tick()
    assert api.texts()[-2].startswith("🩺 در حال بررسی") and api.texts()[-1].startswith("🩺 بررسی سلامت")


@pytest.mark.parametrize("status", [OK, WARN, FAIL])
def test_report_counts(status):
    from orchestra.doctor import Check
    text = report([Check("الف", "یک", status, "جزئیات", "کار")])
    assert {"ok": "✅ 1 درست", "warn": "⚠️ 1 هشدار", "fail": "❌ 1 مشکل"}[status] in text
