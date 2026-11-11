"""Telegram admin bot: drafts -> owner approval -> scheduled publish to the channel."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone


from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import daily, gozaresh, planner, stats
from . import images as pics
from . import platform as plat
from .autopilot import AutoState, auto_times, check_post, headline_of
from .cli import write_heartbeat
from .config import Config, load_config
from .knowledge import load_knowledge
from .slots import fmt_local, next_slots, parse_local
from .sources import Item, SourceError, classify, fetch_article, fetch_source, load_sources, new_http_client, probe, set_bot_name
from .store import Post, Store, iso, parse_iso
from .writer import LLMError, Writer, add_attribution, has_placeholder

log = logging.getLogger("content-agent")

TG_LIMIT = 4096
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

HELP = """\
<b>ادمین کانال {name}</b>
{mode}

/draft موضوع — نوشتن یک پیش‌نویس (محور اختیاری: <code>/draft موضوع | محور</code>)
/plan [تعداد] — تقویم محتوایی؛ پیش‌فرض ۷ پست، با زمان پیشنهادی
/fetch — خواندن منابع (sources.yaml) و ساخت پیش‌نویس از مطالب جدید
/fetch آدرس — بازنویسی یک مقاله یا صفحهٔ مشخص
/sources — فهرست منابع تعریف‌شده
/probe آدرس‌سایت — پیدا کردن فید و لینک‌های خبری یک سایت (برای تنظیم sources.yaml)
/queue — پیش‌نویس‌ها و پست‌های زمان‌بندی‌شده
/show شناسه — نمایش دوبارهٔ یک پیش‌نویس
/edit شناسه دستور — ویرایش با Claude (مثلاً <code>/edit 5 کوتاه‌ترش کن</code>)
/schedule شناسه 1405-07-20 10:30 — زمان‌بندی دستی (به وقت {tz}، تاریخ میلادی)
/cancel — لغو حالت ویرایش
عکس + کپشن <code>/img 5</code> — گذاشتن عکس روی پست شمارهٔ ۵ (کپشن عکسِ کانال همان متن پست است)
/daily — جدول محتوای امروز (خبر، گزارش، تحلیل‌ها)؛ ساخت دستی: <code>/daily شناسه</code>
/dailycheck — تست منابع خبری
/auto — وضعیت حالت خودکار
/dead — پست‌هایی که منتشر نشدند (صف خطا)؛ /retry شماره = ارسال دوباره، /sent شماره = در کانال هست
/id — شناسهٔ عددی خودتان (برای OWNER_IDS)
/pause — توقف اضطراری: هیچ پستی منتشر یا خودکار زمان‌بندی نمی‌شود
/resume — ادامهٔ کار خودکار

