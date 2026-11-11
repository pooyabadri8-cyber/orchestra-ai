"""Messenger platform layer: Telegram (default) or Bale.

Bale's bot API (https://tapi.bale.ai/bot<TOKEN>/<method>) mirrors Telegram's, so the whole bot runs unchanged on top of
python-telegram-bot. The differences are handled here, in ONE place, by a Bot subclass:
  * Bale has no `parse_mode`; text is always Markdown. Our posts are written in Telegram HTML, so every outgoing text is
    converted (<b> -> *bold*, <a href> -> [text](url)).
  * Telegram-only request fields (link_preview_options, disable_notification, ...) are dropped.
  * Methods Bale may not support for a given message (editing the buttons of an old message) never crash a handler.

Formatting details live in bale.yaml so they can be adjusted without touching code.
"""
from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import yaml
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ExtBot
from telegram.request import HTTPXRequest

log = logging.getLogger("content-agent.platform")

BALE_API = "https://tapi.bale.ai/bot"
BALE_FILE_API = "https://tapi.bale.ai/file/bot"
_DROPPED = ("link_preview_options", "disable_web_page_preview", "disable_notification", "protect_content")


@dataclass
class BaleFormat:
    bold: str = "*"  # marker around bold text; "" = no bold at all
    italic: str = "_"  # same for italic; "" = none
    links: str = "markdown"  # markdown: [text](url)   |   plain: text (url)
    underscore: str = "keep"  # underscores in hashtags: keep | escape (\_) | zwnj (replace with a zero-width non-joiner)
    markdown: bool = True  # False = send plain text without any formatting characters


def load_bale_format(path: str | Path = "bale.yaml") -> BaleFormat:
    p = Path(path)
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    f = BaleFormat()
    f.bold = str(raw.get("bold", f.bold))
    f.italic = str(raw.get("italic", f.italic))
    f.links = str(raw.get("links", f.links))
    f.underscore = str(raw.get("underscore", f.underscore))
    f.markdown = bool(raw.get("markdown", f.markdown))
    if f.links not in ("markdown", "plain"):
        raise SystemExit("bale.yaml: links باید markdown یا plain باشد.")
    if f.underscore not in ("keep", "escape", "zwnj"):
        raise SystemExit("bale.yaml: underscore باید keep یا escape یا zwnj باشد.")
    return f


class _ToMarkdown(HTMLParser):
    def __init__(self, fmt: BaleFormat):
        super().__init__(convert_charrefs=True)
        self.f = fmt
        self.out: list[str] = []
        self.links: list[str | None] = []
        self.code = 0

    def _text(self, s: str) -> str:
        if self.code:
            return s
        if self.f.markdown:
            s = s.replace("*", "＊")
            if self.f.underscore == "escape":
                s = s.replace("_", "\\_")
        if self.f.underscore == "zwnj":
            s = s.replace("_", "‌")
        return s

    def handle_starttag(self, tag, attrs):
        f = self.f
        if tag in ("b", "strong") and f.markdown:
            self.out.append(f.bold)
        elif tag in ("i", "em") and f.markdown:
            self.out.append(f.italic)
        elif tag == "a":
            href = dict(attrs).get("href")
            self.links.append(href)
            if href and f.markdown and f.links == "markdown":
                self.out.append("[")
        elif tag in ("code", "pre"):
            self.code += 1
            if f.markdown:
                self.out.append("`" if tag == "code" else "```\n")

    def handle_endtag(self, tag):
        f = self.f
        if tag in ("b", "strong") and f.markdown:
            self.out.append(f.bold)
        elif tag in ("i", "em") and f.markdown:
            self.out.append(f.italic)
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href and f.markdown and f.links == "markdown":
                self.out.append(f"]({href})")
            elif href:
                self.out.append(f" ({href})")
        elif tag in ("code", "pre"):
            self.code = max(0, self.code - 1)
            if f.markdown:
                self.out.append("`" if tag == "code" else "\n```")

    def handle_data(self, data):
        self.out.append(self._text(data))


def html_to_bale(text: str, fmt: BaleFormat | None = None) -> str:
    """Telegram-HTML post -> Bale text."""
    p = _ToMarkdown(fmt or BaleFormat())
    p.feed(text)
    p.close()
    return "".join(p.out)


