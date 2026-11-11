"""Building the text of the daily posts: material for the model, prompts, and the data-only report post.

No network and no Telegram here (see handlers.py), so everything in this file is testable offline.
"""
from __future__ import annotations

import html
import re
from datetime import datetime

from ..gozaresh.parse import Report, Row
from ..gozaresh.report import format_report, gregorian_to_jalali, money_text
from ..sources import Item
from ..template import SEP, Template

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
WEEKDAY_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # python weekday() order
GOLD_WORDS = ["طلا", "سکه", "اونس", "gold", "bullion", "نقره", "silver", "فلزات گرانبها"]
CAPITAL_WORDS = ["افزایش سرمایه"]
SNIPPET = 220


def fa(text: str) -> str:
    return text.translate(_FA)


def jalali_date(now: datetime) -> str:
    y, m, d = gregorian_to_jalali(now.year, now.month, now.day)
    return fa(f"{y}/{m:02d}/{d:02d}")


def weekday_fa(now: datetime) -> str:
    return WEEKDAY_FA[now.weekday()]


# ------------------------------------------------------------------ the data-only report post


def report_post(tpl: Template, rep: Report, now: datetime, persian: bool = True) -> str:
    """The /gozaresh numbers in the channel's fixed layout. No model involved, no source line."""
    lines = format_report(rep, now, persian).splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("📈"))
    body = [ln for ln in lines[start:] if not ln.startswith(("⚠️", "ℹ️", "منبع:"))]
    while body and not body[-1].strip():
        body.pop()
    clock = fa(now.strftime("%H:%M"))
    head = f"گزارش بازار سرمایه | {jalali_date(now)} — ساعت {clock}"
    brand = tpl.brand_hashtag.strip()
    return "\n".join([
        brand, "#گزارش_بازار", "", "",
        f"📌  <b>{head}</b>", "",
        *body, "", "",
        SEP, tpl.footer(),
    ]).rstrip()


# ------------------------------------------------------------------ material for the model


def _signed(x: float, nd: int = 0) -> str:
    return fa(("+" if x > 0 else "−" if x < 0 else "") + f"{abs(x):,.{nd}f}").replace(",", "٬")


def _rows(rows: list[Row]) -> str:
    return "\n".join(f"{fa(str(i))}. {r.name}: {money_text(r.amount)}" for i, r in enumerate(rows, 1)) or "—"


def report_material(rep: Report, now: datetime) -> str:
    idx = rep.index
    flow = rep.retail_flow
    return "\n".join([
        f"آخرین داده‌های ثبت‌شده در تاریخ {jalali_date(now)} ({weekday_fa(now)}) ساعت {fa(now.strftime('%H:%M'))}:",
        f"شاخص کل: {fa(f'{idx.value:,.0f}').replace(',', '٬')} (تغییر {_signed(idx.change)} واحد، {_signed(idx.pct, 2)} درصد)",
        f"ارزش معاملات خرد: {money_text(rep.retail_value)}",
        f"{'ورود' if flow.value >= 0 else 'خروج'} پول حقیقی (معاملات خرد): {money_text(type(flow)(abs(flow.value), flow.unit))}",
        "بیشترین ورود پول حقیقی به سهم:\n" + _rows(rep.top_in_stocks),
        "بیشترین خروج پول حقیقی از سهم:\n" + _rows(rep.top_out_stocks),
        "بیشترین ورود پول حقیقی به گروه:\n" + _rows(rep.top_in_groups),
        "بیشترین خروج پول حقیقی از گروه:\n" + _rows(rep.top_out_groups),
    ])


def items_material(items: list[Item], limit: int = 40, snippet: int = SNIPPET) -> str:
    """Numbered headlines with a short snippet. The model never sees which site/channel an item came from."""
    out = []
    for i, it in enumerate(items[:limit], 1):
        body = re.sub(r"\s+", " ", it.text or "").strip()
        title = re.sub(r"\s+", " ", it.title or "").strip()
        if body.startswith(title[:40]):
            body = body[len(title):].strip()
        out.append(f"[{fa(str(i))}] {title}" + (f"\n    {body[:snippet]}" if body else ""))
    return "\n".join(out)


def match_items(items: list[Item], words: list[str]) -> list[Item]:
    ws = [w.lower() for w in words if w]
    return [it for it in items if any(w in f"{it.title} {it.text}".lower() for w in ws)]


