"""Fixed slide layouts: the same slides, titles, number tiles and charts every day, chosen by code.

For a report type with a layout, the writer no longer designs the carousel. It writes only the short sentences of
each slide; titles, tiles (label and token) and charts come from here, so the daily post looks the same every day and
the reader finds each figure in the same place. Slides whose data is missing today are left out, never invented.
"""
from __future__ import annotations

from dataclasses import dataclass

from .numbers import BoundPoint


@dataclass(frozen=True)
class SlotStat:
    label: str
    token: str  # "{{P3|int}}"


@dataclass(frozen=True)
class Slot:
    id: str  # "slide2": the position, so checker feedback ("slide2.bullet1") points at it
    title: str
    stats: tuple[SlotStat, ...] = ()
    chart: str = ""
    bullets: tuple[str, ...] = ()  # fixed sentences (the cover's date); the writer adds none to these
    sentences: int = 0  # how many short sentences the writer adds
    hint: str = ""  # what the sentences are about


COVER_TITLE = "گزارش پایان بازار"


def _tok(p: BoundPoint, fmt: str | None = None) -> str:
    return f"{{{{{p.key}|{fmt if fmt in p.formats else p.formats[0]}}}}}"


def market_layout(points: tuple[BoundPoint, ...], chart_ids: set[str]) -> tuple[Slot, ...] | None:
    """The market-close carousel. None when the report is not a market report (no index value)."""
    first: dict[str, BoundPoint] = {}
    groups: dict[str, list[BoundPoint]] = {}
    for p in points:
        if p.metric.startswith("real_money_net.group."):
            groups.setdefault(p.metric, []).append(p)
        else:
            first.setdefault(p.metric, p)
    index = first.get("index.total.value")
    if index is None:
        return None
    plan: list[Slot] = []

    date = first.get("run.date")
    plan.append(Slot("cover", COVER_TITLE, bullets=(_tok(date, "name"),) if date else ()))

    technical = [first[m] for m in ("technical.level.support", "technical.level.resistance", "technical.rsi.14") if m in first]
    stats = [SlotStat("شاخص کل", _tok(index, "int"))]
    if "index.total.change_pct" in first:
        stats.append(SlotStat("تغییر امروز", _tok(first["index.total.change_pct"], "signed_pct")))
    if "index.total.change" in first:
        stats.append(SlotStat("تغییر (واحد)", _tok(first["index.total.change"], "int")))
    trend_here = "price_trend" in chart_ids and not technical
    plan.append(Slot("", "شاخص کل بورس", tuple(stats[:2] if trend_here else stats), "price_trend" if trend_here else "",
                     sentences=1 if trend_here else 2, hint="حرکت شاخص کل امروز و اندازهٔ آن"))

    stats = []
    if "retail.trade_value" in first:
        stats.append(SlotStat("ارزش معاملات خرد", _tok(first["retail.trade_value"], "money")))
    net = first.get("retail.real_money_net")
    if net is not None:
        stats.append(SlotStat("خروج پول حقیقی" if net.value < 0 else "ورود پول حقیقی", _tok(net, "abs_money")))
    if "market.flow_intensity_pct" in first:
        stats.append(SlotStat("شدت جابه‌جایی", _tok(first["market.flow_intensity_pct"], "abs_pct")))
    if stats:
        plan.append(Slot("", "پول حقیقی و معاملات خرد", tuple(stats), sentences=2,
                         hint="جهت و شدت جریان پول حقیقی و ارزش معاملات خرد"))

    top_in = groups.get("real_money_net.group.top_in", [])[:1]
    top_out = groups.get("real_money_net.group.top_out", [])[:1]
    if "groups_flow" in chart_ids or top_in or top_out:
        stats = [SlotStat(f"ورود: {p.subject}"[:40], _tok(p, "abs_money")) for p in top_in]
        stats += [SlotStat(f"خروج: {p.subject}"[:40], _tok(p, "abs_money")) for p in top_out]
        chart = "groups_flow" if "groups_flow" in chart_ids else ""
        plan.append(Slot("", "ورود و خروج پول در گروه‌ها", tuple(stats), chart, sentences=1 if chart else 2,
                         hint="کدام گروه‌ها بیشترین ورود و خروج پول حقیقی را داشتند"))

    if technical:
        stats = [SlotStat(label, _tok(p)) for label, p in zip(
            [{"technical.level.support": "نزدیک‌ترین حمایت", "technical.level.resistance": "نزدیک‌ترین مقاومت",
              "technical.rsi.14": "RSI چهارده‌روزه"}[p.metric] for p in technical], technical)]
        chart = "price_trend" if "price_trend" in chart_ids else ""
        plan.append(Slot("", "تحلیل تکنیکال شاخص کل", tuple(stats[:2] if chart else stats), chart,
                         sentences=1 if chart else 2, hint="جایگاه شاخص نسبت به حمایت، مقاومت و میانگین‌ها"))

    fx = [(label, first[m]) for m, label in (("fx.usd.price", "دلار آزاد"), ("gold.gram18.price", "طلای ۱۸ عیار (گرم)"),
                                              ("coin.emami.price", "سکهٔ امامی"), ("gold.ounce.price", "انس جهانی (دلار)"))
          if m in first]
    if fx:
        plan.append(Slot("", "دلار، طلا و سکه", tuple(SlotStat(label, _tok(p)) for label, p in fx), sentences=1,
                         hint="جهت حرکت دلار، طلا و سکه امروز (قیمت لحظه‌ای، بدون پیش‌بینی)"))

    if len(plan) < 3:
        return None  # too little data for the fixed shape: the writer lays out what there is
    return tuple(Slot(f"slide{i}", s.title, s.stats, s.chart, s.bullets, s.sentences, s.hint) for i, s in enumerate(plan, 1))
