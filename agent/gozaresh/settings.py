"""Settings of the daily-report feature. Own file (gozaresh.yaml), independent of config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass
class GozareshSettings:
    url: str = "https://bourse-trader.ir/"
    top_n: int = 5  # how many stocks / groups per list
    persian_digits: bool = True
    off_weekdays: list[int] = field(default_factory=lambda: [3, 4])  # no scheduled reports on closed days (Thu, Fri)
    default_times: list[str] = field(default_factory=list)  # for owners who set nothing themselves; [] = none
    state_file: str = "data/gozaresh.json"
    cache_seconds: int = 45
    late_minutes: int = 15  # a scheduled report is skipped if the bot was down longer than this after its time


def load_settings(path: str | Path = "gozaresh.yaml") -> GozareshSettings:
    p = Path(path)
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    s = GozareshSettings()
    s.url = str(raw.get("url", s.url))
    s.top_n = max(1, min(15, int(raw.get("top_n", s.top_n))))
    s.persian_digits = bool(raw.get("persian_digits", s.persian_digits))
    if "off_weekdays" in raw:
        names = [str(x).strip().lower()[:3] for x in (raw["off_weekdays"] or [])]
        bad = [n for n in names if n not in WEEKDAYS]
        if bad:
            raise SystemExit(f"gozaresh.yaml: نام روز نامعتبر {bad}؛ از mon,tue,wed,thu,fri,sat,sun استفاده کنید.")
        s.off_weekdays = [WEEKDAYS[n] for n in names]
    s.default_times = [str(t) for t in raw.get("default_times", [])]
    s.state_file = str(raw.get("state_file", s.state_file))
    s.cache_seconds = int(raw.get("cache_seconds", s.cache_seconds))
    s.late_minutes = int(raw.get("late_minutes", s.late_minutes))
    return s
