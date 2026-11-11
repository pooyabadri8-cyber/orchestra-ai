"""Technical indicators against a published reference and against independent re-implementations."""
import random
from datetime import date, timedelta

import numpy as np
import pytest

from orchestra.analysis import technical as t

# StockCharts' RSI worksheet (Wilder's method), full-precision closes and the published RSI(14) values.
STOCKCHARTS_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931,
                      46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439]
STOCKCHARTS_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]


def walk(n=300, seed=7, start=100.0):
    rng = random.Random(seed)
    bars, price, day = [], start, date(2025, 1, 1)
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(0.0005, 0.02)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.005)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.005)))
        bars.append(t.Bar(day + timedelta(days=i), o, h, lo, c, rng.randint(1_000, 50_000) * 100))
        price = c
    return bars


def test_rsi_matches_the_published_reference():
    got = [round(x, 2) for x in t.rsi(STOCKCHARTS_CLOSES) if x is not None]
    assert got == STOCKCHARTS_RSI


def test_ema_sma_macd_bollinger_match_independent_numpy_code():
    closes = [b.close for b in walk()]
    x = np.array(closes)
    # SMA
    ref_sma = np.convolve(x, np.ones(20) / 20, mode="valid")
    assert np.allclose([v for v in t.sma(closes, 20) if v is not None], ref_sma)
    # EMA seeded with SMA
    def ref_ema(arr, n):
        out = [arr[:n].mean()]
        k = 2 / (n + 1)
        for v in arr[n:]:
            out.append(v * k + out[-1] * (1 - k))
        return np.array(out)
    assert np.allclose([v for v in t.ema(closes, 12) if v is not None], ref_ema(x, 12))
    # MACD
    e12, e26 = ref_ema(x, 12)[14:], ref_ema(x, 26)
    line_ref = e12 - e26
    sig_ref = ref_ema(line_ref, 9)
    line, sig, hist = t.macd(closes)
    assert np.allclose([v for v in line if v is not None], line_ref)
    assert np.allclose([v for v in sig if v is not None], sig_ref)
    assert np.allclose([v for v in hist if v is not None], line_ref[8:] - sig_ref)
    # Bollinger with population standard deviation
    lo, mid, up = t.bollinger(closes)
    windows = np.lib.stride_tricks.sliding_window_view(x, 20)
    assert np.allclose([v for v in up if v is not None], windows.mean(1) + 2 * windows.std(1, ddof=0))
    assert np.allclose([v for v in lo if v is not None], windows.mean(1) - 2 * windows.std(1, ddof=0))


def test_atr_and_stochastic_match_independent_code():
    bars = walk()
    h, l, c = (np.array([getattr(b, f) for b in bars]) for f in ("high", "low", "close"))
    tr = np.maximum.reduce([h - l, np.abs(h - np.r_[c[0], c[:-1]]), np.abs(l - np.r_[c[0], c[:-1]])])
    tr[0] = h[0] - l[0]
    ref = [tr[:14].mean()]
    for v in tr[14:]:
        ref.append((ref[-1] * 13 + v) / 14)
    assert np.allclose([v for v in t.atr(bars) if v is not None], ref)
    k, d = t.stochastic(bars)
    hh = np.lib.stride_tricks.sliding_window_view(h, 14).max(1)
    ll = np.lib.stride_tricks.sliding_window_view(l, 14).min(1)
    k_ref = (c[13:] - ll) / (hh - ll) * 100
    assert np.allclose([v for v in k if v is not None], k_ref)
    assert np.allclose([v for v in d if v is not None], np.convolve(k_ref, np.ones(3) / 3, mode="valid"))


def test_rsi_edge_cases():
    assert t.rsi([float(i) for i in range(1, 20)])[-1] == 100.0  # only gains
    assert t.rsi([5.0] * 20)[-1] == 50.0  # no movement at all
    assert all(v is None for v in t.rsi([1.0, 2.0, 3.0]))  # not enough history


def test_pivots_and_obv():
    prev = t.Bar(date(2026, 1, 1), 100, 110, 90, 105, 1000)
    p = t.classic_pivots(prev)
    assert (p.pivot, p.r1, p.s1, p.r2, p.s2) == pytest.approx((101.6667, 113.3333, 93.3333, 121.6667, 81.6667), abs=1e-4)
    bars = [t.Bar(date(2026, 1, i + 1), c, c, c, c, v) for i, (c, v) in enumerate([(10, 100), (11, 200), (10, 50), (10, 70), (12, 30)])]
    assert t.obv(bars) == [0, 200, 150, 150, 180]


def test_bad_bars_are_refused():
    with pytest.raises(ValueError):
        t.Bar(date(2026, 1, 1), 100, 99, 90, 95, 10)  # high below open
    with pytest.raises(ValueError):
        t.snapshot([t.Bar(date(2026, 1, 2), 1, 1, 1, 1, 1), t.Bar(date(2026, 1, 1), 1, 1, 1, 1, 1)])  # out of order


def test_snapshot_reports_missing_history_instead_of_guessing():
    snap = t.snapshot(walk(60))
    assert "sma.50" in snap.values and "sma.200" not in snap.values
    assert any("sma.200" in m for m in snap.missing)
    assert snap.trend in ("صعودی کوتاه‌مدت", "نزولی کوتاه‌مدت", "خنثی")


def _trend_bars(direction: int, n=260):
    day, out, price = date(2024, 1, 1), [], 100.0
    for i in range(n):
        price *= 1 + direction * 0.004
        out.append(t.Bar(day + timedelta(days=i), price, price * 1.003, price * 0.997, price, 10_000))
    return out


def test_trend_and_levels():
    up = t.snapshot(_trend_bars(+1))
    down = t.snapshot(_trend_bars(-1))
    assert up.trend == "صعودی" and down.trend == "نزولی"
    assert up.values["range.from_high_pct"] == pytest.approx(-0.2991, abs=1e-3)  # close vs the 250-day high (with wick)
    assert {s.code for s in up.signals} >= {"rsi_overbought"} and {s.code for s in down.signals} >= {"rsi_oversold"}
    assert all(not any(ch.isdigit() for ch in s.text) for s in up.signals + down.signals)  # no numbers in signal text


def test_crosses_are_detected():
    # flat, then a sharp rise: the 50-day average crosses above the 200-day average near the end
    bars, day, price = [], date(2024, 1, 1), 100.0
    for i in range(320):
        price = 100.0 if i < 250 else price * 1.02
        bars.append(t.Bar(day + timedelta(days=i), price, price * 1.001, price * 0.999, price, 10_000))
    s50, s200 = t.sma([b.close for b in bars], 50), t.sma([b.close for b in bars], 200)
    first = next(i for i in range(200, 320) if s50[i] > s200[i])
    assert t._crossed(s50[: first + 1], s200[: first + 1], 3) == "up"
    snap = t.snapshot(bars[: first + 2])
    assert "golden_cross" in {s.code for s in snap.signals}


def test_swing_levels_merge_nearby_points():
    pattern = [100, 104, 108, 104, 100, 96, 92, 96, 100, 104, 108.5, 104, 100, 96, 92.5, 96, 100]
    bars = [t.Bar(date(2026, 1, 1) + timedelta(days=i), p, p, p, p, 1) for i, p in enumerate(pattern)]
    supports, resistances = t.swing_levels(bars, k=2)
    assert supports == pytest.approx([92.25]) and resistances == pytest.approx([108.25])
