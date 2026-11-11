"""Fact Verification and the rule layer of Compliance (deterministic, no LLM).

1. Every token is rendered from its data point; every rendered figure becomes a Claim.
2. The scanner finds any figure that did not come from a token.
3. Rule checks: forbidden phrases and topics, links, leftover model artefacts, untranslated text, length, shape.

A finding with severity "block" stops the post. The checks never edit the text; only the writer writes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..brand import Brand, normalize
from ..content.numbers import BindingError, Claim, render, scan
from ..content.template import PostParts, telegram_html, visible_length
from .analysis import AnalysisReport
from .writer import Draft

TG_LIMIT = 4096
LEAK_MARKERS = ("```", "**", "[[", "]]", "as an ai", "به عنوان یک مدل", "skip")
_LATIN = re.compile(r"[A-Za-z]")
_LETTER = re.compile(r"[A-Za-z؀-ۿ]")
_LINK = re.compile(r"https?://|www\.|t\.me/|@\w{3,}", re.I)
_MARKUP = re.compile(r"</?[a-zA-Z][^>]*>")


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str  # block | warn
    message: str
    field: str = ""


@dataclass
class Verified:
    parts: PostParts | None
    claims: list[Claim] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.parts is None or any(f.severity == "block" for f in self.findings)

    def feedback(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "block"]


def verify(draft: Draft, report: AnalysisReport, brand: Brand) -> Verified:
    if draft.skip:
        return Verified(None, findings=[Finding("W-SKIP", "block", f"نویسنده نوشتن را رد کرد: {draft.skip_reason or 'بدون دلیل'}")])
    points = report.by_key()
    fields = {"hashtag": draft.hashtag, "headline": draft.headline, "cta": draft.cta}
    fields.update({f"paragraph{i}": p for i, p in enumerate(draft.paragraphs, 1)})
    rendered: dict[str, str] = {}
    claims: list[Claim] = []
    findings: list[Finding] = []
    for name, text in fields.items():
        try:
            out, cl, spans = render(text, points, name)
        except BindingError as e:
            findings.append(Finding("V-BINDING", "block", f"{name}: {e}", name))
            continue
        for u in scan(out, spans):
            findings.append(Finding("V-UNBOUND", "block", f"{name}: عدد «{u.text}» از جدول داده نیامده است؛ فقط توکن بنویس", name))
        rendered[name] = out
        claims += cl
    if findings:
        return Verified(None, claims, findings)

    parts = PostParts(hashtag=rendered["hashtag"], headline=rendered["headline"].strip().rstrip("."),
                      paragraphs=tuple(rendered[f"paragraph{i}"].strip() for i in range(1, len(draft.paragraphs) + 1)),
                      cta=rendered["cta"])
    findings += rule_checks(parts, brand)
    return Verified(parts, claims, findings)


def rule_checks(parts: PostParts, brand: Brand) -> list[Finding]:
    out: list[Finding] = []
    body = "\n".join([parts.headline, *parts.paragraphs, parts.cta])
    if len(parts.paragraphs) < 2:
        out.append(Finding("R-SHAPE", "block", "پست باید دست‌کم دو بند داشته باشد"))
    if not parts.headline.strip():
        out.append(Finding("R-SHAPE", "block", "تیتر خالی است"))
    elif len(parts.headline) > 90:
        out.append(Finding("R-HEADLINE", "warn", f"تیتر {len(parts.headline)} نویسه است (بیش از ۹۰)"))
    out += text_rules(body, brand)
    if visible_length(telegram_html(brand.template, parts)) > TG_LIMIT:
        out.append(Finding("R-LENGTH", "block", "پست از حد تلگرام بلندتر است"))
    return out


def text_rules(body: str, brand: Brand, where: str = "") -> list[Finding]:
    """Rules every piece of model text must pass, on any channel."""
    out: list[Finding] = []
    n = normalize(body)
    pre = f"{where}: " if where else ""
    hits = sorted({p for p in brand.guard_phrases if normalize(p) in n})
    if hits:
        out.append(Finding("R-GUARD", "block", pre + "عبارت ممنوع: " + "، ".join(hits[:3]), where))
    if brand.excluded(body):
        out.append(Finding("R-EXCLUDED", "block", pre + "متن دربارهٔ موضوع ممنوع (مثلاً صندوق‌ها) حرف می‌زند", where))
    if _LINK.search(body):
        out.append(Finding("R-LINK", "block", pre + "لینک یا آی‌دی در متن نویسنده هست", where))
    if _MARKUP.search(body):
        out.append(Finding("R-MARKUP", "block", pre + "تگ HTML در متن نویسنده هست؛ قالب‌بندی فقط کار قالب است", where))
    if any(m in n for m in LEAK_MARKERS):
        out.append(Finding("R-LEAK", "block", pre + "نشانهٔ خروجی خراب مدل در متن هست", where))
    latin, letters = len(_LATIN.findall(body)), len(_LETTER.findall(body))
    if letters and latin / letters > 0.35:
        out.append(Finding("R-LATIN", "block", pre + "بخش زیادی از متن انگلیسی است", where))
    return out
