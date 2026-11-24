"""Is today's market data really today's? Every branch of agents/freshness.py."""
from datetime import date

from orchestra.agents.freshness import page_agrees, plan_market_data

TODAY = date(2026, 11, 11)
TSE = {"index.total.value": 7_976_412.3, "index.total.change": 186_480.0, "market.trading_day": 20261111,
       "market.closed": 1.0}
PAGE = {"index.total.value": 7_976_400.0, "index.total.change": 186_492.0}  # the captured page, rounded
YESTERDAY_PAGE = {"index.total.value": 7_789_932.0, "index.total.change": -42_100.0}


def test_today_confirmed_and_page_agrees():
    p = plan_market_data(TODAY, TSE, PAGE)
    assert p.skip is None and p.confirmed and p.index_from == "tse" and p.use_page and p.notes == []


def test_holiday_or_market_not_open_gives_no_report():
    p = plan_market_data(TODAY, {**TSE, "market.trading_day": 20261110}, PAGE)
    assert p.skip and "2026-11-10" in p.skip and not p.use_page


def test_stale_page_is_left_out_but_the_report_goes_on():
    p = plan_market_data(TODAY, TSE, YESTERDAY_PAGE)
    assert p.skip is None and p.index_from == "tse" and not p.use_page
    assert any("bourse-trader" in n for n in p.notes)


def test_stale_page_on_a_quiet_day_is_caught_by_the_change():
    # yesterday's close is within 0.2% of today's, but yesterday's change is not today's change
    tse = {**TSE, "index.total.value": 7_980_000.0, "index.total.change": 8_000.0}
    page = {"index.total.value": 7_972_000.0, "index.total.change": -30_000.0}
    assert not page_agrees(page, tse)
    assert not plan_market_data(TODAY, tse, page).use_page


def test_page_down_report_goes_on_from_tse():
    p = plan_market_data(TODAY, TSE, None)
    assert p.skip is None and p.index_from == "tse" and not p.use_page and p.notes


def test_market_still_open_is_a_warning():
    p = plan_market_data(TODAY, {**TSE, "market.closed": 0.0}, PAGE)
    assert p.use_page and any("بسته نشده" in n for n in p.notes)


def test_tse_down_falls_back_to_the_page_with_a_warning():
    p = plan_market_data(TODAY, None, PAGE)
    assert p.skip is None and not p.confirmed and p.index_from == "page" and p.use_page
    assert any("تأیید نشد" in n for n in p.notes)


def test_nothing_available():
    assert plan_market_data(TODAY, None, None).skip


def test_unreadable_tse_day_is_not_trusted():
    p = plan_market_data(TODAY, {**TSE, "market.trading_day": 123}, PAGE)
    assert not p.confirmed and p.index_from == "page" and len(p.notes) == 1
