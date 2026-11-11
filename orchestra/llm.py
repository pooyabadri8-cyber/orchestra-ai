"""The only path to a language model.

- Providers: Anthropic Messages API and any OpenAI-compatible /chat/completions endpoint, called with httpx.
- A chain per tier: if a model fails (quota, outage) the next one is tried.
- Budgets per run: a hard cap on calls and, when prices are configured, on cost.
- Every call is written to llm_calls (prompt, response, tokens, cost, latency), success or not.
- An answer cut off by the token limit is never returned: one retry with double budget, then an error.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .db import LLMCall


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
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            try:
                r = await http.post(f"{self.base_url}/v1/messages", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise LLMError(f"network: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code in (408, 429, 500, 502, 503, 504, 529))
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

    async def complete(self, model: str, system: str, user: str, max_tokens: int) -> Completion:
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            try:
                r = await http.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as e:
                raise LLMError(f"network: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code in (408, 429, 500, 502, 503, 504))
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
