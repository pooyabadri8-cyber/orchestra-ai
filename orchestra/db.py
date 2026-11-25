"""Database schema (SQLAlchemy 2.0). Postgres in production; the same code runs on SQLite for quick tests.

Every row that belongs to a customer carries org_id. All timestamps are stored and returned as timezone-aware UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Aware datetimes in, aware UTC datetimes out, on every backend (SQLite drops the zone)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to the database; use timezone-aware UTC")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Destination(Base):
    """A channel the platform may publish to. The token is never stored: `secret_env` names the environment variable."""

    __tablename__ = "destinations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(16))  # telegram | bale
    chat_id: Mapped[str] = mapped_column(String(128))
    secret_env: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # what this channel receives: "post" (the written post) or "carousel" (the slides as an album with the short
    # caption). Telegram and Bale can take either; one channel can be listed twice to get both.
    content: Mapped[str] = mapped_column(String(16), default="post", server_default="post")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OrgControl(Base):
    """Per-organization switches. `paused` stops every publish (the emergency stop). The row is also the lock that
    serializes the audit chain of the organization."""

    __tablename__ = "org_controls"
    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PackageRow(Base):
    __tablename__ = "packages"
    package_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    package_hash: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    supersedes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_class: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[str] = mapped_column(Text)  # canonical JSON, exactly what was hashed
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | superseded
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.package_id"))
    package_hash: Mapped[str] = mapped_column(String(64), index=True)
    destination_id: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))  # approved | rejected
    approver: Mapped[str] = mapped_column(String(128))  # "user:<id>" or "policy:<rule_id>"
    policy_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PublishJob(Base):
    """One package to one destination. States:

    pending -> sending -> confirmed
                       -> retry_wait -> sending ...      (certainly not delivered; retried with backoff)
                       -> unknown                         (may have been delivered: never resent automatically)
                       -> failed                          (permanent error, or every delivery step refused)
                       -> dead                            (retries exhausted)
    pending/retry_wait -> expired | cancelled | blocked
    """

    __tablename__ = "publish_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.package_id"))
    package_hash: Mapped[str] = mapped_column(String(64))
    destination_id: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # delivery step actually used
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_publish_jobs_idempotency"),
        Index("ix_publish_jobs_due", "status", "next_attempt_at"),
    )


class AuditEvent(Base):
    """Append-only, hash-chained per organization: each event stores the hash of the previous one."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class DataSnapshot(Base):
    """One fetch of one source. The raw response is kept in the asset store under `raw_sha256`, so every number can be
    traced back to the exact bytes it was parsed from."""

    __tablename__ = "data_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    parser: Mapped[str] = mapped_column(String(64))  # e.g. "bourse_trader@1"
    warnings: Mapped[list] = mapped_column(JSON, default=list)


class DataPointRow(Base):
    """One value from a snapshot (or derived in code from other points: `derivation`)."""

    __tablename__ = "data_points"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("data_snapshots.id"), nullable=True, index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    metric: Mapped[str] = mapped_column(String(96), index=True)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)  # a stock or an industry group
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))  # IRT (toman) | point | percent
    decimals: Mapped[int] = mapped_column(Integer)  # precision the source gave, in the source's own scale
    scale: Mapped[str] = mapped_column(String(4), default="")  # source scale: T, B, M, K or ""
    source_text: Mapped[str] = mapped_column(String(64))  # the exact string on the page, e.g. "18.6T"
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    derivation: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"formula": id, "inputs": [ids]}


