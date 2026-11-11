"""Fetching material from external sources.

Source types:
  rss               RSS/Atom feed
  telegram          public Telegram *channel* (read through the t.me/s web preview)
  telegram_account  any Telegram channel/group your own account can read (via Telethon, see login_telegram.py)
  web               one article page
  links             a news index/home page: article links are collected and each new article is read

Everything fetched here is untrusted data. It is only ever passed to the writer as quoted
material, and every resulting post still needs the owner's approval before publishing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger("content-agent.sources")

BOT = {"name": "ContentAgent"}  # identifies us to websites and in robots.txt; set per customer (profile.yaml bot_name)


def set_bot_name(name: str) -> None:
    BOT["name"] = name or "ContentAgent"


def user_agent() -> str:
    return f"Mozilla/5.0 (compatible; {BOT['name']}/1.0)"
MAX_TEXT = 12000  # characters of source text handed to the model (longer sources are cut at a paragraph break)
MIN_TELEGRAM_TEXT = 80  # skip one-liners / sticker captions
POLITE_DELAY = 1.0  # seconds between article requests to the same site
SOURCE_TYPES = ("rss", "telegram", "telegram_account", "web", "links")


class SourceError(Exception):
    pass


def _clip(text: str, limit: int = MAX_TEXT) -> tuple[str, bool]:
    """Cut overly long source text at a paragraph/sentence break. Returns (text, was_cut)."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    for sep in ("\n\n", "\n", ". ", "؛ ", "؟ ", "! "):
        i = cut.rfind(sep)
        if i > limit * 0.6:
            return cut[: i + len(sep)].rstrip(), True
    return cut, True


@dataclass
class Topic:
    name: str
    keywords: list[str]


@dataclass
class Source:
    name: str
    type: str
    url: str = ""
    channel: str = ""  # telegram: public channel username
    chat: str = ""  # telegram_account: username, invite link or numeric id
    pillar: str | None = None
    topics: list[Topic] = field(default_factory=list)  # ordered by priority; empty = accept everything
    focus: str = ""  # extra editorial instruction for this source (what counts as important, etc.)
    priority: int = 5  # lower number = handled first when several sources have new items
    pattern: str = ""  # links: regex an article URL must match
    max_links: int = 15  # links: how many article links to consider per visit
    link: bool = True  # put a link to the original in the credit line (turn off for private groups)
    min_chars: int = 80  # telegram sources: ignore posts shorter than this (captions of charts, one-liners)
    trusted: bool = False  # approval level «trusted»: only posts from trusted sources are published without the owner's review
    images: bool = True  # false = never use this source's pictures (e.g. channels that put their own logo on every photo)


@dataclass
class Item:
    source: str
    key: str
    title: str
    text: str
    url: str
    published: datetime | None = None
    topic: str | None = None
    rank: int = 0  # position of the matched topic in the source's topic list (0 = highest priority)
    truncated: bool = False  # the source text was longer than MAX_TEXT and its end was cut
    image: str = ""  # address of the item's own picture (feed media, og:image, Telegram photo); "" = none


@dataclass
class Account:
    api_id: int
    api_hash: str
    session: str  # path of the Telethon session file (without extension)


# ------------------------------------------------------------------ config


def load_sources(path: str | Path) -> list[Source]:
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: list[Source] = []
    for i, s in enumerate(raw.get("sources") or []):
        where = f"sources.yaml، مورد {i + 1}"
        typ = str(s.get("type", "")).lower()
        if typ not in SOURCE_TYPES:
            raise SystemExit(f"{where}: type باید یکی از {', '.join(SOURCE_TYPES)} باشد.")
        url = str(s.get("url", "")).strip()
        channel = str(s.get("channel", "")).strip().lstrip("@")
        chat = str(s.get("chat", "")).strip()
        if typ == "telegram" and not channel:
            m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", url)
            channel = m.group(1) if m else ""
        if typ == "telegram" and not channel:
            raise SystemExit(f"{where}: برای telegram فیلد channel لازم است.")
        if typ == "telegram_account" and not chat:
            raise SystemExit(f"{where}: برای telegram_account فیلد chat لازم است (یوزرنیم، لینک دعوت یا شناسهٔ عددی).")
        if typ in ("rss", "web", "links") and not url:
            raise SystemExit(f"{where}: فیلد url لازم است.")
        topics = []
        for t in s.get("topics") or []:
            kws = [str(k).strip() for k in (t.get("keywords") or []) if str(k).strip()]
            if not t.get("name") or not kws:
                raise SystemExit(f"{where}: هر topic باید name و keywords داشته باشد.")
            topics.append(Topic(str(t["name"]), kws))
        out.append(Source(
            name=str(s.get("name") or channel or chat or url), type=typ, url=url, channel=channel,
            chat=chat, pillar=s.get("pillar"), topics=topics, focus=str(s.get("focus") or "").strip(),
            priority=int(s.get("priority", 5)), pattern=str(s.get("pattern") or ""),
            max_links=int(s.get("max_links", 15)),
            link=bool(s.get("link", typ != "telegram_account")),
            trusted=bool(s.get("trusted", False)),
            min_chars=int(s.get("min_chars", MIN_TELEGRAM_TEXT)),
            images=bool(s.get("images", True)),
        ))
    return out


