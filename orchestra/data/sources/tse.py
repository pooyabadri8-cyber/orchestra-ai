"""TSE (tsetmc.com): daily history of the market indices, fetched automatically for the technical analyst.

Two public addresses give the same history; the first has open/high/low/close/volume, the second only levels:
    https://old.tsetmc.com/tsev2/chart/data/IndexFinancial.aspx?i=<code>&t=ph
        "date,high,low,open,close,volume[,...];date,..."  (date as 20251111 or 2025-11-11)
    https://cdn.tsetmc.com/api/Index/GetIndexB2History/<code>
        {"indexB2": [{"dEven": 20251111, "xNivInuClMresIbs": close, "xNivInuPhMresIbs": high, "xNivInuPbMresIbs": low}, ...]}
The first that answers with a usable history wins. The raw answer is stored as the snapshot (parser tag tells which),
so every computed figure can be traced to the bytes it came from, exactly like an imported CSV.

Only indices for now: Pouyan's policy allows index technical analysis, not single stocks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from ...analysis.technical import Bar

INDEX_CODES = {
    "شاخص کل": "32097828799138957",
    "شاخص کل هم وزن": "67130298613737946",
}
ALIASES = {
    "شاخص کل بورس": "شاخص کل", "شاخص بورس": "شاخص کل", "تدپیکس": "شاخص کل", "tedpix": "شاخص کل",
    "شاخص هم وزن": "شاخص کل هم وزن", "شاخص کل هموزن": "شاخص کل هم وزن", "شاخص هموزن": "شاخص کل هم وزن",
}
PARSER_FINANCIAL = "tse_index_financial@1"
PARSER_B2 = "tse_index_b2@1"
MIN_BARS = 30
DEFAULT_OLD = "https://old.tsetmc.com"
DEFAULT_CDN = "https://cdn.tsetmc.com"


class TseError(Exception):
    pass


def canonical_index(subject: str) -> str | None:
    """'شاخص كل', 'شاخص‌کل بورس', 'TEDPIX' ... -> 'شاخص کل'. None if this is not an index we know."""
    s = re.sub(r"\s+", " ", str(subject).replace("ي", "ی").replace("ك", "ک").replace("‌", " ")).strip().lower()
    if s in INDEX_CODES:
        return s
    return ALIASES.get(s)


@dataclass(frozen=True)
class Endpoint:
    url: str
    parser: str


def endpoints(code: str, old_base: str = DEFAULT_OLD, cdn_base: str = DEFAULT_CDN) -> list[Endpoint]:
    return [Endpoint(f"{old_base.rstrip('/')}/tsev2/chart/data/IndexFinancial.aspx?i={code}&t=ph", PARSER_FINANCIAL),
            Endpoint(f"{cdn_base.rstrip('/')}/api/Index/GetIndexB2History/{code}", PARSER_B2)]


def _day(v) -> date:
    t = re.sub(r"\D", "", str(v))
    if len(t) != 8:
        raise ValueError(f"تاریخ نامعتبر: {v}")
    return date(int(t[:4]), int(t[4:6]), int(t[6:]))


def _clean(rows: list[Bar]) -> list[Bar]:
    by_day: dict[date, Bar] = {}
    for b in rows:
        by_day[b.day] = b  # the same day twice: keep the later line
    bars = [by_day[d] for d in sorted(by_day)]
    if len(bars) < MIN_BARS:
        raise TseError(f"سابقهٔ شاخص خیلی کوتاه است ({len(bars)} روز)")
    return bars


def _bar(day: date, o: float, h: float, low: float, c: float, v: float) -> Bar:
    # some days have high/low missing or not containing open/close: widen them, never invent a close
    h2 = max(x for x in (h, o, c) if x > 0)
    lows = [x for x in (low, o, c) if x > 0]
    return Bar(day=day, open=o if o > 0 else c, high=h2, low=min(lows), close=c, volume=max(v, 0.0))


def parse_index_financial(raw: bytes) -> list[Bar]:
    text = raw.decode("utf-8", errors="replace").strip().lstrip("﻿")
    if not text or text.startswith(("<", "{")):
        raise TseError("پاسخ TSE شکل سابقهٔ شاخص را ندارد")
    rows = []
    for rec in text.split(";"):
        f = [x.strip() for x in rec.split(",")]
        if len(f) < 5 or not f[0]:
            continue
        try:
            nums = [float(x) if x else 0.0 for x in f[1:6]] + [0.0] * (6 - len(f))
            high, low, o, c, v = nums[:5]
            if c <= 0:
                continue
            rows.append(_bar(_day(f[0]), o, high, low, c, v))
        except ValueError:
            continue
    return _clean(rows)


def parse_index_b2(raw: bytes) -> list[Bar]:
    try:
        items = json.loads(raw.decode("utf-8", errors="replace"))["indexB2"]
    except (ValueError, KeyError, TypeError) as e:
        raise TseError("پاسخ TSE شکل سابقهٔ شاخص را ندارد") from e
    rows = []
    prev = 0.0
    for it in sorted(items or [], key=lambda x: int(x.get("dEven") or 0)):
        try:
            c = float(it.get("xNivInuClMresIbs") or 0)
            if c <= 0:
                continue
            h = float(it.get("xNivInuPhMresIbs") or 0)
            low = float(it.get("xNivInuPbMresIbs") or 0)
            rows.append(_bar(_day(it["dEven"]), prev or c, h, low, c, 0.0))
            prev = c
        except (ValueError, KeyError, TypeError):
            continue
    return _clean(rows)


PARSERS = {PARSER_FINANCIAL: parse_index_financial, PARSER_B2: parse_index_b2}
