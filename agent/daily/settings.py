"""Settings of the daily content calendar. Own file (daily.yaml), independent of config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
KINDS = ("news", "open", "report", "symbol", "industry", "capital", "gold", "close")


@dataclass
class Entry:
    id: str
    kind: str
    time: str  # "HH:MM", local time
    days: list[int]  # python weekdays (Mon=0) on which it runs
    lead: int | None = None  # minutes before `time` to start writing (None = default)


@dataclass
class DailySettings:
    enabled: bool = True
    lead_minutes: int = 12  # analyses are written this long before their time, so the data is fresh
    late_minutes: int = 20  # if the bot could not produce a post within this long after its time, today's one is skipped
    news_window_hours: int = 20
    context_window_hours: int = 30
    gold_window_hours: int = 48
    news_sources_file: str = "news_sources.yaml"
    capital_sources_file: str = "capital_sources.yaml"
    capital_window_hours: int = 30
    report_stocks: bool = True  # False = the market report post / analyses do not list individual stocks
    state_file: str = "data/daily.json"
    market_days: list[int] = field(default_factory=lambda: [5, 6, 0, 1, 2])  # Sat..Wed
    entries: list[Entry] = field(default_factory=list)


def _days(value, default: list[int], where: str) -> list[int]:
    if value in (None, ""):
        return default
    if str(value).lower() == "all":
        return list(range(7))
    out = []
    for x in value:
        n = str(x).strip().lower()[:3]
        if n not in WEEKDAYS:
            raise SystemExit(f"daily.yaml، {where}: نام روز نامعتبر «{x}»؛ از mon,tue,wed,thu,fri,sat,sun یا all استفاده کنید.")
        out.append(WEEKDAYS[n])
    return out


def load_settings(path: str | Path = "daily.yaml") -> DailySettings:
    p = Path(path)
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    s = DailySettings()
    s.enabled = bool(raw.get("enabled", s.enabled)) and p.exists()
    for key in ("lead_minutes", "late_minutes", "news_window_hours", "context_window_hours", "gold_window_hours", "capital_window_hours"):
        if key in raw:
            setattr(s, key, max(0, int(raw[key])))
    s.news_sources_file = str(raw.get("news_sources_file", s.news_sources_file))
    s.capital_sources_file = str(raw.get("capital_sources_file", s.capital_sources_file))
    s.state_file = str(raw.get("state_file", s.state_file))
    s.report_stocks = bool(raw.get("report_stocks", s.report_stocks))
    s.market_days = _days(raw.get("market_days"), s.market_days, "market_days")
    ids = set()
    for i, e in enumerate(raw.get("schedule") or []):
        where = f"مورد {i + 1} از schedule"
        kind = str(e.get("kind", "")).lower()
        if kind not in KINDS:
            raise SystemExit(f"daily.yaml، {where}: kind باید یکی از {', '.join(KINDS)} باشد.")
        t = str(e.get("time", "")).strip()
        try:
            h, m = t.split(":")
            assert 0 <= int(h) < 24 and 0 <= int(m) < 60
        except (ValueError, AssertionError):
            raise SystemExit(f"daily.yaml، {where}: ساعت «{t}» نامعتبر است (قالب HH:MM).") from None
        eid = str(e.get("id") or f"{kind}-{t.replace(':', '')}")
        if eid in ids:
            raise SystemExit(f"daily.yaml: شناسهٔ تکراری «{eid}».")
        ids.add(eid)
        default_days = list(range(7)) if kind == "news" else s.market_days
        s.entries.append(Entry(
            eid, kind, f"{int(h):02d}:{int(m):02d}", _days(e.get("days"), default_days, where),
            int(e["lead"]) if e.get("lead") is not None else None,
        ))
    s.entries.sort(key=lambda x: x.time)
    return s
