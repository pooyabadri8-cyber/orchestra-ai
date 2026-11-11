"""Deterministic SVG charts. Same spec and colours in, same bytes out. Every label is formatted by code."""
from __future__ import annotations

import html
import math
from datetime import date

from ..brand import BrandColors
from ..content.calendar import MONTHS, to_jalali
from ..content.numbers import fa_number
from .charts import ChartSpec

W = 920
FONT = "Vazirmatn, sans-serif"


_RTL_ANCHOR = {"start": "end", "end": "start", "middle": "middle"}


def _t(x: float, y: float, text: str, *, size: int = 26, fill: str, anchor: str = "middle", weight: int = 400) -> str:
    """`anchor` is geometric: "end" puts the text's right edge at x. In right-to-left text SVG flips start/end, so we map."""
    anchor = _RTL_ANCHOR[anchor]
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}" direction="rtl">{html.escape(text)}</text>')


def render_svg(spec: ChartSpec, colors: BrandColors) -> str:
    if spec.kind == "bar_signed":
        return bar_signed(spec, colors)
    return line(spec, colors)


def bar_signed(spec: ChartSpec, c: BrandColors) -> str:
    n = len(spec.bars)
    row, top = 78, 20
    h = top + n * row + 10
    label_w = 250
    area_l, area_r = 20, W - label_w - 20
    peak = max(abs(b.value) for b in spec.bars) or 1.0
    has_neg = any(b.value < 0 for b in spec.bars)
    has_pos = any(b.value > 0 for b in spec.bars)
    zero = (area_l + area_r) / 2 if has_neg and has_pos else (area_r if has_neg else area_l)
    half = (area_r - area_l) / (2 if has_neg and has_pos else 1)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" style="direction:rtl" viewBox="0 0 {W} {h}" width="{W}" height="{h}">']
    out.append(f'<line x1="{zero:.1f}" y1="{top - 8}" x2="{zero:.1f}" y2="{h - 6}" stroke="{c.muted}" stroke-width="2" opacity="0.6"/>')
    for i, b in enumerate(spec.bars):
        y = top + i * row
        length = abs(b.value) / peak * (half - 150)
        length = max(length, 6)
        color = c.positive if b.value >= 0 else c.negative
        x = zero if b.value >= 0 else zero - length
        out.append(f'<rect x="{x:.1f}" y="{y + 14}" width="{length:.1f}" height="40" rx="8" fill="{color}"/>')
        vx = zero + length + 12 if b.value >= 0 else zero - length - 12
        out.append(_t(vx, y + 44, b.display, size=24, fill=c.text, anchor="start" if b.value >= 0 else "end", weight=700))
        out.append(_t(W - 20, y + 44, b.label, size=26, fill=c.text, anchor="end"))
    out.append("</svg>")
    return "".join(out)


def _nice(lo: float, hi: float, ticks: int = 4) -> list[float]:
    span = hi - lo or abs(hi) or 1.0
    step = 10 ** math.floor(math.log10(span / ticks))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= ticks:
            step *= m
            break
    start = math.floor(lo / step) * step
    vals, v = [], start
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.01:
            vals.append(v)
        v += step
    return vals


def _compact(v: float, unit: str) -> str:
    if unit not in ("IRR", "IRT"):
        return fa_number(v, 0)
    if unit == "IRR":
        v /= 10  # show toman
    a = abs(v)
    for div, word in ((1e9, " میلیارد"), (1e6, " میلیون"), (1e3, " هزار")):
        if a >= div * 10:
            return fa_number(v / div, 1 if a < div * 100 else 0) + word
    return fa_number(v, 0)


def line(spec: ChartSpec, c: BrandColors) -> str:
    h = 560
    pad_l, pad_r, pad_t, pad_b = 150, 30, 70, 60
    n = len(spec.dates)
    values = [v for vals in spec.series.values() for v in vals if v is not None]
    lo, hi = min(values), max(values)
    margin = (hi - lo) * 0.06 or abs(hi) * 0.02 or 1
    lo, hi = lo - margin, hi + margin
    px = lambda i: pad_l + (W - pad_l - pad_r) * (i / max(n - 1, 1))  # noqa: E731
    py = lambda v: pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))  # noqa: E731
    palette = [c.primary, c.accent, c.muted]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" style="direction:rtl" viewBox="0 0 {W} {h}" width="{W}" height="{h}">']
    for tv in _nice(lo, hi):
        y = py(tv)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="{c.muted}" stroke-width="1" opacity="0.25"/>')
        out.append(_t(pad_l - 14, y + 8, _compact(tv, spec.unit), size=20, fill=c.muted, anchor="end"))
    last_month = None
    for i, d in enumerate(spec.dates):
        y_, m_, _ = to_jalali(date.fromisoformat(d))
        if m_ != last_month:
            if last_month is not None and 0.04 < i / max(n - 1, 1) < 0.96:
                out.append(_t(px(i), h - 22, MONTHS[m_ - 1], size=20, fill=c.muted))
            last_month = m_
    lx = W - pad_r
    for k, (name, vals) in enumerate(spec.series.items()):
        color = palette[k % len(palette)]
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals) if v is not None)
        width = 4 if k == 0 else 3
        dash = "" if k == 0 else ' stroke-dasharray="10 6"'
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}"{dash} stroke-linejoin="round"/>')
        out.append(f'<rect x="{lx - 26}" y="{18}" width="26" height="8" rx="4" fill="{color}"/>')
        out.append(_t(lx - 36, 30, name, size=20, fill=c.text, anchor="end"))
        lx -= 36 + 11 * len(name) + 40
    out.append("</svg>")
    return "".join(out)
