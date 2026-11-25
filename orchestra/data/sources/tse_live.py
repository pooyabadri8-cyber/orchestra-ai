"""TSE (cdn.tsetmc.com) live market figures for the daily report.

Two public answers, checked against real captures (2026-11-17):
    /api/Index/GetIndexB1LastAll/All/1   every Bourse index today: {"indexB1": [{"lVal30": "27-فلزات اساسي",
        "xDrNivJIdx004": level, "xVarIdxJRfV": change in percent, "indexChange": change in points, ...}, ...]}
        -> the industry (sector) indices and the equal-weighted total index.
    /api/MarketData/GetMarketOverview/2  the Farabourse: {"marketOverview": {"indexLastValue": ..., "indexChange": ...,
        "marketActivityDEven": 20261117, ...}}
        -> the Farabourse index (آیفکس) and the trading day the figures belong to.
The site writes Arabic ي and ك and puts the industry code before or after the name; names are cleaned here.
"""
from __future__ import annotations

import json
import re

from .base import ParsedPoint, ParseError, ParseResult

DEFAULT_CDN = "https://cdn.tsetmc.com"
MIN_SECTORS = 10
EQUAL_WEIGHT = "شاخص کل (هم وزن)"
_SECTOR = re.compile(r"^\s*(\d{2})\s*-?\s*(.+?)\s*$|^\s*(.+?)\s*(\d{2})\s*$")
NAME_FIXES = {
    "چند رشته ای ص": "چندرشته‌ای صنعتی",
    "استخراج نفت جزکشف": "استخراج نفت",
    "بیمه و بازنشسته": "بیمه و بازنشستگی",
    "سایرمالی": "سایر مالی",
    "دستگاههای برقی": "دستگاه‌های برقی",
    "سرمایه گذاریها": "سرمایه‌گذاری‌ها",
    "انبوه سازی": "انبوه‌سازی",
    "خرده فروشی به جز وسایل نقلیه": "خرده‌فروشی",
    "محصولات کامپیوتری الکترونیکی": "محصولات کامپیوتری",
    "کانه فلزی": "کانه‌های فلزی",
    "رادیویی": "رادیویی و تلویزیونی",
}


def fa_text(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).replace("ي", "ی").replace("ك", "ک")).strip()


def sector_name(raw: str) -> str | None:
    """'27-فلزات اساسي' -> 'فلزات اساسی'; 'بيمه و بازنشسته66' -> 'بیمه و بازنشستگی'; an index that is not an industry -> None."""
    s = fa_text(raw)
    if s.startswith("شاخص"):
        return None
    m = _SECTOR.match(s)
    if not m:
        return None
    name = (m.group(2) or m.group(3) or "").strip(" -")
    if not name or re.search(r"\d", name):
        return None
    return NAME_FIXES.get(name, name)


def _load(raw: bytes, key: str):
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))[key]
    except (ValueError, KeyError, TypeError) as e:
        raise ParseError("پاسخ TSE شکل همیشگی را ندارد؛ احتمالاً ساختار سایت عوض شده است.") from e


def _num(v, what: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError) as e:
        raise ParseError(f"TSE: {what} عدد نیست") from e


