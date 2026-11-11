"""Technical and fundamental analysts (deterministic), and the composer that merges every analyst's part into the one
data table the writer sees.

Each analyst returns a Part: its bound data points and its facts (Persian, without numbers). The composer gives every
point a short key (P1, P2, ...) in a stable order, so the writer and the reviewers share the same references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..analysis.derived import fundamental_points, technical_points
from ..analysis.fundamental import analyze as analyze_fundamentals
from ..analysis.technical import snapshot as technical_snapshot
from ..assets import AssetStore
from ..content.calendar import long_date
from ..content.numbers import BoundPoint
from ..data.imports import load_snapshot_bytes, parse_price_csv, parse_statements_yaml
from .analysis import AnalysisReport


@dataclass
class Part:
    title: str
    points: list[BoundPoint] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)


def technical_part(session: Session, assets: AssetStore, *, history_snapshot_id: str, subject: str, price_unit: str,
                   now: datetime) -> Part:
    bars = parse_price_csv(load_snapshot_bytes(session, assets, history_snapshot_id))
    snap = technical_snapshot(bars)
    points = technical_points(session, snap, subject=subject, source_snapshot_id=history_snapshot_id, price_unit=price_unit, as_of=now)
    facts = [f"روند {subject} بر پایهٔ میانگین‌های متحرک: {snap.trend}."]
    facts += [s.text for s in snap.signals]
    if snap.missing:
        facts.append("برخی شاخص‌ها به‌دلیل کوتاه بودن سابقهٔ قیمت محاسبه نشدند؛ دربارهٔ آن‌ها چیزی ننویس.")
    if "pivot.r1" in snap.values and "level.resistance" in snap.values:
        facts.append("سطوح حمایت و مقاومت از قیمت‌های گذشته محاسبه شده‌اند و پیش‌بینی نیستند؛ آن‌ها را «سطح قابل توجه» بنامی، نه هدف قیمتی.")
    return Part(title=f"تحلیل تکنیکال {subject} (تا {long_date(snap.as_of)})", points=points, facts=facts)


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
    n = 0
    for part in parts:
        for p in part.points:
            if p.key.startswith("D"):
                points.append(p)
                continue
            n += 1
            points.append(p.model_copy(update={"key": f"P{n}", "label": f"[{part.title}] {p.label}"}))
        facts += [f"[{part.title}] {f}" for f in part.facts]
    for p in extra or []:
        points.append(p)
    keys = [p.key for p in points]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate keys in composed report")
    return AnalysisReport(snapshot_id=snapshot_id, points=tuple(points), facts=tuple(facts), excluded=excluded)
