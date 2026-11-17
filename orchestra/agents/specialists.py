"""Technical and fundamental analysts (deterministic), and the composer that merges every analyst's part into the one
data table the writer sees.

Each analyst returns a Part: its bound data points and its facts (Persian, without numbers). The composer gives every
point a short key (P1, P2, ...) in a stable order, so the writer and the reviewers share the same references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..analysis.derived import flow_intensity, fundamental_points, technical_points
from ..analysis.fundamental import analyze as analyze_fundamentals
from ..analysis.technical import Bar
from ..analysis.technical import snapshot as technical_snapshot
from ..assets import AssetStore
from ..content.calendar import long_date
from ..content.numbers import BoundPoint
from ..data.imports import load_bars, load_snapshot_bytes, parse_statements_yaml
from ..brand import Brand
from ..visual.charts import ChartSpec, groups_flow_chart, price_trend_chart
from .analysis import AnalysisReport, analyze_market_close

STRONG_FLOW_PCT = 10.0  # net real money above this share of retail trade value is a strong move


@dataclass
class Part:
    title: str
    points: list[BoundPoint] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    charts: list[ChartSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # for the owner, not for the writer


def market_part(session: Session, *, snapshot_id: str, brand: Brand, run_date: date, now: datetime) -> tuple[Part, tuple[str, ...]]:
    """The market analyst: index, retail value, real-money flow and its intensity, leading groups. Returns the part
    and the subjects excluded by the org's forbidden topics."""
    rep = analyze_market_close(session, snapshot_id, brand, run_date)
    points = list(rep.points)
    facts = list(rep.facts)
    by = {p.metric: p for p in points if p.subject is None}
    trade, net = by.get("retail.trade_value"), by.get("retail.real_money_net")
    if trade is not None and net is not None:
        fi = flow_intensity(session, trade, net, now)
        if fi is not None:
            points.insert(points.index(net) + 1, fi)
            strength = "قابل توجه" if abs(fi.value) >= STRONG_FLOW_PCT else "محدود"
            facts.append(f"شدت جابه‌جایی پول حقیقی نسبت به ارزش معاملات خرد {strength} است.")
    charts = [c for c in [groups_flow_chart(points)] if c]
    return Part(title="بازار", points=points, facts=facts, charts=charts), rep.excluded


MAX_DAY_GAP = 0.10  # an index never moves this much between two trading days; more means the two sources disagree


def with_today(bars: list, today: tuple[date, float] | None) -> tuple[list, bool]:
    """TSE publishes the day's history some time after the close. If today's closing value is known from the market
    page and the history ends before today, today is added as the last day: open = the previous close, high/low only
    as wide as open and close (the day's real range is not known), volume unknown (0)."""
    if not today or not bars or today[0] <= bars[-1].day or today[1] <= 0:
        return bars, False
    prev = bars[-1].close
    if abs(today[1] / prev - 1) > MAX_DAY_GAP:
        raise ValueError(f"عدد امروز ({today[1]:,.0f}) با آخرین عدد سابقه ({prev:,.0f}) نمی‌خواند")
    day = Bar(day=today[0], open=prev, high=max(prev, today[1]), low=min(prev, today[1]), close=today[1], volume=0.0)
    return [*bars, day], True


def technical_part(session: Session, assets: AssetStore, *, history_snapshot_id: str, subject: str, price_unit: str,
                   now: datetime, today: tuple[date, float, str] | None = None) -> Part:
    """`today`: (day, closing value, point id) from the market page, used when the history has not caught up."""
    bars = load_bars(session, assets, history_snapshot_id)
    try:
        bars, added = with_today(bars, today[:2] if today else None)
    except ValueError as e:
        added, mismatch = False, str(e)
    else:
        mismatch = None
    snap = technical_snapshot(bars)
    points = technical_points(session, snap, subject=subject, source_snapshot_id=history_snapshot_id, price_unit=price_unit,
                              as_of=now, extra_inputs=(today[2],) if added else ())
    facts = [f"روند {subject} بر پایهٔ میانگین‌های متحرک: {snap.trend}."]
    part_notes = [mismatch] if mismatch else []
    if added:
        facts.append(f"عدد {long_date(today[0])} همان عدد پایانی امروز در گزارش بازار است؛ سطوح پیوت از آخرین روز کامل "
                     f"سابقه ({long_date(bars[-2].day)}) حساب شده‌اند.")
    facts += [s.text for s in snap.signals]
    if snap.missing:
        facts.append("برخی شاخص‌ها به‌دلیل کوتاه بودن سابقهٔ قیمت محاسبه نشدند؛ دربارهٔ آن‌ها چیزی ننویس.")
    if "pivot.r1" in snap.values and "level.resistance" in snap.values:
        facts.append("سطوح حمایت و مقاومت از قیمت‌های گذشته محاسبه شده‌اند و پیش‌بینی نیستند؛ آن‌ها را «سطح قابل توجه» بنامی، نه هدف قیمتی.")
    charts = [c for c in [price_trend_chart(bars, subject=subject, snapshot_id=history_snapshot_id, price_unit=price_unit)] if c]
    if added:
        charts = [c.model_copy(update={"sources": (history_snapshot_id, today[2])}) for c in charts]
    return Part(title=f"تحلیل تکنیکال {subject} (تا {long_date(snap.as_of)})", points=points, facts=facts, charts=charts,
                notes=part_notes)


def fundamental_part(session: Session, assets: AssetStore, *, statements_snapshot_id: str, price: float, price_day: date,
                     price_point_id: str, now: datetime) -> Part:
    statements = parse_statements_yaml(load_snapshot_bytes(session, assets, statements_snapshot_id))
    rep = analyze_fundamentals(statements, price, price_day)
    points = fundamental_points(session, rep, inputs=[statements_snapshot_id, price_point_id], as_of=now)
    facts = list(rep.notes) + ["نسبت‌های مالی توصیفی‌اند؛ آن‌ها را توصیهٔ خرید یا فروش جلوه نده."]
    return Part(title=f"تحلیل بنیادی {rep.symbol}", points=points, facts=facts)


def compose(snapshot_id: str, parts: list[Part], excluded: tuple[str, ...] = (), extra: list[BoundPoint] | None = None) -> AnalysisReport:
    """Merge parts into one report; keys P1..Pn in part order. Points with a fixed key (the date, D1) keep it."""
    points: list[BoundPoint] = []
    facts: list[str] = []
    charts: list[ChartSpec] = []
    n = 0
    for part in parts:
        for p in part.points:
            if p.key.startswith("D"):
                points.append(p)
                continue
            n += 1
            points.append(p.model_copy(update={"key": f"P{n}", "label": f"[{part.title}] {p.label}"}))
        facts += [f"[{part.title}] {f}" for f in part.facts]
        charts += part.charts
    for p in extra or []:
        points.append(p)
    keys = [p.key for p in points]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate keys in composed report")
    if len({c.id for c in charts}) != len(charts):
        raise ValueError("duplicate chart ids in composed report")
    return AnalysisReport(snapshot_id=snapshot_id, points=tuple(points), facts=tuple(facts), excluded=excluded, charts=tuple(charts))
