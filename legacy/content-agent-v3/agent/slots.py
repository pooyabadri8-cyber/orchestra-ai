"""Picks publication times from the configured daily slots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import _parse_hhmm
from .store import iso


def next_slots(
    now_utc: datetime,
    tz: ZoneInfo,
    post_times: list[str],
    taken: set[str],
    count: int = 1,
    min_lead_minutes: int = 5,
) -> list[datetime]:
    """Return the next `count` free slots (as UTC datetimes), skipping slots in `taken` (UTC ISO strings)."""
    result: list[datetime] = []
    local_now = now_utc.astimezone(tz)
    earliest = now_utc + timedelta(minutes=min_lead_minutes)
    for day in range(0, 120):
        d = (local_now + timedelta(days=day)).date()
        for t in sorted(post_times):
            h, m = _parse_hhmm(t)
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=tz).astimezone(timezone.utc)
            if slot < earliest or iso(slot) in taken:
                continue
            result.append(slot)
            taken = taken | {iso(slot)}
            if len(result) >= count:
                return result
    return result


def parse_local(s: str, tz: ZoneInfo) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' in the configured timezone, return UTC."""
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(timezone.utc)


def fmt_local(utc_iso: str | None, tz: ZoneInfo) -> str:
    if not utc_iso:
        return "—"
    return datetime.fromisoformat(utc_iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")
