"""The numbers of one customer (for its dashboard and for the admin overview): counted from its own database.

Pure functions over a sqlite connection, no network, no secrets. A snapshot (`snapshot`) is what a customer's bot
publishes for the admin overview: aggregated numbers and headlines only, never settings, tokens or full texts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .autopilot import headline_of

SNAPSHOT_EVERY = 300  # seconds between snapshot files written by a running bot
STALE_AFTER = 600  # an admin overview marks a snapshot older than this as stale
HEARTBEAT_STALE = 300


def _rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # an older database without the table yet
        return []


def compute(db: sqlite3.Connection, tz: str = "Asia/Tehran", now: datetime | None = None, days: int = 14, recent: int = 15) -> dict:
    zone = ZoneInfo(tz)
    now = now or datetime.now(timezone.utc)
    local_today = now.astimezone(zone).date()
    since30 = (now - timedelta(days=30)).isoformat(timespec="seconds")

    by_status = dict(_rows(db, "SELECT status, COUNT(*) FROM posts GROUP BY status"))
    published = [r[0] for r in _rows(db, "SELECT published_at FROM posts WHERE status='published' AND published_at IS NOT NULL")]
    per_day = {(local_today - timedelta(days=i)).isoformat(): 0 for i in range(days)}
    today = week = month = 0
    for ts in published:
        d = datetime.fromisoformat(ts).astimezone(zone).date()
        age = (local_today - d).days
        if age == 0:
            today += 1
        if 0 <= age < 7:
            week += 1
        if 0 <= age < 30:
            month += 1
        if d.isoformat() in per_day:
            per_day[d.isoformat()] += 1

    ev = _rows(db, "SELECT post_id, kind, ts FROM events WHERE ts>=? ORDER BY id", (since30,))
    edited_before: dict[int, bool] = {}
    approved = unchanged = rejected = auto = held = retries = 0
    for pid, kind, _ts in ev:
        if kind == "edited":
            edited_before[pid] = True
        elif kind == "approved":
            approved += 1
            unchanged += 0 if edited_before.get(pid) else 1
        elif kind == "rejected":
            rejected += 1
        elif kind == "auto":
            auto += 1
        elif kind == "held":
            held += 1
        elif kind == "retry":
            retries += 1
    decided = approved + rejected
    handled = decided + auto
    seen = dict(_rows(db, "SELECT status, COUNT(*) FROM seen_items GROUP BY status"))

    def post_row(r) -> dict:
        return {"id": r[0], "status": r[1], "headline": (headline_of(r[2]) or r[3] or "")[:110], "pillar": r[4] or "",
                "at": r[5] or r[6], "attempts": r[7], "error": (r[8] or "")[:160]}
    cols = "id, status, text, topic, pillar, published_at, created_at, attempts, error"
    recent_rows = [post_row(r) for r in _rows(db, f"SELECT {cols} FROM posts ORDER BY id DESC LIMIT ?", (recent,))]
    parked = [post_row(r) for r in _rows(db, f"SELECT {cols} FROM posts WHERE status IN ('dead','unconfirmed','failed') ORDER BY id DESC LIMIT 20")]
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "posts": {"by_status": by_status, "published_today": today, "published_7d": week, "published_30d": month,
                  "queue": by_status.get("scheduled", 0), "drafts": by_status.get("draft", 0)},
        "per_day": [{"date": d, "published": n} for d, n in sorted(per_day.items())],
        "review_30d": {"approved": approved, "approved_unchanged": unchanged, "approved_after_edit": approved - unchanged,
                       "rejected": rejected, "approval_rate": round(unchanged / decided, 3) if decided else None},
        "automation_30d": {"auto": auto, "held": held, "reviewed": decided,
                           "auto_share": round(auto / handled, 3) if handled else None},
        "reliability": {"retries_30d": retries, "dead": by_status.get("dead", 0), "unconfirmed": by_status.get("unconfirmed", 0),
                        "failed": by_status.get("failed", 0)},
        "sources": {"seen_by_status": seen, "skip_rate": round(seen.get("skipped", 0) / max(1, seen.get("skipped", 0) + seen.get("drafted", 0)), 3)},
        "recent": recent_rows,
        "parked": parked,
    }


def read_only(db_path: str | Path) -> sqlite3.Connection:
    """The dashboard opens the database read-only: it cannot change a customer's data even by mistake."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def heartbeat_age(path: str | Path = "data/heartbeat.json") -> float | None:
    try:
        return time.time() - json.loads(Path(path).read_text(encoding="utf-8")).get("ts", 0)
    except (OSError, ValueError):
        return None


def snapshot(customer: str, cfg, db: sqlite3.Connection, headlines: bool = True) -> dict:
    """What the admin overview may see about a customer: numbers, status, recent headlines. No settings, secrets or texts."""
    s = compute(db, cfg.timezone)
    if not headlines:
        for key in ("recent", "parked"):
            for r in s[key]:
                r["headline"] = ""
    return {
        "customer": customer, "name": cfg.channel_name, "platform": cfg.platform,
        "approval": cfg.approval if cfg.auto_publish else "review",
        "features": [k for k, v in cfg.profile.features.items() if v],
        "heartbeat_age": heartbeat_age(), **s,
    }


def write_snapshot(stats_dir: str | Path, customer: str, snap: dict) -> Path:
    d = Path(stats_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{customer}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, out)
    return out


def default_stats_dir() -> Path:
    return Path(os.environ.get("STATS_DIR") or Path(__file__).resolve().parent.parent / "stats")
