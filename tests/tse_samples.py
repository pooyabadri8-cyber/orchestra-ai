"""Synthetic TSE index answers in the two public shapes (clearly made-up numbers, real field layout)."""
from __future__ import annotations

import json
from datetime import date, timedelta


def _series(n: int, end: date, last_close: float | None = None):
    level, out = 2_500_000.0, []
    days = []
    d = end
    while len(days) < n:  # trading days only: Saturday to Wednesday
        if d.weekday() in (5, 6, 0, 1, 2):
            days.append(d)
        d -= timedelta(days=1)
    for i, day in enumerate(reversed(days)):
        o = level
        c = level * (1.004 if i % 6 else 0.988)
        h, lo = max(o, c) * 1.003, min(o, c) * 0.997
        out.append((day, o, h, lo, c, 4_000_000_000 + i * 1_000_000))
        level = c
    if last_close:  # rescale so the newest close is this value
        k = last_close / out[-1][4]
        out = [(d, o * k, h * k, lo * k, c * k, v) for d, o, h, lo, c, v in out]
    return out


def index_financial(n: int = 260, end: date = date(2026, 11, 11), last_close: float | None = None) -> bytes:
    """old.tsetmc.com/tsev2/chart/data/IndexFinancial.aspx: date,high,low,open,close,volume,x;..."""
    rows = [f"{d:%Y%m%d},{h:.1f},{lo:.1f},{o:.1f},{c:.1f},{v},0" for d, o, h, lo, c, v in _series(n, end, last_close)]
    return ";".join(rows).encode()


def index_b2(n: int = 260, end: date = date(2026, 11, 11)) -> bytes:
    """cdn.tsetmc.com/api/Index/GetIndexB2History: newest first, levels only."""
    items = [{"insCode": "32097828799138957", "dEven": int(f"{d:%Y%m%d}"), "xNivInuClMresIbs": round(c, 1),
              "xNivInuPhMresIbs": round(h, 1), "xNivInuPbMresIbs": round(lo, 1)} for d, o, h, lo, c, v in _series(n, end)]
    return json.dumps({"indexB2": list(reversed(items))}).encode()
