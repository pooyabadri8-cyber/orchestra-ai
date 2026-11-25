"""CRM settings (orgs/<org>/crm.yaml) and the lead records: create, update, move through stages, notes, reminders.

Personal details (name, phone, ...) stay in the leads table and the owner's chat. The audit chain records only what
happened to which lead number (created, stage), never the details, and no model ever sees them.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import Lead, LeadEvent

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
ANSWER_MAX = 300


class CrmError(Exception):
    pass


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    label: str
    ask: str
    kind: str = "text"  # text | phone

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ("text", "phone"):
            raise ValueError("kind باید text یا phone باشد")
        return v


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    platform: str = "bale"
    token_env: str = "ORCHESTRA_INTAKE_BOT_TOKEN"
    welcome: str
    consent: str
    done: str
    questions: tuple[Question, ...] = Field(min_length=1, max_length=12)

    @field_validator("platform")
    @classmethod
    def _platform(cls, v):
        if v not in ("telegram", "bale"):
            raise ValueError("platform باید telegram یا bale باشد")
        return v


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name_fa: str
    final: bool = False


class CrmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str
    intake: Intake
    stages: tuple[Stage, ...] = Field(min_length=2)
    follow_up_hours: int = Field(24, ge=1, le=24 * 14)
    max_leads_per_day: int = Field(3, ge=1, le=50)

    @model_validator(mode="after")
    def _check(self):
        ids = [s.id for s in self.stages]
        if len(ids) != len(set(ids)) or self.stages[0].final or not any(s.final for s in self.stages):
            raise ValueError("stages: شناسه‌ها یکتا، اولی باز و دست‌کم یکی پایانی (final) باشد")
        q = [x.id for x in self.intake.questions]
        if len(q) != len(set(q)):
            raise ValueError("شناسهٔ سؤال تکراری")
        return self

    def stage(self, stage_id: str) -> Stage | None:
        return next((s for s in self.stages if s.id == stage_id), None)

    @property
    def first(self) -> str:
        return self.stages[0].id

    def stage_fa(self, stage_id: str) -> str:
        s = self.stage(stage_id)
        return s.name_fa if s else stage_id


def load_crm(org_id: str, root: str | Path = "orgs") -> CrmConfig | None:
    path = Path(root) / org_id / "crm.yaml"
    if not path.is_file():
        return None
    try:
        cfg = CrmConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (yaml.YAMLError, ValidationError) as e:
        raise CrmError(f"{path}: {str(e)[:400]}") from e
    if cfg.org_id != org_id:
        raise CrmError(f"{path}: org_id باید {org_id} باشد")
    return cfg


def normalize_phone(text: str) -> str | None:
    """'۰۹۱۲ ۳۴۵ ۶۷۸۹', '+98 912 345 6789', '021-91005777' -> '09123456789' / '02191005777'; None if not a phone."""
    t = re.sub(r"[\s\-()./]", "", text.translate(FA_DIGITS))
    if t.startswith("+98"):
        t = "0" + t[3:]
    elif t.startswith("0098"):
        t = "0" + t[4:]
    elif t.startswith("98") and len(t) == 12:
        t = "0" + t[2:]
    return t if re.fullmatch(r"0\d{10}", t) else None


def clean_answer(q: Question, text: str) -> tuple[str | None, str]:
    """(value, problem). Answers are data: kept as plain text, never formatted or executed."""
    text = " ".join((text or "").split())
    if not text:
        return None, "پاسخ خالی است؛ دوباره بنویسید."
    if len(text) > ANSWER_MAX:
        return None, f"پاسخ بیش از {ANSWER_MAX} نویسه است؛ کوتاه‌تر بنویسید."
    if q.kind == "phone":
        phone = normalize_phone(text)
        return (phone, "") if phone else (None, "شماره درست به نظر نمی‌رسد؛ مثل ۰۹۱۲۳۴۵۶۷۸۹ یا ۰۲۱۹۱۰۰۵۷۷۷ بنویسید.")
    return text, ""


# ------------------------------------------------------------------ records


def _event(session: Session, lead: Lead, actor: str, kind: str, data: dict | None = None, now: datetime | None = None) -> None:
    session.add(LeadEvent(lead_id=lead.id, actor=actor, kind=kind, data=data or {}, **({"ts": now} if now else {})))


def leads_today(session: Session, org_id: str, channel: str, user_id: str, now: datetime) -> int:
    since = now - timedelta(hours=24)
    rows = session.execute(select(LeadEvent).join(Lead, Lead.id == LeadEvent.lead_id)
                           .where(Lead.org_id == org_id, Lead.channel == channel, Lead.user_id == user_id,
                                  LeadEvent.kind.in_(("created", "updated")), LeadEvent.ts >= since)).scalars().all()
    return len(rows)


def submit_lead(session: Session, cfg: CrmConfig, *, channel: str, user_id: str | None, username: str | None, ref: str | None,
                fields: dict, now: datetime, actor: str) -> tuple[Lead, bool]:
    """A person's open lead is updated with the new answers instead of a second one (True = a new lead)."""
    finals = [s.id for s in cfg.stages if s.final]
    lead = None
    if user_id:
        lead = session.execute(select(Lead).where(Lead.org_id == cfg.org_id, Lead.channel == channel, Lead.user_id == user_id,
                                                  Lead.stage.notin_(finals)).order_by(Lead.id.desc()).limit(1)).scalar_one_or_none()
    if lead is not None:
        lead.fields, lead.updated_at, lead.username = dict(fields), now, username
        lead.ref = lead.ref or ref
        _event(session, lead, actor, "updated", {"fields": sorted(fields)}, now)
        session.flush()
        return lead, False
    lead = Lead(org_id=cfg.org_id, channel=channel, user_id=user_id, username=username, ref=ref, fields=dict(fields),
                stage=cfg.first, created_at=now, updated_at=now)
    session.add(lead)
    session.flush()
    _event(session, lead, actor, "created", {"ref": ref}, now)
    audit.record(session, cfg.org_id, actor, "lead.created", f"lead:{lead.id}", {"channel": channel, "ref": ref})
    return lead, True


