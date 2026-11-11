"""Append-only audit trail, hash-chained per organization."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AuditEvent, OrgControl, utcnow

GENESIS = "0" * 64


def ensure_org(session: Session, org_id: str) -> OrgControl:
    row = session.get(OrgControl, org_id)
    if row is None:
        row = OrgControl(org_id=org_id, paused=False)
        session.add(row)
        session.flush()
    return row


def _event_hash(prev: str, org_id: str, ts: datetime, actor: str, action: str, subject: str, data: dict) -> str:
    body = json.dumps(
        {"prev": prev, "org": org_id, "ts": ts.isoformat(), "actor": actor, "action": action, "subject": subject, "data": data},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def record(session: Session, org_id: str, actor: str, action: str, subject: str, data: dict[str, Any] | None = None) -> AuditEvent:
    """Append one event inside the caller's transaction. The org control row is locked first so concurrent writers of
    the same organization append one after another and the chain stays linear."""
    ensure_org(session, org_id)
    session.execute(select(OrgControl).where(OrgControl.org_id == org_id).with_for_update())
    last = session.execute(
        select(AuditEvent.hash).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev = last or GENESIS
    ts = utcnow()
    payload = json.loads(json.dumps(data or {}, default=str))
    ev = AuditEvent(
        org_id=org_id, ts=ts, actor=actor, action=action, subject=subject, data=payload, prev_hash=prev,
        hash=_event_hash(prev, org_id, ts, actor, action, subject, payload),
    )
    session.add(ev)
    session.flush()
    return ev


def verify_chain(session: Session, org_id: str) -> tuple[bool, int | None]:
    """(True, None) when intact; otherwise (False, id of the first broken event)."""
    prev = GENESIS
    for ev in session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)).scalars():
        if ev.prev_hash != prev or ev.hash != _event_hash(prev, ev.org_id, ev.ts, ev.actor, ev.action, ev.subject, ev.data):
            return False, ev.id
        prev = ev.hash
    return True, None
