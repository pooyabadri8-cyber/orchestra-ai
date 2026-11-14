"""PublicationPackage: deterministic hashing, and every published byte is inside the hash."""
import unicodedata
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from orchestra.package import DeliveryStep, DestinationPlan, PublicationPackage, package_hash
from conftest import make_content


def test_same_content_same_hash_and_id():
    a, b = PublicationPackage.build(make_content()), PublicationPackage.build(make_content())
    assert a.package_hash == b.package_hash and a.package_id == b.package_id and a.verify()


def test_unicode_normalization_does_not_change_the_hash():
    composed = "café آب"  # é and آ precomposed
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert package_hash(make_content(body=composed)) == package_hash(make_content(body=decomposed))


@pytest.mark.parametrize("change", ["telegram text", "bale text", "image bytes", "window", "versions", "risk"])
def test_any_published_detail_changes_the_hash(change):
    base = make_content()
    if change == "telegram text":
        other = make_content(body="متن دیگر")
    elif change == "bale text":
        plan = base.destinations[1].model_copy(update={"delivery_plan": (DeliveryStep(mode="text", body="*دیگر*", parse_mode="Markdown"),)})
        other = base.model_copy(update={"destinations": (base.destinations[0], plan)})
    elif change == "image bytes":
        other = base.model_copy(update={"assets": (base.assets[0].model_copy(update={"sha256": "b" * 64}),) + base.assets[1:]})
    elif change == "window":
        other = make_content(expires_at=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))
    elif change == "versions":
        other = base.model_copy(update={"versions": {**base.versions, "writer": "1.0.1"}})
    else:
        other = base.model_copy(update={"checks": base.checks.model_copy(update={"risk_level": "medium"})})
    assert package_hash(base) != package_hash(other)


def test_tampered_package_fails_verification():
    pkg = PublicationPackage.build(make_content())
    forged = pkg.model_copy(update={"content": make_content(body="جعلی")})
    assert not forged.verify()


def test_step_shapes_are_enforced():
    with pytest.raises(ValidationError):
        DeliveryStep(mode="photo", body="x", asset_ids=("a", "b"))
    with pytest.raises(ValidationError):
        DeliveryStep(mode="album", body="x", asset_ids=("a",))
    with pytest.raises(ValidationError):
        DeliveryStep(mode="text", body="x", asset_ids=("a",))
    with pytest.raises(ValidationError):
        DeliveryStep(mode="text", body="   ")


def test_unknown_asset_and_naive_window_are_refused():
    base = make_content()
    bad = DestinationPlan(destination_id="tg_test", platform="telegram",
                          delivery_plan=(DeliveryStep(mode="photo", body="x", asset_ids=("nope",)),))
    with pytest.raises(ValidationError):
        base.model_copy(update={"destinations": (bad,)}).model_validate(base.model_copy(update={"destinations": (bad,)}).model_dump())
    with pytest.raises(ValidationError):
        DestinationPlan(destination_id="d", platform="telegram", not_before=datetime(2026, 1, 1),
                        delivery_plan=(DeliveryStep(mode="text", body="x"),))


def test_package_is_immutable():
    pkg = PublicationPackage.build(make_content())
    with pytest.raises(ValidationError):
        pkg.package_hash = "0" * 64

