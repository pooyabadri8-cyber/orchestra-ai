"""SQLite-backed post queue.

Status flow:  draft -> scheduled -> sending -> published
                 \\-> rejected     \\-> scheduled (retry after a transient error, up to max_publish_attempts)
                                 \\-> failed       (permanent error the owner can fix: edit, then publish again)
                                 \\-> dead         (retries exhausted: dead-letter queue, /dead and /retry)
                                 \\-> unconfirmed  (the send may or may not have reached the channel: owner decides,
                                                    /sent when it is there, /retry to send again)
All timestamps are stored as UTC ISO-8601 strings.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT NOT NULL,
    pillar        TEXT,
    text          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',
    proposed_at   TEXT,
    scheduled_at  TEXT,
    created_at    TEXT NOT NULL,
    published_at  TEXT,
    message_id    INTEGER,
    error         TEXT,
    source_url    TEXT,
    image_path    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_at);

CREATE TABLE IF NOT EXISTS events (   -- what happened to each post: the raw material of the dashboard's numbers
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    post_id  INTEGER,
    kind     TEXT NOT NULL    -- created | edited | approved | rejected | auto | held | published | failed | retry | dead | unconfirmed | requeued
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS seen_items (
    item_key    TEXT PRIMARY KEY,
    source      TEXT,
    title       TEXT,
    url         TEXT,
    status      TEXT NOT NULL,   -- drafted | skipped
    created_at  TEXT NOT NULL
);
"""


