"""Database schema (SQLAlchemy 2.0). Postgres in production; the same code runs on SQLite for quick tests.

Every row that belongs to a customer carries org_id. All timestamps are stored and returned as timezone-aware UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
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
