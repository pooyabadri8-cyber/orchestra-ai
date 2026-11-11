"""Charts and slide rendering (real headless Chromium)."""
import asyncio
import hashlib
from datetime import date, timedelta

import pytest

from orchestra.agents.multichannel import RenderedSlide
from orchestra.visual.charts import Bar, ChartSpec
from orchestra.visual.render import HEIGHT, WIDTH, RenderError, png_size, render_carousel
from orchestra.visual.svg import render_svg

from test_content import BRAND

pytest.importorskip("playwright")

BARS = ChartSpec(id="groups_flow", kind="bar_signed", title="ورود و خروج پول حقیقی گروه‌ها", unit="IRT", bars=(
    Bar(label="فلزات اساسی", value=5e11, point_id="a", display="+۵۰۰ میلیارد تومان"),
    Bar(label="خودرو", value=-2e11, point_id="b", display="−۲۰۰ میلیارد تومان")), sources=("a", "b"))
LINE = ChartSpec(id="price_trend", kind="line", title="روند", unit="point",
                 dates=tuple((date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(60)),
                 series={"مقدار پایانی": tuple(1000.0 + i for i in range(60))}, sources=("snap",))


def test_svg_is_deterministic_and_escapes_text():
    a, b = render_svg(BARS, BRAND.colors), render_svg(BARS, BRAND.colors)
    assert a == b and "فلزات اساسی" in a and "+۵۰۰ میلیارد تومان" in a
    evil = BARS.model_copy(update={"bars": (BARS.bars[0].model_copy(update={"label": "<script>"}), BARS.bars[1])})
    assert "<script>" not in render_svg(evil, BRAND.colors)
    assert "polyline" in render_svg(LINE, BRAND.colors)


def test_carousel_renders_at_instagram_size_with_closing_slide():
    slides = [RenderedSlide(title="بازار امروز", bullets=("چهارشنبه",), stats=(), kind="cover"),
              RenderedSlide(title="شاخص", bullets=("شاخص رشد کرد.",), stats=(("شاخص کل", "۲٬۸۷۴٬۵۱۲"),), chart=BARS),
              RenderedSlide(title="روند", bullets=(), stats=(), chart=LINE)]
    imgs = asyncio.run(render_carousel(BRAND, slides))
    assert [i.kind for i in imgs] == ["cover", "content", "content", "end"]
    for im in imgs:
        assert png_size(im.png) == (WIDTH, HEIGHT) and im.sha256 == hashlib.sha256(im.png).hexdigest()
    assert imgs[1].manifest["chart_sources"] == ["a", "b"] and imgs[1].chart_id == "groups_flow"


def test_text_that_cannot_fit_stops_the_run():
    long = "این یک جملهٔ بسیار طولانی است که برای پر کردن اسلاید تکرار می‌شود " * 6
    slides = [RenderedSlide(title="جلد", bullets=(), stats=(), kind="cover"),
              RenderedSlide(title="شلوغ", bullets=(long, long, long), stats=(("الف", "۱"),) * 4, chart=LINE)]
    with pytest.raises(RenderError, match="جا نشد"):
        asyncio.run(render_carousel(BRAND, slides))