زیر هر پیش‌نویس دکمه‌های تأیید، انتشار فوری، ویرایش و حذف هست.
بعد از هر انتشار هم یک دکمهٔ «حذف از کانال» برایتان می‌آید."""

MODE_MANUAL = "همهٔ پست‌ها اول به صورت پیش‌نویس برای شما می‌آیند و فقط با تأیید شما منتشر می‌شوند."
MODE_AUTO = (
    "🤖 <b>حالت خودکار روشن است:</b> پست‌های ساخته‌شده از منابع بدون تأیید شما زمان‌بندی و در کانال منتشر می‌شوند. "
    "پستی که بررسی خودکار آن را مشکوک بداند منتشر نمی‌شود و برای تأیید شما می‌آید. "
    "پست‌هایی که با /draft یا /plan می‌سازید همچنان اول برای تأیید شما می‌آیند."
)


# ---------------------------------------------------------------- helpers


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> tuple[Config, Store, Writer]:
    d = context.application.bot_data
    return d["cfg"], d["store"], d["writer"]


def _is_owner(update: Update, cfg: Config) -> bool:
    u = update.effective_user
    return bool(u and u.id in cfg.owner_ids)


def keyboard(post: Post) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأیید و زمان‌بندی", callback_data=f"ok:{post.id}"),
                InlineKeyboardButton("🚀 انتشار الان", callback_data=f"now:{post.id}"),
            ],
            [
                InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit:{post.id}"),
                InlineKeyboardButton("❌ حذف", callback_data=f"rej:{post.id}"),
            ],
            *([[InlineKeyboardButton("🖼 حذف عکس", callback_data=f"noimg:{post.id}")]] if post.image_path else []),
        ]
    )


def review_footer(post: Post, cfg: Config) -> str:
    """Only a warning when needed; nothing else is added under the post."""
    if has_placeholder(post.text):
        return "\n\n⚠️ این متن بخش [[ ]] دارد؛ تا تکمیل نشود منتشر نمی‌شود."
    return ""


def caption_fits(cfg: Config, text: str) -> bool:
    """A photo caption is limited (1024 characters of visible text); a longer post goes out as plain text."""
    return len(_plain(text)) <= cfg.caption_max_chars


def photo_of(post: Post) -> str | None:
    return post.image_path if post.image_path and os.path.exists(post.image_path) else None


async def send_review(bot, chat_id: int, post: Post, cfg: Config, note: str = "") -> None:
    body = (f"{note}\n\n" if note else "") + post.text + review_footer(post, cfg)
    img = photo_of(post)
    if img:  # the picture that will go with the post, shown first (the buttons stay under the text)
        try:
            with open(img, "rb") as f:
                await bot.send_photo(chat_id, f, caption="🖼 عکس این پست" + ("" if caption_fits(cfg, post.text) else " (متن از ۱۰۲۴ کاراکتر بلندتر است؛ پست بدون عکس منتشر می‌شود)"))
        except TelegramError as e:
            log.warning("review photo not sent: %s", e)
    try:
        await bot.send_message(
            chat_id, body[:TG_LIMIT], parse_mode=ParseMode.HTML,
            reply_markup=keyboard(post), link_preview_options=NO_PREVIEW,
        )
    except BadRequest:
        await bot.send_message(chat_id, _plain(body)[:TG_LIMIT], reply_markup=keyboard(post))


def message_link(cfg: Config, message_id: int) -> str | None:
    if cfg.platform != "telegram":
        return None  # link formats of other messengers are not assumed
    if cfg.channel.startswith("@"):
        return f"https://t.me/{cfg.channel[1:]}/{message_id}"
    if cfg.channel.startswith("-100"):
        return f"https://t.me/c/{cfg.channel[4:]}/{message_id}"
    return None


async def notify_owners(bot, cfg: Config, text: str, reply_markup=None, silent: bool = False) -> None:
    for oid in cfg.owner_ids:
        try:
            await bot.send_message(
                oid, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                reply_markup=reply_markup, disable_notification=silent,
            )
        except TelegramError as e:
            log.warning("could not notify %s: %s", oid, e)




async def _send_photo_post(bot, cfg: Config, post: Post):
    """Photo + the post text as its caption. None when the picture itself is refused (then the caller sends the text alone)."""
    try:
        with open(post.image_path, "rb") as f:  # type: ignore[arg-type]
            try:
                return await bot.send_photo(cfg.channel, f, caption=post.text, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                if "parse" not in str(e).lower() and "entities" not in str(e).lower():
                    raise
                f.seek(0)
                return await bot.send_photo(cfg.channel, f, caption=_plain(post.text))
    except BadRequest as e:  # bad picture (size/format): never lose the post over it
        log.warning("post #%s: picture refused (%s); sending text only", post.id, e)
        return None
    except OSError as e:
        log.warning("post #%s: picture file unreadable (%s); sending text only", post.id, e)
        return None


async def publish_post(bot, cfg: Config, store: Store, post: Post) -> tuple[bool, str]:
    """Publish one post to the channel. Returns (ok, human message)."""
    if post.status not in ("draft", "scheduled", "failed"):
        return False, f"پست #{post.id} در وضعیت «{post.status}» است و قابل انتشار نیست."
    if has_placeholder(post.text):
        store.mark_failed(post.id, "متن شامل بخش تکمیل‌نشدهٔ [[ ]] است")
        return False, f"پست #{post.id} بخش تکمیل‌نشدهٔ [[ ]] دارد؛ ابتدا ویرایشش کنید."
    if len(post.text) > TG_LIMIT:
        store.mark_failed(post.id, "متن از ۴۰۹۶ کاراکتر بیشتر است")
        return False, f"پست #{post.id} طولانی‌تر از حد تلگرام است؛ کوتاهش کنید."
    if not store.begin_send(post.id):  # claimed in the database first: no second sender, even across a restart
        return False, f"پست #{post.id} در حال انتشار است یا دیگر قابل انتشار نیست."
    try:
        msg = await _send_photo_post(bot, cfg, post) if photo_of(post) and caption_fits(cfg, post.text) else None
        if msg is None:
            try:
                msg = await bot.send_message(
                    cfg.channel, post.text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
                )
            except BadRequest as e:
                if "parse" in str(e).lower() or "entities" in str(e).lower():
                    msg = await bot.send_message(cfg.channel, _plain(post.text), link_preview_options=NO_PREVIEW)
                else:
                    raise
        store.mark_published(post.id, msg.message_id)
        link = message_link(cfg, msg.message_id)
        return True, f"✅ پست #{post.id} منتشر شد." + (f"\n{link}" if link else "")
    except TimedOut as e:
        # the request may have reached Telegram: sending again could post it twice, so a human decides (/sent or /retry)
        store.mark_unconfirmed(post.id, f"پاسخی نیامد: {e}")
        return False, (f"❓ ارسال پست #{post.id} بی‌پاسخ ماند و معلوم نیست در کانال آمده یا نه. کانال را ببینید: "
                       f"اگر هست /sent {post.id}، اگر نیست /retry {post.id}.")
    except (BadRequest, Forbidden) as e:  # permanent (BadRequest is a NetworkError subclass: it must be caught first)
        store.mark_failed(post.id, str(e))
        return False, f"❌ انتشار پست #{post.id} ناموفق بود: {html.escape(str(e))}"
    except (RetryAfter, NetworkError) as e:  # certainly not delivered (flood wait / connection failed): retry with backoff
        n = (store.get(post.id) or post).attempts
        if n >= cfg.max_publish_attempts:
            store.mark_dead(post.id, str(e))
            return False, (f"💀 پست #{post.id} بعد از {n} تلاش منتشر نشد و به صف خطا رفت: {html.escape(str(e))}\n"
                           f"بعد از رفع مشکل، /retry {post.id} را بزنید.")
        wait = getattr(e, "retry_after", 0) or 0
        delay = max(retry_delay(cfg, n), float(wait.total_seconds() if isinstance(wait, timedelta) else wait))
        when = datetime.now(timezone.utc) + timedelta(seconds=delay)
        store.mark_retry(post.id, str(e), when)
        return False, f"🔁 انتشار پست #{post.id} موقتاً ناموفق بود ({html.escape(str(e))}). تلاش {n + 1} از {cfg.max_publish_attempts} ساعت {fmt_local(iso(when), cfg.tz)}."
    except TelegramError as e:  # permanent (bot not admin, bad content...): the owner fixes it, then publishes again
        store.mark_failed(post.id, str(e))
        return False, f"❌ انتشار پست #{post.id} ناموفق بود: {html.escape(str(e))}"


def retry_delay(cfg: Config, attempt: int) -> float:
    """Seconds to wait after the `attempt`-th failed try (the last configured delay repeats)."""
    d = cfg.retry_delays or [30, 120, 600]
    return float(d[min(max(attempt, 1), len(d)) - 1])


# ---------------------------------------------------------------- commands


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, _, _ = _ctx(context)
    if not _is_owner(update, cfg):
        uid = update.effective_user.id if update.effective_user else "?"
        await update.effective_message.reply_text(
            f"این ربات خصوصی است.\nشناسهٔ عددی شما: {uid}\nاگر شما صاحب ربات هستید، این عدد را در OWNER_IDS فایل .env بگذارید و ربات را ری‌استارت کنید."
        )
        return
    state: AutoState = context.application.bot_data["auto"]
    mode = MODE_AUTO if cfg.auto_publish else MODE_MANUAL
    if cfg.auto_publish and state.paused:
        mode += "\n⏸ <b>الان متوقف (pause) است؛</b> برای ادامه /resume."
    extra = ""
    if cfg.profile.feature("market_report"):
        extra += "\n/gozaresh — گزارش روزانهٔ بازار (راهنما: <code>/gozaresh help</code>)"
    if cfg.profile.feature("content_calendar"):
        extra += "\n/calendar — برنامهٔ هفتگی پست‌ها؛ <code>/calendar شناسه</code> = ساخت دستی، <code>/calendar شناسه draft</code> = فقط برای بررسی شما"
    await update.effective_message.reply_text(
        HELP.format(name=html.escape(cfg.channel_name), tz=cfg.timezone, mode=mode) + extra,
        parse_mode=ParseMode.HTML,
    )


async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, writer = _ctx(context)
    if not _is_owner(update, cfg):
        return
    raw = " ".join(context.args).strip()
    if not raw:
        await update.effective_message.reply_text("موضوع را بنویسید: /draft معرفی مسیر ۸ مرحله‌ای")
        return
    topic, _, pillar = (s.strip() for s in raw.partition("|"))
    await update.effective_message.reply_text("در حال نوشتن پیش‌نویس…")
    try:
        text = await writer.draft(topic, pillar or None, avoid=store.recent_topics())
    except LLMError as e:
        await update.effective_message.reply_text(f"خطا در ارتباط با مدل: {e}")
        return
    slot = next_slots(datetime.now(timezone.utc), cfg.tz, cfg.post_times, store.reserved_slots(), 1)
    post = store.add_draft(topic, pillar or None, text, slot[0] if slot else None)
    await send_review(context.bot, update.effective_chat.id, post, cfg)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, writer = _ctx(context)
    if not _is_owner(update, cfg):
        return
    try:
        n = max(1, min(14, int(context.args[0]))) if context.args else 7
    except ValueError:
        await update.effective_message.reply_text("تعداد باید عدد باشد: /plan 7")
        return
    await update.effective_message.reply_text(f"در حال ساخت تقویم {n} پستی… (چند دقیقه طول می‌کشد)")
    try:
        ideas = await writer.plan(n, recent=store.recent_topics())
    except (LLMError, ValueError) as e:
        await update.effective_message.reply_text(f"ساخت تقویم ناموفق بود: {e}")
        return
    slots = next_slots(datetime.now(timezone.utc), cfg.tz, cfg.post_times, store.reserved_slots(), len(ideas))
    made = 0
    for i, idea in enumerate(ideas):
        try:
            text = await writer.draft(idea["topic"], idea["pillar"], avoid=store.recent_topics())
        except LLMError as e:
            await update.effective_message.reply_text(f"نوشتن ایدهٔ «{idea['topic']}» ناموفق بود: {e}")
            continue
        post = store.add_draft(idea["topic"], idea["pillar"], text, slots[i] if i < len(slots) else None)
        await send_review(context.bot, update.effective_chat.id, post, cfg)
        made += 1
    await update.effective_message.reply_text(
        f"{made} پیش‌نویس ساخته شد. هرکدام را که تأیید کنید، در زمان پیشنهادی‌اش منتشر می‌شود."
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    posts = store.queue()
    if not posts:
        await update.effective_message.reply_text("صف خالی است.")
        return
    icon = {"draft": "📝", "scheduled": "🕒", "failed": "⚠️"}
    lines = []
    for p in posts:
        when = fmt_local(p.scheduled_at or p.proposed_at, cfg.tz)
        lines.append(f"{icon.get(p.status, '•')} #{p.id} · {when} · {html.escape(p.topic[:50])}")
    await update.effective_message.reply_text(
        "\n".join(lines) + "\n\n📝 پیش‌نویس · 🕒 زمان‌بندی‌شده · ⚠️ ناموفق\nبرای دیدن متن: /show شناسه"
    )


async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    post = _post_from_args(store, context.args)
    if not post:
        await update.effective_message.reply_text("شناسهٔ معتبر بدهید: /show 5")
        return
    await send_review(context.bot, update.effective_chat.id, post, cfg)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    post = _post_from_args(store, context.args)
    instruction = " ".join(context.args[1:]).strip()
    if not post or not instruction:
        await update.effective_message.reply_text("مثال: /edit 5 کوتاه‌ترش کن و لحن رسمی‌تر")
        return
    await _apply_edit(update, context, post, instruction)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    post = _post_from_args(store, context.args)
    if not post or len(context.args) < 3:
        await update.effective_message.reply_text("مثال: /schedule 5 2026-10-12 10:30")
        return
    try:
        when = parse_local(" ".join(context.args[1:3]), cfg.tz)
    except ValueError:
        await update.effective_message.reply_text("قالب تاریخ درست نیست. مثال: 2026-10-12 10:30")
        return
    if when <= datetime.now(timezone.utc):
        await update.effective_message.reply_text("این زمان گذشته است.")
        return
    if has_placeholder(post.text):
        await update.effective_message.reply_text("این پست بخش [[ ]] دارد؛ اول ویرایشش کنید.")
        return
    store.schedule(post.id, when)
    await update.effective_message.reply_text(f"🕒 پست #{post.id} برای {fmt_local(iso(when), cfg.tz)} زمان‌بندی شد.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, _, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    context.user_data.pop("editing", None)
    await update.effective_message.reply_text("حالت ویرایش لغو شد.")


def _post_from_args(store: Store, args: list[str]) -> Post | None:
    if not args:
        return None
    try:
        return store.get(int(args[0].lstrip("#")))
    except ValueError:
        return None


async def _apply_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, post: Post, instruction: str) -> None:
    cfg, store, writer = _ctx(context)
    if post.status in ("published", "rejected"):
        await update.effective_message.reply_text(f"پست #{post.id} دیگر قابل ویرایش نیست.")
        return
    if instruction.startswith("="):  # manual replacement
        new_text = instruction[1:].strip()
    else:
        await update.effective_message.reply_text("در حال ویرایش…")
        try:
            new_text = await writer.revise(post.text, instruction)
        except LLMError as e:
            await update.effective_message.reply_text(f"خطا در ارتباط با مدل: {e}")
            return
    if not new_text:
        await update.effective_message.reply_text("متن خالی است.")
        return
    store.update_text(post.id, new_text)
    await send_review(context.bot, update.effective_chat.id, store.get(post.id), cfg)  # type: ignore[arg-type]


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    editing = context.user_data.pop("editing", None)
    if editing is None:
        await update.effective_message.reply_text("برای راهنما /start را بزنید.")
        return
    post = store.get(editing)
    if not post:
        await update.effective_message.reply_text("پست پیدا نشد.")
        return
    await _apply_edit(update, context, post, update.effective_message.text.strip())


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The owner sends a photo with the caption `/img 5`: it becomes the photo of post #5 (replaces the current one)."""
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    msg = update.effective_message
    m = re.match(r"^/?img\s+#?(\d+)", (msg.caption or "").strip(), re.I)
    post = store.get(int(m.group(1))) if m else None
    if not post:
        await msg.reply_text("برای گذاشتن عکس روی یک پست، عکس را با این کپشن بفرستید: <code>/img 5</code> (۵ = شناسهٔ پست)", parse_mode=ParseMode.HTML)
        return
    if post.status in ("published", "rejected", "deleted"):
        await msg.reply_text(f"پست #{post.id} دیگر قابل ویرایش نیست.")
        return
    try:
        f = await context.bot.get_file(msg.photo[-1].file_id)
        os.makedirs(cfg.image_dir, exist_ok=True)
        path = os.path.join(cfg.image_dir, f"manual-{post.id}-{int(datetime.now().timestamp())}.jpg")
        await f.download_to_drive(path)
    except TelegramError as e:
        await msg.reply_text(f"دریافت عکس ناموفق بود: {e}")
        return
    store.set_image(post.id, path)
    await send_review(context.bot, update.effective_chat.id, store.get(post.id), cfg)  # type: ignore[arg-type]


