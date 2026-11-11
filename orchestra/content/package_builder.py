"""Assemble the immutable PublicationPackage from a verified post (text only for now; slides come with Visual Production)."""
from __future__ import annotations

from datetime import datetime

from ..brand import Brand
from ..content.numbers import Claim
from ..package import (
    Checks, DataPointRef, DeliveryStep, DestinationPlan, PackageContent, PublicationPackage, Sources, TextPart,
)
from .template import PostParts, bale_markdown, telegram_html


def build_text_package(
    *, brand: Brand, run_id: str, content_class: str, parts: PostParts, claims: list[Claim],
    data_points: list[DataPointRef], destinations: list[tuple[str, str]], versions: dict,
    not_before: datetime | None = None, expires_at: datetime | None = None, risk_level: str = "medium",
    version: int = 1, supersedes: str | None = None,
) -> PublicationPackage:
    """`destinations`: [(destination_id, platform)]. Each platform gets its own pre-rendered text."""
    tg = telegram_html(brand.template, parts)
    bale = bale_markdown(brand.template, parts)
    plans = []
    for dest_id, platform in destinations:
        step = (DeliveryStep(mode="text", body=tg, parse_mode="HTML") if platform == "telegram"
                else DeliveryStep(mode="text", body=bale, parse_mode="Markdown"))
        plans.append(DestinationPlan(destination_id=dest_id, platform=platform, not_before=not_before,
                                     expires_at=expires_at, delivery_plan=(step,)))
    used = {c.point_id for c in claims}
    content = PackageContent(
        org_id=brand.org_id, run_id=run_id, version=version, supersedes=supersedes, content_class=content_class,
        text=TextPart(canonical_html=tg, template=f"post_template@{brand.template.version}",
                      claim_ids=tuple(c.claim_id for c in claims)),
        destinations=tuple(plans),
        sources=Sources(data_points=tuple(sorted((d for d in data_points if d.id in used), key=lambda d: d.id))),
        versions={**versions, "brand": brand.version_tag},
        checks=Checks(risk_level=risk_level),
    )
    return PublicationPackage.build(content)


def build_multichannel_package(
    *, brand: Brand, run_id: str, content_class: str, post: PostParts | None, instagram_caption: str | None,
    slides: list, claims: list[Claim], data_points: list[DataPointRef], destinations: list[tuple[str, str]], versions: dict,
    not_before: datetime | None = None, expires_at: datetime | None = None, risk_level: str = "medium",
    version: int = 1, supersedes: str | None = None, extra_point_ids: frozenset[str] = frozenset(),
) -> PublicationPackage:
    """One package for every channel. Telegram and Bale get the text post; Instagram gets the carousel with its caption.
    `slides`: AssetRef list (already stored, in order). Everything each destination will receive is in the hash."""
    plans = []
    for dest_id, platform in destinations:
        if platform == "instagram":
            if not slides or not instagram_caption:
                raise ValueError("Instagram needs slides and a caption")
            step = DeliveryStep(mode="carousel", body=instagram_caption, parse_mode="none", asset_ids=tuple(a.asset_id for a in slides))
        elif post is None:
            raise ValueError(f"{platform} needs the text post")
        elif platform == "telegram":
            step = DeliveryStep(mode="text", body=telegram_html(brand.template, post), parse_mode="HTML")
        else:
            step = DeliveryStep(mode="text", body=bale_markdown(brand.template, post), parse_mode="Markdown")
        plans.append(DestinationPlan(destination_id=dest_id, platform=platform, not_before=not_before,
                                     expires_at=expires_at, delivery_plan=(step,)))
    canonical = telegram_html(brand.template, post) if post is not None else (instagram_caption or "")
    used = {c.point_id for c in claims} | set(extra_point_ids)  # extra: the points behind the charts
    content = PackageContent(
        org_id=brand.org_id, run_id=run_id, version=version, supersedes=supersedes, content_class=content_class,
        text=TextPart(canonical_html=canonical, template=f"post_template@{brand.template.version}",
                      claim_ids=tuple(sorted({c.claim_id for c in claims}))),
        assets=tuple(slides),
        destinations=tuple(plans),
        sources=Sources(data_points=tuple(sorted((d for d in data_points if d.id in used), key=lambda d: d.id))),
        versions={**versions, "brand": brand.version_tag},
        checks=Checks(risk_level=risk_level),
    )
    return PublicationPackage.build(content)
