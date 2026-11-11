"""Manual data imports that work today, before the TSE and Codal connectors exist.

- Price history: a CSV with columns date, open, high, low, close, volume (Jalali 1404/08/20 or Gregorian 2025-11-11 dates;
  Persian or Latin digits; thousands separators allowed). Extra columns are ignored.
- Financial statements: a YAML file (see orgs/README-financials.md for the shape); money in rials, or set `unit`.

Each import keeps the raw file in the asset store and records a snapshot, like a fetched page, so every computed
figure can still be traced to the exact bytes it came from.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime

import yaml
from sqlalchemy.orm import Session

from ..analysis.fundamental import Statement
from ..analysis.technical import Bar
from ..assets import AssetStore
from ..content.calendar import to_gregorian
from ..db import DataSnapshot
from .sources.base import FA_DIGITS

UNITS = {"rial": 1.0, "thousand_rial": 1e3, "million_rial": 1e6, "billion_rial": 1e9, "toman": 10.0, "million_toman": 1e7}
ALIASES = {
    "date": ("date", "تاریخ", "<dtyyyymmdd>", "day"),
    "open": ("open", "اولین", "باز شدن", "<first>"),
    "high": ("high", "بیشترین", "حداکثر", "<high>"),
    "low": ("low", "کمترین", "حداقل", "<low>"),
    "close": ("close", "پایانی", "آخرین", "<close>", "close_price"),
    "volume": ("volume", "حجم", "<vol>"),
}


class ImportErrorWithLine(Exception):
    pass


def _num(text: str) -> float:
    t = str(text).translate(FA_DIGITS).replace(",", "").replace("٬", "").replace("٫", ".").strip()
    return float(t)


def parse_date(text: str) -> date:
    t = str(text).translate(FA_DIGITS).strip()
    m = re.fullmatch(r"(\d{4})[/\-]?(\d{1,2})[/\-]?(\d{1,2})", t)
    if not m:
        raise ValueError(f"تاریخ نامعتبر: {text}")
    y, mo, d = map(int, m.groups())
    return to_gregorian(y, mo, d) if y < 1700 else date(y, mo, d)


def parse_price_csv(raw: bytes) -> list[Bar]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(reader)]
    idx = {}
    for field, names in ALIASES.items():
        hit = next((i for i, h in enumerate(header) if h in names), None)
        if hit is None:
            raise ImportErrorWithLine(f"ستون «{field}» در سرستون‌ها پیدا نشد: {header}")
        idx[field] = hit
    bars = []
    for line_no, row in enumerate(reader, start=2):
        if not row or not any(c.strip() for c in row):
            continue
        try:
            bars.append(Bar(parse_date(row[idx["date"]]), _num(row[idx["open"]]), _num(row[idx["high"]]),
                            _num(row[idx["low"]]), _num(row[idx["close"]]), _num(row[idx["volume"]])))
        except (ValueError, IndexError) as e:
            raise ImportErrorWithLine(f"سطر {line_no}: {e}") from e
    bars.sort(key=lambda b: b.day)
    days = [b.day for b in bars]
    if len(set(days)) != len(days):
        raise ImportErrorWithLine("تاریخ تکراری در فایل هست")
    return bars


def parse_statements_yaml(raw: bytes) -> list[Statement]:
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    symbol = str(data["symbol"])
    factor = UNITS[data.get("unit", "rial")]
    out = []
    for p in data.get("periods", []):
        money = lambda k: None if p.get(k) is None else _num(p[k]) * factor  # noqa: E731
        out.append(Statement(
            symbol=symbol, fiscal_year=int(p["fiscal_year"]), months=int(p["months"]), period_end=parse_date(p["period_end"]),
            audited=bool(p.get("audited", False)), revenue=money("revenue"), gross_profit=money("gross_profit"),
            operating_profit=money("operating_profit"), net_income=money("net_income"), total_assets=money("total_assets"),
            total_equity=money("total_equity"), total_debt=money("total_debt"), shares=_num(p["shares"]),
            operating_cash_flow=money("operating_cash_flow"), dps=None if p.get("dps") is None else _num(p["dps"]),
        ))
    return out


def record_import(session: Session, assets: AssetStore, *, source_id: str, raw: bytes, parser: str, now: datetime,
                  label: str) -> str:
    sha = assets.put(raw)
    sid = "snap_" + hashlib.sha256(f"{source_id}|{sha}".encode()).hexdigest()[:24]  # same file = same snapshot
    if session.get(DataSnapshot, sid) is None:
        session.add(DataSnapshot(id=sid, source_id=source_id, url=f"import:{label}", fetched_at=now, as_of=now,
                                 raw_sha256=sha, parser=parser, warnings=[]))
        session.flush()
    return sid


def load_snapshot_bytes(session: Session, assets: AssetStore, snapshot_id: str) -> bytes:
    snap = session.get(DataSnapshot, snapshot_id)
    if snap is None:
        raise KeyError(snapshot_id)
    return assets.get(snap.raw_sha256)