class TseIndicesSource:
    """Industry indices and the equal-weighted total index of the Bourse, today."""
    source_id = "tse_indices"
    parser_version = "tse_index_b1@1"

    def __init__(self, cdn_base: str = DEFAULT_CDN):
        self.url = f"{cdn_base.rstrip('/')}/api/Index/GetIndexB1LastAll/All/1"

    def parse(self, raw: bytes) -> ParseResult:
        rows = _load(raw, "indexB1")
        if not isinstance(rows, list):
            raise ParseError("TSE: فهرست شاخص‌ها خالی است")
        points: list[ParsedPoint] = []
        sectors: list[tuple[str, float, str]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = fa_text(row.get("lVal30", ""))
            pct = _num(row.get("xVarIdxJRfV"), f"درصد تغییر «{title}»")
            if title == EQUAL_WEIGHT:
                level = _num(row.get("xDrNivJIdx004"), "شاخص هم‌وزن")
                points.append(ParsedPoint("index.equal.value", level, "point", 0, str(row.get("xDrNivJIdx004"))))
                points.append(ParsedPoint("index.equal.change_pct", pct, "percent", 2, f"{row.get('xVarIdxJRfV')}%"))
                continue
            name = sector_name(title)
            if name is None or name in seen or pct == 0:  # an index that did not move today had no trading
                continue
            seen.add(name)
            sectors.append((name, pct, str(row.get("xVarIdxJRfV"))))
        if len(sectors) < MIN_SECTORS:
            raise ParseError(f"TSE: فقط {len(sectors)} شاخص صنعت خوانده شد")
        for rank, (name, pct, text) in enumerate(sorted(sectors, key=lambda x: -x[1]), 1):
            points.append(ParsedPoint("sector.change_pct", pct, "percent", 2, f"{text}%", subject=name, rank=rank))
        warnings = [] if any(p.metric == "index.equal.value" for p in points) else ["TSE: شاخص هم‌وزن در پاسخ نبود"]
        return ParseResult(points=points, warnings=warnings)


class TseBourseSource:
    """The Bourse's total index today and the trading day it belongs to: the official figures that decide whether
    there is a report today, and that the market page is checked against (see agents/freshness.py)."""
    source_id = "tse_bourse"
    parser_version = "tse_overview_bourse@1"

    def __init__(self, cdn_base: str = DEFAULT_CDN):
        self.url = f"{cdn_base.rstrip('/')}/api/MarketData/GetMarketOverview/1"

    def parse(self, raw: bytes) -> ParseResult:
        o = _load(raw, "marketOverview")
        if not isinstance(o, dict):
            raise ParseError("TSE: خلاصهٔ بورس خالی است")
        last, change = _num(o.get("indexLastValue"), "شاخص کل"), _num(o.get("indexChange"), "تغییر شاخص کل")
        day = int(_num(o.get("marketActivityDEven"), "تاریخ معاملات"))
        if last <= 0 or not 20000101 < day < 21000101:
            raise ParseError("TSE: شاخص کل یا تاریخ معاملات معتبر نیست")
        prev = last - change
        title = fa_text(o.get("marketStateTitle") or "")
        points = [
            ParsedPoint("index.total.value", last, "point", 0, str(o.get("indexLastValue"))),
            ParsedPoint("index.total.change", change, "point", 0, str(o.get("indexChange"))),
            ParsedPoint("index.total.change_pct", change / prev * 100 if prev > 0 else 0.0, "percent", 2,
                        f"{o.get('indexChange')} / {prev}"),
            ParsedPoint("market.trading_day", float(day), "date", 0, str(day)),
        ]
        warnings = []
        if title:  # "بسته" once the session is over; anything else means the figures may still move
            points.append(ParsedPoint("market.closed", 1.0 if "بسته" in title else 0.0, "flag", 0, title))
        else:
            warnings.append("TSE: وضعیت بازار (باز/بسته) در پاسخ نبود")
        return ParseResult(points=points, warnings=warnings)


class TseFarabourseSource:
    """The Farabourse index today, and the trading day the TSE figures belong to."""
    source_id = "tse_farabourse"
    parser_version = "tse_overview@1"

    def __init__(self, cdn_base: str = DEFAULT_CDN):
        self.url = f"{cdn_base.rstrip('/')}/api/MarketData/GetMarketOverview/2"

    def parse(self, raw: bytes) -> ParseResult:
        o = _load(raw, "marketOverview")
        if not isinstance(o, dict):
            raise ParseError("TSE: خلاصهٔ فرابورس خالی است")
        last, change = _num(o.get("indexLastValue"), "شاخص فرابورس"), _num(o.get("indexChange"), "تغییر شاخص فرابورس")
        day = int(_num(o.get("marketActivityDEven"), "تاریخ معاملات"))
        if last <= 0 or not 20000101 < day < 21000101:
            raise ParseError("TSE: شاخص یا تاریخ فرابورس معتبر نیست")
        prev = last - change
        return ParseResult(points=[
            ParsedPoint("index.fara.value", last, "point", 0, str(o.get("indexLastValue"))),
            ParsedPoint("index.fara.change", change, "point", 0, str(o.get("indexChange"))),
            ParsedPoint("index.fara.change_pct", change / prev * 100 if prev > 0 else 0.0, "percent", 2,
                        f"{o.get('indexChange')} / {prev}"),
            ParsedPoint("market.trading_day", float(day), "date", 0, str(day)),
        ])
