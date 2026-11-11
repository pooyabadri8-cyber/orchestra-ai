"""Technical analysis, computed in code.

Indicators follow their standard definitions (Wilder's smoothing for RSI and ATR, SMA-seeded EMA, population standard
deviation for Bollinger Bands). Every series is aligned with the input bars; positions without enough history are None.
`snapshot()` reduces the series to the values of the last bar plus signals. A signal carries a Persian description with
no numbers in it: the numbers travel separately as data points, so the writer can only cite them through tokens.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

Series = list[float | None]

FORMULA_VERSION = "technical@1"


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"{self.day}: inconsistent bar (low/high do not contain open/close)")
        if self.volume < 0 or self.low <= 0:
            raise ValueError(f"{self.day}: negative volume or non-positive price")


# ------------------------------------------------------------------ indicators


def sma(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: list[float], n: int) -> Series:
    """Seeded with the SMA of the first n values; k = 2 / (n + 1)."""
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _ema_of_defined(values: Series, n: int) -> Series:
    """EMA over a series that starts with None values (e.g. MACD line)."""
    start = next((i for i, v in enumerate(values) if v is not None), None)
    out: Series = [None] * len(values)
    if start is None:
        return out
    tail = ema([float(v) for v in values[start:]], n)  # type: ignore[arg-type]
    out[start:] = tail
    return out


def rsi(closes: list[float], n: int = 14) -> Series:
    """Wilder's RSI: first averages are simple means of the first n changes, then smoothed with (n-1)/n."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[n] = value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
        avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = value(avg_g, avg_l)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [(f - s) if f is not None and s is not None else None for f, s in zip(ef, es)]
    sig = _ema_of_defined(line, signal)
    hist: Series = [(m - s) if m is not None and s is not None else None for m, s in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)  # population standard deviation
        upper[i], lower[i] = m + k * sd, m - k * sd
    return lower, mid, upper


def true_range(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: list[Bar], n: int = 14) -> Series:
    """Wilder's ATR: first value is the mean of the first n true ranges, then (prev*(n-1) + TR) / n."""
    tr = true_range(bars)
    out: Series = [None] * len(bars)
    if len(bars) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(bars)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def stochastic(bars: list[Bar], n: int = 14, d: int = 3) -> tuple[Series, Series]:
    k: Series = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        window = bars[i - n + 1:i + 1]
        hh, ll = max(b.high for b in window), min(b.low for b in window)
        k[i] = 50.0 if hh == ll else (bars[i].close - ll) / (hh - ll) * 100
    defined_from = n - 1
    dd: Series = [None] * len(bars)
    for i in range(defined_from + d - 1, len(bars)):
        dd[i] = sum(k[j] for j in range(i - d + 1, i + 1)) / d  # type: ignore[misc]
    return k, dd


def obv(bars: list[Bar]) -> list[float]:
    out, total = [], 0.0
    for i, b in enumerate(bars):
        if i > 0:
            if b.close > bars[i - 1].close:
                total += b.volume
            elif b.close < bars[i - 1].close:
                total -= b.volume
        out.append(total)
    return out


@dataclass(frozen=True)
class Pivots:
    pivot: float
    r1: float
    r2: float
    s1: float
    s2: float


def classic_pivots(prev: Bar) -> Pivots:
    p = (prev.high + prev.low + prev.close) / 3
    return Pivots(pivot=p, r1=2 * p - prev.low, s1=2 * p - prev.high, r2=p + (prev.high - prev.low), s2=p - (prev.high - prev.low))


