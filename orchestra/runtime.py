"""What the workflow steps use at run time: database, asset store, model gateway, sources, notifier.

DBOS steps are module-level functions, so they reach their dependencies through this one registry, set once at
start-up (or by a test).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy.orm import sessionmaker

from .assets import AssetStore
from .data.acquisition import Fetcher
from .data.sources.base import Source
from .db import utcnow
from .llm import Gateway


class ApprovalNotifier(Protocol):
    async def request(self, *, org_id: str, run_id: str, package_id: str, package_hash: str) -> None: ...


class NullNotifier:
    async def request(self, **kw) -> None:  # nobody is told; the decision can still be sent with submit_decision
        return None


@dataclass
class Runtime:
    sessions: sessionmaker
    assets: AssetStore
    gateway: Gateway
    fetcher: Fetcher
    sources: dict[str, Source]
    brand_root: Path = Path("orgs")
    notifier: ApprovalNotifier = field(default_factory=NullNotifier)
    clock: Callable[[], datetime] = utcnow


_current: Runtime | None = None


def set_runtime(rt: Runtime) -> None:
    global _current
    _current = rt


def rt() -> Runtime:
    if _current is None:
        raise RuntimeError("runtime not configured: call orchestra.runtime.set_runtime() at start-up")
    return _current
