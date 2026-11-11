"""Number binding: Persian formatting, claims, and the unbound-figure scanner."""
import pytest

from orchestra.content.numbers import BindingError, BoundPoint, format_point, render, scan

P = {
    "P1": BoundPoint(key="P1", point_id="dp_a", metric="index.total.value", label="شاخص کل", value=7_976_400, unit="point", decimals=0),
    "P2": BoundPoint(key="P2", point_id="dp_b", metric="index.total.change_pct", label="درصد تغییر", value=-2.394, unit="percent", decimals=3),
    "P3": BoundPoint(key="P3", point_id="dp_c", metric="retail.trade_value", label="ارزش معاملات خرد", value=18.6e12, unit="IRT", decimals=1, scale="T"),
    "P4": BoundPoint(key="P4", point_id="dp_d", metric="real_money_net.group.top_out", label="خروج پول", value=-28.6e9, unit="IRT",
                     decimals=1, scale="B", subject="سیمان", formats=("abs_money", "name")),
    "P5": BoundPoint(key="P5", point_id="dp_e", metric="retail.real_money_net", label="ورود پول", value=4e12, unit="IRT", decimals=0, scale="T"),
}


@pytest.mark.parametrize("key,fmt,expected", [
    ("P1", "int", "۷٬۹۷۶٬۴۰۰"),
    ("P2", "pct", "−۲٫۳۹٪"),
    ("P2", "abs_pct", "۲٫۳۹٪"),
    ("P2", "signed_pct", "−۲٫۳۹٪"),
    ("P3", "money", "۱۸٫۶ همت"),
    ("P5", "money", "۴ همت"),
    ("P4", "abs_money", "۲۸٫۶ میلیارد تومان"),
    ("P4", "name", "سیمان"),
])
def test_persian_formats(key, fmt, expected):
    assert format_point(P[key], fmt) == expected


def test_precision_is_never_invented():
    assert "۱۸٫۶۰" not in format_point(P["P3"], "money") and format_point(P["P5"], "money") == "۴ همت"


def test_render_produces_claims_and_clean_scan():
    text = "شاخص کل به {{P1|int}} واحد رسید و ارزش معاملات خرد {{P3}} بود؛ بیشترین خروج پول از گروه {{P4|name}} ({{P4|abs_money}})."
    out, claims, spans = render(text, P)
    assert out == "شاخص کل به ۷٬۹۷۶٬۴۰۰ واحد رسید و ارزش معاملات خرد ۱۸٫۶ همت بود؛ بیشترین خروج پول از گروه سیمان (۲۸٫۶ میلیارد تومان)."
    assert [c.point_id for c in claims] == ["dp_a", "dp_c", "dp_d", "dp_d"]
    assert scan(out, spans) == []


@pytest.mark.parametrize("bad", [
    "شاخص {{P9|int}} بود",          # unknown key
    "شاخص {{P1|wow}} بود",          # unknown format
    "مبلغ {{P1|money}}",            # not money
    "گروه {{P4|money}}",            # format not allowed for this point
    "متن {{P1|int",                  # broken token
])
def test_bad_tokens_are_refused(bad):
    with pytest.raises(BindingError):
        render(bad, P)


@pytest.mark.parametrize("sneaky", [
    "شاخص کل ۲ درصد رشد کرد",
    "شاخص کل 2.5% رشد کرد",
    "ارزش معاملات به ۱۹٫۱ همت رسید",
    "ورود پول سه همت بود",
    "حدود دو و نیم درصد افت",
    "۱۴۰۵/۰۸/۲۰",
])
def test_scanner_catches_figures_the_writer_typed(sneaky):
    text = "{{P1|int}} " + sneaky
    out, _, spans = render(text, P)
    assert scan(out, spans), sneaky


def test_scanner_allows_ordinary_words():
    out, _, spans = render("یک روز پرنوسان؛ دو گروه پیشتاز بودند. شاخص {{P1|int}} واحد شد.", P)
    assert scan(out, spans) == []
