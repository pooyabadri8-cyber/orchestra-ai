"""Fundamental ratios with hand-computed expectations, TTM from cumulative reports, and honest undefined cases."""
from datetime import date

import pytest

from orchestra.analysis.fundamental import Statement, analyze, ttm

B = 1e9  # one billion rials


def st(year, months, revenue, net, *, audited=True, gross=None, op=None, assets=None, equity=None, debt=None, shares=1e9, dps=None):
    return Statement("نماد", year, months, date(year, months, 28), audited, revenue, gross, op, net, assets, equity, debt, shares, None, dps)


HISTORY = [
    st(1403, 6, 400 * B, 40 * B),
    st(1403, 12, 1000 * B, 120 * B, dps=60.0),
    st(1404, 6, 600 * B, 70 * B, audited=False, gross=180 * B, op=110 * B, assets=2000 * B, equity=800 * B, debt=400 * B),
]


def test_ttm_from_cumulative_reports():
    flows, latest, basis = ttm(HISTORY)
    assert flows["revenue"] == pytest.approx(600 * B + 1000 * B - 400 * B)  # 1,200 B
    assert flows["net_income"] == pytest.approx(70 * B + 120 * B - 40 * B)  # 150 B
    assert latest.months == 6 and "۶" not in basis and "6 ماههٔ" in basis
    with pytest.raises(ValueError):
        ttm([HISTORY[2]])  # without last year's figures TTM is not computed, not guessed


def test_ratios_by_hand():
    rep = analyze(HISTORY, price=1500.0, price_date=date(2026, 11, 10))
    v = rep.value
    assert v("market_cap") == pytest.approx(1500 * 1e9)
    assert v("eps_ttm") == pytest.approx(150.0)          # 150 B / 1 B shares
    assert v("pe_ttm") == pytest.approx(10.0)            # 1500 / 150
    assert v("bvps") == pytest.approx(800.0)
    assert v("pb") == pytest.approx(1.875)               # 1500 / 800
    assert v("roe_ttm") == pytest.approx(18.75)          # 150 / 800
    assert v("roa_ttm") == pytest.approx(7.5)            # 150 / 2000
    assert v("net_margin_ttm") == pytest.approx(12.5)    # 150 / 1200
    assert v("debt_to_equity") == pytest.approx(0.5)
    assert v("revenue_growth_yoy") == pytest.approx(50.0)    # 600 vs 400
    assert v("earnings_growth_yoy") == pytest.approx(75.0)   # 70 vs 40
    assert v("dividend_yield") == pytest.approx(4.0)     # 60 / 1500
    assert v("payout_ratio") == pytest.approx(50.0)      # 60 / 120
    # margins that need TTM gross/operating profit are undefined: last year's figures are missing, not invented
    assert v("gross_margin_ttm") is None and rep.ratios["gross_margin_ttm"].reason
    assert "آخرین صورت‌های مالی حسابرسی‌نشده است." in rep.notes


def test_loss_and_negative_equity_are_undefined_not_numbers():
    hist = [st(1403, 6, 400 * B, 10 * B), st(1403, 12, 900 * B, 20 * B),
            st(1404, 6, 300 * B, -50 * B, equity=-100 * B, assets=500 * B, debt=300 * B)]
    rep = analyze(hist, price=900.0, price_date=date(2026, 11, 10))
    assert rep.value("eps_ttm") == pytest.approx(-40.0)  # -50 + 20 - 10 = -40 B
    assert rep.value("pe_ttm") is None and "زیان‌ده" in rep.ratios["pe_ttm"].reason
    assert rep.value("pb") is None and rep.value("roe_ttm") is None and rep.value("debt_to_equity") is None
    assert rep.value("earnings_growth_yoy") is not None  # base year positive: growth is defined (and negative)
    assert any("P/E محاسبه نشد" in n for n in rep.notes)


def test_growth_from_a_loss_base_is_not_a_percentage():
    hist = [st(1403, 6, 400 * B, -10 * B), st(1403, 12, 900 * B, 5 * B), st(1404, 6, 500 * B, 30 * B)]
    rep = analyze(hist, price=500.0, price_date=date(2026, 11, 10))
    assert rep.value("earnings_growth_yoy") is None and "گمراه‌کننده" in rep.ratios["earnings_growth_yoy"].reason


def test_audited_version_wins_over_an_unaudited_duplicate():
    hist = HISTORY[:2] + [st(1404, 6, 600 * B, 70 * B, audited=False), st(1404, 6, 600 * B, 65 * B, audited=True)]
    flows, latest, _ = ttm(hist)
    assert latest.audited and flows["net_income"] == pytest.approx(65 * B + 120 * B - 40 * B)


def test_notes_carry_no_numbers():
    rep = analyze(HISTORY, price=1500.0, price_date=date(2026, 11, 10))
    assert all(not any(ch.isdigit() for ch in n.replace("P/E", "").replace("P/B", "")) for n in rep.notes)
