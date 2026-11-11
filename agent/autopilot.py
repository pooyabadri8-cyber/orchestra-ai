"""Autopilot support: a rule-based safety check for posts that are published without human approval, and the pause flag.

The check is deliberately simple and strict. It is NOT a fact checker: it only catches things that go wrong
mechanically (cut-off text, leftover placeholders, untranslated English, buy/sell advice phrases, links/@ids,
a source name leaking into the text, repeated headline). A post that fails any rule is not published; it is sent to the
owners as a normal draft with the reasons.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import _parse_hhmm
from .template import BULLET, HEADLINE, SEP

TG_LIMIT = 4096
MIN_CORE_CHARS = 180
MAX_LATIN_RATIO = 0.35

# phrases that must never appear in an auto-published post come from the customer profile (profile.yaml: guard_phrases),
# e.g. a financial channel lists «سود تضمینی»; a bakery lists nothing.
LEAK_MARKERS = ["```", "**", "skip", "<source_material", "[[", "]]", "as an ai", "به عنوان یک مدل"]
TERMINATORS = tuple(".!؟?…»)\"'”’٪%:؛")

_TAGS = re.compile(r"<[^>]+>")
_LATIN = re.compile(r"[A-Za-z]")
_LETTER = re.compile(r"[A-Za-z؀-ۿ]")


def _norm(s: str) -> str:
    s = s.replace("‌", " ").replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه")
    return re.sub(r"\s+", " ", s).strip().lower()


def core_of(text: str) -> str:
    """Post without the fixed footer (everything from the ➖➖➖ line on)."""
    out = []
    for ln in text.strip().splitlines():
        if ln.strip() == SEP:
            break
        out.append(ln)
    return "\n".join(out).strip()


def headline_of(text: str) -> str:
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(HEADLINE):
            return html.unescape(_TAGS.sub("", s[len(HEADLINE):])).strip()
    return ""


def check_post(
    text: str, recent_texts: list[str] | None = None, banned_names: list[str] | None = None, exclusions=None,
    guard_phrases: list[str] | None = None,
) -> list[str]:
    """Return the list of reasons why this post must NOT be auto-published (empty list = looks fine).
    `exclusions` (agent.exclude.Exclusions) = topics the channel no longer posts about;
    `guard_phrases` = the customer's own never-publish phrases (compared after normalising Persian letters)."""
    reasons: list[str] = []
    if len(text) > TG_LIMIT:
        reasons.append("طولانی‌تر از حد تلگرام")
    core = core_of(text)
    plain = html.unescape(_TAGS.sub("", core))
    if HEADLINE not in core or BULLET not in core:
        reasons.append("قالب پست ناقص است (تیتر یا بند اصلی ندارد)")
    if len(plain) < MIN_CORE_CHARS:
        reasons.append("متن خیلی کوتاه است")

    body = [ln.strip() for ln in plain.splitlines() if ln.strip() and not ln.strip().startswith(("☝", "#"))]
    if body and not body[-1].endswith(TERMINATORS):
        reasons.append("به نظر می‌رسد متن نیمه‌تمام تمام شده است")

    n = _norm(plain)
    if any(m in n for m in LEAK_MARKERS):
        reasons.append("نشانهٔ خروجی خراب مدل (مثل ``` یا ** یا [[ ]] یا SKIP)")
    hits = sorted({p for p in (guard_phrases or []) if _norm(p) in n})
    if hits:
        reasons.append("عبارت ممنوع این مشتری (guard_phrases): " + "، ".join(hits[:3]))
    if re.search(r"https?://|www\.|t\.me/|@\w{3,}", plain):
        reasons.append("لینک یا آی‌دی در متن پست هست")
    latin, letters = len(_LATIN.findall(plain)), len(_LETTER.findall(plain))
    if letters and latin / letters > MAX_LATIN_RATIO:
        reasons.append("بخش زیادی از متن ترجمه نشده (انگلیسی) است")
    for name in banned_names or []:
        if len(name) >= 4 and _norm(name) in n:
            reasons.append(f"نام منبع («{name}») در متن آمده است")
            break

    if exclusions is not None:
        why = exclusions.reason(headline_of(text), plain)
        if why:
            reasons.append(why)

    head = _norm(headline_of(text))
    if head:
        for old in recent_texts or []:
            if old and _norm(headline_of(old)) == head:
                reasons.append("تیتر با یکی از پست‌های اخیر یکسان است")
                break
    return reasons


class AutoState:
    """Tiny persistent flag so /pause survives restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @property
    def paused(self) -> bool:
        try:
            return bool(json.loads(self.path.read_text(encoding="utf-8")).get("paused", False))
        except (OSError, ValueError):
            return False

    def set_paused(self, value: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"paused": bool(value)}), encoding="utf-8")


def auto_times(
    now: datetime, tz: ZoneInfo, busy: list[datetime], count: int, gap_minutes: int, win_from: str = "", win_to: str = "",
) -> list[datetime]:
    """Publication times (UTC) for `count` posts: RIGHT NOW, unless another post (`busy`: published or scheduled times)
    is within `gap_minutes` - then just after it. Posts never wait in a long queue. An optional daily window
    [win_from, win_to) can be set; empty = around the clock."""
    gap = timedelta(minutes=gap_minutes)
    window = bool(win_from and win_to)
    if window:
        fh, fm = _parse_hhmm(win_from)
        th, tm = _parse_hhmm(win_to)
    taken = list(busy)
    t = now
    out = []
    for _ in range(count):
        for _guard in range(200):
            if window:
                local = t.astimezone(tz)
                start = local.replace(hour=fh, minute=fm, second=0, microsecond=0)
                end = local.replace(hour=th, minute=tm, second=0, microsecond=0)
                if local < start:
                    t = start.astimezone(timezone.utc)
                elif local >= end:
                    t = (start + timedelta(days=1)).astimezone(timezone.utc)
            clash = [b for b in taken if abs(t - b) < gap]
            if not clash:
                break
            t = max(clash) + gap
        out.append(t)
        taken.append(t)
    return out
