from orchestra.agents.multichannel import MultiChannelDraft, apply_layout
from orchestra.content.layouts import market_layout
from orchestra.content.numbers import BoundPoint


def bp(key, metric, value, formats, subject=None, unit="IRT"):
    return BoundPoint(key=key, point_id=key, metric=metric, label=metric, value=value, unit=unit, decimals=0,
                      subject=subject, formats=formats)


POINTS = (
    bp("P1", "index.total.value", 3_000_000, ("int",), unit="point"),
    bp("P2", "index.total.change_pct", -0.5, ("pct", "abs_pct", "signed_pct"), unit="percent"),
    bp("P3", "retail.trade_value", 5e12, ("money",)),
    bp("P4", "retail.real_money_net", -1.9e12, ("abs_money",)),
    bp("P5", "real_money_net.group.top_in", 2.9e11, ("abs_money", "name"), subject="دارویی"),
    bp("P6", "real_money_net.group.top_out", -1.3e12, ("abs_money", "name"), subject="شیمیایی"),
    bp("P7", "gold.gram18.price", 9_000_000, ("price",)),
    BoundPoint(key="D1", point_id="d", metric="run.date", label="تاریخ", value=0, unit="date", decimals=0,
               subject="سه‌شنبه", formats=("name",)),
)


def test_same_layout_every_day():
    a = market_layout(POINTS, {"groups_flow"})
    b = market_layout(POINTS, {"groups_flow"})
    assert a == b
    assert [s.title for s in a] == ["گزارش پایان بازار", "شاخص کل بورس", "پول حقیقی و معاملات خرد",
                                    "ورود و خروج پول در گروه‌ها", "دلار، طلا و سکه"]
    assert a[0].bullets == ("{{D1|name}}",)
    assert a[2].stats[1].label == "خروج پول حقیقی"
    assert a[3].chart == "groups_flow" and a[3].sentences == 1


def test_not_a_market_report():
    assert market_layout(POINTS[6:], set()) is None


def test_writer_only_fills_sentences():
    layout = market_layout(POINTS, {"groups_flow"})
    draft = MultiChannelDraft.model_validate({"slide_text": {"slide2": ["الف", "ب", "اضافه"], "slide4": ["ج"]},
                                              "slides": [{"title": "تیتر دلخواه نویسنده"}]})
    out = apply_layout(draft, layout)
    assert [s.title for s in out.slides] == [s.title for s in layout]
    assert out.slides[1].bullets == ("الف", "ب")  # extra sentences are cut
    assert out.slides[3].bullets == ("ج",) and out.slides[3].chart == "groups_flow"
    assert out.slides[1].stats[0].value == "{{P1|int}}"
