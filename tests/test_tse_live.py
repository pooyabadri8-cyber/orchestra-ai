import json
from pathlib import Path

import pytest

from orchestra.data.sources.base import ParseError
from orchestra.data.sources.tse_live import TseFarabourseSource, TseIndicesSource, sector_name

FX = Path(__file__).parent / "fixtures"


def test_sector_names_are_cleaned():
    assert sector_name("27-فلزات اساسي") == "فلزات اساسی"
    assert sector_name("بيمه و بازنشسته66") == "بیمه و بازنشستگی"
    assert sector_name("26محصولات كامپيوتري الكترونيكي") == "محصولات کامپیوتری"
    assert sector_name("39-چند رشته اي ص") == "چندرشته‌ای صنعتی"
    assert sector_name("شاخص 30 شركت بزرگ") is None and sector_name("شاخص كل") is None


def test_real_index_list():
    res = TseIndicesSource().parse((FX / "tse_index_b1_sample.json").read_bytes())
    by = {(p.metric, p.subject): p for p in res.points}
    assert by[("index.equal.value", None)].value == 2425838.62
    assert by[("index.equal.change_pct", None)].value == pytest.approx(0.5403)
    sectors = [p for p in res.points if p.metric == "sector.change_pct"]
    assert len(sectors) > 30 and sectors[0].rank == 1
    assert sectors[0].value == max(p.value for p in sectors) and sectors[-1].value == min(p.value for p in sectors)
    assert all(p.value != 0 for p in sectors)  # untraded industries are left out
    assert by[("sector.change_pct", "ذغال سنگ")].value == pytest.approx(2.9766)
    assert not any("شاخص" in (p.subject or "") or any(c.isdigit() for c in (p.subject or "")) for p in sectors)


def test_real_farabourse_overview():
    res = TseFarabourseSource().parse((FX / "tse_overview_2_sample.json").read_bytes())
    by = {p.metric: p.value for p in res.points}
    assert by["index.fara.value"] == 71618.95 and by["market.trading_day"] == 20261117
    assert by["index.fara.change_pct"] == pytest.approx(286.62 / (71618.95 - 286.62) * 100)


@pytest.mark.parametrize("raw", [b"<html>", b'{"indexB1": []}', b'{"marketOverview": {"indexLastValue": 0}}'])
def test_changed_site_is_reported(raw):
    for src in (TseIndicesSource(), TseFarabourseSource()):
        with pytest.raises(ParseError):
            src.parse(raw)
