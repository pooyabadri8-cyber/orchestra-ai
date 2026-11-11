"""Topics the channel must NOT post about (configured per customer: exclude_words in config.yaml; none by default), enforced at four points:

1. incoming source items about the topic are dropped before any model call,
2. rows about the topic are removed from the market data of the optional market-report module (and any listed symbols),
3. the writer is told never to write about the topic,
4. the final post is checked (headline or repeated mentions) and, if it still is about the topic, is not auto-published.

Everything is plain text matching. It is configured with `exclude_words` / `exclude_symbols` in config.yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

BODY_ITEM_HITS = 3  # an incoming item is "about" the topic if the title mentions it or the body does this often
BODY_POST_HITS = 2  # a finished post is "about" the topic if its headline mentions it or the body does this often


def _norm(s: str) -> str:
    s = (s or "").replace("‌", " ").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", s).strip().lower()


def _word_regex(w: str) -> re.Pattern:
    w = _norm(w)
    if w.isascii():  # whole word (+ plural) so "funding" / "crowdfunding" do not match "fund"
        return re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?:s|es)?(?![a-z0-9])")
    return re.compile(re.escape(w))  # Persian: substring (handles ها / ی suffixes)


@dataclass
class Exclusions:
    words: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)  # exact symbol / group names to drop from market data
    ignore: list[str] = field(default_factory=list)  # phrases containing a word that are NOT the topic (exclude_ignore)

    def __post_init__(self) -> None:
        self._rx = [_word_regex(w) for w in self.words if w and w.strip()]
        self._ign = [re.compile(re.escape(_norm(p))) for p in self.ignore if p and p.strip()]
        self._sym = {_norm(s) for s in self.symbols if s and s.strip()}

    @property
    def enabled(self) -> bool:
        return bool(self._rx or self._sym)

    def count(self, text: str) -> int:
        """How many times the excluded words appear (innocent phrases listed in `exclude_ignore` removed first)."""
        if not self._rx:
            return 0
        t = _norm(text)
        for r in self._ign:
            t = r.sub(" ", t)
        return sum(len(r.findall(t)) for r in self._rx)

    # ------------------------------------------------------------------ incoming items
    def about(self, title: str, text: str) -> bool:
        return self.count(title) >= 1 or self.count((text or "")[:4000]) >= BODY_ITEM_HITS

    def keep_items(self, items):
        return [it for it in items if not self.about(it.title, it.text)]

    # ------------------------------------------------------------------ market data (gozaresh Report)
    def banned_row(self, name: str) -> bool:
        return _norm(name) in self._sym or self.count(name) > 0

    def clean_report(self, rep):
        """Copy of the report without fund rows (the original, which may be cached elsewhere, is not changed)."""
        if not self.enabled:
            return rep
        def keep(rows):
            return [r for r in rows if not self.banned_row(r.name)]
        return replace(
            rep, top_in_stocks=keep(rep.top_in_stocks), top_out_stocks=keep(rep.top_out_stocks),
            top_in_groups=keep(rep.top_in_groups), top_out_groups=keep(rep.top_out_groups),
        )

    def _first(self, text: str) -> str:
        t = _norm(text)
        return next((w for w in self.words if w and _word_regex(w).search(t)), "") or "؟"

    # ------------------------------------------------------------------ finished post
    def reason(self, headline: str, body: str) -> str | None:
        """Why a finished post must not go out automatically, or None."""
        if not self._rx:
            return None
        if self.count(headline) >= 1:
            return f"موضوع پست یکی از موضوع‌های ممنوع است ({self._first(headline)} در تیتر آمده) و انتشار آن متوقف شده است"
        if self.count(body) >= BODY_POST_HITS:
            return f"متن پست چندبار دربارهٔ موضوع ممنوع ({self._first(body)}) حرف می‌زند و انتشار آن متوقف شده است"
        return None


def from_raw(words, symbols, ignore=None) -> Exclusions:
    return Exclusions(
        words=[str(w) for w in (words or [])], symbols=[str(s) for s in (symbols or [])],
        ignore=[str(p) for p in ignore] if ignore is not None else [],
    )


def writer_rule(words: list[str], text: str = "", exception: str = "") -> str:
    """The rule given to the model whenever the forbidden topic (config: exclude_words) is switched on.
    `text` = the customer's own wording (profile.yaml: exclusion_rule); without it a plain generic sentence is built from
    the words. `exception` = an optional sentence appended to it (e.g. "mentions inside our own product are allowed")."""
    base = text.strip() or (
        f"موضوع ممنوع: دربارهٔ «{'، '.join(w for w in words if w)}» پست نساز و در هیچ پستی از آن نام نبر. "
        "اگر اصل مطلب دربارهٔ همین موضوع است فقط بنویس: SKIP. اگر فقط بخشی از مطلب دربارهٔ آن است، همان بخش را حذف کن و بقیه را بنویس."
    )
    return f"{base} {exception}".strip()