def symbol_candidates(rep: Report, used: list[str], day: int) -> list[tuple[str, str]]:
    """Items of the day in the order they are tried: (kind 'سهم'|'گروه', name). Alternates stock/group by day;
    names used recently come last."""
    stocks = [("سهم", r.name) for r in rep.top_in_stocks[:4]]
    groups = [("گروه", r.name) for r in rep.top_in_groups[:3]]
    order = (stocks + groups) if day % 2 == 0 else (groups + stocks)
    return [c for c in order if c[1] not in used] + [c for c in order if c[1] in used]


def industry_candidates(rep: Report, used: list[str]) -> list[tuple[str, str]]:
    """Industry groups (never single stocks) with the most real-money inflow; recently used names come last."""
    order = [("گروه", r.name) for r in rep.top_in_groups[:4]]
    return [c for c in order if c[1] not in used] + [c for c in order if c[1] in used]


def pick_symbol(rep: Report, used: list[str], day: int) -> tuple[str, str] | None:
    cands = symbol_candidates(rep, used, day)
    return cands[0] if cands else None


def force_headline(text: str, headline: str) -> str:
    """Replace the 📌 headline with an exact, code-made one (used for the dated news digest)."""
    return re.sub(r"^(📌\s*)<b>.*?</b>", lambda m: f"{m.group(1)}<b>{html.escape(headline)}</b>", text, count=1, flags=re.M)


# ------------------------------------------------------------------ prompts

COMMON = (
    "ساختار: 📌 تیتر، سپس ۲ تا ۴ بند 🔹 و در آخر یک فراخوان ☝ بی‌لینک. جمله‌ها کوتاه و روشن، لحن حرفه‌ای و بی‌طرف. "
    "اعداد و تاریخ‌ها را با ارقام فارسی و دقیق بنویس."
)


def news_task(now: datetime) -> tuple[str, str, str]:
    headline = f"خبرهای موثر بر بازار {weekday_fa(now)} {jalali_date(now)}"
    task = (
        f"پست «خبرهای موثر بر بازار» امروز را بنویس؛ این اولین پست روز کانال است، پس فقط تازه‌ترین و مهم‌ترین رویدادها را بیاور. "
        "از میان خبرهای زیر حداکثر ۵ خبر را انتخاب کن و به این ترتیب اولویت بده: "
        "(۱) جدیدترین تحولات جنگ و تنش ایران و آمریکا و هر جنگ یا درگیری دیگری که می‌تواند روی بازارها اثر بگذارد (خاورمیانه، تنگهٔ هرمز، اوکراین و ...)؛ "
        "(۲) دلار، ارز و نرخ‌ها؛ (۳) نفت، انرژی و طلا؛ (۴) مذاکره و توافق‌ها و تحریم‌ها؛ (۵) تصمیم‌های مهم اقتصادی. "
        "اگر خبر تازه‌ای در یک دسته نیست، آن دسته را ننویس. خبر ورزشی، فرهنگی، حاشیه، تکراری و کم‌اهمیت نیاور.\n"
        "هر خبر یک بند 🔹 است به شکل «برچسب کوتاه: یک تا دو جمله» (مثلاً «ترامپ: …»، «حوثی‌ها: …»، «روسیه و آمریکا: …»). "
        "هر ادعا را به گوینده‌اش نسبت بده (چه کسی گفته)، بدون صفت و قضاوت؛ اگر دو روایت متفاوت از یک رویداد هست، هر دو را با نسبت‌دادن بنویس. "
        "خبر قطعی‌نشده را قطعی ننویس. حداکثر ۵ بند.\n"
        f"تیتر دقیقاً همین باشد: {headline}\n"
        "اگر هیچ خبر مهمی برای بازار نیست SKIP.\n" + COMMON
    )
    return headline, task, "#اخبار_موثر_بر_بازار"


def open_task(now: datetime) -> tuple[str, str]:
    task = (
        f"تحلیل کوتاه «آغاز بازار» امروز ({weekday_fa(now)} {jalali_date(now)}) را بنویس که قبل از ساعت ۹:۳۰ منتشر می‌شود. "
        "نام هیچ سهم یا نماد خاصی را نیاور. بر پایهٔ سه چیز: آخرین داده‌های بازار (اگر ارزش معاملات صفر است یعنی بازار هنوز شروع نشده؛ از عددش حرف نزن)، "
        "مطالب و تحلیل‌های اخیر بازار، و خبرهای مهم. بگو فضای امروز چیست، چه عواملی اثر دارند و چه چیزهایی را باید زیر نظر داشت. "
        "پیش‌بینی قیمت یا شاخص نده.\n" + COMMON
    )
    return task, "#تحلیل_بازار"


