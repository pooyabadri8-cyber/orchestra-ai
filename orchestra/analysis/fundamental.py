"""Fundamental analysis, computed in code.

Statements are cumulative from the start of the fiscal year (3, 6, 9, 12 months), as Codal interim reports are.
Trailing-twelve-month (TTM) flows are therefore:

    TTM = current cumulative period + last full fiscal year - the same cumulative period of the previous year

All money is in rials (normalise units when parsing; Codal tables are often in million rials). A ratio that cannot be
computed honestly (a loss for P/E, negative equity for P/B, a missing input) is returned as undefined with the
reason, never as a number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

FORMULA_VERSION = "fundamental@1"


@dataclass(frozen=True)
class Statement:
    symbol: str
    fiscal_year: int  # the fiscal year the period belongs to (year of fiscal year end)
    months: int  # cumulative months since the start of the fiscal year: 3, 6, 9 or 12
    period_end: date
    audited: bool
    # income statement (cumulative for the period), rials
    revenue: float
    gross_profit: float | None
    operating_profit: float | None
    net_income: float  # attributable to the parent's shareholders
    # balance sheet (at period end), rials
    total_assets: float | None
    total_equity: float | None
    total_debt: float | None  # interest-bearing borrowings
    shares: float  # shares outstanding at period end
    operating_cash_flow: float | None = None
    dps: float | None = None  # dividend per share declared for this fiscal year (annual statements only)

    def __post_init__(self) -> None:
        if self.months not in (3, 6, 9, 12):
            raise ValueError(f"{self.symbol}: months must be 3, 6, 9 or 12")
        if self.shares <= 0:
            raise ValueError(f"{self.symbol}: shares must be positive")


@dataclass(frozen=True)
class Ratio:
    metric: str
    value: float | None
    formula: str
    inputs: tuple[str, ...]
    reason: str = ""  # why it is undefined

    @property
    def defined(self) -> bool:
        return self.value is not None


@dataclass
class FundamentalReport:
    symbol: str
    price: float
    price_date: date
    basis: str  # e.g. "TTM to 1405-06 (6 months)" or "fiscal year 1404"
    ratios: dict[str, Ratio] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # Persian, no numbers

    def value(self, metric: str) -> float | None:
        r = self.ratios.get(metric)
        return r.value if r else None


FLOWS = ("revenue", "gross_profit", "operating_profit", "net_income", "operating_cash_flow")


def _find(statements: list[Statement], year: int, months: int) -> Statement | None:
    hits = [s for s in statements if s.fiscal_year == year and s.months == months]
    if len(hits) > 1:
        audited = [s for s in hits if s.audited]  # prefer the audited version of the same period
        return audited[0] if audited else hits[-1]
    return hits[0] if hits else None


def ttm(statements: list[Statement]) -> tuple[dict[str, float | None], Statement, str]:
    """TTM flows from the latest statement. Returns (flows, latest statement, Persian basis description)."""
    if not statements:
        raise ValueError("no statements")
    latest = max(statements, key=lambda s: (s.fiscal_year, s.months, s.audited))
    if latest.months == 12:
        return {f: getattr(latest, f) for f in FLOWS}, latest, f"سال مالی {latest.fiscal_year}"
    last_fy = _find(statements, latest.fiscal_year - 1, 12)
    same_prev = _find(statements, latest.fiscal_year - 1, latest.months)
    if last_fy is None or same_prev is None:
        raise ValueError(f"{latest.symbol}: TTM needs the full previous year and the same {latest.months}-month period of it")
    flows: dict[str, float | None] = {}
    for f in FLOWS:
        a, b, c = getattr(latest, f), getattr(last_fy, f), getattr(same_prev, f)
        flows[f] = None if None in (a, b, c) else a + b - c
    return flows, latest, f"دوازده ماه منتهی به {latest.months} ماههٔ سال مالی {latest.fiscal_year}"


def _div(metric, num, den, formula, inputs, *, need_positive_den=False, undefined_if_negative_num=False, why="") -> Ratio:
    if num is None or den is None:
        return Ratio(metric, None, formula, inputs, "دادهٔ لازم موجود نیست")
    if den == 0 or (need_positive_den and den < 0):
        return Ratio(metric, None, formula, inputs, why or "مخرج صفر یا منفی است")
    if undefined_if_negative_num and num <= 0:
        return Ratio(metric, None, formula, inputs, why or "صورت کسر منفی است")
    return Ratio(metric, num / den, formula, inputs)


def analyze(statements: list[Statement], price: float, price_date: date) -> FundamentalReport:
    if price <= 0:
        raise ValueError("price must be positive")
    flows, latest, basis = ttm(statements)
    rep = FundamentalReport(symbol=latest.symbol, price=price, price_date=price_date, basis=basis)
    r = rep.ratios
    shares = latest.shares
    ni, rev = flows["net_income"], flows["revenue"]

    r["market_cap"] = Ratio("market_cap", price * shares, "price × shares", ("price", "shares"))
    r["eps_ttm"] = _div("eps_ttm", ni, shares, "net_income_ttm / shares", ("net_income_ttm", "shares"))
    eps = r["eps_ttm"].value
    r["pe_ttm"] = (Ratio("pe_ttm", None, "price / eps_ttm", ("price", "eps_ttm"), "شرکت در دورهٔ اخیر زیان‌ده است؛ P/E معنا ندارد")
                   if eps is not None and eps <= 0 else _div("pe_ttm", price, eps, "price / eps_ttm", ("price", "eps_ttm")))
    bvps = None if latest.total_equity is None else latest.total_equity / shares
    r["bvps"] = Ratio("bvps", bvps, "total_equity / shares", ("total_equity", "shares"), "" if bvps is not None else "حقوق صاحبان سهام موجود نیست")
    r["pb"] = _div("pb", price, bvps, "price / bvps", ("price", "bvps"), need_positive_den=True,
                   why="حقوق صاحبان سهام منفی است؛ P/B معنا ندارد")
    r["roe_ttm"] = _div("roe_ttm", ni, latest.total_equity, "net_income_ttm / total_equity × 100", ("net_income_ttm", "total_equity"),
                        need_positive_den=True, why="حقوق صاحبان سهام منفی است")
    r["roa_ttm"] = _div("roa_ttm", ni, latest.total_assets, "net_income_ttm / total_assets × 100", ("net_income_ttm", "total_assets"),
                        need_positive_den=True)
    for key, num in (("gross_margin_ttm", flows["gross_profit"]), ("operating_margin_ttm", flows["operating_profit"]), ("net_margin_ttm", ni)):
        r[key] = _div(key, num, rev, f"{key.rsplit('_', 2)[0]} / revenue × 100", (key, "revenue_ttm"), need_positive_den=True)
    for key in ("roe_ttm", "roa_ttm", "gross_margin_ttm", "operating_margin_ttm", "net_margin_ttm"):
        if r[key].value is not None:
            r[key] = Ratio(key, r[key].value * 100, r[key].formula, r[key].inputs)
    r["debt_to_equity"] = _div("debt_to_equity", latest.total_debt, latest.total_equity, "total_debt / total_equity",
                               ("total_debt", "total_equity"), need_positive_den=True, why="حقوق صاحبان سهام منفی است")

    prev_same = _find(statements, latest.fiscal_year - 1, latest.months)
    for key, field_name in (("revenue_growth_yoy", "revenue"), ("earnings_growth_yoy", "net_income")):
        now_v = getattr(latest, field_name)
        before = getattr(prev_same, field_name) if prev_same else None
        if before is None:
            r[key] = Ratio(key, None, f"{field_name} / same period last year − 1", (field_name,), "دورهٔ مشابه سال قبل موجود نیست")
        elif before <= 0:
            r[key] = Ratio(key, None, f"{field_name} / same period last year − 1", (field_name,), "مبنای سال قبل صفر یا منفی است؛ درصد رشد گمراه‌کننده می‌شود")
        else:
            r[key] = Ratio(key, (now_v / before - 1) * 100, f"{field_name} / same period last year − 1", (field_name,))

    annual = _find(statements, latest.fiscal_year if latest.months == 12 else latest.fiscal_year - 1, 12)
    if annual is not None and annual.dps is not None:
        r["dividend_yield"] = Ratio("dividend_yield", annual.dps / price * 100, "dps / price × 100", ("dps", "price"))
        annual_eps = annual.net_income / annual.shares
        r["payout_ratio"] = (_div("payout_ratio", annual.dps, annual_eps, "dps / annual_eps × 100", ("dps", "annual_eps"),
                                  need_positive_den=True, why="سود سال مالی منفی بوده است"))
        if r["payout_ratio"].value is not None:
            r["payout_ratio"] = Ratio("payout_ratio", r["payout_ratio"].value * 100, r["payout_ratio"].formula, r["payout_ratio"].inputs)
    rep.notes = _notes(rep, latest)
    return rep


def _notes(rep: FundamentalReport, latest: Statement) -> list[str]:
    n = []
    if not latest.audited:
        n.append("آخرین صورت‌های مالی حسابرسی‌نشده است.")
    for metric, label in (("pe_ttm", "P/E"), ("pb", "P/B")):
        rr = rep.ratios.get(metric)
        if rr is not None and not rr.defined:
            n.append(f"{label} محاسبه نشد: {rr.reason}.")
    g = rep.value("earnings_growth_yoy")
    if g is not None:
        n.append("سود خالص نسبت به دورهٔ مشابه سال قبل افزایش یافته است." if g > 0 else "سود خالص نسبت به دورهٔ مشابه سال قبل کاهش یافته است.")
    de = rep.value("debt_to_equity")
    if de is not None and de > 1:
        n.append("بدهی‌های بهره‌دار شرکت از حقوق صاحبان سهامش بیشتر است.")
    return n