@dataclass
class Post:
    id: int
    topic: str
    pillar: str | None
    text: str
    status: str
    proposed_at: str | None
    scheduled_at: str | None
    created_at: str
    published_at: str | None
    message_id: int | None
    error: str | None
    source_url: str | None = None
    image_path: str | None = None
    attempts: int = 0
    last_attempt_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.extra_busy = None  # optional callable -> datetimes the posting clock will use soon (calendar slots)
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(posts)")}
        if "source_url" not in cols:  # database created by an older version
            self.db.execute("ALTER TABLE posts ADD COLUMN source_url TEXT")
        if "image_path" not in cols:
            self.db.execute("ALTER TABLE posts ADD COLUMN image_path TEXT")
        if "attempts" not in cols:
            self.db.execute("ALTER TABLE posts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "last_attempt_at" not in cols:
            self.db.execute("ALTER TABLE posts ADD COLUMN last_attempt_at TEXT")
        self.db.commit()

    def _ev(self, post_id: int | None, kind: str) -> None:  # inside the caller's transaction
        self.db.execute("INSERT INTO events (ts, post_id, kind) VALUES (?,?,?)", (_now(), post_id, kind))

    def log_event(self, post_id: int | None, kind: str) -> None:
        self._ev(post_id, kind)
        self.db.commit()

    @staticmethod
    def _post(row: sqlite3.Row | None) -> Post | None:
        return Post(**dict(row)) if row else None

    def add_draft(
        self, topic: str, pillar: str | None, text: str,
        proposed_at: datetime | None = None, source_url: str | None = None, image_path: str | None = None,
    ) -> Post:
        cur = self.db.execute(
            "INSERT INTO posts(topic, pillar, text, proposed_at, created_at, source_url, image_path) VALUES (?,?,?,?,?,?,?)",
            (topic, pillar, text, iso(proposed_at) if proposed_at else None, _now(), source_url, image_path),
        )
        self._ev(cur.lastrowid, "created")
        self.db.commit()
        return self.get(cur.lastrowid)  # type: ignore[arg-type]

    def get(self, post_id: int) -> Post | None:
        return self._post(self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())

    def update_text(self, post_id: int, text: str) -> None:
        cur = self.db.execute("UPDATE posts SET text=?, error=NULL WHERE id=? AND status IN ('draft','scheduled','failed')", (text, post_id))
        if cur.rowcount:
            self._ev(post_id, "edited")
        self.db.commit()

    def set_image(self, post_id: int, path: str | None) -> None:
        self.db.execute("UPDATE posts SET image_path=? WHERE id=? AND status IN ('draft','scheduled','failed')", (path, post_id))
        self.db.commit()

    def schedule(self, post_id: int, when: datetime) -> None:
        self.db.execute(
            "UPDATE posts SET status='scheduled', scheduled_at=?, error=NULL WHERE id=? AND status IN ('draft','scheduled','failed')",
            (iso(when), post_id),
        )
        self.db.commit()

    def reject(self, post_id: int) -> None:
        cur = self.db.execute("UPDATE posts SET status='rejected' WHERE id=? AND status IN ('draft','scheduled','failed')", (post_id,))
        if cur.rowcount:
            self._ev(post_id, "rejected")
        self.db.commit()

    # ---- reliable publishing: a post is CLAIMED in the database before anything is sent, so two jobs (or a restart in
    # the middle of a send) can never publish the same post twice.
    def begin_send(self, post_id: int) -> bool:
        """Atomically claim a post for sending. False if somebody else holds it or it is not publishable."""
        cur = self.db.execute(
            "UPDATE posts SET status='sending', attempts=attempts+1, last_attempt_at=? "
            "WHERE id=? AND status IN ('draft','scheduled','failed')", (_now(), post_id),
        )
        self.db.commit()
        return cur.rowcount == 1

    def mark_retry(self, post_id: int, error: str, when: datetime) -> None:
        """Transient failure, certainly not delivered: try again at `when`."""
        self.db.execute("UPDATE posts SET status='scheduled', scheduled_at=?, error=? WHERE id=? AND status='sending'", (iso(when), error[:500], post_id))
        self._ev(post_id, "retry")
        self.db.commit()

    def mark_dead(self, post_id: int, error: str) -> None:
        self.db.execute("UPDATE posts SET status='dead', error=? WHERE id=? AND status IN ('sending','scheduled','failed')", (error[:500], post_id))
        self._ev(post_id, "dead")
        self.db.commit()

    def mark_unconfirmed(self, post_id: int, error: str) -> None:
        self.db.execute("UPDATE posts SET status='unconfirmed', error=? WHERE id=? AND status='sending'", (error[:500], post_id))
        self._ev(post_id, "unconfirmed")
        self.db.commit()

    def recover_sending(self) -> list[int]:
        """At start-up: a post still 'sending' means the bot stopped mid-send. It may be in the channel already, so it is
        NOT resent automatically: it becomes 'unconfirmed' for the owner to decide."""
        ids = [r[0] for r in self.db.execute("SELECT id FROM posts WHERE status='sending'").fetchall()]
        for i in ids:
            self.db.execute("UPDATE posts SET status='unconfirmed', error='ربات هنگام ارسال متوقف شد' WHERE id=?", (i,))
            self._ev(i, "unconfirmed")
        self.db.commit()
        return ids

    def parked(self) -> list[Post]:
        rows = self.db.execute("SELECT * FROM posts WHERE status IN ('dead','unconfirmed') ORDER BY id").fetchall()
        return [self._post(r) for r in rows]  # type: ignore[misc]

    def requeue(self, post_id: int, when: datetime) -> bool:
        """Owner decision: send a dead / unconfirmed post again (attempts start from zero)."""
        cur = self.db.execute(
            "UPDATE posts SET status='scheduled', scheduled_at=?, attempts=0, error=NULL WHERE id=? AND status IN ('dead','unconfirmed','failed')",
            (iso(when), post_id),
        )
        if cur.rowcount:
            self._ev(post_id, "requeued")
        self.db.commit()
        return cur.rowcount == 1

    def confirm_sent(self, post_id: int) -> bool:
        """Owner decision: the unconfirmed post is in the channel."""
        cur = self.db.execute(
            "UPDATE posts SET status='published', published_at=COALESCE(last_attempt_at, ?), error=NULL WHERE id=? AND status='unconfirmed'",
            (_now(), post_id),
        )
        self.db.commit()
        return cur.rowcount == 1

    def mark_published(self, post_id: int, message_id: int) -> None:
        self.db.execute(
            "UPDATE posts SET status='published', published_at=?, message_id=?, error=NULL WHERE id=?",
            (_now(), message_id, post_id),
        )
        self._ev(post_id, "published")
        self.db.commit()

    def mark_deleted(self, post_id: int) -> None:
        """A published post that was removed from the channel by the owner."""
        self.db.execute("UPDATE posts SET status='deleted' WHERE id=? AND status='published'", (post_id,))
        self.db.commit()

    def busy_times(self, exclude: set[int] | None = None) -> list:
        """Times that other posts occupy: every scheduled post (any kind) and the most recent published one.
        New posts keep a few minutes away from these."""
        ex = exclude or set()
        rows = self.db.execute("SELECT id, scheduled_at FROM posts WHERE status='scheduled'").fetchall()
        times = [parse_iso(r[1]) for r in rows if r[1] and r[0] not in ex]
        last = self.db.execute("SELECT MAX(published_at) FROM posts WHERE status='published'").fetchone()[0]
        if last:
            times.append(parse_iso(last))
        if self.extra_busy:  # upcoming calendar posts: source posts keep their distance from them too
            times += list(self.extra_busy())
        return times

    def count_regular_since(self, since: datetime) -> int:
        """Posts that are not calendar posts (source posts and manual ones) published or waiting since `since`."""
        return self.db.execute(
            "SELECT COUNT(*) FROM posts WHERE status IN ('scheduled','published') AND created_at>=? "
            "AND COALESCE(pillar,'') NOT LIKE 'daily:%'", (iso(since),),
        ).fetchone()[0]

    def count_scheduled(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM posts WHERE status='scheduled' AND COALESCE(pillar,'') NOT LIKE 'daily:%'").fetchone()[0]

    def recent_headlines(self, limit: int = 30) -> list[str]:
        rows = self.db.execute(
            "SELECT text FROM posts WHERE status IN ('scheduled','published','draft') ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r[0] for r in rows]

    def mark_failed(self, post_id: int, error: str) -> None:
        self.db.execute("UPDATE posts SET status='failed', error=? WHERE id=?", (error[:500], post_id))
        self._ev(post_id, "failed")
        self.db.commit()

    def due(self, now: datetime) -> list[Post]:
        rows = self.db.execute(
            "SELECT * FROM posts WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at",
            (iso(now),),
        ).fetchall()
        return [self._post(r) for r in rows]  # type: ignore[misc]

    def queue(self) -> list[Post]:
        rows = self.db.execute(
            "SELECT * FROM posts WHERE status IN ('draft','scheduled','failed') ORDER BY COALESCE(scheduled_at, proposed_at, created_at)"
        ).fetchall()
        return [self._post(r) for r in rows]  # type: ignore[misc]

    def taken_slots(self) -> set[str]:
        """Scheduled times already in use (UTC ISO), so new posts don't collide."""
        rows = self.db.execute("SELECT scheduled_at FROM posts WHERE status='scheduled'").fetchall()
        return {r[0] for r in rows}

    def reserved_slots(self) -> set[str]:
        """Times already used by scheduled posts or proposed for open drafts (avoid proposing the same slot twice)."""
        rows = self.db.execute(
            "SELECT scheduled_at FROM posts WHERE status='scheduled' "
            "UNION SELECT proposed_at FROM posts WHERE status='draft' AND proposed_at IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows}

    def recent_topics(self, limit: int = 15) -> list[str]:
        rows = self.db.execute(
            "SELECT topic FROM posts WHERE status IN ('scheduled','published','draft') "
            "AND COALESCE(pillar,'') NOT LIKE 'daily:%' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    # ---- source items already processed (so the same article is never drafted twice)

    def is_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen_items WHERE item_key=?", (key,)).fetchone() is not None

    def mark_seen(self, key: str, source: str, title: str, url: str, status: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO seen_items(item_key, source, title, url, status, created_at) VALUES (?,?,?,?,?,?)",
            (key, source, title[:200], url, status, _now()),
        )
        self.db.commit()

    def seen_counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT source, COUNT(*) FROM seen_items GROUP BY source").fetchall()
        return {r[0]: r[1] for r in rows}
