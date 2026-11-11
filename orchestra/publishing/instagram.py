"""Instagram publishing through the Instagram Graph API (business/creator accounts).

Flow for a carousel: put each image at a public URL -> create one item container per image -> create the carousel
container with the caption -> wait until it is FINISHED -> media_publish. Only media_publish makes a post visible, so
everything before it is safe to repeat; an unclear answer to media_publish is UNKNOWN and is never retried by itself.

Instagram fetches images from a public URL, so an AssetHost is needed (a folder served by your web server, object
storage, ...). Without one the job fails as Permanent with a clear message; nothing is half-sent.

Error mapping (Graph API error codes):
    4, 17, 32, 613, 80002 ..... rate limits                -> Transient (retry later)
    190, 10, 200-299 .......... token or permission         -> Permanent
    2, 1 (unknown/temporary) .. before publish: Transient; on media_publish: Unknown
    other 4xx ................. content refused             -> Rejected (try the next pre-approved step)
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from ..package import DeliveryStep
from .adapters import Delivered, Media, Outcome, Permanent, Rejected, Transient, Unknown

GRAPH = "https://graph.facebook.com/v21.0"
RATE = {4, 17, 32, 613, 80002, 80001}
AUTH = {190, 10, 102, 463, 467}
CAPTION_LIMIT = 2200


class AssetHost(Protocol):
    async def url_for(self, media: Media) -> str: ...


@dataclass
class DirectoryHost:
    """Copies the image into a folder that a web server publishes at `base_url` (content-addressed file names)."""
    directory: Path
    base_url: str

    async def url_for(self, media: Media) -> str:
        ext = {"image/jpeg": "jpg", "image/png": "png"}.get(media.mime, "bin")
        name = f"{hashlib.sha256(media.data).hexdigest()}.{ext}"
        path = Path(self.directory) / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(media.data)
            tmp.replace(path)
        return f"{self.base_url.rstrip('/')}/{name}"


class NoHost:
    async def url_for(self, media: Media) -> str:
        raise HostError("برای اینستاگرام محل انتشار عمومی تصویرها تنظیم نشده است (ORCHESTRA_PUBLIC_ASSET_DIR و ORCHESTRA_PUBLIC_ASSET_URL)")


class HostError(Exception):
    pass


class _Stop(Exception):
    def __init__(self, outcome: Outcome):
        self.outcome = outcome


@dataclass
class InstagramAdapter:
    ig_user_id: str
    token: str
    host: AssetHost
    timeout: float = 60.0
    poll_interval: float = 3.0
    poll_attempts: int = 20
    client: httpx.AsyncClient | None = None
    base_url: str = GRAPH
    sleep: object = field(default=asyncio.sleep)
    _own_client: bool = field(default=False, init=False)

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=15))
            self._own_client = True
        return self.client

    async def aclose(self) -> None:
        if self._own_client and self.client is not None:
            await self.client.aclose()

    async def send(self, step: DeliveryStep, media: list[Media]) -> Outcome:
        if step.mode not in ("carousel", "photo"):
            return Rejected(f"Instagram cannot send a {step.mode} step")
        if len(step.body) > CAPTION_LIMIT:
            return Rejected("caption longer than 2200 characters")
        try:
            urls = [await self.host.url_for(m) for m in media]
        except HostError as e:
            return Permanent(str(e))
        except OSError as e:
            return Transient(f"could not stage image: {e}")
        try:
            if step.mode == "photo":
                creation = await self._create({"image_url": urls[0], "caption": step.body})
            else:
                children = [await self._create({"image_url": u, "is_carousel_item": "true"}) for u in urls]
                for c in children:
                    await self._wait(c)
                creation = await self._create({"media_type": "CAROUSEL", "children": ",".join(children), "caption": step.body})
            await self._wait(creation)
        except _Stop as stop:
            return stop.outcome
        return await self._publish(creation)

    # -------------------------------------------------------------- calls before publishing (safe to repeat)
    async def _call(self, method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        try:
            if method == "POST":
                resp = await self._http().post(f"{self.base_url}/{path}", data={**(data or {}), "access_token": self.token})
            else:
                resp = await self._http().get(f"{self.base_url}/{path}", params={**(params or {}), "access_token": self.token})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Stop(Transient(f"Instagram not reached before publishing: {type(e).__name__}")) from e
        body = _json(resp)
        if resp.status_code == 200 and "error" not in body:
            return body
        raise _Stop(_classify_error(resp.status_code, body, publishing=False))

    async def _create(self, data: dict) -> str:
        body = await self._call("POST", f"{self.ig_user_id}/media", data=data)
        if not body.get("id"):
            raise _Stop(Transient("container created without an id"))
        return str(body["id"])

    async def _wait(self, container_id: str) -> None:
        for _ in range(self.poll_attempts):
            body = await self._call("GET", container_id, params={"fields": "status_code,status"})
            status = body.get("status_code")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise _Stop(Rejected(f"Instagram could not process the media: {body.get('status') or status}"))
            await self.sleep(self.poll_interval)
        raise _Stop(Transient("Instagram is still processing the media"))

    # -------------------------------------------------------------- the one step that makes the post visible
    async def _publish(self, creation_id: str) -> Outcome:
        try:
            resp = await self._http().post(f"{self.base_url}/{self.ig_user_id}/media_publish",
                                           data={"creation_id": creation_id, "access_token": self.token})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            return Transient(f"connection not made: {type(e).__name__}")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            return Unknown(f"no answer to media_publish: {type(e).__name__} (container {creation_id})")
        body = _json(resp)
        if resp.status_code == 200 and body.get("id"):
            try:
                return Delivered([int(body["id"])])
            except (TypeError, ValueError):
                return Unknown(f"published id is not numeric: {body.get('id')}")
        if resp.status_code == 200:
            return Unknown("media_publish answered without an id")
        return _classify_error(resp.status_code, body, publishing=True)


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


def _classify_error(status: int, body: dict, *, publishing: bool) -> Outcome:
    err = body.get("error") or {}
    code = err.get("code")
    msg = str(err.get("error_user_msg") or err.get("message") or f"HTTP {status}")[:300]
    if code in RATE or status == 429:
        return Transient(f"rate limited: {msg}", retry_after=60.0)
    if code in AUTH or (isinstance(code, int) and 200 <= code <= 299) or status in (401, 403):
        return Permanent(msg)
    if status >= 500 or code in (1, 2) or err.get("is_transient"):
        return Unknown(f"server error: {msg}") if publishing else Transient(f"server error: {msg}")
    if 400 <= status < 500:
        return Rejected(msg)
    return Unknown(f"unexpected answer {status}: {msg}") if publishing else Transient(f"unexpected answer {status}: {msg}")
