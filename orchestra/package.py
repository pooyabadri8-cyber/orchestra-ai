"""PublicationPackage: the immutable unit that is approved and published.

Approval is bound to `package_hash`, a SHA-256 over the canonical JSON of everything that would reach a reader:
the text, the exact bytes sent to each destination (every fallback step included), the hashes of every image, the
sources, and the versions of everything that produced it. Change any of it and the hash changes, which invalidates
earlier approvals.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Platform = Literal["telegram", "bale"]
DeliveryMode = Literal["album", "photo", "text"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AssetRef(Frozen):
    asset_id: str
    order: int
    role: Literal["slide", "chart", "photo"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["image/png", "image/jpeg", "image/webp"]
    bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    template: str | None = None  # e.g. "market_close@1"
    chart_spec_id: str | None = None
    provenance: str | None = None  # where a photo came from (URL), None for generated assets


class DeliveryStep(Frozen):
    """One way of sending the package to one destination. Steps are tried in order; every step is pre-rendered and
    approved together with the package, so a fallback never sends something nobody saw."""

    mode: DeliveryMode
    body: str  # caption for album/photo, message text for text
    parse_mode: Literal["HTML", "Markdown", "none"] = "HTML"
    asset_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "DeliveryStep":
        if self.mode == "text" and self.asset_ids:
            raise ValueError("a text step carries no assets")
        if self.mode == "photo" and len(self.asset_ids) != 1:
            raise ValueError("a photo step carries exactly one asset")
        if self.mode == "album" and not (2 <= len(self.asset_ids) <= 10):
            raise ValueError("an album carries 2 to 10 assets")
        if not self.body.strip():
            raise ValueError("empty body")
        return self


class DestinationPlan(Frozen):
    destination_id: str
    platform: Platform
    not_before: datetime | None = None
    expires_at: datetime | None = None
    delivery_plan: tuple[DeliveryStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _window(self) -> "DestinationPlan":
        for t in (self.not_before, self.expires_at):
            if t is not None and t.tzinfo is None:
                raise ValueError("publication window times must be timezone-aware")
        if self.not_before and self.expires_at and self.expires_at <= self.not_before:
            raise ValueError("expires_at must be after not_before")
        return self


class DataPointRef(Frozen):
    id: str
    metric: str
    subject: str | None = None
    source_text: str = ""
    value: float
    unit: str
    as_of: datetime
    source_id: str
    raw_sha256: str


class EvidenceRef(Frozen):
    id: str
    url: str
    title: str
    fetched_at: datetime
    content_sha256: str


class Sources(Frozen):
    data_points: tuple[DataPointRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()


class TextPart(Frozen):
    canonical_html: str
    template: str  # e.g. "post_template@7"
    claim_ids: tuple[str, ...] = ()


class Checks(Frozen):
    verification_report_id: str | None = None
    compliance_report_id: str | None = None
    risk_level: Literal["low", "medium", "high", "blocked"] = "medium"


class PackageContent(Frozen):
    """Everything that is hashed. Nothing here may depend on wall-clock time of building or on random ids."""

    org_id: str
    run_id: str
    version: int = Field(ge=1)
    supersedes: str | None = None  # package_id of the previous version
    content_class: str
    language: str = "fa"
    text: TextPart
    assets: tuple[AssetRef, ...] = ()
    destinations: tuple[DestinationPlan, ...] = Field(min_length=1)
    sources: Sources = Sources()
    versions: dict[str, Any] = Field(default_factory=dict)
    checks: Checks = Checks()

    @model_validator(mode="after")
    def _references(self) -> "PackageContent":
        ids = [a.asset_id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate asset_id")
        known = set(ids)
        dest_ids = [d.destination_id for d in self.destinations]
        if len(dest_ids) != len(set(dest_ids)):
            raise ValueError("duplicate destination_id")
        for d in self.destinations:
            for step in d.delivery_plan:
                missing = set(step.asset_ids) - known
                if missing:
                    raise ValueError(f"{d.destination_id}: unknown assets {sorted(missing)}")
        return self

    def destination(self, destination_id: str) -> DestinationPlan:
        for d in self.destinations:
            if d.destination_id == destination_id:
                return d
        raise KeyError(destination_id)

    def asset(self, asset_id: str) -> AssetRef:
        for a in self.assets:
            if a.asset_id == asset_id:
                return a
        raise KeyError(asset_id)


# ------------------------------------------------------------------ canonical hashing


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_normalize(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)  # 5.0 and 5 must hash the same
    return value


def canonical_json(content: PackageContent) -> str:
    data = _normalize(content.model_dump(mode="json"))
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def package_hash(content: PackageContent) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def package_id_for(content_hash: str) -> str:
    return f"pkg_{content_hash[:20]}"


class PublicationPackage(Frozen):
    package_id: str
    package_hash: str
    content: PackageContent

    @classmethod
    def build(cls, content: PackageContent) -> "PublicationPackage":
        h = package_hash(content)
        return cls(package_id=package_id_for(h), package_hash=h, content=content)

    def verify(self) -> bool:
        """True when the stored hash still matches the content (detects tampering in storage)."""
        h = package_hash(self.content)
        return h == self.package_hash and self.package_id == package_id_for(h)
