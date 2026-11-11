"""Shared fixtures. Every database test runs on SQLite and, when reachable, on Postgres.

Postgres URL: ORCHESTRA_TEST_DATABASE_URL, default postgresql+psycopg://orchestra:orchestra@localhost:5432/orchestra_test.
Set ORCHESTRA_TEST_DATABASE_URL=skip to run on SQLite only.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from orchestra.assets import AssetStore
from orchestra.db import Base, Destination, make_engine, make_sessionmaker
from orchestra.package import (
    AssetRef, Checks, DataPointRef, DeliveryStep, DestinationPlan, PackageContent, PublicationPackage, Sources, TextPart,
)

PG_URL = os.environ.get("ORCHESTRA_TEST_DATABASE_URL", "postgresql+psycopg://orchestra:orchestra@localhost:5432/orchestra_test")


def _postgres_available() -> bool:
    if PG_URL == "skip":
        return False
    try:
        eng = create_engine(PG_URL)
        with eng.connect() as c:
            c.execute(text("select 1"))
        eng.dispose()
        return True
    except Exception:
        return False


BACKENDS = ["sqlite"] + (["postgres"] if _postgres_available() else [])


@pytest.fixture(params=BACKENDS)
def sessions(request, tmp_path: Path):
    if request.param == "sqlite":
        engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    else:
        engine = make_engine(PG_URL)
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield make_sessionmaker(engine)
    if request.param == "postgres":
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def assets(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 11, 11, 7, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


PNG = b"\x89PNG\r\n\x1a\n" + b"slide-bytes-"


def image(n: int) -> bytes:
    return PNG + str(n).encode() * 40


def add_destinations(sessions, org: str = "pouyan") -> None:
    with sessions.begin() as s:
        s.merge(Destination(id="tg_test", org_id=org, platform="telegram", chat_id="@pouyan_test", secret_env="TELEGRAM_TEST_BOT_TOKEN"))
        s.merge(Destination(id="bale_test", org_id=org, platform="bale", chat_id="@pouyan_test_bale", secret_env="BALE_TEST_BOT_TOKEN"))


def make_content(store: AssetStore | None = None, *, version: int = 1, supersedes: str | None = None, body: str = "متن پست",
                 not_before: datetime | None = None, expires_at: datetime | None = None, slides: int = 3,
                 org: str = "pouyan") -> PackageContent:
    refs = []
    for i in range(slides):
        data = image(i)
        sha = store.put(data) if store else hashlib.sha256(data).hexdigest()
        refs.append(AssetRef(asset_id=f"s{i}", order=i, role="slide", sha256=sha, mime="image/png", bytes=len(data),
                             width=1080, height=1350, template="market_close@1"))
    ids = tuple(a.asset_id for a in refs)
    tg_plan = []
    if slides >= 2:
        tg_plan.append(DeliveryStep(mode="album", body=f"<b>{body}</b>", asset_ids=ids))
    if slides >= 1:
        tg_plan.append(DeliveryStep(mode="photo", body=f"<b>{body}</b>", asset_ids=ids[:1]))
    tg_plan.append(DeliveryStep(mode="text", body=f"<b>{body}</b>"))
    bale_plan = [DeliveryStep(mode="text", body=f"*{body}*", parse_mode="Markdown")]
    return PackageContent(
        org_id=org, run_id="run_1", version=version, supersedes=supersedes, content_class="market_close",
        text=TextPart(canonical_html=f"<b>{body}</b>", template="post_template@7", claim_ids=("clm_1",)),
        assets=tuple(refs),
        destinations=(
            DestinationPlan(destination_id="tg_test", platform="telegram", not_before=not_before, expires_at=expires_at,
                            delivery_plan=tuple(tg_plan)),
            DestinationPlan(destination_id="bale_test", platform="bale", not_before=not_before, expires_at=expires_at,
                            delivery_plan=tuple(bale_plan)),
        ),
        sources=Sources(data_points=(DataPointRef(id="dp_1", metric="index.total", value=3_412_000, unit="point",
                                                  as_of=datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc),
                                                  source_id="tse", raw_sha256="a" * 64),)),
        versions={"workflow": "market_close_v1@1", "writer": "1.0.0"},
        checks=Checks(risk_level="low"),
    )


def make_package(store: AssetStore | None = None, **kw) -> PublicationPackage:
    return PublicationPackage.build(make_content(store, **kw))
