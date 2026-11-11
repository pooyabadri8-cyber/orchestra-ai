"""Persian text of the daily report (Telegram HTML)."""
from __future__ import annotations

import html
from datetime import datetime

from .parse import Money, Report, Row

UNITS = {"T": "همت", "B": "میلیارد تومان", "M": "میلیون تومان", "K": "هزار تومان", "": "تومان"}
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
SOURCE_URL = "https://bourse-trader.ir"


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy, gy = 979, gy - 1600
    else:
        jy, gy = 0, gy - 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def _fa(text: str, persian: bool) -> str:
    return text.translate(_FA) if persian else text


def _num(value: float, decimals: int | None = None) -> str:
    """12345.6 -> '12,345.6'; whole numbers have no decimals."""
    if decimals is None:
        decimals = 0 if float(value).is_integer() else (2 if abs(value) < 100 else 1)
    text = f"{abs(value):,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def money_text(m: Money, persian: bool = True) -> str:
    """'503.6B' -> '۵۰۳.۶ میلیارد تومان' (absolute value; the caller decides how to word the sign)."""
    if m.is_zero:
        return _fa("0", persian) + " تومان"
    text = f"{_num(m.value)} {UNITS.get(m.unit, '')}"
    return _fa(text, persian).replace(",", "٬" if persian else ",")


def _signed(value: float, decimals: int, persian: bool, suffix: str = "") -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    text = f"{sign}{_num(value, decimals)}{suffix}"
    return _fa(text, persian).replace(",", "٬" if persian else ",")


def _list(rows: list[Row], persian: bool) -> str:
    lines = [
        f"{_fa(str(i), persian)}. {html.escape(r.name)}: {money_text(Money(abs(r.amount.value), r.amount.unit), persian)}"
        for i, r in enumerate(rows, 1)
    ]
    return "\n".join(lines)


def format_report(rep: Report, now: datetime, persian: bool = True) -> str:
    jy, jm, jd = gregorian_to_jalali(now.year, now.month, now.day)
    date = _fa(f"{jy}/{jm:02d}/{jd:02d}", persian)
    clock = _fa(now.strftime("%H:%M"), persian)
    idx = rep.index
    arrow = "🟢" if idx.change > 0 else "🔴" if idx.change < 0 else "⚪"
    flow = rep.retail_flow
    flow_word = "ورود" if flow.value >= 0 else "خروج"
    flow_icon = "🟢" if flow.value > 0 else "🔴" if flow.value < 0 else "⚪"

    parts = [
        "📊 <b>گزارش روزانهٔ بازار سرمایه</b>",
        f"🗓 {date} — ساعت {clock}",
        "",
        f"📈 <b>شاخص کل:</b> {_fa(_num(idx.value, 0), persian).replace(',', '٬' if persian else ',')} {arrow}",
        f"تغییر: {_signed(idx.change, 0, persian)} ({_signed(idx.pct, 2, persian, '٪' if persian else '%')})",
        "",
        f"💰 <b>ارزش معاملات خرد:</b> {money_text(rep.retail_value, persian)}",
        f"{flow_icon} <b>{flow_word} پول حقیقی (معاملات خرد):</b> "
        f"{money_text(Money(abs(flow.value), flow.unit), persian)}",
    ]
    sections = [
        ("🟢 <b>بیشترین ورود پول حقیقی</b>", "سهم", rep.top_in_stocks, "گروه", rep.top_in_groups),
        ("🔴 <b>بیشترین خروج پول حقیقی</b>", "سهم", rep.top_out_stocks, "گروه", rep.top_out_groups),
    ]
    for title, s_label, stocks, g_label, groups in sections:
        parts += ["", title]
        if stocks:
            parts += [f"<b>{s_label}</b>", _list(stocks, persian)]
        if groups:
            parts += [f"<b>{g_label}</b>", _list(groups, persian)]
    if rep.warnings:
        parts += [""] + [f"⚠️ {html.escape(w)}" for w in rep.warnings]
    if rep.retail_value.is_zero:
        parts += ["", "ℹ️ ارزش معاملات صفر است؛ احتمالاً بازار هنوز باز نشده یا امروز تعطیل است و عددها قدیمی‌اند."]
    parts += ["", f"منبع: {SOURCE_URL.replace('https://', '')}"]
    return "\n".join(parts)
