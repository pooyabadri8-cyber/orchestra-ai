"""Per-organization brand profile (voice, rules, forbidden topics, post layout), versioned and loaded from YAML."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class FooterLink(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    url: str | None = None


class PostTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: int
    brand_hashtag: str
    default_topic_hashtag: str
    default_cta: str = ""
    instagram_cta: str = ""  # Instagram is a page, not a channel; empty = default_cta
    footer_text: str = ""
    footer_links: tuple[FooterLink, ...] = ()
    carousel_cover: bool = True  # a title-only first slide
    carousel_end: bool = True  # the closing slide with the call to action and contact links


class BrandColors(BaseModel):
    """Slide and chart palette. Placeholder defaults until the brand's real colours are set in brand.yaml."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    background: str = "#FFFFFF"
    surface: str = "#EEF3F8"
    primary: str = "#1F4E9C"
    accent: str = "#5DC094"
    text: str = "#1B2C5A"
    muted: str = "#5A6788"
    positive: str = "#2E9E6B"
    negative: str = "#D64545"


class Brand(BaseModel):
    model_config = ConfigDict(frozen=True)
    org_id: str
    name: str
    about: str = ""
    audience: str = ""
    rules: tuple[str, ...] = ()
    topic_rule: str = ""
    exclusion_rule: str = ""
    guard_phrases: tuple[str, ...] = ()
    exclude_words: tuple[str, ...] = ()
    exclude_ignore: tuple[str, ...] = ()
    template: PostTemplate
    single_stock_content: bool = False  # may the org publish about one named stock (single-stock technical/fundamental)?
    logo_file: str | None = None  # full logo (mark + name), path relative to the org folder
    logo_mark_file: str | None = None  # mark only, for small headers
    logo_path: Path | None = Field(default=None, description="filled by the loader")
    logo_mark_path: Path | None = Field(default=None, description="filled by the loader")
    colors: "BrandColors" = Field(default_factory=lambda: BrandColors())
    version_tag: str = Field(default="", description="filled by the loader: sha of the file")

    def excluded(self, text: str) -> bool:
        """True when `text` mentions a forbidden topic (e.g. investment funds), after removing innocent phrases."""
        t = normalize(text)
        for phrase in self.exclude_ignore:
            t = t.replace(normalize(phrase), " ")
        for w in self.exclude_words:
            nw = normalize(w)
            if nw.isascii():
                if re.search(rf"(?<![a-z0-9]){re.escape(nw)}(?:s|es)?(?![a-z0-9])", t):
                    return True
            elif nw in t:
                return True
        return False


def normalize(s: str) -> str:
    s = (s or "").replace("‌", " ").replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه")
    return re.sub(r"\s+", " ", s).strip().lower()


def load_brand(org_id: str, root: str | Path = "orgs") -> Brand:
    import hashlib

    path = Path(root) / org_id / "brand.yaml"
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    data["version_tag"] = f"brand@{data.get('template', {}).get('version', 0)}#{hashlib.sha256(raw).hexdigest()[:12]}"
    for key, dest in (("logo_file", "logo_path"), ("logo_mark_file", "logo_mark_path")):
        if data.get(key):
            f = path.parent / data[key]
            if not f.is_file():
                raise ValueError(f"{path}: {key} points to a missing file: {f}")
            data[dest] = f
    brand = Brand.model_validate(data)
    if brand.org_id != org_id:
        raise ValueError(f"{path}: org_id is {brand.org_id}, expected {org_id}")
    return brand
