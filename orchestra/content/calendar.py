"""Jalali (Persian) dates, computed in code so a date in a post is bound data, not something the model types."""
from __future__ import annotations

from datetime import date

MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # date.weekday() order
FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_jalali(d: date) -> tuple[int, int, int]:
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
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


def long_date(d: date) -> str:
    """'سه‌شنبه ۲۰ آبان ۱۴۰۵'"""
    y, m, day = to_jalali(d)
    return f"{WEEKDAYS[d.weekday()]} {str(day).translate(FA)} {MONTHS[m - 1]} {str(y).translate(FA)}"
