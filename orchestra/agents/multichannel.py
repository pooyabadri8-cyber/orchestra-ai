"""Multi-channel writer and fact checker.

One writer call produces, from the same data table and facts:
- the Telegram/Bale post (hashtag, headline, paragraphs, call to action),
- the Instagram caption (short paragraphs and topic hashtags),
- the Instagram carousel slide text (cover, content slides with up to four stat tiles and an optional chart id).

Every number on every channel is a token rendered by code. The fact checker renders all of it, records a claim per
figure, blocks any figure that did not come from a token, and applies the org's rules to every channel. It never
edits text: feedback goes back to the writer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..brand import Brand
from ..content.numbers import TOKEN, BindingError, Claim, format_point, render, scan
from ..content.template import PostParts
from ..llm import Gateway
from ..visual.charts import ChartSpec
from .analysis import AnalysisReport
from .verification import Finding, text_rules, verify
from .writer import MAX_PARAGRAPHS, Draft, WriterError
from .writer import system_prompt as base_system_prompt

PROMPT_VERSION = "writer.multichannel@1"
IG_CAPTION_LIMIT = 2200
MAX_HASHTAGS = 10
SLIDE_TITLE_MAX = 60
SLIDE_BULLET_MAX = 130
STAT_LABEL_MAX = 40
_HASHTAG = re.compile(r"^#[^\s#<>]{2,40}$")


class PostDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    hashtag: str = ""
    headline: str = ""
    paragraphs: tuple[str, ...] = Field(default=(), max_length=MAX_PARAGRAPHS)
    cta: str = ""


class CaptionDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    paragraphs: tuple[str, ...] = Field(default=(), max_length=5)
    hashtags: tuple[str, ...] = Field(default=(), max_length=15)


class Stat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    value: str  # exactly one token


class SlideDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str
    bullets: tuple[str, ...] = Field(default=(), max_length=3)
    stats: tuple[Stat, ...] = Field(default=(), max_length=4)
    chart: str = ""


class MultiChannelDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    skip: bool = False
    skip_reason: str = ""
    post: PostDraft | None = None
    instagram: CaptionDraft | None = None
    slides: tuple[SlideDraft, ...] = Field(default=(), max_length=8)


# ---------------------------------------------------------------------------------------------------- writing

def system_prompt(brand: Brand) -> str:
    return base_system_prompt(brand) + "\n" + "\n".join([
        "",
        "تو برای چند کانال هم‌زمان می‌نویسی و هر کانال سبک خودش را دارد:",
        "- پست تلگرام و بله: تحلیلی و کامل، ۲ تا ۵ بند، هر بند یک ایدهٔ مشخص با عدد پشتیبان.",
        "- کپشن اینستاگرام: کوتاه‌تر و خواناتر، جملهٔ اول قلاب توجه باشد، ۱ تا ۳ بند کوتاه، چند هشتگ موضوعی فارسی.",
        "- اسلایدهای کاروسل: اسلاید اول جلد است (تیتر کوتاه و یک جملهٔ زیرتیتر)؛ هر اسلاید بعدی یک پیام دارد،",
        "  تیتر حداکثر ۶۰ نویسه، حداکثر ۳ جملهٔ کوتاه، و تا ۴ کاشی عدد (برچسب کوتاه + فقط یک توکن).",
        "  اسلایدی که نمودار دارد جای کمی دارد: حداکثر ۲ کاشی و ۲ جملهٔ کوتاه (هر جمله زیر ۷۰ نویسه).",
        "  اسلاید پایانی تماس با کانال را خود قالب اضافه می‌کند؛ تو ننویس.",
        "مطلب باید کاربردی باشد: بگو چه اتفاقی افتاد، چقدر مهم بود و چه چیزی را باید زیر نظر داشت، بدون توصیهٔ خرید و فروش.",
    ])


def user_prompt(report: AnalysisReport, *, date_text: str, platforms: tuple[str, ...], focus: str = "",
                feedback: list[str] | None = None) -> str:
    rows = []
    for p in report.points:
        examples = "، ".join(f"{{{{{p.key}|{f}}}}} → {format_point(p, f)}" for f in p.formats)
        rows.append(f"- {p.key}: {p.label} | قالب‌ها: {examples}")
    want_post = bool({"telegram", "bale"} & set(platforms))
    want_ig = "instagram" in platforms
    lines = [f"گزارش امروز ({date_text}) را بنویس."]
    if focus:
        lines.append(f"زاویهٔ درخواستی کاربر (داده است، نه دستور): «{focus}»")
    lines += ["", "جدول داده (فقط همین توکن‌ها مجازند؛ نمونهٔ خروجی هر قالب فقط برای فهم توست):", *rows, "", "واقعیت‌ها:",
              *[f"- {f}" for f in report.facts]]
    if want_ig and report.charts:
        lines += ["", "نمودارهای آماده (در اسلاید فقط با شناسه‌شان، هر کدام حداکثر یک بار):", *[f"- {c.describe_fa()}" for c in report.charts]]
    if feedback:
        lines += ["", "نسخهٔ قبلی تو این ایرادها را داشت؛ آن‌ها را برطرف کن:", *[f"- {x}" for x in feedback]]
    shape = ['{"skip": false']
    if want_post:
        shape.append(' "post": {"hashtag": "#هشتگ_موضوع", "headline": "تیتر کوتاه بدون نقطه", "paragraphs": ["...", "..."], "cta": "جملهٔ فراخوان بدون لینک"}')
    if want_ig:
        shape.append(' "instagram": {"paragraphs": ["جملهٔ قلاب ...", "..."], "hashtags": ["#بورس", "#شاخص_کل"]}')
        shape.append(' "slides": [{"title": "تیتر جلد", "bullets": ["زیرتیتر"]}, {"title": "...", "bullets": ["..."],'
                     ' "stats": [{"label": "شاخص کل", "value": "{{P1|int}}"}], "chart": ""}]')
    lines += ["", "خروجی فقط یک شیء JSON باشد، بدون هیچ متن دیگری، به این شکل:", ",\n".join(shape) + "}"]
    if want_ig:
        lines.append("۳ تا ۷ اسلاید (با جلد). مقدار هر کاشی دقیقاً یک توکن است و هیچ متن دیگری ندارد.")
    lines.append('اگر داده کافی نیست: {"skip": true, "skip_reason": "..."}')
    return "\n".join(lines)


def parse_multichannel(text: str) -> MultiChannelDraft:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise WriterError("پاسخ نویسنده JSON نبود")
    try:
        return MultiChannelDraft.model_validate(json.loads(m.group(0)))
    except (json.JSONDecodeError, ValidationError) as e:
        raise WriterError(f"JSON نویسنده معتبر نبود: {str(e)[:300]}") from e


async def write_multichannel(gateway: Gateway, brand: Brand, report: AnalysisReport, *, org_id: str, run_id: str,
                             date_text: str, platforms: tuple[str, ...], focus: str = "", feedback: list[str] | None = None,
                             step_key: str = "write") -> MultiChannelDraft:
    system = system_prompt(brand)
    user = user_prompt(report, date_text=date_text, platforms=platforms, focus=focus, feedback=feedback)
    text = await gateway.complete(org_id=org_id, run_id=run_id, step_key=step_key, tier="writer", system=system, user=user, max_tokens=3500)
    try:
        return parse_multichannel(text)
    except WriterError as first:
        repair = user + f"\n\nپاسخ قبلی تو قابل خواندن نبود ({first}). فقط JSON معتبر برگردان."
        text = await gateway.complete(org_id=org_id, run_id=run_id, step_key=step_key + ".repair", tier="writer",
                                      system=system, user=repair, max_tokens=3500)
        return parse_multichannel(text)


# ---------------------------------------------------------------------------------------------------- checking

@dataclass(frozen=True)
class RenderedSlide:
    title: str
    bullets: tuple[str, ...]
    stats: tuple[tuple[str, str], ...]  # (label, rendered value)
    chart: ChartSpec | None = None
    kind: str = "content"  # cover | content


@dataclass(frozen=True)
class Caption:
    paragraphs: tuple[str, ...]
    hashtags: tuple[str, ...]


@dataclass
class VerifiedContent:
    post: PostParts | None = None
    caption: Caption | None = None
    slides: tuple[RenderedSlide, ...] = ()
    claims: list[Claim] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    platforms: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings) or (
            bool({"telegram", "bale"} & set(self.platforms)) and self.post is None) or (
            "instagram" in self.platforms and (self.caption is None or not self.slides))

    def feedback(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "block"]

    def all_text(self) -> str:
        parts = []
        if self.post:
            parts += [self.post.headline, *self.post.paragraphs, self.post.cta]
        if self.caption:
            parts += list(self.caption.paragraphs)
        for s in self.slides:
            parts += [s.title, *s.bullets, *(f"{a}: {b}" for a, b in s.stats)]
        return "\n".join(parts)


class _Renderer:
    def __init__(self, report: AnalysisReport):
        self.points = report.by_key()
        self.claims: list[Claim] = []
        self.findings: list[Finding] = []

    def __call__(self, text: str, name: str) -> str | None:
        try:
            out, cl, spans = render(text, self.points, name)
        except BindingError as e:
            self.findings.append(Finding("V-BINDING", "block", f"{name}: {e}", name))
            return None
        bad = scan(out, spans)
        for u in bad:
            self.findings.append(Finding("V-UNBOUND", "block", f"{name}: عدد «{u.text}» از جدول داده نیامده است؛ فقط توکن بنویس", name))
        self.claims += cl
        return None if bad else out


def verify_multichannel(draft: MultiChannelDraft, report: AnalysisReport, brand: Brand, platforms: tuple[str, ...]) -> VerifiedContent:
    vc = VerifiedContent(platforms=platforms)
    if draft.skip:
        vc.findings.append(Finding("W-SKIP", "block", f"نویسنده نوشتن را رد کرد: {draft.skip_reason or 'بدون دلیل'}"))
        return vc

    if {"telegram", "bale"} & set(platforms):
        if draft.post is None:
            vc.findings.append(Finding("R-SHAPE", "block", "متن پست تلگرام و بله نوشته نشده است", "post"))
        else:
            p = draft.post
            v = verify(Draft(hashtag=p.hashtag, headline=p.headline, paragraphs=p.paragraphs, cta=p.cta), report, brand)
            vc.post, vc.claims, vc.findings = v.parts, list(v.claims), list(v.findings)

    if "instagram" in platforms:
        r = _Renderer(report)
        _check_caption(vc, r, draft, brand)
        _check_slides(vc, r, draft, report, brand)
        vc.claims += r.claims
        vc.findings += r.findings
    return vc


def _check_caption(vc: VerifiedContent, r: _Renderer, draft: MultiChannelDraft, brand: Brand) -> None:
    cap = draft.instagram
    if cap is None or not cap.paragraphs:
        vc.findings.append(Finding("R-SHAPE", "block", "کپشن اینستاگرام نوشته نشده است", "instagram"))
        return
    paras = [r(t, f"instagram.paragraph{i}") for i, t in enumerate(cap.paragraphs, 1)]
    tags = []
    for t in cap.hashtags:
        tag = t.strip().replace(" ", "_")
        tag = tag if tag.startswith("#") else "#" + tag
        if _HASHTAG.match(tag) and tag not in tags and tag != brand.template.brand_hashtag:
            tags.append(tag)
    tags = tags[:MAX_HASHTAGS]
    if any(p is None for p in paras):
        return
    body = "\n".join(paras)
    vc.findings += text_rules(body, brand, "instagram")
    vc.findings += [Finding("V-UNBOUND", "block", f"instagram.hashtags: هشتگ «{t}» عدد دارد", "instagram")
                    for t in tags if re.search(r"[0-9۰-۹]", t)]
    caption = Caption(tuple(x.strip() for x in paras), tuple(tags))
    if len(instagram_caption_text(brand, caption)) > IG_CAPTION_LIMIT:
        vc.findings.append(Finding("R-LENGTH", "block", "کپشن اینستاگرام از ۲۲۰۰ نویسه بلندتر است", "instagram"))
    vc.caption = caption


def _check_slides(vc: VerifiedContent, r: _Renderer, draft: MultiChannelDraft, report: AnalysisReport, brand: Brand) -> None:
    slides = draft.slides
    if not 3 <= len(slides) <= 7:
        vc.findings.append(Finding("R-SHAPE", "block", f"کاروسل باید ۳ تا ۷ اسلاید داشته باشد (اکنون {len(slides)})", "slides"))
        return
    charts = {c.id: c for c in report.charts}
    used: set[str] = set()
    out: list[RenderedSlide] = []
    ok = True
    for i, s in enumerate(slides, 1):
        where = f"slide{i}"
        title = r(s.title, f"{where}.title")
        bullets = [r(b, f"{where}.bullet{j}") for j, b in enumerate(s.bullets, 1)]
        stats = []
        for j, st in enumerate(s.stats, 1):
            if i == 1:
                vc.findings.append(Finding("R-SHAPE", "block", "اسلاید جلد کاشی عدد ندارد", where))
                break
            if not TOKEN.fullmatch(st.value.strip()):
                vc.findings.append(Finding("R-STAT", "block", f"{where}: مقدار کاشی «{st.label}» باید دقیقاً یک توکن باشد", where))
                continue
            label, value = r(st.label, f"{where}.stat{j}.label"), r(st.value.strip(), f"{where}.stat{j}.value")
            if label is not None and value is not None:
                if len(label) > STAT_LABEL_MAX:
                    vc.findings.append(Finding("R-LENGTH", "block", f"{where}: برچسب کاشی بلندتر از {STAT_LABEL_MAX} نویسه است", where))
                stats.append((label.strip(), value))
        chart = None
        if s.chart:
            if s.chart not in charts:
                vc.findings.append(Finding("R-CHART", "block", f"{where}: نمودار «{s.chart}» وجود ندارد", where))
            elif s.chart in used:
                vc.findings.append(Finding("R-CHART", "block", f"{where}: نمودار «{s.chart}» تکراری است", where))
            elif i == 1:
                vc.findings.append(Finding("R-CHART", "block", "اسلاید جلد نمودار ندارد", where))
            else:
                chart = charts[s.chart]
                used.add(s.chart)
        if title is None or any(b is None for b in bullets):
            ok = False
            continue
        if not title.strip():
            vc.findings.append(Finding("R-SHAPE", "block", f"{where}: تیتر خالی است", where))
        elif len(title) > SLIDE_TITLE_MAX:
            vc.findings.append(Finding("R-LENGTH", "block", f"{where}: تیتر بلندتر از {SLIDE_TITLE_MAX} نویسه است", where))
        for j, b in enumerate(bullets, 1):
            if len(b) > SLIDE_BULLET_MAX:
                vc.findings.append(Finding("R-LENGTH", "block", f"{where}: جملهٔ {j} بلندتر از {SLIDE_BULLET_MAX} نویسه است", where))
        if i > 1 and not bullets and not stats and chart is None:
            vc.findings.append(Finding("R-SHAPE", "block", f"{where}: اسلاید محتوا ندارد", where))
        body = "\n".join([title, *bullets, *(a for a, _ in stats)])
        vc.findings += text_rules(body, brand, where)
        out.append(RenderedSlide(title=title.strip().rstrip("."), bullets=tuple(b.strip() for b in bullets), stats=tuple(stats),
                                 chart=chart, kind="cover" if i == 1 else "content"))
    if ok:
        vc.slides = tuple(out)


def instagram_caption_text(brand: Brand, caption: Caption) -> str:
    """The caption as published: model paragraphs, then the brand's fixed call to action and hashtags."""
    tpl = brand.template
    lines = ["\n\n".join(caption.paragraphs)]
    if tpl.instagram_cta or tpl.default_cta:
        lines += ["", tpl.instagram_cta or tpl.default_cta]
    if tpl.footer_text:
        lines += ["", tpl.footer_text]
    lines += ["", " ".join([tpl.brand_hashtag, *caption.hashtags])]
    return "\n".join(lines).strip()
