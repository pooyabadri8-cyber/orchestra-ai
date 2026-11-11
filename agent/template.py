"""Fixed post layout for the channel (modelled on the Etebar Taban format).

The model writes only the *core*: topic hashtag, 📌 headline, 🔹 paragraphs and a ☝ call to action. This module parses
whatever it returned, repairs the layout and adds the fixed parts (brand hashtag, source line, ➖➖➖, footer), so every
post looks the same no matter how the model drifted.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEP = "➖➖➖"
HEADLINE, BULLET, CTA = "📌", "🔹", "☝"
_TAG_LINE = re.compile(r"^(?:#[^\s#]+[\s]*)+$")
_TAG = re.compile(r"#[^\s#]+")
_SOURCE_LINE = re.compile(r"^(?:<a\b[^>]*>)?\s*منبع:", re.I)
_FENCE = re.compile(r"^```\w*\s*|\s*```$")


@dataclass
class Template:
    brand_hashtag: str = "#برند"
    default_topic_hashtag: str = "#اخبار"
    headline_bold: bool = True
    cta_url: str = ""
    default_cta: str = ""
    footer_text: str = ""
    footer_links: list[dict] = field(default_factory=list)

    # ---- assembling
    def _footer(self) -> str:
        lines = []
        if self.footer_text:
            lines.append(html.escape(self.footer_text))
        links = []
        for item in self.footer_links:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if "url" not in item:
                links.append(html.escape(text))
            elif str(item["url"] or "").strip():
                links.append(f'<a href="{html.escape(str(item["url"]).strip(), quote=True)}">{html.escape(text)}</a>')
        if links:
            lines += ["", *links]
        return "\n".join(lines)

    def footer(self) -> str:
        """The fixed footer block (text + links) that follows the ➖➖➖ line."""
        return self._footer()

    def assemble(self, tags: list[str], headline: str, bullets: list[str], cta: str, source_line: str = "") -> str:
        brand = self.brand_hashtag.strip()
        topic = [t for t in tags if t != brand][:1] or ([self.default_topic_hashtag] if self.default_topic_hashtag else [])
        head = headline.strip()
        if self.headline_bold and "<b>" not in head:
            head = f"<b>{head}</b>"
        cta = cta.strip() or html.escape(self.default_cta)
        if cta and self.cta_url and "<a " not in cta:
            cta = f'<a href="{html.escape(self.cta_url, quote=True)}">{cta}</a>'
        parts = ["\n".join([brand, *topic] if brand else topic), "", ""]
        parts += [f"{HEADLINE}  {head}", ""]
        parts += ["\n\n".join(f"{BULLET}{b}" for b in bullets), "", ""]
        if cta:
            parts += [f"{CTA} {cta}", "", ""]
        if source_line:
            parts += [source_line, ""]
        parts += [SEP, self._footer()]
        return "\n".join(parts).rstrip()

    # ---- parsing model output / old posts
    def split(self, text: str) -> tuple[str, str]:
        """Assembled post (or anything) -> (core text without footer and source line, source line or '')."""
        lines = text.strip().splitlines()
        for i, ln in enumerate(lines):
            if ln.strip() == SEP:
                lines = lines[:i]
                break
        source = ""
        for i in range(len(lines) - 1, -1, -1):
            if not lines[i].strip():
                continue
            if _SOURCE_LINE.match(lines[i].strip()):
                source = lines.pop(i).strip()
            break
        return "\n".join(lines).strip(), source

    def finalize(self, text: str, source_line: str = "") -> str:
        """Model output (core, in any reasonable shape) -> final post in the fixed layout."""
        core, found_source = self.split(_FENCE.sub("", text.strip()))
        tags: list[str] = []
        headline = ""
        blocks: list[list[str]] = []  # [kind, lines]
        cur: list | None = None
        for raw in core.splitlines():
            s = raw.strip()
            if not s:
                cur = None
                continue
            if _TAG_LINE.match(s):
                tags += _TAG.findall(s)
                cur = None
            elif s.startswith(HEADLINE):
                headline = s[len(HEADLINE):].strip()
                cur = None
            elif s.startswith(BULLET):
                cur = ["b", [s[len(BULLET):].strip()]]
                blocks.append(cur)
            elif s.startswith(CTA):
                cur = ["c", [s.lstrip(CTA + "️ ").strip()]]
                blocks.append(cur)
            elif cur is not None:
                cur[1].append(s)  # continuation of the paragraph above
            elif not headline and not blocks:
                headline = s
            else:
                cur = ["b", [s]]
                blocks.append(cur)
        bullets = ["\n".join(b[1]) for b in blocks if b[0] == "b" and any(b[1])]
        ctas = ["\n".join(b[1]) for b in blocks if b[0] == "c" and any(b[1])]
        if not headline and bullets:  # no headline at all: promote the first paragraph's first line
            first, _, rest = bullets[0].partition("\n")
            headline, bullets[0] = first, rest.strip()
            if not bullets[0]:
                bullets.pop(0)
        seen, uniq = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return self.assemble(uniq, headline, bullets, ctas[0] if ctas else "", source_line or found_source)


def load_template(path: str | Path = "post_template.yaml") -> Template:
    p = Path(path)
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    t = Template()
    for key in ("brand_hashtag", "default_topic_hashtag", "cta_url", "default_cta", "footer_text"):
        if key in raw:
            setattr(t, key, str(raw[key] or ""))
    if "headline_bold" in raw:
        t.headline_bold = bool(raw["headline_bold"])
    if "footer_links" in raw:
        t.footer_links = [x for x in (raw["footer_links"] or []) if isinstance(x, dict)]
    if t.brand_hashtag and not t.brand_hashtag.startswith("#"):
        t.brand_hashtag = "#" + t.brand_hashtag
    return t
