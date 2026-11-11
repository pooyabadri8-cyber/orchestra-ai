"""bourse-trader.ir home page (server-rendered HTML).

Amounts on the page are in toman with a scale suffix (T = همت, B = میلیارد, M, K) and are rounded to one decimal,
so every point keeps the precision the page gave (`decimals`) and the exact text it was read from.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import SCALE, ParsedPoint, ParseError, ParseResult, decimals_of, norm

_AMOUNT = re.compile(r"^(-?)\s*([\d,]*\.?\d+)\s*([TBMK]?)$")
_INDEX = re.compile(r"^([\d,]*\.?\d+)\s*\(\s*(-?)\s*([\d,]*\.?\d+)\s*\)\s*(-?)\s*([\d.]+)\s*%$")

LISTS = [
    # (first header, second header, metric, label used in warnings)
    ("نماد", "ورود پول", "real_money_net.stock.top_in", "بیشترین ورود پول حقیقی به سهم"),
    ("نماد", "خروج پول", "real_money_net.stock.top_out", "بیشترین خروج پول حقیقی از سهم"),
    ("گروه", "ورود پول", "real_money_net.group.top_in", "بیشترین ورود پول حقیقی به گروه"),
    ("گروه", "خروج پول", "real_money_net.group.top_out", "بیشترین خروج پول حقیقی از گروه"),
]


def money(text: str, metric: str, subject: str | None = None, rank: int | None = None) -> ParsedPoint:
    raw = norm(text).replace("−", "-")
    m = _AMOUNT.match(raw)
    if not m:
        raise ParseError(f"مقدار پولی نامعتبر: «{raw[:30]}»")
    sign, num, scale = m.groups()
    value = float(num.replace(",", "")) * SCALE[scale] * (-1 if sign else 1)
    return ParsedPoint(metric=metric, value=value, unit="IRT", decimals=decimals_of(num), source_text=raw,
                       scale=scale, subject=subject, rank=rank)


class BourseTraderSource:
    source_id = "bourse_trader"
    parser_version = "bourse_trader@1"
    url = "https://bourse-trader.ir/"

    def parse(self, raw: bytes) -> ParseResult:
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
        table = soup.find("table", class_="marketTable")
        if table is None:
            raise ParseError("جدول «گزارش بازار» پیدا نشد؛ احتمالاً ساختار سایت عوض شده است.")
        rows = {}
        for tr in table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) >= 2:
                rows[norm(tds[0].get_text(" "))] = tds[1]

        points: list[ParsedPoint] = []
        warnings: list[str] = []

        if "شاخص کل" not in rows:
            raise ParseError("ردیف «شاخص کل» پیدا نشد.")
        points += self._index(rows["شاخص کل"])

        if "معاملات خرد" not in rows:
            raise ParseError("ردیف «معاملات خرد» پیدا نشد.")
        labeled = self._labeled(rows["معاملات خرد"])
        if "معاملات" not in labeled or "ورود پول" not in labeled:
            raise ParseError("ارزش معاملات یا ورود پول خرد پیدا نشد.")
        points.append(money(labeled["معاملات"], "retail.trade_value"))
        points.append(money(labeled["ورود پول"], "retail.real_money_net"))

        for first, second, metric, label in LISTS:
            t = self._find_table(soup, first, second)
            found = self._rows(t, metric) if t is not None else []
            if not found:
                warnings.append(f"بخش «{label}» در صفحه پیدا نشد.")
            points += found
        return ParseResult(points=points, warnings=warnings)

    # ------------------------------------------------------------------ pieces

    @staticmethod
    def _index(td) -> list[ParsedPoint]:
        text = norm(td.get_text(" "))
        m = _INDEX.match(text)
        if not m:
            raise ParseError(f"قالب شاخص کل شناخته نشد: «{text[:40]}»")
        value, csign, change, psign, pct = m.groups()
        span = td.find("span")
        down = bool(csign or psign or (span is not None and "text-danger" in (span.get("class") or [])))
        sign = -1 if down else 1  # the site omits the minus sign and only colours the span red
        change_text = f"{'-' if down else ''}{change}"
        pct_text = f"{'-' if down else ''}{pct}%"
        return [
            ParsedPoint("index.total.value", float(value.replace(",", "")), "point", decimals_of(value), value),
            ParsedPoint("index.total.change", sign * float(change.replace(",", "")), "point", decimals_of(change), change_text),
            ParsedPoint("index.total.change_pct", sign * float(pct), "percent", decimals_of(pct), pct_text),
        ]

    @staticmethod
    def _labeled(td) -> dict[str, str]:
        out = {}
        for small in td.find_all("small"):
            box = small.find_parent("div")
            bold = box.find(class_="fw-bold") if box else None
            if bold is not None:
                out[norm(small.get_text())] = norm(bold.get_text())
        return out

    @staticmethod
    def _find_table(soup, first: str, second: str):
        for t in soup.find_all("table"):
            head = t.find("thead")
            row = head.find("tr") if head else t.find("tr")
            cells = [norm(c.get_text(" ")) for c in row.find_all(["th", "td"])] if row else []
            if len(cells) >= 2 and cells[0] == first and cells[1] == second:
                return t
        return None

    @staticmethod
    def _rows(table, metric: str) -> list[ParsedPoint]:
        out = []
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            name = norm(tds[0].get_text(" "))
            if not name:
                continue
            try:
                out.append(money(tds[1].get_text(), metric, subject=name, rank=len(out) + 1))
            except ParseError:
                continue
        return out
