"""Knowledge curator (deterministic, no LLM): the facts and notes of one knowledge-base topic, as an analysis part.

The knowledge file is stored like an imported file (raw bytes in the asset store, one snapshot per file version) and
each fact becomes a data point of that snapshot, so a figure in a topic post traces back to the exact file it came from.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..assets import AssetStore
from ..content.numbers import BoundPoint
from ..data.acquisition import point_id
from ..data.imports import record_import
from ..db import DataPointRow
from ..knowledge import PARSER, UNITS, Knowledge
from .specialists import Part

RULE = ("هر عدد، نام طرح یا واقعیتی که می‌نویسی فقط از جدول داده و همین یادداشت‌ها بیاید؛ چیزی را که اینجا نیست "
        "(مثل مدت ارزیابی، کارمزد یا سقف تامین مالی) ننویس و حدس نزن.")


def source_id(org_id: str) -> str:
    return f"knowledge:{org_id}"


def store_knowledge(session: Session, assets: AssetStore, kb: Knowledge, raw: bytes, now: datetime) -> str:
    """The snapshot of this knowledge file, with one data point per fact (created once per file version)."""
    sid = record_import(session, assets, source_id=source_id(kb.org_id), raw=raw, parser=PARSER, now=now,
                        label="knowledge.yaml")
    for f in kb.facts:
        pid = point_id(sid, f.metric, f.subject, None)
        if session.get(DataPointRow, pid) is None:
            session.add(DataPointRow(id=pid, snapshot_id=sid, source_id=source_id(kb.org_id), metric=f.metric,
                                     subject=f.subject, rank=None, value=f.base_value, unit=f.unit, decimals=0 if f.value.is_integer() else 1,
                                     scale=f.scale, source_text=f"{f.value:g}{f.scale}", as_of=now))
    session.flush()
    return sid


def knowledge_part(session: Session, assets: AssetStore, kb: Knowledge, raw: bytes, *, topic_id: str, now: datetime) -> Part:
    topic = kb.topic(topic_id)  # KnowledgeError for an unknown topic
    sid = store_knowledge(session, assets, kb, raw, now)
    points = []
    for f in kb.facts_for(topic_id):
        row = session.get(DataPointRow, point_id(sid, f.metric, f.subject, None))
        formats = UNITS[f.unit] + (("name",) if f.subject else ())
        points.append(BoundPoint(key="P0", point_id=row.id, metric=f.metric, label=f.label + (f": {f.subject}" if f.subject else ""),
                                 value=row.value, unit=row.unit, decimals=row.decimals, scale=row.scale, subject=f.subject,
                                 formats=formats))
    return Part(title=topic.title, points=points, facts=[*topic.notes, RULE])
