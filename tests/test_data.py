"""Data acquisition from a real Bourse Trader page: every value, its precision, its raw text, and traceability."""
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from orchestra.data.acquisition import AcquisitionError, acquire
from orchestra.data.sources.base import ParseError
from orchestra.data.sources.bourse_trader import BourseTraderSource
from orchestra.db import DataPointRow, DataSnapshot

PAGE = (Path(__file__).parent / "fixtures" / "bourse_trader_sample.html").read_bytes()


def by(points, metric, rank=None):
    return next(p for p in points if p.metric == metric and p.rank == rank)


def test_real_page_values():
    r = BourseTraderSource().parse(PAGE)
    pts = r.points
    assert by(pts, "index.total.value").value == 7_976_400
    assert by(pts, "index.total.change").value == 186_492
    assert by(pts, "index.total.change_pct").value == pytest.approx(2.394)
    trade = by(pts, "retail.trade_value")
    assert trade.value == pytest.approx(18.6e12) and (trade.decimals, trade.scale, trade.source_text) == (1, "T", "18.6T")
    assert by(pts, "retail.real_money_net").value == pytest.approx(4e12)
    top_in = [p for p in pts if p.metric == "real_money_net.stock.top_in"]
    assert [p.subject for p in top_in] == ["فولاد", "دوایکس", "فملی", "تاصیکو"] and top_in[0].value == pytest.approx(510.4e9)
    out_groups = [p for p in pts if p.metric == "real_money_net.group.top_out"]
    assert out_groups[0].subject == "صندوق درآمد ثابت" and out_groups[0].value == pytest.approx(-3.1e12)
    assert not r.warnings


def test_falling_market_sign_comes_from_the_colour():
    page = PAGE.replace(b'<span class="text-success">(186,492) 2.394%</span>', b'<span class="text-danger">(186,492) 2.394%</span>')
    pts = BourseTraderSource().parse(page).points
    assert by(pts, "index.total.change").value == -186_492 and by(pts, "index.total.change_pct").value == pytest.approx(-2.394)


def test_changed_layout_is_an_error_not_a_guess():
    with pytest.raises(ParseError):
        BourseTraderSource().parse(b"<html><body>maintenance</body></html>")
    page = PAGE.replace("گروه".encode(), "صنعت".encode())
    r = BourseTraderSource().parse(page)
    assert len(r.warnings) == 2 and not [p for p in r.points if "group" in p.metric]


def test_acquire_stores_raw_bytes_and_traceable_points(sessions, assets, clock):
    async def fetch(url):
        return PAGE
    with sessions.begin() as s:
        got = asyncio.run(acquire(s, assets, BourseTraderSource(), fetch, clock()))
    with sessions() as s:
        snap = s.get(DataSnapshot, got.snapshot_id)
        assert assets.get(snap.raw_sha256) == PAGE  # the exact bytes every number came from
        rows = s.execute(select(DataPointRow).where(DataPointRow.snapshot_id == snap.id)).scalars().all()
        assert len(rows) == len(got.point_ids) == 5 + 16
        assert all(r.as_of == clock() for r in rows)


def test_acquire_is_repeatable_and_failures_are_classified(sessions, assets, clock):
    async def fetch(url):
        return PAGE
    with sessions.begin() as s:
        a = asyncio.run(acquire(s, assets, BourseTraderSource(), fetch, clock()))
    with sessions.begin() as s:
        b = asyncio.run(acquire(s, assets, BourseTraderSource(), fetch, clock()))
    assert a == b  # same fetch time and bytes: same snapshot, no duplicates

    async def broken(url):
        return b"<html>changed</html>"
    with sessions.begin() as s, pytest.raises(AcquisitionError) as e:
        asyncio.run(acquire(s, assets, BourseTraderSource(), broken, clock()))
    assert e.value.retryable is False