def swing_levels(bars: list[Bar], k: int = 3, tolerance: float = 0.015) -> tuple[list[float], list[float]]:
    """Swing highs and lows (a bar is a swing high when its high is the highest of the k bars on each side), with
    levels closer than `tolerance` (relative) merged into their average. Returns (supports, resistances) sorted."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        window = bars[i - k:i + k + 1]
        if bars[i].high == max(b.high for b in window):
            highs.append(bars[i].high)
        if bars[i].low == min(b.low for b in window):
            lows.append(bars[i].low)

    def merge(levels: list[float]) -> list[float]:
        merged: list[list[float]] = []
        for lv in sorted(levels):
            if merged and abs(lv - merged[-1][-1]) / merged[-1][-1] <= tolerance:
                merged[-1].append(lv)
            else:
                merged.append([lv])
        return [sum(g) / len(g) for g in merged]

    return merge(lows), merge(highs)


# ------------------------------------------------------------------ snapshot and signals


@dataclass(frozen=True)
class Signal:
    code: str
    bias: str  # bullish | bearish | neutral
    text: str  # Persian, no numbers


@dataclass
class TechnicalSnapshot:
    as_of: date
    bars: int
    values: dict[str, float] = field(default_factory=dict)  # metric -> value at the last bar
    signals: list[Signal] = field(default_factory=list)
    trend: str = "نامشخص"
    missing: list[str] = field(default_factory=list)  # indicators skipped for lack of history


def _last(series: Series) -> float | None:
    return series[-1] if series else None


def _crossed(a: Series, b: Series, lookback: int) -> str | None:
    """'up' when a crossed above b within the last `lookback` bars, 'down' for below, else None."""
    n = len(a)
    for i in range(max(1, n - lookback), n):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if a[i - 1] <= b[i - 1] and a[i] > b[i]:  # type: ignore[operator]
            return "up"
        if a[i - 1] >= b[i - 1] and a[i] < b[i]:  # type: ignore[operator]
            return "down"
    return None


def snapshot(bars: list[Bar]) -> TechnicalSnapshot:
    if len(bars) < 2:
        raise ValueError("at least two bars are needed")
    days = [b.day for b in bars]
    if days != sorted(days) or len(set(days)) != len(days):
        raise ValueError("bars must be in date order without duplicates")
    closes = [b.close for b in bars]
    last = bars[-1]
    snap = TechnicalSnapshot(as_of=last.day, bars=len(bars))
    v = snap.values
    v["price.close"] = last.close
    v["price.change_pct"] = (last.close / bars[-2].close - 1) * 100

    def put(name: str, value: float | None, need: int) -> None:
        if value is None:
            snap.missing.append(f"{name} (به {need} روز داده نیاز دارد)")
        else:
            v[name] = value

    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    put("sma.20", _last(s20), 20)
    put("sma.50", _last(s50), 50)
    put("sma.200", _last(s200), 200)
    r = rsi(closes)
    put("rsi.14", _last(r), 15)
    line, sig, hist = macd(closes)
    put("macd.line", _last(line), 26)
    put("macd.signal", _last(sig), 34)
    put("macd.hist", _last(hist), 34)
    lo, mid, up = bollinger(closes)
    put("bollinger.lower", _last(lo), 20)
    put("bollinger.upper", _last(up), 20)
    a = atr(bars)
    put("atr.14", _last(a), 14)
    if "atr.14" in v:
        v["atr.14_pct"] = v["atr.14"] / last.close * 100
    k, d = stochastic(bars)
    put("stoch.k", _last(k), 14)
    vol20 = sma([b.volume for b in bars], 20)
    if _last(vol20):
        v["volume.ratio_20"] = last.volume / vol20[-1]  # type: ignore[operator]
    window = bars[-250:]
    v["range.high_250"] = max(b.high for b in window)
    v["range.low_250"] = min(b.low for b in window)
    v["range.from_high_pct"] = (last.close / v["range.high_250"] - 1) * 100
    piv = classic_pivots(bars[-2])
    v.update({"pivot.p": piv.pivot, "pivot.r1": piv.r1, "pivot.s1": piv.s1, "pivot.r2": piv.r2, "pivot.s2": piv.s2})
    supports, resistances = swing_levels(bars[-120:])
    below = [s for s in supports if s < last.close]
    above = [x for x in resistances if x > last.close]
    if below:
        v["level.support"] = max(below)
    if above:
        v["level.resistance"] = min(above)

    snap.trend = _trend(v, s50)
    snap.signals = _signals(v, r, line, sig, s50, s200, closes, lo, up)
    return snap


def _trend(v: dict[str, float], s50: Series) -> str:
    c = v["price.close"]
    if "sma.50" not in v:
        return "نامشخص"
    slope_up = len(s50) > 10 and s50[-11] is not None and s50[-1] > s50[-11]  # type: ignore[operator]
    above50 = c > v["sma.50"]
    if "sma.200" in v:
        if above50 and v["sma.50"] > v["sma.200"] and slope_up:
            return "صعودی"
        if not above50 and v["sma.50"] < v["sma.200"] and not slope_up:
            return "نزولی"
        return "خنثی"
    if above50 and slope_up:
        return "صعودی کوتاه‌مدت"
    if not above50 and not slope_up:
        return "نزولی کوتاه‌مدت"
    return "خنثی"


def _signals(v, r, line, sig, s50, s200, closes, lo, up) -> list[Signal]:
    out: list[Signal] = []
    if "rsi.14" in v:
        if v["rsi.14"] >= 70:
            out.append(Signal("rsi_overbought", "bearish", "شاخص قدرت نسبی (RSI) در محدودهٔ اشباع خرید است؛ احتمال اصلاح کوتاه‌مدت بیشتر می‌شود."))
        elif v["rsi.14"] <= 30:
            out.append(Signal("rsi_oversold", "bullish", "شاخص قدرت نسبی (RSI) در محدودهٔ اشباع فروش است؛ احتمال برگشت کوتاه‌مدت بیشتر می‌شود."))
    cross = _crossed(line, sig, 3)
    if cross == "up":
        out.append(Signal("macd_cross_up", "bullish", "خط MACD در روزهای اخیر خط سیگنال را به سمت بالا قطع کرده است."))
    elif cross == "down":
        out.append(Signal("macd_cross_down", "bearish", "خط MACD در روزهای اخیر خط سیگنال را به سمت پایین قطع کرده است."))
    gc = _crossed(s50, s200, 5)
    if gc == "up":
        out.append(Signal("golden_cross", "bullish", "میانگین متحرک پنجاه‌روزه از میانگین دویست‌روزه بالاتر رفته است (تقاطع طلایی)."))
    elif gc == "down":
        out.append(Signal("death_cross", "bearish", "میانگین متحرک پنجاه‌روزه به زیر میانگین دویست‌روزه رفته است (تقاطع مرگ)."))
    if up[-1] is not None and closes[-1] > up[-1]:
        out.append(Signal("above_upper_band", "neutral", "قیمت پایانی بالاتر از باند بالایی بولینگر است؛ حرکت قوی ولی کشیده."))
    elif lo[-1] is not None and closes[-1] < lo[-1]:
        out.append(Signal("below_lower_band", "neutral", "قیمت پایانی پایین‌تر از باند پایینی بولینگر است؛ فشار فروش شدید."))
    if v.get("volume.ratio_20", 0) >= 2:
        out.append(Signal("volume_spike", "neutral", "حجم معاملات امروز بیش از دو برابر میانگین بیست‌روزه است."))
    return out
