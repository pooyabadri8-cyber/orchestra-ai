"""Instagram Graph API adapter: carousel flow and conservative outcome classification."""
import asyncio

import httpx
import pytest
import respx

from orchestra.package import DeliveryStep, DestinationPlan
from orchestra.publishing.adapters import Delivered, Media, Permanent, Rejected, Transient, Unknown
from orchestra.publishing.instagram import GRAPH, DirectoryHost, InstagramAdapter, NoHost

STEP = DeliveryStep(mode="carousel", body="کپشن", parse_mode="none", asset_ids=("a1", "a2"))
MEDIA = [Media("a1", b"\xff\xd8one", "image/jpeg"), Media("a2", b"\xff\xd8two", "image/jpeg")]


async def _nosleep(_):
    return None


def adapter(tmp_path, **kw):
    return InstagramAdapter(ig_user_id="17841", token="T", host=DirectoryHost(tmp_path, "https://cdn.example/ig"),
                            sleep=_nosleep, **kw)


def mock_happy(router, publish=None, c1_status=None):
    ids = iter(["c1", "c2", "car"])
    if c1_status:
        router.get(url__regex=rf"{GRAPH}/c1\?.*").mock(return_value=httpx.Response(200, json={"status_code": c1_status}))
    router.post(f"{GRAPH}/17841/media").mock(side_effect=lambda req: httpx.Response(200, json={"id": next(ids)}))
    router.get(url__regex=rf"{GRAPH}/(c1|c2|car)\?.*").mock(return_value=httpx.Response(200, json={"status_code": "FINISHED"}))
    router.post(f"{GRAPH}/17841/media_publish").mock(**(publish or {"return_value": httpx.Response(200, json={"id": "1790001"})}))


def test_carousel_happy_path(tmp_path):
    with respx.mock(assert_all_called=True) as router:
        mock_happy(router)
        out = asyncio.run(adapter(tmp_path).send(STEP, MEDIA))
        creates = [c.request for c in router.calls if c.request.url.path.endswith("/media")]
        carousel = creates[-1].content.decode()
    assert out == Delivered([1790001])
    assert "media_type=CAROUSEL" in carousel and "children=c1%2Cc2" in carousel
    assert len(list(tmp_path.glob("*.jpg"))) == 2  # images staged under content-addressed names


@pytest.mark.parametrize("publish,expected", [
    ({"side_effect": httpx.ReadTimeout("t")}, Unknown),
    ({"return_value": httpx.Response(500, json={"error": {"code": 2, "message": "x"}})}, Unknown),
    ({"return_value": httpx.Response(400, json={"error": {"code": 4, "message": "limit"}})}, Transient),
    ({"return_value": httpx.Response(400, json={"error": {"code": 190, "message": "token"}})}, Permanent),
    ({"return_value": httpx.Response(400, json={"error": {"code": 9004, "message": "bad image"}})}, Rejected),
    ({"side_effect": httpx.ConnectError("no")}, Transient),
])
def test_media_publish_outcomes(tmp_path, publish, expected):
    with respx.mock(assert_all_called=False) as router:
        mock_happy(router, publish)
        assert isinstance(asyncio.run(adapter(tmp_path).send(STEP, MEDIA)), expected)


def test_failures_before_publish_never_count_as_unknown(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{GRAPH}/17841/media").mock(side_effect=httpx.ReadTimeout("t"))
        assert isinstance(asyncio.run(adapter(tmp_path).send(STEP, MEDIA)), Transient)
    with respx.mock(assert_all_called=False) as router:
        mock_happy(router, c1_status="ERROR")
        assert isinstance(asyncio.run(adapter(tmp_path).send(STEP, MEDIA)), Rejected)
    with respx.mock(assert_all_called=False) as router:
        mock_happy(router, c1_status="IN_PROGRESS")
        assert isinstance(asyncio.run(adapter(tmp_path, poll_attempts=3).send(STEP, MEDIA)), Transient)


def test_no_public_host_is_permanent_and_sends_nothing():
    with respx.mock(assert_all_mocked=True) as router:
        out = asyncio.run(InstagramAdapter("17841", "T", NoHost()).send(STEP, MEDIA))
        assert isinstance(out, Permanent) and "ORCHESTRA_PUBLIC_ASSET" in out.reason and not router.calls


def test_package_rules_for_instagram():
    with pytest.raises(ValueError, match="only carousel"):
        DestinationPlan(destination_id="ig", platform="instagram", delivery_plan=(DeliveryStep(mode="text", body="x", parse_mode="none"),))
    with pytest.raises(ValueError, match="Instagram mode"):
        DestinationPlan(destination_id="tg", platform="telegram", delivery_plan=(STEP,))
    with pytest.raises(ValueError, match="plain text"):
        DeliveryStep(mode="carousel", body="x", parse_mode="HTML", asset_ids=("a", "b"))
