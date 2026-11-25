"""Team memory: standing instructions the owner chose to keep, in their own words.

When the owner sends content back, rejects it with a reason, or revises a content plan, the control bot offers to keep
that note as a lesson. Only lessons the owner approved reach the writer and the editor (scope "writing") or the
marketing planner (scope "planning"). They are added after the organization's rules and can never override them: the
hard rules (the number rule, forbidden phrases and topics, links) are enforced in code whatever a lesson says, and a
lesson that itself contains a forbidden phrase, topic or link is refused. Every package records the fingerprint of
the lessons it was written under.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import audit
from .brand import Brand, normalize
from .db import Lesson

SCOPES = ("writing", "planning")
MAX_LEN = 300
MAX_ACTIVE = 30


class LessonError(Exception):
    pass


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def offer(session: Session, org_id: str, scope: str, text: str, origin: str) -> Lesson | None:
    """A lesson waiting for the owner's yes. None when the note is empty, too long, or already a lesson."""
    text = _clean(text)
    if scope not in SCOPES or not text or len(text) > MAX_LEN:
        return None
    for t in active(session, org_id, scope):
        if normalize(t) == normalize(text):
            return None
    row = Lesson(org_id=org_id, scope=scope, text=text, status="offered", origin=origin)
    session.add(row)
    session.flush()
    return row


def activate(session: Session, brand: Brand, lesson_id: int, actor: str, now: datetime) -> Lesson:
    from .agents.verification import text_rules
    row = session.get(Lesson, lesson_id)
    if row is None or row.org_id != brand.org_id or row.status != "offered":
        raise LessonError(f"درس {lesson_id} منتظر تصمیم نیست")
    problems = [f.message for f in text_rules(row.text, brand) if f.severity == "block" and f.rule_id != "R-TYPO"]
    if problems:
        row.status, row.decided_by, row.decided_at = "dismissed", actor, now
        raise LessonError("این متن نمی‌تواند قاعده شود: " + "؛ ".join(problems))
    if len(active(session, brand.org_id, row.scope)) >= MAX_ACTIVE:
        raise LessonError(f"حداکثر {MAX_ACTIVE} درس فعال؛ اول یکی را با /lessons بردارید")
    n = session.execute(update(Lesson).where(Lesson.id == lesson_id, Lesson.status == "offered")
                        .values(status="active", decided_by=actor, decided_at=now)).rowcount
    if n != 1:
        raise LessonError(f"درس {lesson_id} منتظر تصمیم نیست")
    audit.record(session, brand.org_id, actor, "lesson.active", f"lesson:{lesson_id}", {"scope": row.scope, "text": row.text})
    session.refresh(row)
    return row


def close(session: Session, org_id: str, lesson_id: int, actor: str, now: datetime) -> Lesson:
    """Dismiss an offered lesson, or retire an active one."""
    row = session.get(Lesson, lesson_id)
    if row is None or row.org_id != org_id or row.status not in ("offered", "active"):
        raise LessonError(f"درس {lesson_id} پیدا نشد")
    was = row.status
    row.status, row.decided_by, row.decided_at = ("dismissed" if was == "offered" else "retired"), actor, now
    if was == "active":
        audit.record(session, org_id, actor, "lesson.retired", f"lesson:{lesson_id}", {"scope": row.scope})
    session.flush()
    return row


def active_rows(session: Session, org_id: str, scope: str | None = None) -> list[Lesson]:
    q = select(Lesson).where(Lesson.org_id == org_id, Lesson.status == "active")
    if scope:
        q = q.where(Lesson.scope == scope)
    return list(session.execute(q.order_by(Lesson.id)).scalars())


def active(session: Session, org_id: str, scope: str) -> list[str]:
    return [r.text for r in active_rows(session, org_id, scope)]


def fingerprint(texts: list[str] | tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()[:12] if texts else "none"


def prompt_block(texts: list[str] | tuple[str, ...], what: str = "نوشتن") -> list[str]:
    if not texts:
        return []
    return ["", f"درس‌هایی که صاحب کانال از بازخوردهای قبلی برای {what} ثبت کرده است (همیشه رعایت کن؛ اگر با قواعد بالا "
            "تضاد داشت، قواعد بالا مقدم‌اند):", *[f"- {t}" for t in texts]]
