"""tgju connector and the currency-and-gold analyst, on a real answer captured on the user's computer (2026-11-17)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestra.agents.specialists import fx_part
from orchestra.content.numbers import format_point
from orchestra.data.acquisition import acquire
from orchestra.data.sources.base import ParseError
from orchestra.data.sources.tgju import TgjuSource

RAW = (Path(__file__).parent / "fixtures" / "tgju_ajax_sample.json").read_bytes()


def test_real_answer_parses_into_toman_with_signed_changes():
    pts = {p.metric: p for p in TgjuSource().parse(RAW).points}
    assert pts["fx.usd.price"].value == 267_900 and pts["fx.usd.price"].unit == "IRT" and pts["fx.usd.price"].source_text == "2,679,000"
    assert pts["fx.usd.change_pct"].value == -0.3  # "dt": "low"
    assert pts["coin.emami.price"].value == 251_985_000 and pts["coin.emami.change_pct"].value == 0.57
    assert pts["gold.gram18.price"].value == 24_638_000
    assert pts["gold.ounce.price"].value == 3857.88 and pts["gold.ounce.price"].unit == "USD"


def test_broken_answers_are_refused_not_guessed():
    src = TgjuSource()
    bad = json.loads(RAW)
    for broken in (b"<html>", b'{"x": 1}', json.dumps({"current": {k: v for k, v in bad["current"].items() if k != "sekee"}}).encode(),
                   json.dumps({"current": {**bad["current"], "price_dollar_rl": {**bad["current"]["price_dollar_rl"], "p": "?"}}}).encode()):
        with pytest.raises(ParseError):
            src.parse(broken)
    no_euro = json.dumps({"current": {k: v for k, v in bad["current"].items() if k != "price_eur"}}).encode()
    assert any("price_eur" in w for w in src.parse(no_euro).warnings)


def test_fx_part_states_directions_and_formats_prices_in_full(sessions, assets):
    async def fetch(url):
        return RAW
    import asyncio
    with sessions.begin() as s:
        got = asyncio.run(acquire(s, assets, TgjuSource(), fetch, datetime(2026, 11, 17, 10, 20, tzinfo=timezone.utc)))
        part = fx_part(s, snapshot_id=got.snapshot_id)
    usd = next(p for p in part.points if p.metric == "fx.usd.price")
    assert format_point(usd, "price") == "۲۶۷٬۹۰۰ تومان"
    coin = next(p for p in part.points if p.metric == "coin.emami.price")
    assert format_point(coin, "price") == "۲۵۱٬۹۸۵٬۰۰۰ تومان"
    pct = next(p for p in part.points if p.metric == "fx.usd.change_pct")
    assert format_point(pct, "signed_pct") == "−۰٫۳٪" or format_point(pct, "signed_pct").endswith("۰٫۳٪")
    assert "دلار آمریکا (بازار آزاد) امروز پایین آمد." in part.facts and "سکهٔ امامی امروز بالا رفت." in part.facts
    ounce = next(p for p in part.points if p.metric == "gold.ounce.price")
    assert ounce.formats == ("num",) and format_point(ounce, "num") == "۳٬۸۵۷٫۸۸"