# ---------------------------------------------------------------- buttons


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    q = update.callback_query
    if not _is_owner(update, cfg):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return
    action, _, sid = (q.data or "").partition(":")
    post = store.get(int(sid)) if sid.isdigit() else None
    if not post:
        await q.answer("پست پیدا نشد.", show_alert=True)
        return
    if action == "del":
        if post.status != "published" or not post.message_id:
            await q.answer("این پست در کانال منتشر نشده یا قبلاً حذف شده است.", show_alert=True)
            return
        try:
            await context.bot.delete_message(cfg.channel, post.message_id)
        except TelegramError as e:
            await q.answer(f"حذف نشد: {e}. (تلگرام اجازهٔ حذف پیام‌های قدیمی‌تر از ۴۸ ساعت را نمی‌دهد؛ دستی حذف کنید.)", show_alert=True)
            return
        store.mark_deleted(post.id)
        await q.answer("از کانال حذف شد")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"🗑 پست #{post.id} از کانال حذف شد.")
        return
    if post.status in ("published", "rejected", "deleted"):
        await q.answer(f"این پست قبلاً «{post.status}» شده است.", show_alert=True)
        return

    if action in ("ok", "now"):
        store.log_event(post.id, "approved")  # the owner's decision (whether it was edited first is known from the log)
    if action == "ok":
        if has_placeholder(post.text):
            await q.answer("این متن بخش [[ ]] دارد؛ ابتدا ویرایشش کنید.", show_alert=True)
            return
        if auto_active(cfg, context.application.bot_data["auto"]):  # auto mode: approved = out now, no waiting for a slot
            await q.answer("در حال انتشار…")
            await q.edit_message_reply_markup(None)
            await release(context.bot, cfg, store, post)
            return
        now = datetime.now(timezone.utc)
        when = parse_iso(post.proposed_at) if post.proposed_at else None
        if (post.pillar or "").startswith("daily:"):
            # a timed post (market report, analysis) belongs to its own time: never move it to a later slot of
            # post_times, where it would go out with old numbers. Late but still fresh = now; too late = refused.
            target = when or now
            if now - target > DAILY_STALE:
                await q.answer("این پست زمان‌دار دیر شده و داده‌اش کهنه است؛ منتشر نمی‌شود. اگر باز هم می‌خواهید، «🚀 انتشار الان» را بزنید.", show_alert=True)
                return
            store.schedule(post.id, max(target, now))
            await q.answer("زمان‌بندی شد")
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(f"🕒 پست #{post.id} برای {fmt_local(iso(max(target, now)), cfg.tz)} زمان‌بندی شد.")
            return
        taken = store.taken_slots() - ({post.scheduled_at} if post.scheduled_at else set())
        if not when or when < now or iso(when) in taken:
            slot = next_slots(now, cfg.tz, cfg.post_times, taken, 1)
            when = slot[0] if slot else None
        if not when:
            await q.answer("زمان آزاد پیدا نشد؛ از /schedule استفاده کنید.", show_alert=True)
            return
        store.schedule(post.id, when)
        await q.answer("زمان‌بندی شد")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"🕒 پست #{post.id} برای {fmt_local(iso(when), cfg.tz)} زمان‌بندی شد.")

    elif action == "now":
        await q.answer("در حال انتشار…")
        await q.edit_message_reply_markup(None)
        _, text = await publish_post(context.bot, cfg, store, post)
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    elif action == "edit":
        context.user_data["editing"] = post.id
        await q.answer()
        await q.message.reply_text(
            f"ویرایش پست #{post.id}: دستورتان را بفرستید (مثلاً «کوتاه‌ترش کن»).\n"
            "اگر می‌خواهید خودتان متن را عوض کنید، متن کامل جدید را با علامت = در اول پیام بفرستید.\n"
            "لغو: /cancel"
        )

    elif action == "noimg":
        store.set_image(post.id, None)
        await q.answer("عکس حذف شد؛ پست بدون عکس منتشر می‌شود.")
        await q.edit_message_reply_markup(keyboard(store.get(post.id)))  # type: ignore[arg-type]

    elif action == "rej":
        store.reject(post.id)
        await q.answer("حذف شد")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"❌ پست #{post.id} لغو شد و منتشر نمی‌شود.")