class LLMCall(Base):
    """Every model call: what was asked, what came back, what it cost. Kept for audit and for cost control."""

    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    step_key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(96))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    system: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = price not configured
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Run(Base):
    """One workflow run as the business sees it. The id is the DBOS workflow id, which makes starting the same run
    twice impossible. Every step writes its progress here, so lineage reads from one table."""

    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    template: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))  # running | awaiting_approval | approved | rejected | blocked | expired | skipped | failed
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ControlState(Base):
    """Small durable values of the control bot (e.g. the next update offset), so a restart never replays a message."""

    __tablename__ = "control_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ControlPrompt(Base):
    """One approval question shown to the owner in the control chat. A decision is accepted only while the prompt is
    `pending` and its package is still the run's package, so a button pressed twice or on old content does nothing."""

    __tablename__ = "control_prompts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    package_id: Mapped[str] = mapped_column(String(64))
    package_hash: Mapped[str] = mapped_column(String(64))
    round: Mapped[int] = mapped_column(Integer, default=0)  # the run's approval round this question belongs to
    # sending -> pending -> deciding -> approved | rejected | revising | unclear | closed;  void = never fully shown. Rows are never
    # deleted, so a number that was once on a button can never come to mean another run.
    state: Mapped[str] = mapped_column(String(16), default="pending")
    messages: Mapped[list] = mapped_column(JSON, default=list)  # [[chat_id, message_id], ...] of the question itself
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ControlRequest(Base):
    """A request that arrived through the control chat (or the daily schedule), kept until its outcome is reported."""

    __tablename__ = "control_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    origin: Mapped[str] = mapped_column(String(128), unique=True)  # "update:<id>" or "schedule:<name>:<day>"
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None: tell every owner
    run_id: Mapped[str] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    reported: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ControlJobReport(Base):
    """The last state of a publish job that the owner was told about."""

    __tablename__ = "control_job_reports"
    job_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))


class EvidenceItem(Base):
    """One news item (a feed entry or a channel post) as fetched: the research agent's evidence. The feed's raw bytes
    are the snapshot; the item's figures are data points of that snapshot (metric news.figure, subject = this id)."""

    __tablename__ = "evidence_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # ev_<hash of source and the item's own id>
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    source_name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))


class ContentPlanRow(Base):
    """A content calendar proposed by the marketing agent. Approval is bound to `plan_hash`; a revision is a new row
    (the old one becomes superseded), so a button on an older version can never approve the newer one.
    proposed -> approved | rejected | superseded (a newer version, or a newer approved plan for the same days)."""

    __tablename__ = "content_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    start: Mapped[str] = mapped_column(String(10))  # ISO day
    days: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_json: Mapped[str] = mapped_column(Text)  # canonical JSON, exactly what was hashed
    plan_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)  # the owner's note that asked for this version
    messages: Mapped[list] = mapped_column(JSON, default=list)  # [[chat_id, message_id], ...] of the question
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Lead(Base):
    """A financing lead. Personal details live only here (and in the owner's chat), never in a model prompt or the
    audit chain."""

    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(16))  # telegram | bale | manual
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # where it came from (?start=...)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    first_contact_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LeadEvent(Base):
    __tablename__ = "lead_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # created | updated | stage | note | reminded
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class IntakeChat(Base):
    """Where one person is in the intake bot's questions (kept so a restart does not lose a half-filled form)."""

    __tablename__ = "intake_chats"
    key: Mapped[str] = mapped_column(String(96), primary_key=True)  # "<org>:<platform>:<user id>"
    step: Mapped[int] = mapped_column(Integer, default=-1)  # -1: not started; len(questions): confirming
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Lesson(Base):
    """A standing instruction the owner chose to keep, in their own words (team memory). offered -> active |
    dismissed; active -> retired. Only active lessons reach the writer, the editor or the planner."""

    __tablename__ = "lessons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(16))  # writing | planning
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="offered")
    origin: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "revise:12", "reject:7", "plan:3", "typed"
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _record):  # pragma: no cover - backend detail
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


# Columns added after a database may already exist. create_all() makes new tables only, so these are added here.
_ADDED_COLUMNS = (("destinations", "content", "VARCHAR(16) NOT NULL DEFAULT 'post'"),)


def _add_missing_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table, column, ddl in _ADDED_COLUMNS:
        if insp.has_table(table) and column not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
