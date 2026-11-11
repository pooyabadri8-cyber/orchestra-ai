"""Financial Analysis for the market-close post (deterministic, no LLM).

It chooses which data points the writer may use, gives each a short key and the formats allowed for it, and states
the direction of every move as plain facts computed in code, so the model never has to work out up or down itself.
Forbidden topics (investment funds for Pouyan) and single stocks are removed here, before any model sees them.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..brand import Brand
from ..content.calendar import long_date
from ..content.numbers import BoundPoint, format_point
from ..db import DataPointRow
from ..visual.charts import ChartSpec

LABELS = {
    "index.total.value": "شاخص کل بورس",
    "index.total.change": "تغییر شاخص کل (واحد)",
    "index.total.change_pct": "درصد تغییر شاخص کل",
    "retail.trade_value": "ارزش معاملات خرد",
    "retail.real_money_net": "خالص ورود (+) یا خروج (−) پول حقیقی در معاملات خرد",
    "real_money_net.group.top_in": "ورود پول حقیقی به گروه",
    "real_money_net.group.top_out": "خروج پول حقیقی از گروه",
}
FORMATS = {
    "index.total.value": ("int",),
    "index.total.change": ("signed", "int"),
    "index.total.change_pct": ("pct", "abs_pct", "signed_pct"),
    "retail.trade_value": ("money",),
    "retail.real_money_net": ("abs_money",),
    "real_money_net.group.top_in": ("abs_money", "name"),
    "real_money_net.group.top_out": ("abs_money", "name"),
}
MAX_GROUPS = 3


class AnalysisReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    snapshot_id: str
    points: tuple[BoundPoint, ...]
    facts: tuple[str, ...]
    excluded: tuple[str, ...]  # subjects removed because of a forbidden topic, for the audit trail
    charts: tuple[ChartSpec, ...] = ()

    def by_key(self) -> dict[str, BoundPoint]:
        return {p.key: p for p in self.points}


class InsufficientData(Exception):
    pass


def analyze_market_close(session: Session, snapshot_id: str, brand: Brand, run_date: date | None = None) -> AnalysisReport:
    rows = session.execute(select(DataPointRow).where(DataPointRow.snapshot_id == snapshot_id)).scalars().all()
    by_metric: dict[str, list[DataPointRow]] = {}
    for r in rows:
        by_metric.setdefault(r.metric, []).append(r)
    for needed in ("index.total.value", "index.total.change_pct", "retail.trade_value"):
        if needed not in by_metric:
            raise InsufficientData(f"دادهٔ «{LABELS[needed]}» در این نمونه نیست")
    trade = by_metric["retail.trade_value"][0]
    if trade.value <= 0:
        raise InsufficientData("ارزش معاملات صفر است؛ بازار هنوز باز نشده یا امروز تعطیل است")

    chosen: list[DataPointRow] = []
    excluded: list[str] = []
    for metric in ("index.total.value", "index.total.change", "index.total.change_pct", "retail.trade_value", "retail.real_money_net"):
        chosen += by_metric.get(metric, [])[:1]
    for metric in ("real_money_net.group.top_in", "real_money_net.group.top_out"):
        kept = 0
        for r in sorted(by_metric.get(metric, []), key=lambda x: x.rank or 0):
            if brand.excluded(r.subject or ""):
                excluded.append(r.subject or "")
                continue
            if kept < MAX_GROUPS:
                chosen.append(r)
                kept += 1

    points = []
    for i, r in enumerate(chosen, 1):
        label = LABELS[r.metric] + (f": {r.subject}" if r.subject else "")
        points.append(BoundPoint(key=f"P{i}", point_id=r.id, metric=r.metric, label=label, value=r.value, unit=r.unit,
                                 decimals=r.decimals, scale=r.scale, subject=r.subject, formats=FORMATS[r.metric]))
    if run_date is not None:  # the date is bound data too, so the writer never types it
        points.append(BoundPoint(key="D1", point_id=f"date:{run_date.isoformat()}", metric="run.date", label="تاریخ امروز",
                                 value=0, unit="date", decimals=0, subject=long_date(run_date), formats=("name",)))
    return AnalysisReport(snapshot_id=snapshot_id, points=tuple(points), facts=tuple(_facts(points)), excluded=tuple(excluded))


def _facts(points: list[BoundPoint]) -> list[str]:
    pick = {p.metric: p for p in points if p.subject is None}
    facts = []
    pct = pick.get("index.total.change_pct")
    if pct is not None:
        facts.append("شاخص کل امروز رشد کرد." if pct.value > 0 else "شاخص کل امروز افت کرد." if pct.value < 0 else "شاخص کل امروز بدون تغییر ماند.")
    flow = pick.get("retail.real_money_net")
    if flow is not None:
        facts.append("در معاملات خرد، پول حقیقی وارد بازار شد." if flow.value > 0
                     else "در معاملات خرد، پول حقیقی از بازار خارج شد." if flow.value < 0 else "ورود و خروج پول حقیقی خرد برابر بود.")
    ins = [p for p in points if p.metric == "real_money_net.group.top_in"]
    outs = [p for p in points if p.metric == "real_money_net.group.top_out"]
    if ins:
        facts.append("بیشترین ورود پول حقیقی به گروه‌ها به ترتیب: " + "، ".join(format_point(p, "name") for p in ins) + ".")
    if outs:
        facts.append("بیشترین خروج پول حقیقی از گروه‌ها به ترتیب: " + "، ".join(format_point(p, "name") for p in outs) + ".")
    return facts
