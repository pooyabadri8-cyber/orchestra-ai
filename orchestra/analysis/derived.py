"""Turning computed analysis into stored, traceable data points.

A derived point records its formula and the ids of what it was computed from (a price-history snapshot, a set of
statements, other points). Its id is a hash of those, so recomputing the same thing gives the same id.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ..content.numbers import BoundPoint
from ..db import DataPointRow
from .fundamental import FundamentalReport
from .technical import FORMULA_VERSION as TECH_V
from .technical import TechnicalSnapshot


@dataclass(frozen=True)
class Spec:
    label: str
    unit: str  # point | IRR | IRT | percent | ratio | days
    decimals: int
    formats: tuple[str, ...]


def derived_id(metric: str, subject: str | None, formula: str, inputs: list[str]) -> str:
    key = f"{metric}|{subject or ''}|{formula}|{'|'.join(sorted(inputs))}"
    return "dv_" + hashlib.sha256(key.encode()).hexdigest()[:24]


def store_derived(session: Session, *, metric: str, subject: str | None, value: float, spec: Spec, formula: str,
                  inputs: list[str], as_of: datetime) -> BoundPoint:
    pid = derived_id(metric, subject, formula, inputs)
    if session.get(DataPointRow, pid) is None:
        session.add(DataPointRow(id=pid, snapshot_id=None, source_id="derived", metric=metric, subject=subject, rank=None,
                                 value=value, unit=spec.unit, decimals=spec.decimals, scale="", source_text=formula,
                                 as_of=as_of, derivation={"formula": formula, "inputs": sorted(inputs)}))
        session.flush()
    label = spec.label + (f" — {subject}" if subject else "")
    return BoundPoint(key="?", point_id=pid, metric=metric, label=label, value=value, unit=spec.unit,
                      decimals=spec.decimals, subject=subject, formats=spec.formats)


# ------------------------------------------------------------------ technical

def _price_spec(label: str, price_unit: str) -> Spec:
    if price_unit == "point":
        return Spec(label, "point", 0, ("int",))
    return Spec(label, "IRR", 0, ("rial",))


def technical_specs(price_unit: str) -> dict[str, Spec]:
    p = lambda label: _price_spec(label, price_unit)  # noqa: E731
    return {
        "price.close": p("قیمت پایانی" if price_unit != "point" else "مقدار پایانی"),
        "price.change_pct": Spec("درصد تغییر روزانه", "percent", 2, ("pct", "abs_pct", "signed_pct")),
        "sma.20": p("میانگین متحرک ساده بیست‌روزه"),
        "sma.50": p("میانگین متحرک ساده پنجاه‌روزه"),
        "sma.200": p("میانگین متحرک ساده دویست‌روزه"),
        "rsi.14": Spec("شاخص قدرت نسبی چهارده‌روزه (RSI)", "ratio", 1, ("num",)),
        "macd.hist": Spec("هیستوگرام MACD", "ratio", 2, ("num",)),
        "atr.14_pct": Spec("میانگین دامنهٔ واقعی چهارده‌روزه به درصد قیمت", "percent", 1, ("abs_pct",)),
        "stoch.k": Spec("استوکاستیک %K", "ratio", 1, ("num",)),
        "volume.ratio_20": Spec("نسبت حجم امروز به میانگین بیست‌روزه", "ratio", 1, ("num",)),
        "range.high_250": p("سقف قیمت در دوره (حداکثر ۲۵۰ روز)"),
        "range.low_250": p("کف قیمت در دوره (حداکثر ۲۵۰ روز)"),
        "range.from_high_pct": Spec("فاصله از سقف دوره", "percent", 1, ("abs_pct", "pct")),
        "pivot.r1": p("مقاومت اول (پیوت کلاسیک)"),
        "pivot.s1": p("حمایت اول (پیوت کلاسیک)"),
        "level.support": p("نزدیک‌ترین حمایت (کف‌های اخیر)"),
        "level.resistance": p("نزدیک‌ترین مقاومت (سقف‌های اخیر)"),
    }


def technical_points(session: Session, snap: TechnicalSnapshot, *, subject: str, source_snapshot_id: str,
                     price_unit: str, as_of: datetime, extra_inputs: tuple[str, ...] = ()) -> list[BoundPoint]:
    specs = technical_specs(price_unit)
    out = []
    for metric, spec in specs.items():
        if metric in snap.values:
            out.append(store_derived(session, metric=f"technical.{metric}", subject=subject, value=snap.values[metric],
                                     spec=spec, formula=f"{TECH_V}:{metric}", inputs=[source_snapshot_id, *extra_inputs], as_of=as_of))
    return out


# ------------------------------------------------------------------ fundamental

FUNDAMENTAL_SPECS = {
    "market_cap": Spec("ارزش بازار", "IRR", 0, ("money",)),
    "eps_ttm": Spec("سود هر سهم دوازده‌ماهه (EPS)", "IRR", 0, ("rial",)),
    "pe_ttm": Spec("نسبت قیمت به سود دوازده‌ماهه (P/E)", "ratio", 1, ("num",)),
    "pb": Spec("نسبت قیمت به ارزش دفتری (P/B)", "ratio", 2, ("num",)),
    "roe_ttm": Spec("بازده حقوق صاحبان سهام (ROE)", "percent", 1, ("pct",)),
    "roa_ttm": Spec("بازده دارایی‌ها (ROA)", "percent", 1, ("pct",)),
    "gross_margin_ttm": Spec("حاشیهٔ سود ناخالص", "percent", 1, ("pct",)),
    "operating_margin_ttm": Spec("حاشیهٔ سود عملیاتی", "percent", 1, ("pct",)),
    "net_margin_ttm": Spec("حاشیهٔ سود خالص", "percent", 1, ("pct",)),
    "debt_to_equity": Spec("نسبت بدهی بهره‌دار به حقوق صاحبان سهام", "ratio", 2, ("num",)),
    "revenue_growth_yoy": Spec("رشد درآمد نسبت به دورهٔ مشابه سال قبل", "percent", 1, ("pct", "abs_pct", "signed_pct")),
    "earnings_growth_yoy": Spec("رشد سود خالص نسبت به دورهٔ مشابه سال قبل", "percent", 1, ("pct", "abs_pct", "signed_pct")),
    "dividend_yield": Spec("بازده نقدی (سود نقدی به قیمت)", "percent", 1, ("pct",)),
    "payout_ratio": Spec("نسبت تقسیم سود", "percent", 0, ("pct",)),
}


def fundamental_points(session: Session, rep: FundamentalReport, *, inputs: list[str], as_of: datetime) -> list[BoundPoint]:
    from .fundamental import FORMULA_VERSION as FUND_V
    out = []
    for metric, spec in FUNDAMENTAL_SPECS.items():
        r = rep.ratios.get(metric)
        if r is not None and r.defined:
            out.append(store_derived(session, metric=f"fundamental.{metric}", subject=rep.symbol, value=r.value, spec=spec,
                                     formula=f"{FUND_V}:{r.formula}", inputs=inputs, as_of=as_of))
    return out


# ------------------------------------------------------------------ market overview (from a market snapshot)

def flow_intensity(session: Session, trade_value: BoundPoint, net_flow: BoundPoint, as_of: datetime) -> BoundPoint | None:
    """Real-money net flow as a share of retail trade value: how strongly money moved, independent of market size."""
    if trade_value.value <= 0:
        return None
    return store_derived(
        session, metric="market.flow_intensity_pct", subject=None, value=net_flow.value / trade_value.value * 100,
        spec=Spec("شدت ورود یا خروج پول حقیقی (نسبت به ارزش معاملات خرد)", "percent", 1, ("abs_pct",)),
        formula="market@1:retail.real_money_net / retail.trade_value × 100", inputs=[trade_value.point_id, net_flow.point_id], as_of=as_of)