# ------------------------------------------------------------------ topic filter


def _kw_regex(kw: str) -> re.Pattern:
    if kw.isascii():  # whole-word match so "gold" does not match "Goldman"; allow plural
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?:s|es)?(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(kw))  # Persian: plain substring (handles ی/ها suffixes)


def classify(src: Source, item: Item) -> tuple[str | None, int] | None:
    """Return (topic name, rank) of the highest-priority topic the item is really about, or None to drop it.

    A topic qualifies when a keyword appears in the title, or at least 3 times in the body.
    Sources without topics accept everything (rank 0).
    """
    if not src.topics:
        return None, 0
    body = item.text[:4000]
    for rank, topic in enumerate(src.topics):
        rxs = [_kw_regex(k) for k in topic.keywords]
        if any(r.search(item.title) for r in rxs) or sum(len(r.findall(body)) for r in rxs) >= 3:
            return topic.name, rank
    return None


# ------------------------------------------------------------------ helpers


def _key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


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


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        return _aware(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        pass
    try:
        return _aware(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


_BAD_IMAGE = re.compile(r"logo|favicon|sprite|avatar|placeholder|default[-_.]|blank|pixel|spacer|banner-ad|/ads?/|\.svg|\.gif($|\?)", re.I)


def clean_image_url(url: str | None, base: str = "") -> str:
    """A usable absolute picture address, or "" (relative addresses are resolved; logos/icons/ads/SVG are rejected)."""
    u = (url or "").strip().replace("&amp;", "&")
    if not u or u.startswith("data:"):
        return ""
    u = urljoin(base, u) if base else u
    if not re.match(r"^https?://", u, re.I) or _BAD_IMAGE.search(u):
        return ""
    return u


def _check_url(url: str) -> None:
    if not re.match(r"^https?://", url, re.I):
        raise SourceError("فقط آدرس‌های http/https پذیرفته می‌شوند.")


# ------------------------------------------------------------------ parsers (pure, testable)


def parse_feed(content: bytes | str, source: Source) -> list[Item]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise SourceError(f"فید معتبر نیست: {e}") from e

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    items: list[Item] = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        img = ""
        for ch in el:
            n = local(ch.tag)
            if n in ("content", "thumbnail", "enclosure") and ch.get("url") and not img:
                kind = (ch.get("type") or ch.get("medium") or "image").lower()  # media:thumbnail has no type: it is a picture
                if kind.startswith("image") or n == "thumbnail":
                    img = clean_image_url(ch.get("url"))
                continue
            if n == "link":
                d.setdefault("link", (ch.get("href") or ch.text or "").strip())
            elif n in ("title", "guid", "id", "pubDate", "updated", "published",
                       "description", "summary", "encoded", "content"):
                d.setdefault(n, (ch.text or "").strip())
        title = html_to_text(d.get("title", ""))
        body = d.get("encoded") or d.get("content") or d.get("description") or d.get("summary") or ""
        text, cut = _clip(html_to_text(body))
        url = d.get("link", "")
        if not img:  # no media tag: the first picture inside the item's own HTML
            m = re.search(r"<img[^>]+src=[\"']([^\"']+)", body, re.I)
            img = clean_image_url(m.group(1), url) if m else ""
        if not title or not text:
            continue
        ident = d.get("guid") or d.get("id") or url or title
        items.append(Item(
            source=source.name, key=_key(source.name, ident), title=title[:200], text=text, url=url,
            published=_parse_date(d.get("pubDate") or d.get("published") or d.get("updated")), truncated=cut, image=img,
        ))
    return items


def parse_telegram(html: str, source: Source) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = msg["data-post"]  # e.g. "channelname/123"
        body = msg.select_one(".tgme_widget_message_text")
        if not body:
            continue
        text = html_to_text(str(body))
        if len(text) < source.min_chars:
            continue
        text, cut = _clip(text)
        t = msg.select_one("time[datetime]")
        ph = msg.select_one("a.tgme_widget_message_photo_wrap")
        m = re.search(r"url\(['\"]?([^'\")]+)", (ph.get("style") or "") if ph else "")
        items.append(Item(
            source=source.name, key=_key("tg", post_id),
            title=text.split("\n", 1)[0][:80], text=text,
            url=f"https://t.me/{post_id}", published=_parse_date(t["datetime"]) if t else None, truncated=cut,
            image=clean_image_url(m.group(1)) if m else "",
        ))
    return items


def parse_article(html: str, url: str, source_name: str) -> Item:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    title = (og.get("content") if og and og.get("content") else None) or (
        soup.title.get_text(strip=True) if soup.title else "")
    pub = soup.find("meta", property="article:published_time")
    published = _parse_date(pub.get("content")) if pub and pub.get("content") else None
    im = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
    image = clean_image_url(im.get("content"), url) if im and im.get("content") else ""
    for t in soup(["script", "style", "nav", "footer", "aside", "form", "header"]):
        t.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = [b.get_text(" ", strip=True) for b in container.find_all(["h1", "h2", "h3", "p", "li"])]
    text, cut = _clip("\n".join(b for b in blocks if len(b) > 25))
    if len(text) < 200:
        raise SourceError("متن قابل‌استفاده‌ای در این صفحه پیدا نشد (ممکن است با جاوااسکریپت ساخته شود یا پشت ورود باشد).")
    return Item(source=source_name, key=_key("web", url), title=title[:200] or url, text=text, url=url,
                published=published, truncated=cut, image=image)


def parse_links(html: str, base_url: str, pattern: str = "", limit: int = 15) -> list[tuple[str, str]]:
    """Article links on an index page: [(absolute url, anchor text)], page order, same site only.

    With a `pattern` the URL must match it; without one, a link counts as a headline when its text is
    long enough and it is not the site root.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.removeprefix("www.")
    rx = re.compile(pattern) if pattern else None
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].split("#")[0].strip())
        u = urlparse(href)
        if u.scheme not in ("http", "https") or u.netloc.removeprefix("www.") != base_host:
            continue
        if href.rstrip("/") == base_url.rstrip("/") or href in seen:
            continue
        text = a.get_text(" ", strip=True)
        ok = bool(rx.search(href)) if rx else (len(text) >= 25 and bool(u.path.strip("/")))
        if not ok:
            continue
        seen.add(href)
        out.append((href, text))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ robots.txt (articles/index pages only)

_robots: dict[str, RobotFileParser | None] = {}


async def robots_allowed(http: httpx.AsyncClient, url: str) -> bool:
    u = urlparse(url)
    origin = f"{u.scheme}://{u.netloc}"
    if origin not in _robots:
        rp: RobotFileParser | None = None
        try:
            r = await http.get(f"{origin}/robots.txt")
            if r.status_code == 200:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            log.info("robots.txt not reachable for %s; assuming allowed", origin)
        _robots[origin] = rp
    rp = _robots[origin]
    return True if rp is None else rp.can_fetch(BOT["name"], url)


# ------------------------------------------------------------------ network


async def _get(http: httpx.AsyncClient, url: str, check_robots: bool = False) -> httpx.Response:
    _check_url(url)
    if check_robots and not await robots_allowed(http, url):
        raise SourceError(f"robots.txt این سایت خواندن این صفحه را برای ربات‌ها ممنوع کرده: {url}")
    try:
        r = await http.get(url)
    except httpx.HTTPError as e:
        raise SourceError(f"دریافت ناموفق ({type(e).__name__}){(': ' + str(e)) if str(e) else ' — سایت جواب نداد؛ احتمالاً تایم‌اوت یا مسدود بودن از شبکهٔ شما'}") from e
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code} از {url}")
    return r


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": user_agent()}, timeout=httpx.Timeout(45, connect=20), follow_redirects=True)


async def fetch_article(http: httpx.AsyncClient, url: str, source_name: str | None = None) -> Item:
    r = await _get(http, url, check_robots=True)
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return parse_article(r.text, url, source_name or host)


def message_to_item(msg, src: Source, username: str | None) -> Item | None:
    """Convert a Telethon message into an Item (None for service messages, media-only or short posts)."""
    text = (getattr(msg, "message", None) or "").strip()
    if len(text) < src.min_chars:
        return None
    text, cut = _clip(text)
    url = f"https://t.me/{username}/{msg.id}" if username and src.link else ""
    return Item(
        source=src.name, key=_key("tga", src.chat, str(msg.id)), title=text.split("\n", 1)[0][:80],
        text=text, url=url, published=_aware(msg.date) if getattr(msg, "date", None) else None, truncated=cut,
    )


async def fetch_account_source(src: Source, account: Account, limit: int = 40) -> list[Item]:
    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise SourceError("بستهٔ telethon نصب نیست: pip install telethon") from e
    client = TelegramClient(account.session, account.api_id, account.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SourceError("حساب تلگرام هنوز وارد نشده؛ یک بار «python login_telegram.py» را اجرا کنید.")
        ref: int | str = int(src.chat) if re.fullmatch(r"-?\d+", src.chat) else src.chat
        entity = await client.get_entity(ref)
        msgs = await client.get_messages(entity, limit=limit)
        username = getattr(entity, "username", None)
        return [it for m in msgs if (it := message_to_item(m, src, username))]
    except SourceError:
        raise
    except Exception as e:  # Telethon raises many specific errors; report them readably
        raise SourceError(f"خواندن از حساب تلگرام ناموفق بود ({type(e).__name__}: {e})") from e
    finally:
        await client.disconnect()


async def fetch_source(
    http: httpx.AsyncClient, src: Source,
    is_seen: Callable[[str], bool] | None = None, account: Account | None = None,
) -> list[Item]:
    """Items currently available from one source (newest first). Age/topic filtering is done by the caller.

    For `links` sources, `is_seen` lets us skip articles already handled before downloading them.
    """
    if src.type == "rss":
        items = parse_feed((await _get(http, src.url)).content, src)
    elif src.type == "telegram":
        items = parse_telegram((await _get(http, f"https://t.me/s/{src.channel}")).text, src)
    elif src.type == "telegram_account":
        if account is None:
            raise SourceError("برای telegram_account مقدارهای TELEGRAM_API_ID و TELEGRAM_API_HASH را در .env بگذارید.")
        items = await fetch_account_source(src, account)
    elif src.type == "links":
        page = await _get(http, src.url, check_robots=True)
        items = []
        for href, _text in parse_links(page.text, src.url, src.pattern, src.max_links):
            if is_seen and is_seen(_key("web", href)):
                continue
            try:
                items.append(await fetch_article(http, href, src.name))
            except SourceError as e:
                log.info("skipping %s: %s", href, e)
            await asyncio.sleep(POLITE_DELAY)
    else:  # single web page
        items = [await fetch_article(http, src.url, src.name)]
    items.sort(key=lambda i: i.published or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return items


async def probe(http: httpx.AsyncClient, url: str) -> str:
    """Help the owner set up a source: find feeds and article-looking links on a site."""
    page = await _get(http, url, check_robots=True)
    soup = BeautifulSoup(page.text, "html.parser")
    feeds = [
        urljoin(url, link["href"]) for link in soup.find_all("link", href=True)
        if (link.get("type") or "").lower() in ("application/rss+xml", "application/atom+xml")
    ]
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    for path in ("/feed", "/rss", "/rss.xml", "/feed/"):
        if len(feeds) >= 3:
            break
        try:
            r = await http.get(origin + path)
        except httpx.HTTPError:
            continue
        head = r.text[:300].lower()
        if r.status_code == 200 and ("<rss" in head or "<feed" in head or "<?xml" in head):
            feeds.append(origin + path)
    links = parse_links(page.text, url, "", 6)
    lines = [f"🔎 {url}", ""]
    lines += ["فید پیدا شد (بهترین حالت: type: rss):"] + [f"• {f}" for f in dict.fromkeys(feeds)] if feeds else [
        "فید پیدا نشد؛ از type: links استفاده کنید."]
    if links:
        lines += ["", "نمونه لینک‌های خبری که ربات تشخیص داد:"] + [f"• {t[:60]} — {h}" for h, t in links]
    else:
        lines += ["", "لینک خبری مشخصی تشخیص داده نشد؛ احتمالاً صفحه با جاوااسکریپت ساخته می‌شود یا باید pattern بدهید."]
    return "\n".join(lines)
