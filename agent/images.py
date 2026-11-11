"""Pictures for posts: download the photo that belongs to a source item, check it, keep it on disk.

Only the post's own photo (feed media, og:image, the photo of a Telegram post) is ever used: it is downloaded by the bot
(so an expired or hot-link-protected address cannot break publishing later) and checked: real image, not too small or big.
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

import httpx

log = logging.getLogger("content-agent.images")

MIN_BYTES = 12_000  # smaller files are icons / tracking pixels
MAX_BYTES = 9_000_000  # Telegram accepts photos up to 10 MB
_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}


def sniff(data: bytes) -> str | None:
    """File type from the first bytes (the server's Content-Type header is not trusted)."""
    for magic, ext in _TYPES.items():
        if data.startswith(magic):
            return ext if ext != "webp" or data[8:12] == b"WEBP" else None
    return None


def size_of(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # Pillow missing or unreadable picture
        return None


async def download(http: httpx.AsyncClient, url: str, folder: str, min_width: int = 400) -> str | None:
    """Fetch `url` into `folder`; returns the file path, or None when the picture is missing / unsuitable."""
    if not url:
        return None
    try:
        r = await http.get(url, headers={"Accept": "image/*"})
    except httpx.HTTPError as e:
        log.info("picture not downloaded (%s): %s", type(e).__name__, url)
        return None
    data = r.content
    if r.status_code != 200 or not (MIN_BYTES <= len(data) <= MAX_BYTES):
        return None
    ext = sniff(data)
    if not ext:
        return None
    dims = size_of(data)
    if dims:
        w, h = dims
        if w < min_width or w + h > 9000 or max(w, h) / max(1, min(w, h)) > 4:
            return None  # too small, or a banner/strip that Telegram would reject or crop badly
    Path(folder).mkdir(parents=True, exist_ok=True)
    path = Path(folder) / f"{hashlib.sha1(url.encode()).hexdigest()[:20]}.{ext}"
    path.write_bytes(data)
    return str(path)


def pick(key: str, ratio: float) -> bool:
    """Stable choice, from the item's key, of whether this item gets its picture (about `ratio` of all items)."""
    if ratio >= 1:
        return True
    if ratio <= 0:
        return False
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < ratio
