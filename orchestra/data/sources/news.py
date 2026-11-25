"""News sources for the research agent: RSS/Atom feeds and public Telegram channels (t.me/s/<channel>).

Parsers are pure functions from raw bytes to items (ported from V3's agent/sources.py, which ran on real feeds and
channels). An item's text is evidence, never instructions.

`figures()` finds every figure in an item's text, with the unit word that follows it («۸۰ دلار», «۲٫۵ درصد»,
«۳۰۰ میلیون بشکه»), so the writer can cite it only through a token that renders exactly what the source said, unit
included, and every number in a news post traces back to the sentence and the bytes it came from.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from .base import FA_DIGITS, ParseError

PARSER_FEED = "news_feed@1"
PARSER_TELEGRAM = "telegram_preview@1"
MAX_TEXT = 1500  # characters of an item kept as evidence


@dataclass(frozen=True)
class NewsItem:
    ident: str  # stable per source: the feed's guid/link, or the channel post id
    title: str
    text: str
    url: str
    published: datetime | None


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"]):
        blk.append("\n")  # block elements end a line; inline tags (b, a, span) must not split text
    text = soup.get_text("")
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind("."), cut.rfind("۔"), cut.rfind("\n"))
    return (cut[:end + 1] if end > limit * 0.6 else cut).strip()


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for parse in (parsedate_to_datetime, lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))):
        try:
            dt = parse(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def parse_feed(raw: bytes) -> list[NewsItem]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ParseError(f"فید معتبر نیست: {e}") from e

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        for ch in el:
            n = local(ch.tag)
            if n == "link":
                d.setdefault("link", (ch.get("href") or ch.text or "").strip())
            elif n in ("title", "guid", "id", "pubDate", "updated", "published", "description", "summary", "encoded", "content"):
                d.setdefault(n, (ch.text or "").strip())
        title = html_to_text(d.get("title", ""))
        text = _clip(html_to_text(d.get("encoded") or d.get("content") or d.get("description") or d.get("summary") or ""))
        if not title:
            continue
        url = d.get("link", "")
        items.append(NewsItem(ident=d.get("guid") or d.get("id") or url or title, title=title[:200], text=text or title,
                              url=url, published=_date(d.get("pubDate") or d.get("published") or d.get("updated"))))
    if not items and local(root.tag) not in ("rss", "feed", "RDF"):
        raise ParseError("پاسخ شکل فید RSS یا Atom را ندارد")
    return items


def parse_telegram(raw: bytes, min_chars: int = 40) -> list[NewsItem]:
    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
    posts = soup.select("div.tgme_widget_message[data-post]")
    if not posts and not soup.select_one(".tgme_channel_info, .tgme_page"):
        raise ParseError("صفحهٔ کانال تلگرام شکل همیشگی را ندارد (کانال خصوصی است یا نشانی اشتباه است)")
    items = []
    for msg in posts:
        body = msg.select_one(".tgme_widget_message_text")
        if not body:
            continue
        text = html_to_text(str(body))
        if len(text) < min_chars:
            continue
        t = msg.select_one("time[datetime]")
        post_id = msg["data-post"]  # "channel/123"
        items.append(NewsItem(ident=post_id, title=text.split("\n", 1)[0][:120], text=_clip(text),
                              url=f"https://t.me/{post_id}", published=_date(t["datetime"]) if t else None))
    return items


PARSERS = {PARSER_FEED: parse_feed, PARSER_TELEGRAM: parse_telegram}


def telegram_url(channel: str) -> str:
    return f"https://t.me/s/{channel.lstrip('@')}"


def item_id(source_id: str, ident: str) -> str:
    return "ev_" + hashlib.sha256(f"{source_id}|{ident}".encode()).hexdigest()[:24]


# ------------------------------------------------------------------ figures in a text

_D = r"[0-9۰-۹٠-٩]"
UNITS = ("درصد", "٪", "%", "دلار", "یورو", "درهم", "یوان", "تومان", "ریال", "همت", "هزار", "میلیون", "میلیارد", "هزارمیلیارد",
         "تریلیون", "بشکه", "تن", "اونس", "انس", "واحد", "برابر", "سنت", "گرم", "مثقال", "کیلوگرم", "مگاوات", "متر")
_UNIT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
FIGURE = re.compile(rf"(?<![\w{_D[1:-1]}])[-−+]?{_D}+(?:[.,٫٬/]{_D}+)*(?:\s*(?:{_UNIT})(?![\w])){{0,3}}")
_LATIN = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_END = re.compile(r"[.!?؟](?=\s|$)|\n|؛")  # a sentence ends here (a decimal point is not an end)


def _value(text: str) -> float:
    m = re.match(r"[-−+]?[0-9.,٫٬/]+", text.translate(FA_DIGITS).replace("−", "-"))
    t = (m.group(0) if m else "0").replace(",", "").replace("٬", "").replace("٫", ".").split("/")[0]
    try:
        return float(t)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class Figure:
    text: str  # as the source wrote it, digits in Persian: «۸۰ دلار»
    value: float  # the leading number, for records only; posts show `text`
    start: int
    end: int
    sentence: str  # the sentence it appears in, shown to the writer as its label


def figures(text: str, limit: int = 12) -> list[Figure]:
    out = []
    for m in FIGURE.finditer(text):
        raw = m.group(0).strip()
        shown = re.sub(r"\s+", " ", raw).translate(_LATIN).replace("%", "٪").replace("−", "-")
        shown = re.sub(r"(?<=[۰-۹])\.(?=[۰-۹])", "٫", re.sub(r"(?<=[۰-۹]),(?=[۰-۹]{3})", "٬", shown))
        a = max((e.end() for e in _END.finditer(text, 0, m.start())), default=0)
        nxt = _END.search(text, m.end())
        b = nxt.start() if nxt else len(text)
        sentence = " ".join(text[a:b].split())[:220]
        out.append(Figure(shown, _value(raw), m.start(), m.end(), sentence))
        if len(out) >= limit:
            break
    return out
