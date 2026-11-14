"""Chart specifications: built in code from stored data, so a chart can only show numbers the system holds.

The analysts produce ChartSpecs next to their data points; the writer may only pick a chart by id for a slide; the
designer draws it. Every spec names the data it came from (point ids or a price-history snapshot).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Bar(BaseModel):
    model_config = ConfigDict(frozen=True)
    label: str
    value: float
    point_id: str
    display: str  # the value as shown, formatted by code


class ChartSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    kind: Literal["bar_signed", "line"]
    title: str  # Persian, written by code, no numbers
    unit: str  # IRT | IRR | point | percent
    bars: tuple[Bar, ...] = ()
    dates: tuple[str, ...] = ()  # ISO days for a line chart
    series: dict[str, tuple[float | None, ...]] = Field(default_factory=dict)  # name -> values aligned with dates
    sources: tuple[str, ...] = ()

    def describe_fa(self) -> str:
        if self.kind == "bar_signed":
            return f"{self.id}: نمودار میله‌ای «{self.title}» ({len(self.bars)} ردیف)"
        return f"{self.id}: نمودار خطی «{self.title}» ({len(self.dates)} روز؛ {'، '.join(self.series)})"


def groups_flow_chart(points) -> ChartSpec | None:
    """Net real-money flow per industry group: inflows positive, outflows negative."""
    from ..content.numbers import format_point
    bars = []
    for p in points:
        if p.metric == "real_money_net.group.top_in":
            bars.append(Bar(label=p.subject or "", value=abs(p.value), point_id=p.point_id, display=format_point(p, "abs_money")))
        elif p.metric == "real_money_net.group.top_out":
            bars.append(Bar(label=p.subject or "", value=-abs(p.value), point_id=p.point_id, display=format_point(p, "abs_money")))
    if len(bars) < 2:
        return None
    unit = next(p.unit for p in points if p.metric.startswith("real_money_net.group"))
    return ChartSpec(id="groups_flow", kind="bar_signed", title="ورود و خروج پول حقیقی گروه‌ها", unit=unit,
                     bars=tuple(sorted(bars, key=lambda b: -b.value)), sources=tuple(b.point_id for b in bars))


def price_trend_chart(bars, *, subject: str, snapshot_id: str, price_unit: str, days: int = 120) -> ChartSpec | None:
    from ..analysis.technical import sma
    if len(bars) < 30:
        return None
    closes = [b.close for b in bars]
    series = {"قیمت پایانی" if price_unit != "point" else "مقدار پایانی": closes}
    for n, name in ((20, "میانگین بیست‌روزه"), (50, "میانگین پنجاه‌روزه")):
        if len(closes) >= n + 10:
            series[name] = sma(closes, n)
    cut = max(0, len(bars) - days)
    return ChartSpec(id="price_trend", kind="line", title=f"روند {subject} و میانگین‌های متحرک", unit=price_unit if price_unit == "point" else "IRR",
                     dates=tuple(b.day.isoformat() for b in bars[cut:]),
                     series={k: tuple(None if v is None else float(v) for v in vals[cut:]) for k, vals in series.items()},
                     sources=(snapshot_id,))

