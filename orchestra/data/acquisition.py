"""Data Acquisition (deterministic, no LLM): fetch a source, keep the raw bytes, parse, store every value.

Nothing is estimated or filled in: a value that is not on the page does not exist.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets import AssetStore
from ..db import DataPointRow, DataSnapshot
from .sources.base import ParseError, Source

Fetcher = Callable[[str], Awaitable[bytes]]


class AcquisitionError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def point_id(snapshot_id: str, metric: str, subject: str | None, rank: int | None) -> str:
    return "dp_" + hashlib.sha256(f"{snapshot_id}|{metric}|{subject or ''}|{rank or ''}".encode()).hexdigest()[:24]


def snapshot_id(source_id: str, fetched_at: datetime, raw_sha256: str) -> str:
    return "snap_" + hashlib.sha256(f"{source_id}|{fetched_at.isoformat()}|{raw_sha256}".encode()).hexdigest()[:24]


def http_fetcher(timeout: float = 40.0, user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36") -> Fetcher:
    async def fetch(url: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15), follow_redirects=True,
                                         headers={"User-Agent": user_agent, "Accept-Language": "fa,en;q=0.8"},
                                         trust_env=False) as http:  # Iranian sources: no foreign proxy
                r = await http.get(url)
        except httpx.HTTPError as e:
            raise AcquisitionError(f"دریافت ناموفق ({type(e).__name__})", retryable=True) from e
        if r.status_code != 200:
            raise AcquisitionError(f"HTTP {r.status_code} از {url}", retryable=r.status_code in (429, 500, 502, 503, 504))
        return r.content
    return fetch


@dataclass
class Acquired:
    snapshot_id: str
    point_ids: list[str]
    warnings: list[str]


async def acquire(session: Session, assets: AssetStore, source: Source, fetch: Fetcher, now: datetime) -> Acquired:
    raw = await fetch(source.url)
    raw_sha = assets.put(raw)
    try:
        parsed = source.parse(raw)
    except ParseError as e:
        raise AcquisitionError(f"{source.source_id}: {e} (پاسخ خام: {raw_sha[:12]})", retryable=False) from e
    if not parsed.points:
        raise AcquisitionError(f"{source.source_id}: هیچ مقداری از صفحه خوانده نشد", retryable=False)
    sid = snapshot_id(source.source_id, now, raw_sha)
    if session.get(DataSnapshot, sid) is None:
        session.add(DataSnapshot(id=sid, source_id=source.source_id, url=source.url, fetched_at=now, as_of=now,
                                 raw_sha256=raw_sha, parser=source.parser_version, warnings=parsed.warnings))
        session.flush()  # the snapshot row must exist before its points reference it
        for p in parsed.points:
            session.add(DataPointRow(
                id=point_id(sid, p.metric, p.subject, p.rank), snapshot_id=sid, source_id=source.source_id,
                metric=p.metric, subject=p.subject, rank=p.rank, value=p.value, unit=p.unit, decimals=p.decimals,
                scale=p.scale, source_text=p.source_text, as_of=now,
            ))
        session.flush()
    ids = session.execute(select(DataPointRow.id).where(DataPointRow.snapshot_id == sid).order_by(DataPointRow.metric, DataPointRow.rank)).scalars().all()
    return Acquired(snapshot_id=sid, point_ids=list(ids), warnings=list(parsed.warnings))
