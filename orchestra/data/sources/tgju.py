"""tgju.org live prices (ajax.json): free-market dollar and euro, gold, gold coin, world gold ounce.

Shape (checked against a real answer, 2026-11-17):
    {"current": {"price_dollar_rl": {"p": "2,679,000", "h": ..., "l": ..., "d": "8,050", "dp": 0.3, "dt": "low",
                                     "ts": "2026-11-17 13:48:37"}, ...}, ...}
`p` is the price in rial (the ounce in US dollars), `dp` the change in percent, `dt` its direction ("high" = up,
"low" = down). Prices are stored in toman, as everywhere else; the exact page text is kept with each value.
"""
from __future__ import annotations

import json
import re

from .base import ParsedPoint, ParseError, ParseResult, decimals_of

# key on tgju, our metric, unit of `p`
ITEMS = (
    ("price_dollar_rl", "fx.usd", "IRR"),
    ("price_eur", "fx.eur", "IRR"),
    ("geram18", "gold.gram18", "IRR"),
    ("sekee", "coin.emami", "IRR"),
    ("ons", "gold.ounce", "USD"),
)
REQUIRED = {"fx.usd", "gold.gram18", "coin.emami"}
_NUMBER = re.compile(r"^\d{1,3}(,\d{3})*(\.\d+)?$|^\d+(\.\d+)?$")


class TgjuSource:
    source_id = "tgju"
    parser_version = "tgju_ajax@1"
    url = "https://call1.tgju.org/ajax.json"

    def parse(self, raw: bytes) -> ParseResult:
        try:
            current = json.loads(raw.decode("utf-8", errors="replace"))["current"]
        except (ValueError, KeyError, TypeError) as e:
            raise ParseError("پاسخ tgju شکل همیشگی را ندارد؛ احتمالاً ساختار سایت عوض شده است.") from e
        points, warnings = [], []
        for key, metric, unit in ITEMS:
            item = current.get(key)
            if not isinstance(item, dict):
                if metric in REQUIRED:
                    raise ParseError(f"tgju: قیمت «{key}» در پاسخ نبود")
                warnings.append(f"tgju: «{key}» نبود")
                continue
            text = str(item.get("p", "")).strip()
            if not _NUMBER.match(text):
                raise ParseError(f"tgju: قیمت «{key}» عدد نیست: «{text[:20]}»")
            price = float(text.replace(",", ""))
            if price <= 0:
                raise ParseError(f"tgju: قیمت «{key}» صفر است")
            if unit == "IRR":
                points.append(ParsedPoint(f"{metric}.price", price / 10, "IRT", 0, text))  # rial -> toman
            else:
                points.append(ParsedPoint(f"{metric}.price", price, unit, decimals_of(text), text))
            try:
                pct = abs(float(item.get("dp") or 0))
            except (TypeError, ValueError):
                warnings.append(f"tgju: درصد تغییر «{key}» خوانده نشد")
                continue
            sign = -1 if item.get("dt") == "low" else 1
            points.append(ParsedPoint(f"{metric}.change_pct", sign * pct, "percent", 2,
                                      f"{'-' if sign < 0 and pct else ''}{item.get('dp')}%"))
        return ParseResult(points=points, warnings=warnings)
