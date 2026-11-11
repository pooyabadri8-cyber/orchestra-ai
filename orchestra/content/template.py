"""The channel's fixed post layout, rendered by code for each platform from the same structured parts.

Model text is always escaped: formatting (bold headline, footer links) comes only from the template, so nothing the
model writes can add a link or markup. Telegram gets HTML; Bale gets its Markdown.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from ..brand import PostTemplate

SEP = "➖➖➖"
HEADLINE, BULLET, CTA = "📌", "🔹", "☝"
_HASHTAG = re.compile(r"^#[^\s#<>]{2,60}$")


@dataclass(frozen=True)
class PostParts:
    hashtag: str
    headline: str
    paragraphs: tuple[str, ...]
    cta: str


def topic_hashtag(tpl: PostTemplate, proposed: str) -> str:
    tag = (proposed or "").strip().replace(" ", "_")
    if tag and not tag.startswith("#"):
        tag = "#" + tag
    return tag if _HASHTAG.match(tag) and tag != tpl.brand_hashtag else tpl.default_topic_hashtag


def telegram_html(tpl: PostTemplate, parts: PostParts) -> str:
    e = html.escape
    lines = [tpl.brand_hashtag, topic_hashtag(tpl, parts.hashtag), "", "", f"{HEADLINE}  <b>{e(parts.headline)}</b>", ""]
    lines.append("\n\n".join(f"{BULLET}{e(p)}" for p in parts.paragraphs))
    cta = parts.cta.strip() or tpl.default_cta
    if cta:
        lines += ["", "", f"{CTA} {e(cta)}"]
    lines += ["", "", SEP]
    if tpl.footer_text:
        lines.append(e(tpl.footer_text))
    links = [f'<a href="{e(link.url, quote=True)}">{e(link.text)}</a>' if link.url else e(link.text) for link in tpl.footer_links]
    if links:
        lines += ["", *links]
    return "\n".join(lines).rstrip()


def _bale_text(s: str) -> str:
    return s.replace("*", "＊").replace("[", "［").replace("]", "］")  # model text can never become Markdown


def bale_markdown(tpl: PostTemplate, parts: PostParts) -> str:
    t = _bale_text
    lines = [tpl.brand_hashtag, topic_hashtag(tpl, parts.hashtag), "", "", f"{HEADLINE}  *{t(parts.headline)}*", ""]
    lines.append("\n\n".join(f"{BULLET}{t(p)}" for p in parts.paragraphs))
    cta = parts.cta.strip() or tpl.default_cta
    if cta:
        lines += ["", "", f"{CTA} {t(cta)}"]
    lines += ["", "", SEP]
    if tpl.footer_text:
        lines.append(t(tpl.footer_text))
    links = [f"[{t(link.text)}]({link.url})" if link.url else t(link.text) for link in tpl.footer_links]
    if links:
        lines += ["", *links]
    return "\n".join(lines).rstrip()


def visible_length(telegram_post: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", telegram_post)))