# ---------------------------------------------------------------- sources (rewrite external content)

_fetch_lock = asyncio.Lock()
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


async def make_draft_from_item(
    cfg: Config, store: Store, writer: Writer, item: Item, pillar: str | None, focus: str = "", link: bool = True,
) -> Post | None:
    """Rewrite (and translate if needed) one source item into a draft. None if the model judged it unimportant/off-topic.

    When the item has its own picture (and this item is one of those that get one) the picture is downloaded first and the
    text is written short enough to be its caption; if it still does not fit, the post simply goes out without the picture."""
    image = None
    limit = None
    if cfg.images and item.image and pics.pick(item.key, cfg.image_ratio):
        async with new_http_client() as http:
            image = await pics.download(http, item.image, cfg.image_dir, cfg.image_min_width)
        if image:
            limit = max(300, cfg.caption_max_chars - len(_plain(writer.tpl.footer())) - 90)  # room left for the hashtags + footer
    text = await writer.rewrite(
        item.title, item.text, item.url, item.source, pillar,
        avoid=store.recent_topics(), focus=focus, topic=item.topic, truncated=item.truncated, max_chars=limit,
    )
    if text is None:
        store.mark_seen(item.key, item.source, item.title, item.url, "skipped")
        return None
    if cfg.attribution:
        text = add_attribution(text, item.source, item.url if link else "")
    if image and not caption_fits(cfg, text):
        image = None  # too long to be a caption: text-only post
    slot = next_slots(datetime.now(timezone.utc), cfg.tz, cfg.post_times, store.reserved_slots(), 1)
    post = store.add_draft(item.title, pillar, text, slot[0] if slot else None, source_url=item.url or None, image_path=image)
    store.mark_seen(item.key, item.source, item.title, item.url, "drafted")
    return post


