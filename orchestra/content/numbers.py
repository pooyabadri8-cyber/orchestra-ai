"""Numbers are bound, not written.

The writer never types a figure. It writes tokens such as {{P3|money}} that point at a data point; this module
replaces each token with the value formatted in Persian, records a Claim for it, and then scans the result: any
numeral that did not come from a token is an unbound figure and blocks the draft.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
THOUSANDS, DECIMAL, MINUS = "٬", "٫", "−"
SCALE_VALUE = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3, "": 1.0}
SCALE_WORD = {"T": "همت", "B": "میلیارد تومان", "M": "میلیون تومان", "K": "هزار تومان", "": "تومان"}

FORMATS = ("money", "abs_money", "int", "num", "signed", "pct", "abs_pct", "signed_pct", "name", "rial", "ratio", "price")

TOKEN = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:\|\s*([a-z_]+)\s*)?\}\}")


class BindingError(Exception):
    pass


class BoundPoint(BaseModel):
    """What the writer may refer to. `key` is short (P1, P2...) so the model can copy it reliably."""

    model_config = ConfigDict(frozen=True)
    key: str
    point_id: str
    metric: str
    label: str  # Persian description shown to the writer
    value: float
    unit: str
    decimals: int
    scale: str = ""
    subject: str | None = None
    formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class Claim:
    claim_id: str
    key: str
    point_id: str
    fmt: str
    rendered: str


def fa_number(value: float, decimals: int) -> str:
    text = f"{abs(value):,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".") if decimals else text
    return text.replace(",", "\x00").replace(".", DECIMAL).replace("\x00", THOUSANDS).translate(FA)


def _auto_scale(toman: float) -> tuple[str, int]:
    a = abs(toman)
    for scale in ("T", "B", "M", "K"):
        if a >= SCALE_VALUE[scale]:
            return scale, 1
    return "", 0


def format_point(p: BoundPoint, fmt: str) -> str:
    if fmt not in FORMATS:
        raise BindingError(f"قالب ناشناخته «{fmt}»")
    if p.formats and fmt not in p.formats:
        raise BindingError(f"قالب «{fmt}» برای {p.key} مجاز نیست (مجاز: {', '.join(p.formats)})")
    if fmt == "name":
        if not p.subject:
            raise BindingError(f"{p.key} نام ندارد")
        return p.subject
    if fmt in ("money", "abs_money"):
        if p.unit not in ("IRT", "IRR"):
            raise BindingError(f"{p.key} مبلغ نیست")
        toman = p.value / 10 if p.unit == "IRR" else p.value
        scale, decimals = (p.scale, p.decimals) if p.scale else _auto_scale(toman)
        shown = toman / SCALE_VALUE[scale]
        if fmt == "money" and p.value < 0:
            return f"{MINUS}{fa_number(shown, decimals)} {SCALE_WORD[scale]}"
        return f"{fa_number(shown, decimals)} {SCALE_WORD[scale]}"
    if fmt == "price":  # a market price, written in full: «۲۶۷٬۹۰۰ تومان»
        if p.unit not in ("IRT", "IRR"):
            raise BindingError(f"{p.key} قیمت تومانی نیست")
        return f"{fa_number(p.value / 10 if p.unit == 'IRR' else p.value, 0)} تومان"
    if fmt == "rial":
        if p.unit != "IRR":
            raise BindingError(f"{p.key} مبلغ ریالی نیست")
        return f"{MINUS if p.value < 0 else ''}{fa_number(p.value, p.decimals)} ریال"
    if fmt in ("num", "ratio"):
        if p.unit in ("IRT", "IRR", "date"):
            raise BindingError(f"{p.key} عدد سادهٔ بدون واحد نیست")
        return f"{MINUS if p.value < 0 else ''}{fa_number(p.value, p.decimals)}"
    if fmt == "int":
        if p.value < 0:
            return f"{MINUS}{fa_number(p.value, 0)}"
        return fa_number(p.value, 0)
    if fmt == "signed":
        sign = "+" if p.value > 0 else MINUS if p.value < 0 else ""
        return f"{sign}{fa_number(p.value, p.decimals)}"
    if p.unit != "percent":
        raise BindingError(f"{p.key} درصد نیست")
    body = f"{fa_number(p.value, min(p.decimals, 2))}٪"
    if fmt == "abs_pct":
        return body
    if fmt == "signed_pct":
        return ("+" if p.value > 0 else MINUS if p.value < 0 else "") + body
    return (MINUS if p.value < 0 else "") + body


def render(text: str, points: dict[str, BoundPoint], field_name: str = "") -> tuple[str, list[Claim], list[tuple[int, int]]]:
    """Replace every token. Returns the text, the claims, and the character spans that came from tokens."""
    out, claims, spans, pos = [], [], [], 0
    length = 0
    for m in TOKEN.finditer(text):
        key, fmt = m.group(1), m.group(2) or "money"
        if key not in points:
            raise BindingError(f"ارجاع به دادهٔ ناموجود: {m.group(0)}")
        piece = text[pos:m.start()]
        out.append(piece)
        length += len(piece)
        rendered = format_point(points[key], fmt)
        spans.append((length, length + len(rendered)))
        out.append(rendered)
        length += len(rendered)
        cid = "clm_" + hashlib.sha256(f"{field_name}|{len(claims)}|{key}|{fmt}".encode()).hexdigest()[:16]
        claims.append(Claim(cid, key, points[key].point_id, fmt, rendered))
        pos = m.end()
    out.append(text[pos:])
    result = "".join(out)
    if "{{" in result or "}}" in result:
        raise BindingError("توکن نیمه‌کاره در متن مانده است")
    return result, claims, spans


# ------------------------------------------------------------------ the scanner

DIGIT = r"[0-9۰-۹٠-٩]"
NUMERAL = re.compile(rf"{DIGIT}+(?:[.,٫٬]{DIGIT}+)*")
NUMBER_WORDS = (
    "یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده|یازده|دوازده|سیزده|چهارده|پانزده|شانزده|هفده|هجده|نوزده|بیست|سی|چهل|"
    "پنجاه|شصت|هفتاد|هشتاد|نود|صد|دویست|سیصد|چهارصد|پانصد|ششصد|هفتصد|هشتصد|نهصد|هزار|نیم|ربع|دهها|صدها|هزاران"
)
UNIT_WORDS = "درصد|همت|میلیارد|میلیون|هزار|تومان|ریال|دلار|واحد|برابر|میلیاردی|میلیونی"
WORD_FIGURE = re.compile(rf"(?<![\w؀-ۿ])(?:{NUMBER_WORDS})(?:\s+و\s+(?:{NUMBER_WORDS}))*\s*‌?\s*(?:{UNIT_WORDS})(?![\w؀-ۿ])")


@dataclass(frozen=True)
class Unbound:
    text: str
    start: int


# Numbers that are part of a name, not a figure: "طلای ۱۸ عیار", "طلای ۲۴ عیار". Only these exact names pass.
NAME_NUMBERS = re.compile(r"(?<![0-9۰-۹٠-٩])(?:18|24|۱۸|۲۴|١٨|٢٤)(?=\s*‌?\s*عیار)")


def scan(text: str, token_spans: list[tuple[int, int]]) -> list[Unbound]:
    """Every numeral or written-out figure that is not inside a span produced by a token (names such as
    «۱۸ عیار» excepted)."""
    names = [(m.start(), m.end()) for m in NAME_NUMBERS.finditer(text)]

    def covered(a: int, b: int) -> bool:
        return any(s <= a and b <= e for s, e in token_spans) or (a, b) in names

    found = []
    for m in NUMERAL.finditer(text):
        if not covered(m.start(), m.end()):
            found.append(Unbound(m.group(0), m.start()))
    for m in WORD_FIGURE.finditer(text):
        if not covered(m.start(), m.end()):
            found.append(Unbound(m.group(0), m.start()))
    return sorted(found, key=lambda u: u.start)
