"""The only path to a language model.

- Providers: Anthropic Messages API and any OpenAI-compatible /chat/completions endpoint, called with httpx.
- A chain per tier: if a model fails (quota, outage) the next one is tried.
- Budgets per run: a hard cap on calls and, when prices are configured, on cost.
- Every call is written to llm_calls (prompt, response, tokens, cost, latency), success or not.
- An answer cut off by the token limit is never returned: one retry with double budget, then an error.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .db import LLMCall


CONNECT_TIMEOUT = 15.0
UNREACHABLE_HINT = "به سرویس مدل وصل نشد؛ اگر آن سرویس از ایران بسته است، VPN را روشن کنید"
BAD_URL_MESSAGE = "آدرس سرویس مدل («{url}») درست نیست؛ باید با https:// شروع شود (ORCHESTRA_LLM_BASE_URL در .env)"


GEMINI_DEFAULT_MODELS = "gemini-3.6-flash,gemini-3.5-flash-lite"  # what setup offers for Gemini
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{1,80}")


def http_error(status: int, text: str, model: str) -> str:
    """One plain sentence (Persian) for a model service's error answer, with what to do; the service's own short
    message is kept at the end for whoever needs it. Never the raw JSON."""
    import json
    detail = ""
    try:
        body = json.loads(text)
        body = body[0] if isinstance(body, list) and body else body
        err = body.get("error") if isinstance(body, dict) else None
        detail = str((err.get("message") if isinstance(err, dict) else err) or "").strip()
    except (ValueError, AttributeError, TypeError):
        detail = " ".join((text or "").split())
    detail = detail.replace("\\n", " ").split("\n")[0][:160]
    low = detail.lower()
    if status == 400 and "model" in low or status == 404:
        why = f"سرویس مدل، نام مدل «{model}» را نمی‌شناسد؛ ORCHESTRA_LLM_MODEL را در .env درست کنید (مثلاً {GEMINI_DEFAULT_MODELS})."
    elif status in (401, 403):
        why = "کلید مدل پذیرفته نشد؛ ORCHESTRA_LLM_API_KEY را در .env بررسی کنید (یا setup.bat)."
    elif status == 429:
        why = "سهمیهٔ سرویس مدل فعلاً تمام شده یا درخواست‌ها زیاد بوده؛ کمی بعد دوباره امتحان می‌شود."
    elif status >= 500:
        why = "سرویس مدل فعلاً جواب درست نمی‌دهد (مشکل از سمت سرویس است)؛ کمی بعد دوباره امتحان کنید."
    else:
        why = "سرویس مدل درخواست را نپذیرفت."
    return f"{why} (HTTP {status}{': ' + detail if detail else ''})"


def check_base_url(url: str) -> None:
    from urllib.parse import urlparse
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(BAD_URL_MESSAGE.format(url=url))


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class BudgetExceeded(LLMError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False)


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    truncated: bool


class Provider(Protocol):
    name: str

    async def complete(self, model: str, system: str, user: str, max_tokens: int) -> Completion: ...


@dataclass
class AnthropicProvider:
    api_key: str
    base_url: str = "https://api.anthropic.com"
    timeout: float = 120.0
    name: str = "anthropic"

    async def complete(self, model: str, system: str, user: str, max_tokens: int) -> Completion:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=CONNECT_TIMEOUT)) as http:
            try:
                r = await http.post(f"{self.base_url}/v1/messages", json=body, headers=headers)
            except (httpx.UnsupportedProtocol, httpx.InvalidURL) as e:
                raise LLMError(BAD_URL_MESSAGE.format(url=self.base_url), retryable=False) from e
            except (httpx.ConnectTimeout, httpx.ConnectError) as e:
                raise LLMError(f"network: {type(e).__name__} — {UNREACHABLE_HINT}") from e
            except httpx.HTTPError as e:
                raise LLMError(f"network: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(http_error(r.status_code, r.text[:2000], model), retryable=r.status_code in (408, 429, 500, 502, 503, 504, 529))
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        usage = data.get("usage", {})
        return Completion(text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), data.get("stop_reason") == "max_tokens")


@dataclass
class OpenAICompatibleProvider:
    api_key: str
    base_url: str
    timeout: float = 120.0
    name: str = "openai_compatible"
    # "Thinking" models (Gemini 2.5+, o-series ...) spend part of max_tokens on hidden reasoning; a small limit then
    # cuts the visible answer short. Only the tokens actually used are paid for, so the limit is kept generous.
    min_output_tokens: int = 8192

    async def complete(self, model: str, system: str, user: str, max_tokens: int) -> Completion:
        body = {"model": model, "max_tokens": max(max_tokens, self.min_output_tokens),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=CONNECT_TIMEOUT)) as http:
            try:
                r = await http.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {self.api_key}"})
            except (httpx.UnsupportedProtocol, httpx.InvalidURL) as e:
                raise LLMError(BAD_URL_MESSAGE.format(url=self.base_url), retryable=False) from e
            except (httpx.ConnectTimeout, httpx.ConnectError) as e:
                raise LLMError(f"network: {type(e).__name__} — {UNREACHABLE_HINT}") from e
            except httpx.HTTPError as e:
                raise LLMError(f"network: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(http_error(r.status_code, r.text[:2000], model), retryable=r.status_code in (408, 429, 500, 502, 503, 504))
        data = r.json()
        try:
            choice = data["choices"][0]
            text = (choice["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected reply: {str(data)[:200]}") from e
        usage = data.get("usage") or {}
        return Completion(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                          choice.get("finish_reason") == "length")


@dataclass(frozen=True)
class Route:
    provider: Provider
    model: str
    usd_per_mtok_in: float | None = None
    usd_per_mtok_out: float | None = None

    def cost(self, c: Completion) -> float | None:
        if self.usd_per_mtok_in is None or self.usd_per_mtok_out is None:
            return None
        return c.input_tokens / 1e6 * self.usd_per_mtok_in + c.output_tokens / 1e6 * self.usd_per_mtok_out


@dataclass
class Gateway:
    sessions: sessionmaker
    tiers: dict[str, list[Route]]
    max_calls_per_run: int = 12
    max_cost_per_run_usd: float | None = None
    clock_ms: object = field(default=lambda: int(time.monotonic() * 1000))

    def _spent(self, run_id: str) -> tuple[int, float]:
        with self.sessions() as s:
            n, cost = s.execute(select(func.count(LLMCall.id), func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                .where(LLMCall.run_id == run_id)).one()
        return int(n), float(cost)

    async def complete(self, *, org_id: str, run_id: str, step_key: str, tier: str, system: str, user: str,
                       max_tokens: int = 2000) -> str:
        routes = self.tiers.get(tier)
        if not routes:
            raise LLMError(f"no model configured for tier {tier}", retryable=False)
        errors = []
        for route in routes:
            budget = max_tokens
            for _ in range(2):  # a truncated answer gets one retry with double the budget
                n, cost = self._spent(run_id)
                if n >= self.max_calls_per_run:
                    raise BudgetExceeded(f"run {run_id} reached {n} model calls")
                if self.max_cost_per_run_usd is not None and cost >= self.max_cost_per_run_usd:
                    raise BudgetExceeded(f"run {run_id} reached ${cost:.3f}")
                started = self.clock_ms()
                try:
                    c = await route.provider.complete(route.model, system, user, budget)
                except LLMError as e:
                    self._log(org_id, run_id, step_key, route, system, user, None, None, self.clock_ms() - started, str(e))
                    errors.append(f"{route.model}: {e}")
                    if not e.retryable:
                        raise
                    break  # next route
                self._log(org_id, run_id, step_key, route, system, user, c, route.cost(c), self.clock_ms() - started,
                          "truncated" if c.truncated else (None if c.text else "empty answer"))
                if c.truncated:
                    budget *= 2
                    continue
                if not c.text:
                    errors.append(f"{route.model}: empty answer")
                    break
                return c.text
            else:
                errors.append(f"{route.model}: answer cut off at the token limit twice")
        raise LLMError(" | ".join(errors))

    def _log(self, org_id, run_id, step_key, route: Route, system, user, c: Completion | None, cost, latency, error) -> None:
        with self.sessions.begin() as s:
            s.add(LLMCall(
                org_id=org_id, run_id=run_id, step_key=step_key, provider=route.provider.name, model=route.model,
                prompt_sha256=hashlib.sha256((system + "\n\x00\n" + user).encode()).hexdigest(), system=system, prompt=user,
                response=c.text if c else None, input_tokens=c.input_tokens if c else 0, output_tokens=c.output_tokens if c else 0,
                cost_usd=cost, latency_ms=int(latency), ok=error is None, error=error,
            ))