def cancel_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو این پست", callback_data=f"rej:{post_id}")]])


def delete_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑 حذف از کانال", callback_data=f"del:{post_id}")]])


async def release(bot, cfg: Config, store: Store, post: Post, when: datetime | None = None, notice: bool = True) -> datetime:
    """Auto mode: publish a ready post NOW. If another post went out / is due within `auto_gap_minutes`, it is scheduled
    right after that one instead (so posts are never more than a few minutes apart and never wait in a queue)."""
    now = datetime.now(timezone.utc)
    if when is None:
        when = auto_times(now, cfg.tz, store.busy_times({post.id}), 1, cfg.auto_gap_minutes, cfg.auto_from, cfg.auto_to)[0]
    store.schedule(post.id, when)
    if when <= now + timedelta(seconds=2):
        ok, text = await publish_post(bot, cfg, store, store.get(post.id))  # type: ignore[arg-type]
        await notify_owners(bot, cfg, text, reply_markup=delete_keyboard(post.id) if ok else None)
        return when
    if not notice:
        return when
    head = html.escape(headline_of(post.text) or post.topic)
    await notify_owners(
        bot, cfg, f"🕒 پست #{post.id} ساعت {fmt_local(iso(when), cfg.tz)} (چند دقیقهٔ دیگر) منتشر می‌شود:\n{head}",
        reply_markup=cancel_keyboard(post.id), silent=True,
    )
    return when


def auto_active(cfg: Config, state: AutoState | None) -> bool:
    return bool(cfg.auto_publish and state is not None and not state.paused)


async def dispatch_post(
    bot, cfg: Config, store: Store, state: AutoState | None, post: Post, chat_ids, banned: list[str] | None = None,
    trusted: bool = True,
) -> str:
    """New post from a source. Auto mode: check it and schedule it. Otherwise (or if it looks suspicious): send for review.
    Approval level "trusted": only posts from sources marked `trusted: true` may skip the owner's review.
    Returns "auto" | "held" | "review"."""
    if not auto_active(cfg, state) or (cfg.approval == "trusted" and not trusted):
        for cid in chat_ids:
            await send_review(bot, cid, post, cfg)
        return "review"
    others = [t for t in store.recent_headlines() if t != post.text]
    reasons = check_post(post.text, others, banned, cfg.exclusions, cfg.profile.guard_phrases)
    if reasons:
        store.log_event(post.id, "held")
        note = "⏸ <b>این پست خودکار منتشر نشد؛ لطفاً خودتان بررسی کنید:</b>\n" + "\n".join("• " + html.escape(r) for r in reasons)
        for cid in chat_ids:
            await send_review(bot, cid, post, cfg, note=note)
        return "held"
    store.log_event(post.id, "auto")
    await release(bot, cfg, store, post)
    return "auto"


async def run_fetch(
    bot, cfg: Config, store: Store, writer: Writer, sources, chat_ids, limit: int, state: AutoState | None = None,
) -> str:
    """Check every source, draft up to `limit` new relevant items, send them for review. Returns a summary."""
    if _fetch_lock.locked():
        return "یک بررسی منابع هم‌اکنون در حال اجراست."
    async with _fetch_lock:
        if auto_active(cfg, state):
            room = cfg.auto_max_queue - store.count_scheduled()
            if room <= 0:
                return f"صف خودکار پر است ({cfg.auto_max_queue} پست در انتظار انتشار)؛ بعد از انتشار پست‌ها ادامه می‌دهم."
            limit = min(limit, room)
            if cfg.auto_max_per_day:
                start = datetime.now(cfg.tz).replace(hour=0, minute=0, second=0, microsecond=0)
                left = cfg.auto_max_per_day - store.count_regular_since(start)
                if left <= 0:
                    return f"سقف امروز پر است ({cfg.auto_max_per_day} پست از منابع)؛ فردا ادامه می‌دهم."
                limit = min(limit, left)
        errors: list[str] = []
        candidates = []
        filtered = excluded_n = 0
        excl = cfg.exclusions
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.max_age_days)
        async with new_http_client() as http:
            async def one(src):  # all sources at once: a slow or dead site no longer delays the others
                try:
                    return src, await fetch_source(http, src, is_seen=store.is_seen, account=cfg.tg_account)
                except SourceError as e:
                    return src, e

            for src, items in await asyncio.gather(*(one(x) for x in sources)):
                if isinstance(items, SourceError):
                    errors.append(f"{src.name}: {items}")
                    continue
                for it in items:
                    if store.is_seen(it.key):
                        continue
                    if it.published and it.published < cutoff:
                        store.mark_seen(it.key, it.source, it.title, it.url, "old")
                        continue
                    if excl.about(it.title, it.text):  # a topic the channel no longer covers (funds): no model call at all
                        store.mark_seen(it.key, it.source, it.title, it.url, "excluded")
                        excluded_n += 1
                        continue
                    cls = classify(src, it)
                    if cls is None:  # not about any of the source's topics
                        store.mark_seen(it.key, it.source, it.title, it.url, "filtered")
                        filtered += 1
                        continue
                    it.topic, it.rank = cls
                    if not src.images:
                        it.image = ""
                    candidates.append((src, it))
        # source priority first, then topic priority (e.g. gold before silver before copper), then newest
        candidates.sort(key=lambda c: (c[0].priority, c[1].rank, -(c[1].published or _EPOCH).timestamp()))

        drafted = skipped = calls = 0
        outcome = {"auto": 0, "held": 0, "review": 0}
        banned = [x.name for x in sources]
        llm_error = ""
        for src, item in candidates:
            if drafted >= limit or calls >= limit * 3:  # cap model calls: protects free-tier quota
                break
            calls += 1
            try:
                post = await make_draft_from_item(cfg, store, writer, item, src.pillar, src.focus, src.link)
            except LLMError as e:
                llm_error = str(e)
                break
            if post is None:
                skipped += 1
                continue
            drafted += 1
            outcome[await dispatch_post(bot, cfg, store, state, post, chat_ids, banned, trusted=src.trusted)] += 1

        lines = [f"{drafted} {'پست' if auto_active(cfg, state) else 'پیش‌نویس'} از منابع ساخته شد."]
        if outcome["auto"]:
            lines.append(f"🕒 {outcome['auto']} پست خودکار زمان‌بندی شد.")
        if outcome["held"]:
            lines.append(f"⏸ {outcome['held']} پست مشکوک بود و برای بررسی شما فرستاده شد.")
        if not candidates and not errors:
            lines = ["مطلب جدید و مرتبطی در منابع پیدا نشد."]
        if skipped:
            lines.append(f"{skipped} مطلب بی‌اهمیت یا نامرتبط رد شد.")
        if filtered:
            lines.append(f"{filtered} مطلب خارج از موضوع‌های تعریف‌شده (فیلتر کلیدواژه) کنار گذاشته شد.")
        if excluded_n:
            lines.append(f"{excluded_n} مطلب دربارهٔ موضوع ممنوع (exclude_words) بود و کنار گذاشته شد.")
        left = len(candidates) - drafted - skipped
        if left > 0 and not llm_error:
            lines.append(f"{left} مطلب جدید دیگر هست؛ دوباره /fetch بزنید.")
        if llm_error:
            lines.append(f"⚠️ خطای مدل (مثلاً سقف رایگان): {llm_error}")
        if errors:
            lines.append("⚠️ منابع ناموفق:\n" + "\n".join(errors))
        return "\n".join(lines)


async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, writer = _ctx(context)
    if not _is_owner(update, cfg):
        return
    msg = update.effective_message
    if context.args and context.args[0].lower().startswith("http"):
        await msg.reply_text("در حال خواندن و بازنویسی صفحه…")
        try:
            async with new_http_client() as http:
                item = await fetch_article(http, context.args[0])
            post = await make_draft_from_item(cfg, store, writer, item, None)
        except SourceError as e:
            await msg.reply_text(f"خواندن منبع ناموفق بود: {e}")
            return
        except LLMError as e:
            await msg.reply_text(f"خطا در ارتباط با مدل: {e}")
            return
        if post is None:
            await msg.reply_text("این مطلب به موضوع کانال مربوط نبود و رد شد.")
            return
        await send_review(context.bot, update.effective_chat.id, post, cfg)
        return

    sources = context.application.bot_data["sources"]
    if not sources:
        await msg.reply_text("هنوز منبعی تعریف نشده. فایل sources.yaml را پر کنید و ربات را ری‌استارت کنید، یا یک آدرس بدهید: /fetch https://…")
        return
    await msg.reply_text(f"در حال بررسی {len(sources)} منبع…")
    await msg.reply_text(
        await run_fetch(
            context.bot, cfg, store, writer, sources, [update.effective_chat.id], cfg.max_new_per_poll,
            state=context.application.bot_data["auto"],
        )
    )


async def cmd_probe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, _, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    if not context.args or not context.args[0].lower().startswith("http"):
        await update.effective_message.reply_text("مثال: /probe https://example.com")
        return
    try:
        async with new_http_client() as http:
            report = await probe(http, context.args[0])
    except SourceError as e:
        await update.effective_message.reply_text(f"بررسی ناموفق بود: {e}")
        return
    await update.effective_message.reply_text(report[:4000], link_preview_options=NO_PREVIEW)


async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    sources = context.application.bot_data["sources"]
    if not sources:
        await update.effective_message.reply_text("منبعی تعریف نشده (فایل sources.yaml).")
        return
    seen = store.seen_counts()
    lines = [f"• {s.name} [{s.type}] — {seen.get(s.name, 0)} مطلب پردازش‌شده" for s in sources]
    auto = f"هر {cfg.source_poll_hours:g} ساعت" if cfg.source_poll_hours > 0 else "غیرفعال"
    await update.effective_message.reply_text("\n".join(lines) + f"\n\nبررسی خودکار: {auto} · حداکثر {cfg.max_new_per_poll} پیش‌نویس در هر بار")


POLL_RETRY_SECONDS = 600
POLL_MAX_RETRIES = 4


