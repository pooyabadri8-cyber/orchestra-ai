"""A small Bot API client for the control chat. Telegram and Bale share this API shape.

Only what the control bot needs: read updates, send text (with buttons), send an album, answer a button press, and
replace a message's text. Every failure is a BotApiError that says whether trying again later makes sense.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ..publishing.adapters import API_BASE

TEXT_LIMIT = 4096
ALLOWED_UPDATES = ["message", "callback_query"]
ALBUM_LIMIT = 10


class BotApiError(Exception):
    def __init__(self, message: str, *, retryable: bool, code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class BotApi:
    def __init__(self, platform: str, token: str, *, base_url: str | None = None, timeout: float = 30.0,
                 client: httpx.Client | None = None):
        if platform not in API_BASE:
            raise ValueError(f"control platform must be one of {sorted(API_BASE)}, not {platform!r}")
        self.platform = platform
        self._base = f"{base_url or API_BASE[platform]}{token}/"
        self.timeout = timeout
        # Bale is an Iranian service: reached directly, never through a proxy meant for foreign sites
        self.http = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15), trust_env=platform != "bale")

    def close(self) -> None:
        self.http.close()

    def call(self, method: str, data: dict | None = None, files: dict | None = None, timeout: float | None = None) -> Any:
        try:
            resp = self.http.post(self._base + method, data=data or {}, files=files, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            raise BotApiError(f"{method}: {type(e).__name__}", retryable=True) from e
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and isinstance(body, dict) and body.get("ok"):
            return body.get("result")
        desc = str((body or {}).get("description") or f"HTTP {resp.status_code}")[:300] if isinstance(body, dict) else f"HTTP {resp.status_code}"
        raise BotApiError(f"{method}: {desc}", retryable=resp.status_code == 429 or resp.status_code >= 500, code=resp.status_code)

    # ------------------------------------------------------------------ reading

    def get_updates(self, offset: int, wait_seconds: int) -> list[dict]:
        # allowed_updates is always sent: Telegram otherwise keeps whatever the last getUpdates call asked for, and a
        # list without callback_query (set once by another tool) makes every button silently dead.
        result = self.call("getUpdates", {"offset": offset, "timeout": wait_seconds, "limit": 50,
                                          "allowed_updates": json.dumps(ALLOWED_UPDATES)}, timeout=wait_seconds + 15)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------ writing

    def send_text(self, chat_id: str, text: str, *, parse_mode: str | None = None,
                  buttons: list[list[tuple[str, str]]] | None = None) -> int | None:
        """Send one message (the caller keeps it within TEXT_LIMIT). `buttons`: rows of (label, callback_data)."""
        data: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode and self.platform == "telegram":
            data["parse_mode"] = parse_mode
        if self.platform == "telegram":
            data["link_preview_options"] = json.dumps({"is_disabled": True})
        if buttons:
            data["reply_markup"] = json.dumps({"inline_keyboard": [
                [{"text": label, "callback_data": cb} for label, cb in row] for row in buttons]}, ensure_ascii=False)
        try:
            result = self.call("sendMessage", data)
        except BotApiError as e:
            if e.retryable or "parse_mode" not in data:
                raise
            data.pop("parse_mode")  # the platform could not read the formatting: show the words anyway
            data["text"] = _strip_tags(text)
            result = self.call("sendMessage", data)
        return result.get("message_id") if isinstance(result, dict) else None

    def send_album(self, chat_id: str, images: list[tuple[str, bytes, str]]) -> None:
        """images: (file name, bytes, mime). One image is sent as a photo; more as albums of up to ten. If the
        platform refuses the album form, the images are sent one by one, so the owner still sees every slide."""
        for start in range(0, len(images), ALBUM_LIMIT):
            part = images[start:start + ALBUM_LIMIT]
            if len(part) > 1:
                media = [{"type": "photo", "media": f"attach://f{i}"} for i in range(len(part))]
                files = {f"f{i}": (name, data, mime) for i, (name, data, mime) in enumerate(part)}
                try:
                    self.call("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)}, files=files)
                    continue
                except BotApiError as e:
                    if e.retryable:
                        raise
            for name, data, mime in part:
                self.call("sendPhoto", {"chat_id": chat_id}, files={"photo": (name, data, mime)})

    def answer_button(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except BotApiError:
            pass  # only stops the button's spinner; never worth failing for

    def replace_text(self, chat_id: str, message_id: int, text: str) -> None:
        """Replace a message's text and remove its buttons. If the platform refuses, the caller's own state still
        protects against a second press, so this is best effort."""
        try:
            self.call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:TEXT_LIMIT]})
        except BotApiError:
            pass


def _strip_tags(text: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on line breaks so that each part fits one message."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts
