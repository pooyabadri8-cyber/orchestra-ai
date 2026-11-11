"""Sending one delivery step to Telegram or Bale through their Bot API, and classifying what happened.

The classification is the safety mechanism, so it is conservative: whenever the request may have reached the platform
and we did not get a clear answer, the outcome is UNKNOWN and the publisher will not resend on its own.

    Delivered  -> confirmed, with the platform's message ids
    Rejected   -> the platform refused this step's content or method (HTTP 400/404): try the next pre-approved step
    Transient  -> certainly not delivered (connection never made, HTTP 429): retry later
    Unknown    -> may have been delivered (read timeout, dropped connection, HTTP 5xx): a human decides
    Permanent  -> will not work until someone fixes it (HTTP 401/403, chat not found)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..package import DeliveryStep

API_BASE = {"telegram": "https://api.telegram.org/bot", "bale": "https://tapi.bale.ai/bot"}


@dataclass(frozen=True)
class Delivered:
    message_ids: list[int]


@dataclass(frozen=True)
class Rejected:
    reason: str


@dataclass(frozen=True)
class Transient:
    reason: str
    retry_after: float = 0.0


@dataclass(frozen=True)
class Unknown:
    reason: str


@dataclass(frozen=True)
class Permanent:
    reason: str


Outcome = Delivered | Rejected | Transient | Unknown | Permanent


@dataclass(frozen=True)
class Media:
    asset_id: str
    data: bytes
    mime: str


class DestinationAdapter(Protocol):
    async def send(self, step: DeliveryStep, media: list[Media]) -> Outcome: ...


def _ext(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "bin")


@dataclass
class BotApiAdapter:
    """Telegram and Bale share the Bot API shape. Differences handled here: Bale takes no parse_mode or link-preview
    options (its text is pre-rendered Markdown in the package), and Bale is reached without the system proxy."""

    platform: str
    token: str
    chat_id: str
    timeout: float = 45.0
    client: httpx.AsyncClient | None = None
    base_url: str | None = None
    _own_client: bool = field(default=False, init=False)

    def _url(self, method: str) -> str:
        return f"{self.base_url or API_BASE[self.platform]}{self.token}/{method}"

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            # Bale is an Iranian service: reach it directly, never through a VPN/system proxy meant for foreign sites
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=15), trust_env=self.platform != "bale")
            self._own_client = True
        return self.client

    async def aclose(self) -> None:
        if self._own_client and self.client is not None:
            await self.client.aclose()

    def _format(self, step: DeliveryStep) -> dict:
        if self.platform == "bale" or step.parse_mode == "none":
            return {}
        return {"parse_mode": step.parse_mode}

    async def send(self, step: DeliveryStep, media: list[Media]) -> Outcome:
        fmt = self._format(step)
        try:
            if step.mode == "text":
                data = {"chat_id": self.chat_id, "text": step.body, **fmt}
                if self.platform == "telegram":
                    data["link_preview_options"] = json.dumps({"is_disabled": True})
                resp = await self._http().post(self._url("sendMessage"), data=data)
            elif step.mode == "photo":
                m = media[0]
                files = {"photo": (f"{m.asset_id}.{_ext(m.mime)}", m.data, m.mime)}
                resp = await self._http().post(self._url("sendPhoto"), data={"chat_id": self.chat_id, "caption": step.body, **fmt}, files=files)
            else:  # album
                items, files = [], {}
                for i, m in enumerate(media):
                    key = f"f{i}"
                    item = {"type": "photo", "media": f"attach://{key}"}
                    if i == 0:
                        item.update({"caption": step.body, **fmt})
                    items.append(item)
                    files[key] = (f"{m.asset_id}.{_ext(m.mime)}", m.data, m.mime)
                resp = await self._http().post(self._url("sendMediaGroup"), data={"chat_id": self.chat_id, "media": json.dumps(items, ensure_ascii=False)}, files=files)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            return Transient(f"connection not made: {type(e).__name__}")
        except httpx.TimeoutException as e:  # read/write timeout: the request may have been processed
            return Unknown(f"no answer: {type(e).__name__}")
        except httpx.TransportError as e:  # dropped connection, protocol error after sending
            return Unknown(f"connection lost: {type(e).__name__}")
        return classify_response(resp)


def classify_response(resp: httpx.Response) -> Outcome:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    desc = str(body.get("description") or resp.text[:200] or f"HTTP {resp.status_code}")
    code = resp.status_code
    if code == 200 and body.get("ok") is True:
        result = body.get("result")
        items = result if isinstance(result, list) else [result]
        ids = [int(r["message_id"]) for r in items if isinstance(r, dict) and "message_id" in r]
        if not ids:
            return Unknown("platform said ok but returned no message id")
        return Delivered(ids)
    if code == 429:
        retry = (body.get("parameters") or {}).get("retry_after") or 0
        return Transient(f"rate limited: {desc}", retry_after=float(retry))
    if code in (401, 403):
        return Permanent(desc)
    if code == 400 and "chat not found" in desc.lower():
        return Permanent(desc)
    if code in (400, 404) or (code == 200 and body.get("ok") is False):
        return Rejected(desc)
    if code >= 500:
        return Unknown(f"server error {code}: {desc}")
    return Unknown(f"unexpected answer {code}: {desc}")
