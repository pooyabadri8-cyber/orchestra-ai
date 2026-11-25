"""Is today's market data really today's? (deterministic, no LLM)

The market page (bourse-trader) carries no date: on a holiday, or before the site has updated, it can still show the
last trading day's figures, and they would be presented as today's. TSE's market overview does say which trading day
its figures belong to, so it is the authority:

- TSE says the last trading day is not today      -> no report today (holiday, or the market has not opened).
- TSE confirms today                               -> the index figures come from TSE; the market page is used for
                                                      retail value, real-money flow and groups only if its index
                                                      agrees with TSE's (otherwise it is stale and left out).
- TSE cannot be reached                            -> the market page is used as before, with a warning to the
                                                      approver that the date could not be confirmed.

All of this is pure logic on plain numbers, so every branch is tested without a database or network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

INDEX_VALUE = "index.total.value"
INDEX_CHANGE = "index.total.change"
TRADING_DAY = "market.trading_day"
MARKET_CLOSED = "market.closed"

# The market page and TSE are read a few minutes apart and round differently; real agreement is far tighter than this.
VALUE_TOLERANCE = 0.002  # 0.2% of the index level
CHANGE_TOLERANCE = 0.25  # 25% of TSE's change ...
CHANGE_FLOOR = 0.0005  # ... but never less than 0.05% of the index level (for days with a tiny change)


@dataclass
class DataPlan:
    """What the market analyst may use today."""
    skip: str | None = None  # a reason: no report today
    index_from: str | None = None  # "tse" | "page": where the total index figures come from
    use_page: bool = False  # retail value, real-money flow and groups from the market page
    confirmed: bool = False  # the trading day was confirmed by TSE
    notes: list[str] = field(default_factory=list)  # warnings for the approver (never written into the post)


def _day(value: float) -> date | None:
    t = str(int(value))
    if len(t) != 8:
        return None
    try:
        return date(int(t[:4]), int(t[4:6]), int(t[6:]))
    except ValueError:
        return None


def page_agrees(page: dict[str, float], tse: dict[str, float]) -> bool:
    """The market page shows the same day as TSE: the index level and the day's change both match."""
    pv, tv = page.get(INDEX_VALUE), tse.get(INDEX_VALUE)
    if not pv or not tv or tv <= 0:
        return False
    if abs(pv / tv - 1) > VALUE_TOLERANCE:
        return False
    pc, tc = page.get(INDEX_CHANGE), tse.get(INDEX_CHANGE)
    if pc is None or tc is None:
        return True  # the level alone agrees; nothing more to compare
    return abs(pc - tc) <= max(CHANGE_TOLERANCE * abs(tc), CHANGE_FLOOR * tv)


def plan_market_data(run_date: date, tse: dict[str, float] | None, page: dict[str, float] | None) -> DataPlan:
    """`tse`, `page`: metric -> value of the TSE overview and the market page (None when that source failed)."""
    plan = DataPlan()
    if tse and INDEX_VALUE in tse and TRADING_DAY in tse:
        day = _day(tse[TRADING_DAY])
        if day is None:
            plan.notes.append("تاریخ معاملات در پاسخ TSE خوانا نبود؛ تاریخ داده‌ها تأیید نشد.")
        elif day != run_date:
            plan.skip = (f"امروز روز معاملاتی نبوده یا بازار هنوز باز نشده است (آخرین روز معاملات در TSE: "
                         f"{day.isoformat()})؛ گزارشی ساخته نشد.")
            return plan
        else:
            plan.confirmed = True
            plan.index_from = "tse"
            if tse.get(MARKET_CLOSED) == 0:
                plan.notes.append("هنگام گرفتن داده، بازار هنوز بسته نشده بود؛ عددها ممکن است عدد پایانی امروز نباشند.")
            if page is None:
                plan.notes.append("صفحهٔ bourse-trader خوانده نشد؛ بخش معاملات خرد، پول حقیقی و گروه‌ها در گزارش نیست.")
            elif page_agrees(page, tse):
                plan.use_page = True
            else:
                plan.notes.append("عددهای bourse-trader با شاخص امروزِ TSE نمی‌خواند (احتمالاً هنوز به‌روز نشده است)؛ "
                                  "بخش معاملات خرد، پول حقیقی و گروه‌ها کنار گذاشته شد.")
            return plan
    # TSE did not answer (or answered without a usable day): the market page alone, as before, with a warning
    if page is None or INDEX_VALUE not in page:
        plan.skip = "نه TSE و نه صفحهٔ bourse-trader دادهٔ قابل استفاده‌ای ندادند؛ گزارشی ساخته نشد."
        return plan
    plan.index_from = "page"
    plan.use_page = True
    if not plan.notes:
        plan.notes.append("TSE در دسترس نبود و تاریخ داده‌ها تأیید نشد؛ پیش از تأیید مطمئن شوید عددها مال امروز است.")
    return plan