def symbol_task(now: datetime, kind: str, name: str) -> tuple[str, str]:
    task = (
        f"تحلیل «{kind} روز» را دربارهٔ «{name}» بنویس ({weekday_fa(now)} {jalali_date(now)}). این {kind} امروز در فهرست بیشترین "
        "ورود پول حقیقی است. بر پایهٔ داده‌ها توضیح بده ورود پول به آن چه اندازه است، در مقایسه با بقیهٔ فهرست و بازار چه جایگاهی دارد و "
        "اگر مطلب مرتبطی در داده‌ها هست، آن را هم بیاور. علت‌یابی و وعده نده؛ اگر دلیل ورود پول در داده‌ها نیست، نگو دلیلش چیست. "
        f"توصیهٔ خرید و پیش‌بینی قیمت ممنوع. اگر «{name}» یک صندوق سرمایه‌گذاری یا ETF است (نه سهم شرکت یا گروه صنعت)، "
        "فقط یک کلمه بنویس: SKIP\n" + COMMON
    )
    return task, "#تحلیل_نماد" if kind == "سهم" else "#تحلیل_صنعت"


def capital_headline(now: datetime) -> str:
    return f"سهم‌های دارای افزایش سرمایه | {weekday_fa(now)} {jalali_date(now)}"


def capital_task(now: datetime) -> tuple[str, str]:
    task = (
        f"پست روزانهٔ «سهم‌هایی که افزایش سرمایه داده‌اند» ({weekday_fa(now)} {jalali_date(now)}) را بنویس. "
        "این پست تنها استثنای قاعدهٔ «پست دربارهٔ سهم نساز» است. از میان خبرهای زیر، سهم‌هایی را معرفی کن که خبر افزایش سرمایه‌شان آمده "
        "(مجوز یا تصمیم هیئت‌مدیره، مجمع، ثبت، پذیره‌نویسی و ...). حداکثر ۶ سهم، مهم‌ترین‌ها و تازه‌ترین‌ها اول. "
        "هر سهم یک بند 🔹 به شکل «نماد: ...» و فقط با اطلاعاتی که در خبر هست: درصد یا مبلغ افزایش سرمایه، محل تامین (آورده نقدی، "
        "سود انباشته، تجدید ارزیابی دارایی‌ها) و مرحلهٔ کار. اگر عددی در خبر نیست ننویس و حدس نزن. "
        "فقط گزارش و معرفی باشد: توصیهٔ خرید و فروش، ارزنده یا ارزان‌بودن سهم، پیش‌بینی اثر روی قیمت و وعدهٔ سود ممنوع است. "
        "یک خبر تکراری را فقط یک بار بیاور. اگر هیچ خبر روشنی از افزایش سرمایه نیست، SKIP.\n" + COMMON
    )
    return task, "#افزایش_سرمایه"


def close_task(now: datetime) -> tuple[str, str]:
    task = (
        f"تحلیل «بعد از بازار» امروز ({weekday_fa(now)} {jalali_date(now)}) را بنویس: شاخص کل و تغییرش، ارزش معاملات خرد، "
        "ورود یا خروج پول حقیقی، گروه‌های صنعت پیشرو در ورود و خروج پول (نه تک‌تک سهم‌ها) و عوامل اثرگذار امروز (از خبرها و تحلیل‌های داده‌شده). "
        "جمع‌بندی کوتاه از رفتار بازار بده؛ پیش‌بینی قیمت یا شاخص و توصیهٔ خرید و فروش نده. "
        "نام هیچ سهم یا نماد خاصی را نیاور؛ فقط شاخص، کل بازار و گروه‌های صنعت.\n" + COMMON
    )
    return task, "#تحلیل_بازار"


def gold_task(now: datetime) -> tuple[str, str]:
    task = (
        f"تحلیل «طلا» امروز ({weekday_fa(now)} {jalali_date(now)}) را بنویس. فقط از مطلب‌های داده‌شده: قیمت‌ها و تغییرشان، "
        "عوامل اثرگذار (دلار، نرخ بهره، تنش‌های منطقه‌ای، خرید بانک‌های مرکزی و ...) و پیش‌بینی نهادها یا بانک‌ها. "
        "پیش‌بینی هر نهاد را فقط با نام همان نهاد و به‌صورت «از نظر …» بنویس، نه به‌عنوان نظر خودت؛ "
        "قیمت دلاری را با قیمت داخلی قاطی نکن و واحدها را درست بنویس. توصیهٔ خرید و فروش ممنوع.\n" + COMMON
    )
    return task, "#طلا"
