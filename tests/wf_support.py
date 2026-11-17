"""Helpers for workflow tests: a fresh DBOS system database per test, and a runtime with a scripted model."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from orchestra.assets import AssetStore
from orchestra.data.sources.bourse_trader import BourseTraderSource
from orchestra.data.sources.tgju import TgjuSource
from orchestra.db import Base, Destination, Run, make_engine, make_sessionmaker
from orchestra.llm import Completion, Gateway, Route
from orchestra.runtime import Runtime

ROOT = Path(__file__).resolve().parent.parent
PAGE = (Path(__file__).parent / "fixtures" / "bourse_trader_sample.html").read_bytes()
PG_ADMIN = os.environ.get("ORCHESTRA_TEST_PG_ADMIN", "postgresql+psycopg://orchestra:orchestra@localhost:5432/postgres")
SYS_DB = "orchestra_dbos_wf"
SYS_URL = f"postgresql://orchestra:orchestra@localhost:5432/{SYS_DB}"

GOOD = {
    "skip": False, "hashtag": "#تحلیل_بازار",
    "headline": "رشد {{P3|abs_pct}} شاخص کل در {{D1|name}}",
    "paragraphs": [
        "شاخص کل بورس امروز با رشد {{P3|abs_pct}} به {{P1|int}} واحد رسید و ارزش معاملات خرد {{P4}} بود.",
        "در معاملات خرد {{P5|abs_money}} پول حقیقی وارد بازار شد و گروه {{P6|name}} بیشترین ورود پول را داشت.",
    ],
    "cta": "نظرتان را دربارهٔ بازار امروز بنویسید.",
}
TYPED_NUMBER = {**GOOD, "headline": "رشد ۲ درصدی شاخص کل"}


def postgres_available() -> bool:
    try:
        eng = create_engine(PG_ADMIN)
        with eng.connect() as c:
            c.execute(text("select 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def fresh_system_db() -> str:
    eng = create_engine(PG_ADMIN, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SYS_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SYS_DB}"))
    eng.dispose()
    return SYS_URL


class ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self.replies = [json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r for r in replies]
        self.calls = []

    async def complete(self, model, system, user, max_tokens):
        self.calls.append(user)
        return Completion(self.replies.pop(0), 800, 250, False)


class RecordingNotifier:
    def __init__(self):
        self.requests = []

    async def request(self, **kw):
        self.requests.append(kw)


TSE_DOWN = False  # set by a test to make TSE unreachable
TGJU_DOWN = False


def make_runtime(workdir: Path, replies, page: bytes = PAGE) -> tuple[Runtime, ScriptedProvider, RecordingNotifier]:
    engine = make_engine(f"sqlite:///{workdir / 'app.db'}")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    with sessions.begin() as s:
        for d in (Destination(id="tg_test", org_id="pouyan", platform="telegram", chat_id="@t", secret_env="X"),
                  Destination(id="bale_test", org_id="pouyan", platform="bale", chat_id="@b", secret_env="Y")):
            s.merge(d)
    provider, notifier = ScriptedProvider(replies), RecordingNotifier()

    async def fetch(url):
        if "tgju" in url:
            if TGJU_DOWN:
                from orchestra.data.acquisition import AcquisitionError
                raise AcquisitionError("HTTP 404 از tgju", retryable=False)
            return (Path(__file__).parent / "fixtures" / "tgju_ajax_sample.json").read_bytes()
        if "tsetmc" in url:
            if TSE_DOWN:
                from orchestra.data.acquisition import AcquisitionError
                raise AcquisitionError("HTTP 404 از tsetmc", retryable=False)
            from tse_samples import index_b2, index_financial
            return index_financial() if "IndexFinancial" in url else index_b2()
        return page

    runtime = Runtime(
        sessions=sessions, assets=AssetStore(workdir / "assets"),
        gateway=Gateway(sessions, {"writer": [Route(provider, "scripted-model")]}),
        fetcher=fetch, sources={"bourse_trader": BourseTraderSource(), "tgju": TgjuSource()}, brand_root=ROOT / "orgs",
        notifier=notifier, clock=lambda: datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc),
    )
    return runtime, provider, notifier


def wait_for_status(sessions, run_id: str, wanted: set[str], timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sessions() as s:
            run = s.get(Run, run_id)
            if run is not None and run.status in wanted:
                return run.status
        time.sleep(0.2)
    raise TimeoutError(f"{run_id} did not reach {wanted}")
