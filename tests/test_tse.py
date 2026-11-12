from datetime import date

import pytest

from orchestra.analysis.technical import snapshot as technical_snapshot
from orchestra.data.sources import tse
from tse_samples import index_b2, index_financial


def test_financial_history_parses_in_order_with_volume():
    bars = tse.parse_index_financial(index_financial())
    assert len(bars) == 260 and bars[-1].day == date(2026, 11, 11) and bars[0].day < bars[-1].day
    assert all(b.low <= b.open <= b.high and b.low <= b.close <= b.high for b in bars)
    assert bars[-1].volume > 0
    snap = technical_snapshot(bars)
    assert {"sma.50", "rsi.14", "macd.hist", "atr.14", "stoch.k"} <= set(snap.values)


def test_b2_history_is_reordered_and_has_levels():
    bars = tse.parse_index_b2(index_b2())
    assert len(bars) == 260 and bars[-1].day == date(2026, 11, 11)
    assert bars[-1].close == pytest.approx(tse.parse_index_financial(index_financial())[-1].close, rel=1e-6)
    assert "rsi.14" in technical_snapshot(bars).values


def test_dates_with_dashes_duplicates_and_junk_lines():
    raw = index_financial(60).decode()
    first = raw.split(";")[0]
    d = first.split(",")[0]
    dashed = raw.replace(d, f"{d[:4]}-{d[4:6]}-{d[6:]}", 1)
    raw2 = dashed + ";" + first + ";;garbage,line;20261111,0,0,0,0,0"
    bars = tse.parse_index_financial(raw2.encode())
    assert len(bars) == 60 and len({b.day for b in bars}) == 60


def test_refuses_what_is_not_a_history():
    for bad in (b"", b"<html>error</html>", b'{"x":1}', index_financial(10)):
        with pytest.raises(tse.TseError):
            tse.parse_index_financial(bad)
    for bad in (b"not json", b'{"indexB2": []}', b'{"other": 1}'):
        with pytest.raises(tse.TseError):
            tse.parse_index_b2(bad)


def test_index_names():
    for s in ("شاخص کل", "شاخص كل", "شاخص‌کل", " شاخص  کل بورس ", "TEDPIX"):
        assert tse.canonical_index(s) == "شاخص کل", s
    assert tse.canonical_index("شاخص هم‌وزن") == "شاخص کل هم وزن"
    assert tse.canonical_index("فولاد") is None
    urls = [e.url for e in tse.endpoints("1", "https://a/", "https://b")]
    assert urls == ["https://a/tsev2/chart/data/IndexFinancial.aspx?i=1&t=ph", "https://b/api/Index/GetIndexB2History/1"]
