"""Bot API adapter: what is sent, and how every answer is classified."""
import asyncio
import json

import httpx
import pytest
import respx

from orchestra.package import DeliveryStep
from orchestra.publishing.adapters import (
    BotApiAdapter, Delivered, Media, Permanent, Rejected, Transient, Unknown, classify_response,
)

TG = "https://api.telegram.org/botT"
BALE = "https://tapi.bale.ai/botT"
MEDIA = [Media("s0", b"\x89PNG" + b"0" * 50, "image/png"), Media("s1", b"\x89PNG" + b"1" * 50, "image/png")]


def run(adapter, step, media=()):
    async def go():
        async with httpx.AsyncClient() as c:
            adapter.client = c
            return await adapter.send(step, list(media))
    return asyncio.run(go())


@respx.mock
def test_telegram_text_keeps_html_and_disables_preview():
    route = respx.post(f"{TG}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 5}}))
    out = run(BotApiAdapter("telegram", "T", "@c"), DeliveryStep(mode="text", body="<b>سلام</b>"))
    assert out == Delivered([5])
    sent = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert sent["text"] == "<b>سلام</b>" and sent["parse_mode"] == "HTML" and "link_preview_options" in sent


@respx.mock
def test_bale_gets_the_prerendered_text_without_parse_mode():
    route = respx.post(f"{BALE}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 6}}))
    out = run(BotApiAdapter("bale", "T", "@b"), DeliveryStep(mode="text", body="*سلام*", parse_mode="Markdown"))
    sent = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert out == Delivered([6]) and sent["text"] == "*سلام*" and "parse_mode" not in sent and "link_preview_options" not in sent


@respx.mock
def test_album_sends_every_file_with_caption_on_the_first():
    route = respx.post(f"{TG}/sendMediaGroup").mock(return_value=httpx.Response(
        200, json={"ok": True, "result": [{"message_id": 10}, {"message_id": 11}]}))
    out = run(BotApiAdapter("telegram", "T", "@c"), DeliveryStep(mode="album", body="کپشن", asset_ids=("s0", "s1")), MEDIA)
    assert out == Delivered([10, 11])
    body = route.calls[0].request.content
    assert body.count(b"\x89PNG") == 2
    media_json = body.split(b'name="media"')[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0]
    items = json.loads(media_json.decode())
    assert items[0]["caption"] == "کپشن" and "caption" not in items[1] and items[1]["media"] == "attach://f1"


@pytest.mark.parametrize("exc,kind", [
    (httpx.ConnectError("refused"), Transient),
    (httpx.ConnectTimeout("slow"), Transient),
    (httpx.ReadTimeout("no answer"), Unknown),
    (httpx.RemoteProtocolError("dropped"), Unknown),
])
@respx.mock
def test_network_failures(exc, kind):
    respx.post(f"{TG}/sendMessage").mock(side_effect=exc)
    assert isinstance(run(BotApiAdapter("telegram", "T", "@c"), DeliveryStep(mode="text", body="x")), kind)


@pytest.mark.parametrize("status,body,kind", [
    (429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 17}}, Transient),
    (403, {"ok": False, "description": "Forbidden: bot is not a member"}, Permanent),
    (401, {"ok": False, "description": "Unauthorized"}, Permanent),
    (400, {"ok": False, "description": "Bad Request: chat not found"}, Permanent),
    (400, {"ok": False, "description": "Bad Request: can't parse entities"}, Rejected),
    (404, {"ok": False, "description": "Not Found"}, Rejected),
    (502, {}, Unknown),
    (200, {"ok": True, "result": {}}, Unknown),
])
def test_answers_are_classified(status, body, kind):
    out = classify_response(httpx.Response(status, json=body))
    assert isinstance(out, kind)
    if status == 429:
        assert out.retry_after == 17
