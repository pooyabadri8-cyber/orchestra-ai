"""Imports (CSV prices, YAML statements), the technical and fundamental analysts, derived-point traceability, and the
composer that builds the writer's data table."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from orchestra.agents.specialists import Part, compose, fundamental_part, technical_part
from orchestra.content.calendar import to_jalali
from orchestra.content.numbers import BoundPoint, format_point
from orchestra.data.imports import ImportErrorWithLine, parse_price_csv, parse_statements_yaml, record_import
from orchestra.db import DataPointRow

NOW = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)
FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def synthetic_csv(n=230, persian=True) -> bytes:
    """Clearly synthetic test series (a steady rise with a wobble); Jalali dates, optionally Persian digits."""
    lines = ["تاریخ,اولین,بیشترین,کمترین,پایانی,حجم"]
    price, day = 10_000.0, date(2025, 3, 1)
    for i in range(n):
        o = price
        c = price * (1.003 if i % 5 else 0.99)
        h, lo = max(o, c) * 1.004, min(o, c) * 0.996
        y, m, d = to_jalali(day + timedelta(days=i))
        row = f'{y}/{m:02d}/{d:02d},"{o:,.0f}","{h:,.0f}","{lo:,.0f}","{c:,.0f}",{1_000_000 + i * 1000}'
        lines.append(row.translate(FA) if persian else row)
        price = c
    return ("\n".join(lines)).encode("utf-8")


STATEMENTS = """
symbol: نمونه
unit: million_rial
periods:
  - {fiscal_year: 1403, months: 6, period_end: 1403/06/31, audited: true, revenue: 400000, net_income: 40000, shares: 1000000000}
  - {fiscal_year: 1403, months: 12, period_end: 1403/12/30, audited: true, revenue: 1000000, net_income: 120000, shares: 1000000000, dps: 60}
  - {fiscal_year: 1404, months: 6, period_end: 1404/06/31, audited: false, revenue: 600000, net_income: 70000,
     total_assets: 2000000, total_equity: 800000, total_debt: 400000, shares: 1000000000}
""".encode("utf-8")


def test_price_csv_with_jalali_dates_and_persian_digits():
    bars = parse_price_csv(synthetic_csv(30))
    assert len(bars) == 30 and bars[0].day == date(2025, 3, 1) and bars[0].close > 0
    with pytest.raises(ImportErrorWithLine):
        parse_price_csv("date,open,high,low,close\n2025-01-01,1,1,1,1\n".encode())  # no volume column
    with pytest.raises(ImportErrorWithLine) as e:
        parse_price_csv("date,open,high,low,close,volume\n2025-01-01,1,1,1,1,5\n2025-01-02,x,1,1,1,5\n".encode())
    assert "سطر 3" in str(e.value)


def test_statements_yaml_units():
    st = parse_statements_yaml(STATEMENTS)
    assert st[2].revenue == pytest.approx(600_000 * 1e6) and st[1].dps == 60 and st[2].period_end.year == 2025


def test_technical_part_is_traceable(sessions, assets):
    with sessions.begin() as s:
        sid = record_import(s, assets, source_id="import:prices", raw=synthetic_csv(), parser="price_csv@1", now=NOW, label="نمونه.csv")
        part = technical_part(s, assets, history_snapshot_id=sid, subject="نمونه", price_unit="IRR", now=NOW)
    metrics = {p.metric for p in part.points}
    assert {"technical.rsi.14", "technical.sma.200", "technical.macd.hist", "technical.pivot.r1"} <= metrics
    with sessions() as s:
        row = s.get(DataPointRow, next(p.point_id for p in part.points if p.metric == "technical.rsi.14"))
        assert row.source_id == "derived" and row.derivation["inputs"] == [sid] and row.derivation["formula"].startswith("technical@1:")
    assert part.facts[0].startswith("روند نمونه") and all(not any(c.isdigit() for c in f) for f in part.facts)
    rsi = next(p for p in part.points if p.metric == "technical.rsi.14")
    assert 0 <= rsi.value <= 100 and all(ch in "۰۱۲۳۴۵۶۷۸۹٫" for ch in format_point(rsi, "num"))


def test_fundamental_part(sessions, assets):
    with sessions.begin() as s:
        sid = record_import(s, assets, source_id="import:statements", raw=STATEMENTS, parser="statements_yaml@1", now=NOW, label="نمونه.yaml")
        part = fundamental_part(s, assets, statements_snapshot_id=sid, price=1500.0, price_day=date(2026, 11, 10),
                                price_point_id="dp_price", now=NOW)
    by = {p.metric: p for p in part.points}
    assert format_point(by["fundamental.pe_ttm"], "num") == "۱۰"
    assert format_point(by["fundamental.eps_ttm"], "rial") == "۱۵۰ ریال"
    assert format_point(by["fundamental.market_cap"], "money") == "۱۵۰ میلیارد تومان"  # 1,500 billion rials
    assert format_point(by["fundamental.roe_ttm"], "pct") == "۱۸٫۸٪"
    assert "fundamental.gross_margin_ttm" not in by  # undefined ratios never become points
    assert any("حسابرسی‌نشده" in f for f in part.facts)


def test_compose_gives_stable_unique_keys():
    def bp(metric):
        return BoundPoint(key="?", point_id=metric, metric=metric, label=metric, value=1, unit="ratio", decimals=0, formats=("num",))
    date_point = BoundPoint(key="D1", point_id="date:x", metric="run.date", label="تاریخ", value=0, unit="date", decimals=0,
                            subject="چهارشنبه", formats=("name",))
    rep = compose("snap", [Part("بازار", [bp("a"), bp("b"), date_point], ["واقعیت یک"]), Part("تکنیکال", [bp("c")], ["واقعیت دو"])])
    assert [p.key for p in rep.points] == ["P1", "P2", "D1", "P3"]
    assert rep.points[3].label.startswith("[تکنیکال]") and rep.facts == ("[بازار] واقعیت یک", "[تکنیکال] واقعیت دو")
