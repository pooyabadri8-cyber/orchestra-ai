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


def market_part(session: Session, *, snapshot_id: str | None, brand: Brand, run_date: date, now: datetime,
                official_snapshot_id: str | None = None) -> tuple[Part, tuple[str, ...]]:
    """The market analyst: index, retail value, real-money flow and its intensity, leading groups. Returns the part
    and the subjects excluded by the org's forbidden topics. `official_snapshot_id`: TSE's overview, which confirms
    the trading day (agents/freshness.py); the part's notes say what was left out and why."""
    rep = analyze_market_close(session, snapshot_id, brand, run_date, official_snapshot_id=official_snapshot_id)
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
    return Part(title="بازار", points=points, facts=facts, charts=charts, notes=list(rep.notes)), rep.excluded


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


# ------------------------------------------------------------------ currency and gold

FX_LABELS = {
    "fx.usd": "دلار آمریکا (بازار آزاد)",
    "fx.eur": "یورو (بازار آزاد)",
    "gold.gram18": "طلای ۱۸ عیار (هر گرم)",
    "coin.emami": "سکهٔ امامی",
    "gold.ounce": "انس جهانی طلا (دلار)",
}


def fx_part(session: Session, *, snapshot_id: str) -> Part:
    """The currency and gold analyst: prices and their change today, from one tgju snapshot. The direction of every
    move is stated in facts computed here, so the writer never has to infer it."""
    from sqlalchemy import select

    from ..db import DataPointRow
    rows = {r.metric: r for r in session.execute(select(DataPointRow).where(DataPointRow.snapshot_id == snapshot_id)).scalars()}
    points: list[BoundPoint] = []
    facts: list[str] = []
    for base, label in FX_LABELS.items():
        price, pct = rows.get(f"{base}.price"), rows.get(f"{base}.change_pct")
        if price is None:
            continue
        price_formats = ("price",) if price.unit in ("IRT", "IRR") else ("num",)
        points.append(BoundPoint(key="P0", point_id=price.id, metric=price.metric, label=f"قیمت {label}", value=price.value,
                                 unit=price.unit, decimals=price.decimals, formats=price_formats))
        if pct is not None:
            points.append(BoundPoint(key="P0", point_id=pct.id, metric=pct.metric, label=f"درصد تغییر امروز {label}",
                                     value=pct.value, unit="percent", decimals=2, formats=("abs_pct", "signed_pct")))
            move = "بالا رفت" if pct.value > 0 else "پایین آمد" if pct.value < 0 else "تغییری نکرد"
            facts.append(f"{label} امروز {move}.")
    if points:
        facts.append("قیمت‌ها لحظه‌ای‌اند و از tgju خوانده شده‌اند؛ آن‌ها را «قیمت بازار آزاد در زمان تهیهٔ گزارش» بنام، "
                     "نه قیمت پایانی، و دربارهٔ آینده‌شان پیش‌بینی نکن.")
    return Part(title="بازار ارز و طلا", points=points, facts=facts)


TOP_SECTORS = 3


def indices_part(session: Session, *, indices_snapshot_id: str | None, fara_snapshot_id: str | None, brand: Brand,
                 run_date: date) -> Part:
    """The other market indices (equal-weighted, Farabourse) and the industries that rose and fell most today, from TSE.
    Figures from an earlier trading day are not used: they would be presented as today's."""
    from sqlalchemy import select

    from ..db import DataPointRow
    from ..visual.charts import Bar

    def rows(sid):
        return list(session.execute(select(DataPointRow).where(DataPointRow.snapshot_id == sid)).scalars()) if sid else []

    fara = {r.metric: r for r in rows(fara_snapshot_id)}
    part = Part(title="شاخص‌ها و صنایع")
    day = fara.get("market.trading_day")
    if day is not None and int(day.value) != int(run_date.strftime("%Y%m%d")):
        part.notes.append(f"آخرین روز معاملات در TSE {int(day.value)} است، نه امروز؛ شاخص‌های فرعی و صنایع اضافه نشدند.")
        return part
    indices = rows(indices_snapshot_id)
    by = {r.metric: r for r in indices if r.subject is None}

    def add(metric: str, label: str, formats: tuple[str, ...]):
        r = by.get(metric) or fara.get(metric)
        if r is None:
            return None
        p = BoundPoint(key="P0", point_id=r.id, metric=metric, label=label, value=r.value, unit=r.unit,
                       decimals=r.decimals, formats=formats)
        part.points.append(p)
        return p

    pct_formats = ("signed_pct", "abs_pct", "pct")
    for name, prefix in (("شاخص کل هم‌وزن", "index.equal"), ("شاخص کل فرابورس", "index.fara")):
        add(f"{prefix}.value", name, ("int",))
        pct = add(f"{prefix}.change_pct", f"درصد تغییر امروز {name}", pct_formats)
        if pct is not None:
            move = "رشد کرد" if pct.value > 0 else "افت کرد" if pct.value < 0 else "بدون تغییر ماند"
            part.facts.append(f"{name} امروز {move}.")

    sectors = sorted((r for r in indices if r.metric == "sector.change_pct" and not brand.excluded(r.subject or "")),
                     key=lambda r: -r.value)
    ups = [r for r in sectors if r.value > 0][:TOP_SECTORS]
    downs = [r for r in reversed(sectors) if r.value < 0][:TOP_SECTORS]
    bars = []
    for metric, chosen in (("sector.top_up", ups), ("sector.top_down", downs)):
        for r in chosen:
            p = BoundPoint(key="P0", point_id=r.id, metric=metric, label=f"درصد تغییر شاخص صنعت: {r.subject}", value=r.value,
                           unit="percent", decimals=r.decimals, subject=r.subject, formats=("signed_pct", "abs_pct", "name"))
            part.points.append(p)
            from ..content.numbers import format_point
            bars.append(Bar(label=r.subject or "", value=r.value, point_id=r.id, display=format_point(p, "signed_pct")))
    if ups:
        part.facts.append("بیشترین رشد شاخص صنایع امروز به ترتیب: " + "، ".join(r.subject or "" for r in ups) + ".")
    if downs:
        part.facts.append("بیشترین افت شاخص صنایع امروز به ترتیب: " + "، ".join(r.subject or "" for r in downs) + ".")
    if len(bars) >= 2:
        part.charts.append(ChartSpec(id="sectors_change", kind="bar_signed", title="بیشترین رشد و افت شاخص صنایع",
                                     unit="percent", bars=tuple(sorted(bars, key=lambda b: -b.value)),
                                     sources=tuple(b.point_id for b in bars)))
    return part