class BaleBot(ExtBot):
    """python-telegram-bot Bot that talks to Bale and translates what the rest of the code sends."""

    def __init__(self, token: str, fmt: BaleFormat | None = None, **kw):
        super().__init__(token, base_url=BALE_API, base_file_url=BALE_FILE_API, **kw)
        object.__setattr__(self, "fmt", fmt or BaleFormat())  # PTB bots refuse normal attribute assignment

    async def send_message(self, chat_id, text, *args, **kwargs):
        mode = kwargs.pop("parse_mode", None)
        for k in _DROPPED:
            kwargs.pop(k, None)
        if mode == ParseMode.HTML:
            text = html_to_bale(text, self.fmt)
        return await super().send_message(chat_id, text, *args, **kwargs)

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        mode = kwargs.pop("parse_mode", None)
        for k in _DROPPED:
            kwargs.pop(k, None)
        caption = kwargs.get("caption")
        if caption and mode == ParseMode.HTML:
            kwargs["caption"] = html_to_bale(caption, self.fmt)
        return await super().send_photo(chat_id, photo, *args, **kwargs)

    async def edit_message_text(self, text, *args, **kwargs):
        mode = kwargs.pop("parse_mode", None)
        for k in _DROPPED:
            kwargs.pop(k, None)
        if mode == ParseMode.HTML:
            text = html_to_bale(text, self.fmt)
        return await super().edit_message_text(text, *args, **kwargs)

    async def edit_message_reply_markup(self, *args, **kwargs):
        try:  # removing the buttons of an answered message is cosmetic; never fail a handler over it
            return await super().edit_message_reply_markup(*args, **kwargs)
        except TelegramError as e:
            log.info("edit_message_reply_markup not applied on Bale: %s", e)
            return True


def _bale_request(pool: int) -> HTTPXRequest:
    """Bale is an Iranian service: it is reached DIRECTLY, ignoring the system / VPN proxy (HTTP_PROXY, Windows proxy
    settings) that the model provider and foreign sites may need. Set BALE_PROXY in .env only if Bale itself needs one."""
    proxy = os.environ.get("BALE_PROXY", "").strip() or None
    return HTTPXRequest(connection_pool_size=pool, proxy=proxy, httpx_kwargs={"trust_env": False})


def make_bot(cfg) -> Bot:
    """The Bot object for the configured platform (telegram: PTB's default, built by the Application builder)."""
    if cfg.platform == "bale":
        return BaleBot(
            cfg.bot_token, load_bale_format(), request=_bale_request(16), get_updates_request=_bale_request(1),
        )
    return None  # type: ignore[return-value]


# ---------------------------------------------------------------- commands that exist because of the platform

SAMPLE = (
    "#برند_نمونه\n#موضوع_نمونه\n\n\n📌  <b>تیتر آزمایشی</b>\n\n"
    "🔹این یک بند آزمایشی است. <b>این قسمت پررنگ</b> و این قسمت عادی است.\n\n"
    '☝ <a href="https://example.com">لینک آزمایشی</a>\n\n\n➖➖➖\nپاورقی آزمایشی'
)


def register(app, cfg) -> None:
    """/id works everywhere (to find your numeric id for OWNER_IDS); /balecheck only on Bale."""
    from telegram import Update
    from telegram.ext import CommandHandler, ContextTypes, filters

    async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        await update.effective_message.reply_text(f"شناسهٔ عددی شما: {u.id}\nاین عدد را در OWNER_IDS فایل .env بگذارید.")

    async def cmd_balecheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        if not u or u.id not in cfg.owner_ids:
            return
        bot = context.bot
        await bot.send_message(u.id, "آزمایش قالب‌بندی: نمونهٔ پست با تنظیمات فعلی (bale.yaml). "
                                     "اگر هشتگ‌ها، پررنگ‌ها یا لینک درست نمایش داده نشد، bale.yaml را طبق README عوض کنید.")
        await bot.send_message(u.id, SAMPLE, parse_mode=ParseMode.HTML)
        await bot.send_message(u.id, "همان نمونه به‌صورت متن ساده (اگر نمونهٔ بالا خراب بود، در bale.yaml بنویسید markdown: false):")
        await bot.send_message(u.id, html_to_bale(SAMPLE, BaleFormat(markdown=False, links="plain")))

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("id", cmd_id, filters=private))
    if cfg.platform == "bale":
        app.add_handler(CommandHandler("balecheck", cmd_balecheck, filters=private))
