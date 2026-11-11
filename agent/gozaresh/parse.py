"""Parsing of the bourse-trader.ir home page (server-rendered HTML) into a small report structure.

Pure functions only: no network, no Telegram. The site shows amounts in Toman with suffixes
T (trillion = همت), B (billion = میلیارد), M (million), K (thousand).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup


class GozareshError(Exception):
    """The page could not be read or no longer looks like what the parser expects."""


@dataclass
class Money:
    value: float  # signed, in the unit below
    unit: str  # "T", "B", "M", "K" or ""

    @property
    def negative(self) -> bool:
        return self.value < 0

    @property
    def is_zero(self) -> bool:
        return self.value == 0


@dataclass
class Index:
    value: float
    change: float  # signed points
    pct: float  # signed percent


@dataclass
class Row:
    name: str
    amount: Money


@dataclass
class Report:
    index: Index
    retail_value: Money  # ارزش معاملات خرد
    retail_flow: Money  # ورود (+) / خروج (-) پول حقیقی در معاملات خرد
    top_in_stocks: list[Row] = field(default_factory=list)
    top_out_stocks: list[Row] = field(default_factory=list)
    top_in_groups: list[Row] = field(default_factory=list)
    top_out_groups: list[Row] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_AMOUNT = re.compile(r"^(-?)\s*([\d,]*\.?\d+)\s*([TBMK]?)$")
_INDEX = re.compile(r"^([\d,]*\.?\d+)\s*\(\s*(-?)\s*([\d,]*\.?\d+)\s*\)\s*(-?)\s*([\d.]+)\s*%$")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_DIGITS).replace("‌", " ")).strip()


def parse_money(text: str) -> Money:
    m = _AMOUNT.match(_norm(text).replace("−", "-"))
    if not m:
        raise GozareshError(f"مقدار پولی نامعتبر: «{text.strip()[:30]}»")
    sign, num, unit = m.groups()
    value = float(num.replace(",", ""))
    return Money(-value if sign else value, unit)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _parse_index(td) -> Index:
    text = _norm(td.get_text(" "))
    m = _INDEX.match(text)
    if not m:
        raise GozareshError(f"قالب شاخص کل شناخته نشد: «{text[:40]}»")
    value, csign, change, psign, pct = m.groups()
    change_v, pct_v = _num(change), float(pct)
    span = td.find("span")
    down = "text-danger" in (span.get("class") or []) if span else False
    if csign or psign or down:  # the site omits the minus sign and only colors the span red
        change_v, pct_v = -abs(change_v), -abs(pct_v)
    return Index(_num(value), change_v, pct_v)


def _header_cells(table) -> list[str]:
    head = table.find("thead")
    row = head.find("tr") if head else table.find("tr")
    return [_norm(c.get_text(" ")) for c in row.find_all(["th", "td"])] if row else []


def _find_table(soup, first: str, second: str):
    for t in soup.find_all("table"):
        h = _header_cells(t)
        if len(h) >= 2 and h[0] == first and h[1] == second:
            return t
    return None


def _rows(table, top_n: int) -> list[Row]:
    out: list[Row] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        name = _norm(tds[0].get_text(" "))
        if not name:
            continue
        try:
            out.append(Row(name, parse_money(tds[1].get_text())))
        except GozareshError:
            continue
        if len(out) >= top_n:
            break
    return out


def _market_rows(soup) -> dict[str, object]:
    table = soup.find("table", class_="marketTable")
    if table is None:
        raise GozareshError("جدول «گزارش بازار» در صفحه پیدا نشد (ظاهراً ساختار سایت عوض شده).")
    rows = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) >= 2:
            rows[_norm(tds[0].get_text(" "))] = tds[1]
    return rows


def _labeled_values(td) -> dict[str, str]:
    """`<div class=fw-bold>18.2T</div><small>معاملات</small>` pairs inside one cell -> {label: value}."""
    out = {}
    for small in td.find_all("small"):
        box = small.find_parent("div")
        bold = box.find(class_="fw-bold") if box else None
        if bold is not None:
            out[_norm(small.get_text())] = _norm(bold.get_text())
    return out


def parse_page(html: str, top_n: int = 5) -> Report:
    soup = BeautifulSoup(html, "html.parser")
    rows = _market_rows(soup)

    if "شاخص کل" not in rows:
        raise GozareshError("ردیف «شاخص کل» پیدا نشد.")
    index = _parse_index(rows["شاخص کل"])

    if "معاملات خرد" not in rows:
        raise GozareshError("ردیف «معاملات خرد» پیدا نشد.")
    vals = _labeled_values(rows["معاملات خرد"])
    if "معاملات" not in vals or "ورود پول" not in vals:
        raise GozareshError("مقدار معاملات یا ورود پول خرد در صفحه پیدا نشد.")
    retail_value = parse_money(vals["معاملات"])
    retail_flow = parse_money(vals["ورود پول"])

    report = Report(index, retail_value, retail_flow)
    for attr, first, second, label in [
        ("top_in_stocks", "نماد", "ورود پول", "بیشترین ورود پول حقیقی به سهم"),
        ("top_out_stocks", "نماد", "خروج پول", "بیشترین خروج پول حقیقی از سهم"),
        ("top_in_groups", "گروه", "ورود پول", "بیشترین ورود پول حقیقی به گروه"),
        ("top_out_groups", "گروه", "خروج پول", "بیشترین خروج پول حقیقی از گروه"),
    ]:
        table = _find_table(soup, first, second)
        items = _rows(table, top_n) if table is not None else []
        if not items:
            report.warnings.append(f"بخش «{label}» در صفحه پیدا نشد.")
        setattr(report, attr, items)
    return report
