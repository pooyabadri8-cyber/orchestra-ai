"""What every data source provides: a URL to fetch and a pure parser from raw bytes to values."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
SCALE = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3, "": 1.0}


class ParseError(Exception):
    """The page could be fetched but does not look like what the parser expects (the site probably changed)."""


@dataclass(frozen=True)
class ParsedPoint:
    metric: str
    value: float  # in the base unit (toman, point, percent)
    unit: str
    decimals: int
    source_text: str
    scale: str = ""
    subject: str | None = None
    rank: int | None = None


@dataclass
class ParseResult:
    points: list[ParsedPoint]
    warnings: list[str] = field(default_factory=list)


class Source(Protocol):
    source_id: str
    parser_version: str
    url: str

    def parse(self, raw: bytes) -> ParseResult: ...


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(FA_DIGITS).replace("‌", " ")).strip()


def decimals_of(number_text: str) -> int:
    return len(number_text.split(".", 1)[1]) if "." in number_text else 0
