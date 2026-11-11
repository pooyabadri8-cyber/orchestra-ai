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
    footer_text: str = ""
    footer_links: tuple[FooterLink, ...] = ()


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
    brand = Brand.model_validate(data)
    if brand.org_id != org_id:
        raise ValueError(f"{path}: org_id is {brand.org_id}, expected {org_id}")
    return brand