async def poll_sources(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, writer = _ctx(context)
    data = context.application.bot_data
    state: AutoState = data["auto"]
    summary = await run_fetch(
        context.bot, cfg, store, writer, data["sources"], sorted(cfg.owner_ids), cfg.max_new_per_poll, state=state,
    )
    log.info("source poll: %s", summary.replace("\n", " | "))
    # the model was overloaded (e.g. HTTP 503 "high demand"): try again in 10 minutes instead of waiting for the next round
    busy = "خطای مدل" in summary
    tries = data.get("poll_retries", 0)
    retrying = busy and tries < POLL_MAX_RETRIES
    if retrying:
        data["poll_retries"] = tries + 1
        context.job_queue.run_once(poll_sources, POLL_RETRY_SECONDS, name="poll_retry")
        summary += f"\n🔁 {POLL_RETRY_SECONDS // 60} دقیقهٔ دیگر دوباره تلاش می‌کنم ({tries + 1} از {POLL_MAX_RETRIES})."
    else:
        data["poll_retries"] = 0
    # in auto mode nobody reads /fetch output, so problems are pushed - but the same warning is not repeated every round
    if auto_active(cfg, state) and "⚠️" in summary:
        key = "|".join(ln for ln in summary.splitlines() if ln.startswith(("⚠️", "منابع")) or "—" in ln)
        first_of_series = not retrying or tries == 0
        if first_of_series and (busy or data.get("last_warn") != key):
            await notify_owners(context.bot, cfg, html.escape(summary), silent=True)
        data["last_warn"] = key
    else:
        data.pop("last_warn", None)


# ---------------------------------------------------------------- scheduler


STALE_AFTER = timedelta(hours=2)
DAILY_STALE = timedelta(minutes=45)


async def publish_due(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    state: AutoState = context.application.bot_data["auto"]
    if state.paused:  # emergency stop: nothing leaves, nothing is lost
        return
    now = datetime.now(timezone.utc)
    for post in store.due(now):
        if (post.pillar or "").startswith("daily:") and post.scheduled_at and now - parse_iso(post.scheduled_at) > DAILY_STALE:
            # timed posts (reports, news) lose their meaning when late: drop instead of publishing hours later
            store.reject(post.id)
            await notify_owners(context.bot, cfg, f"⌛ پست زمان‌دار #{post.id} دیر شده بود و منتشر نشد.", silent=True)
            continue
        if cfg.auto_publish and post.scheduled_at and now - parse_iso(post.scheduled_at) > STALE_AFTER:
            # the bot was off / paused for a long time: don't dump old news, move it to the next free slot
            slot = auto_times(now, cfg.tz, store.busy_times({post.id}), 1, cfg.auto_gap_minutes, cfg.auto_from, cfg.auto_to)
            store.schedule(post.id, slot[0])
            await notify_owners(
                context.bot, cfg, f"⏩ پست #{post.id} دیر شده بود و به {fmt_local(iso(slot[0]), cfg.tz)} منتقل شد.", silent=True,
            )
            continue
        ok, text = await publish_post(context.bot, cfg, store, post)
        await notify_owners(context.bot, cfg, text, reply_markup=delete_keyboard(post.id) if ok else None)


async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.application.bot_data
    try:
        write_heartbeat(d["store"])
        if time.monotonic() - d.get("snapshot_at", -1e9) >= stats.SNAPSHOT_EVERY:  # numbers for the admin overview
            d["snapshot_at"] = time.monotonic()
            snap = stats.snapshot(os.path.basename(os.getcwd()), d["cfg"], d["store"].db)
            stats.write_snapshot(stats.default_stats_dir(), snap["customer"], snap)
    except (OSError, ValueError) as e:  # a full disk (or a stats folder we may not write) must not stop the bot
        log.warning("heartbeat/snapshot not written: %s", e)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, _, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    context.application.bot_data["auto"].set_paused(True)
    await update.effective_message.reply_text(
        "⏸ متوقف شد. تا /resume هیچ پستی منتشر یا خودکار زمان‌بندی نمی‌شود (پست‌های زمان‌بندی‌شده در صف می‌مانند)."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, _, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    context.application.bot_data["auto"].set_paused(False)
    await update.effective_message.reply_text("▶️ ادامه پیدا کرد.")


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    state: AutoState = context.application.bot_data["auto"]
    if not cfg.auto_publish:
        status = "خاموش (همه چیز با تأیید شما)؛ برای روشن کردن در config.yaml مقدار auto_publish را true کنید."
    elif state.paused:
        status = "⏸ روشن ولی متوقف (pause)؛ برای ادامه /resume"
    else:
        status = "🤖 روشن و در حال کار" + (" (سطح «معتمد»: فقط منبع‌های trusted بدون تأیید منتشر می‌شوند)" if cfg.approval == "trusted" else "")
    await update.effective_message.reply_text(
        f"حالت خودکار: {status}\nپست در صف انتشار: {store.count_scheduled()} از حداکثر {cfg.auto_max_queue}\n"
        f"پست‌های منابع بلافاصله منتشر می‌شوند (حداقل {cfg.auto_gap_minutes} دقیقه فاصله، بین {cfg.auto_from} تا {cfg.auto_to})\nمشاهدهٔ صف: /queue"
    )


def _parked_line(p: Post, cfg: Config) -> str:
    kind = "💀 صف خطا" if p.status == "dead" else "❓ نامعلوم"
    return f"{kind} #{p.id} ({p.attempts} تلاش): {html.escape(headline_of(p.text) or p.topic)[:70]}\n   علت: {html.escape((p.error or '')[:120])}"


async def cmd_dead(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    parked = store.parked()
    if not parked:
        await update.effective_message.reply_text("صف خطا خالی است؛ هیچ پستی گیر نکرده.")
        return
    await update.effective_message.reply_text(
        "\n".join(_parked_line(p, cfg) for p in parked)
        + "\n\nارسال دوباره: /retry شماره\nاگر پستِ «نامعلوم» در کانال هست: /sent شماره", parse_mode=ParseMode.HTML,
    )


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    post = _post_from_args(store, context.args)
    if not post or not store.requeue(post.id, datetime.now(timezone.utc)):
        await update.effective_message.reply_text("پست پیدا نشد یا در صف خطا/نامعلوم نیست. فهرست: /dead")
        return
    await update.effective_message.reply_text(f"🔁 پست #{post.id} دوباره در صف انتشار قرار گرفت (چند ثانیهٔ دیگر ارسال می‌شود).")


async def cmd_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store, _ = _ctx(context)
    if not _is_owner(update, cfg):
        return
    post = _post_from_args(store, context.args)
    if not post or not store.confirm_sent(post.id):
        await update.effective_message.reply_text("پست پیدا نشد یا در وضعیت «نامعلوم» نیست. فهرست: /dead")
        return
    await update.effective_message.reply_text(f"✅ پست #{post.id} به‌عنوان منتشرشده ثبت شد.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("خطای داخلی رخ داد؛ جزئیات در لاگ سرور ثبت شد.")
        except TelegramError:
            pass


def respace_waiting(cfg: Config, store: Store) -> int:
    """Auto mode: posts that were scheduled for far-away slots (older behaviour) are moved to 'as soon as possible'."""
    waiting = [p for p in store.queue() if p.status == "scheduled" and p.source_url and not (p.pillar or "").startswith("daily:")]
    if not waiting:
        return 0
    times = auto_times(
        datetime.now(timezone.utc), cfg.tz, store.busy_times({p.id for p in waiting}), len(waiting),
        cfg.auto_gap_minutes, cfg.auto_from, cfg.auto_to,
    )
    for p, t in zip(sorted(waiting, key=lambda x: x.scheduled_at or ""), times):
        store.schedule(p.id, t)
    return len(waiting)


async def post_init(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    me = await app.bot.get_me()
    log.info("bot @%s ready; channel=%s owners=%s", me.username, cfg.channel, sorted(cfg.owner_ids))
    stuck = app.bot_data["store"].recover_sending()
    if stuck:  # stopped in the middle of a send: it may already be in the channel, so it is not resent on its own
        await notify_owners(
            app.bot, cfg,
            "❓ ربات هنگام ارسال این پست‌ها متوقف شده بود و معلوم نیست در کانال آمده‌اند: "
            + "، ".join(f"#{i}" for i in stuck) + "\nکانال را ببینید؛ اگر هست /sent شماره، اگر نیست /retry شماره (فهرست: /dead).",
        )
    if cfg.auto_publish:
        moved = respace_waiting(cfg, app.bot_data["store"])
        if moved:
            log.info("auto mode: %s waiting posts moved to the next minutes", moved)
        paused = app.bot_data["auto"].paused
        await notify_owners(
            app.bot, cfg,
            "🤖 ربات روشن شد؛ حالت خودکار فعال است." + (" ⏸ ولی در حالت توقف است (/resume)." if paused else " برای توقف اضطراری: /pause"),
            silent=True,
        )
    try:
        member = await app.bot.get_chat_member(cfg.channel, me.id)
        can_post = getattr(member, "can_post_messages", False)
        if cfg.platform == "telegram" and (member.status != "administrator" or not can_post):
            await notify_owners(
                app.bot, cfg,
                f"⚠️ ربات در کانال {html.escape(cfg.channel)} ادمین با دسترسی «Post messages» نیست؛ انتشار کار نمی‌کند.",
            )
    except TelegramError as e:
        await notify_owners(app.bot, cfg, f"⚠️ دسترسی به کانال {html.escape(cfg.channel)} ممکن نیست: {html.escape(str(e))}")


# ---------------------------------------------------------------- entrypoint


def build_app(cfg: Config) -> Application:
    set_bot_name(cfg.profile.bot_name)
    builder = Application.builder().post_init(post_init)
    bale_bot = plat.make_bot(cfg)
    app = (builder.bot(bale_bot) if bale_bot is not None else builder.token(cfg.bot_token)).build()
    app.bot_data.update(
        cfg=cfg,
        store=Store(cfg.db_path),
        writer=Writer(cfg, load_knowledge(cfg.knowledge_dir)),
        sources=load_sources(cfg.sources_file),
        auto=AutoState(cfg.auto_state_file),
    )
    private = filters.ChatType.PRIVATE
    for name, fn in [
        ("start", cmd_start), ("help", cmd_start), ("draft", cmd_draft), ("plan", cmd_plan),
        ("queue", cmd_queue), ("show", cmd_show), ("edit", cmd_edit),
        ("schedule", cmd_schedule), ("cancel", cmd_cancel), ("fetch", cmd_fetch), ("sources", cmd_sources), ("probe", cmd_probe),
        ("pause", cmd_pause), ("resume", cmd_resume), ("auto", cmd_auto),
        ("dead", cmd_dead), ("retry", cmd_retry), ("sent", cmd_sent),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & filters.PHOTO, on_photo))
    app.add_error_handler(on_error)
    plat.register(app, cfg)  # /id (همه‌جا) و /balecheck (فقط بله)
    if cfg.profile.feature("market_report"):  # optional vertical modules, switched on per customer in profile.yaml
        gozaresh.register(app, cfg)  # گزارش روزانه: ماژول مستقل (agent/gozaresh)
    if cfg.profile.feature("daily_calendar"):
        daily.register(app, cfg)  # جدول محتوای روزانه (agent/daily)؛ باید بعد از gozaresh ثبت شود
    if cfg.profile.feature("content_calendar"):
        planner.register(app, cfg)  # برنامهٔ هفتگی عمومی (agent/planner)، مستقل از صنعت
    app.job_queue.run_repeating(publish_due, interval=10, first=5)
    app.job_queue.run_repeating(heartbeat, interval=60, first=3)  # `main.py check --live` reads this
    if cfg.source_poll_hours > 0 and app.bot_data["sources"]:
        app.job_queue.run_repeating(poll_sources, interval=cfg.source_poll_hours * 3600, first=90)
    return app


def main() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:  # per-customer log file next to the settings (the journal keeps its own copy)
        os.makedirs("logs", exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler("logs/agent.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    # Bale supports only a few update types and ignores the filter; Telegram gets all of them
    build_app(cfg).run_polling(bootstrap_retries=-1, allowed_updates=Update.ALL_TYPES if cfg.platform == "telegram" else None)


if __name__ == "__main__":
    main()