def set_stage(session: Session, cfg: CrmConfig, lead_id: int, stage: str, actor: str, now: datetime) -> Lead:
    st = cfg.stage(stage)
    if st is None:
        raise CrmError(f"مرحلهٔ «{stage}» تعریف نشده است؛ مرحله‌ها: {', '.join(s.id for s in cfg.stages)}")
    lead = session.get(Lead, lead_id)
    if lead is None or lead.org_id != cfg.org_id:
        raise CrmError(f"سرنخ {lead_id} پیدا نشد")
    if lead.stage == stage:
        return lead
    before = lead.stage
    lead.stage, lead.updated_at = stage, now
    if before == cfg.first and lead.first_contact_at is None:
        lead.first_contact_at = now
    lead.closed_at = now if st.final else None
    _event(session, lead, actor, "stage", {"from": before, "to": stage}, now)
    audit.record(session, cfg.org_id, actor, "lead.stage", f"lead:{lead.id}", {"from": before, "to": stage})
    session.flush()
    return lead


def add_note(session: Session, cfg: CrmConfig, lead_id: int, note: str, actor: str, now: datetime) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None or lead.org_id != cfg.org_id:
        raise CrmError(f"سرنخ {lead_id} پیدا نشد")
    note = " ".join(note.split())[:1000]
    if not note:
        raise CrmError("یادداشت خالی است")
    _event(session, lead, actor, "note", {"text": note}, now)
    lead.updated_at = now
    session.flush()
    return lead


def overdue(session: Session, cfg: CrmConfig, now: datetime) -> list[Lead]:
    """Leads still in the first stage after follow_up_hours."""
    limit = now - timedelta(hours=cfg.follow_up_hours)
    return list(session.execute(select(Lead).where(Lead.org_id == cfg.org_id, Lead.stage == cfg.first, Lead.created_at <= limit)
                                .order_by(Lead.id)).scalars())


def open_leads(session: Session, cfg: CrmConfig) -> list[Lead]:
    finals = [s.id for s in cfg.stages if s.final]
    return list(session.execute(select(Lead).where(Lead.org_id == cfg.org_id, Lead.stage.notin_(finals))
                                .order_by(Lead.id)).scalars())


def events(session: Session, lead_id: int) -> list[LeadEvent]:
    return list(session.execute(select(LeadEvent).where(LeadEvent.lead_id == lead_id).order_by(LeadEvent.id)).scalars())
