"""The publisher: turns an approved package into a message on a channel, at most once.

Guarantees (each one has a test in tests/test_publisher.py):
1. Nothing is sent without a valid approval for the exact package hash and destination.
2. A job is claimed in the database before anything is sent; two workers can never send the same job.
3. Right before sending, the stored package is re-hashed and every image file is re-hashed; any mismatch blocks it.
4. A send whose outcome is unclear becomes `unknown` and is never resent automatically.
5. Transient failures are retried with backoff; after `max_attempts` the job goes to `dead`.
6. A job still `sending` at start-up (crash mid-send) becomes `unknown`.
7. The organization's pause switch stops every send; nothing is lost.
8. Every state change is written to the audit chain.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .. import audit
from ..approvals import ApprovalError, load_package, valid_approval
from ..assets import AssetIntegrityError, AssetStore
from ..db import Destination, OrgControl, PublishJob, utcnow
from ..package import DestinationPlan, PackageContent
from .adapters import Delivered, DestinationAdapter, Media, Permanent, Rejected, Transient, Unknown

AdapterFactory = Callable[[Destination], DestinationAdapter]


class PublishError(Exception):
    pass


def idempotency_key(org_id: str, destination_id: str, package_hash: str) -> str:
    return hashlib.sha256(f"{org_id}|{destination_id}|{package_hash}".encode()).hexdigest()


@dataclass
class Prepared:
    job_id: int
    org_id: str
    attempts: int
    plan: DestinationPlan
    content: PackageContent
    destination: Destination


class Publisher:
    def __init__(
        self, sessions: sessionmaker, assets: AssetStore, adapter_factory: AdapterFactory,
        max_attempts: int = 4, retry_delays: list[int] | None = None, clock: Callable[[], datetime] = utcnow,
    ):
        self.sessions = sessions
        self.assets = assets
        self.adapter_factory = adapter_factory
        self.max_attempts = max(1, max_attempts)
        self.retry_delays = retry_delays or [30, 120, 600]
        self.clock = clock

    # ------------------------------------------------------------------ creating jobs

    def enqueue(self, session: Session, package_id: str, destination_id: str, actor: str = "system") -> PublishJob:
        """Create the publish job for an approved package. Calling it again returns the same job."""
        pkg = load_package(session, package_id)
        plan = pkg.content.destination(destination_id)
        appr = valid_approval(session, package_id, pkg.package_hash, destination_id)
        if appr is None:
            raise PublishError("no valid approval for this package and destination")
        dest = session.get(Destination, destination_id)
        if dest is None or dest.org_id != pkg.content.org_id:
            raise PublishError(f"destination {destination_id} does not belong to {pkg.content.org_id}")
        key = idempotency_key(pkg.content.org_id, destination_id, pkg.package_hash)
        existing = session.execute(select(PublishJob).where(PublishJob.idempotency_key == key)).scalar_one_or_none()
        if existing is not None:
            return existing
        job = PublishJob(
            org_id=pkg.content.org_id, package_id=package_id, package_hash=pkg.package_hash, destination_id=destination_id,
            approval_id=appr.id, idempotency_key=key, status="pending",
            next_attempt_at=max(self.clock(), plan.not_before) if plan.not_before else self.clock(),
        )
        try:
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:  # another caller created it at the same moment
            return session.execute(select(PublishJob).where(PublishJob.idempotency_key == key)).scalar_one()
        audit.record(session, job.org_id, actor, "publish.enqueued", f"job:{job.id}",
                     {"package": package_id, "destination": destination_id, "approval_id": appr.id})
        return job

    # ------------------------------------------------------------------ controls

    def set_paused(self, org_id: str, paused: bool, actor: str) -> None:
        with self.sessions.begin() as s:
            row = audit.ensure_org(s, org_id)
            row.paused = paused
            audit.record(s, org_id, actor, "org.paused" if paused else "org.resumed", f"org:{org_id}")

    def recover_sending(self, actor: str = "system:startup") -> list[int]:
        """Run once at start-up. A job left `sending` means the process stopped mid-send: it may be on the channel."""
        with self.sessions.begin() as s:
            jobs = s.execute(select(PublishJob).where(PublishJob.status == "sending")).scalars().all()
            for job in jobs:
                job.status = "unknown"
                job.error = "process stopped during the send"
                audit.record(s, job.org_id, actor, "publish.unknown", f"job:{job.id}", {"reason": job.error})
            return [j.id for j in jobs]

    def confirm_sent(self, job_id: int, actor: str, message_ids: list[int] | None = None) -> bool:
        """A human checked the channel: the `unknown` post is there."""
        with self.sessions.begin() as s:
            n = s.execute(update(PublishJob).where(PublishJob.id == job_id, PublishJob.status == "unknown")
                          .values(status="confirmed", message_ids=message_ids, error=None)).rowcount
            if n:
                job = s.get(PublishJob, job_id)
                audit.record(s, job.org_id, actor, "publish.confirmed_by_human", f"job:{job_id}", {"message_ids": message_ids})
            return n == 1

    def requeue(self, job_id: int, actor: str) -> bool:
        """A human decided to send again (an `unknown` post is not on the channel, or the cause of a failure is fixed)."""
        with self.sessions.begin() as s:
            n = s.execute(update(PublishJob).where(PublishJob.id == job_id, PublishJob.status.in_(("unknown", "dead", "failed")))
                          .values(status="pending", attempts=0, next_attempt_at=self.clock(), error=None)).rowcount
            if n:
                job = s.get(PublishJob, job_id)
                audit.record(s, job.org_id, actor, "publish.requeued", f"job:{job_id}")
            return n == 1

    # ------------------------------------------------------------------ running jobs

    async def run_due(self, limit: int = 20) -> dict[int, str]:
        with self.sessions() as s:
            ids = s.execute(
                select(PublishJob.id).where(PublishJob.status.in_(("pending", "retry_wait")), PublishJob.next_attempt_at <= self.clock())
                .order_by(PublishJob.next_attempt_at).limit(limit)
            ).scalars().all()
        return {i: await self.process(i) for i in ids}

    async def process(self, job_id: int) -> str:
        prepared = self._prepare_and_claim(job_id)
        if isinstance(prepared, str):
            return prepared
        try:
            media = self._load_media(prepared)
        except (AssetIntegrityError, KeyError) as e:
            return self._finish(prepared, "blocked", error=f"asset check failed: {e}", action="publish.blocked")

        adapter = self.adapter_factory(prepared.destination)
        refused: list[str] = []
        try:
            for index, step in enumerate(prepared.plan.delivery_plan):
                try:
                    outcome = await adapter.send(step, [media[a] for a in step.asset_ids])
                except Exception as e:  # a bug or an unexpected library error mid-send: it may have gone out
                    outcome = Unknown(f"adapter error: {type(e).__name__}: {e}")
                if isinstance(outcome, Delivered):
                    return self._finish(prepared, "confirmed", mode=step.mode, step_index=index,
                                        message_ids=outcome.message_ids, action="publish.confirmed")
                if isinstance(outcome, Rejected):
                    refused.append(f"{step.mode}: {outcome.reason}")
                    continue
                break
            else:
                return self._finish(prepared, "failed", error=" | ".join(refused), action="publish.failed")
        finally:
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()

        if isinstance(outcome, Unknown):
            return self._finish(prepared, "unknown", error=outcome.reason, action="publish.unknown")
        if isinstance(outcome, Permanent):
            return self._finish(prepared, "failed", error=outcome.reason, action="publish.failed")
        assert isinstance(outcome, Transient)
        if prepared.attempts >= self.max_attempts:
            return self._finish(prepared, "dead", error=outcome.reason, action="publish.dead")
        delay = max(self._retry_delay(prepared.attempts), outcome.retry_after)
        return self._finish(prepared, "retry_wait", error=outcome.reason, action="publish.retry",
                            next_attempt_at=self.clock() + timedelta(seconds=delay))

    def _retry_delay(self, attempt: int) -> float:
        d = self.retry_delays
        return float(d[min(max(attempt, 1), len(d)) - 1])

    def _prepare_and_claim(self, job_id: int) -> Prepared | str:
        """All checks, then the claim, in one transaction. Returns the job's state when it is not sent now."""
        now = self.clock()
        with self.sessions.begin() as s:
            job = s.get(PublishJob, job_id)
            if job is None:
                return "missing"
            if job.status not in ("pending", "retry_wait") or job.next_attempt_at > now:
                return job.status
            control = s.get(OrgControl, job.org_id)
            if control is not None and control.paused:
                return "paused"

            def stop(status: str, why: str, action: str) -> str:
                job.status, job.error = status, why
                audit.record(s, job.org_id, "system:publisher", action, f"job:{job.id}", {"reason": why})
                return status

            try:
                pkg = load_package(s, job.package_id)
            except ApprovalError as e:
                return stop("blocked", str(e), "publish.blocked")
            if pkg.package_hash != job.package_hash:
                return stop("blocked", "job and package hashes differ", "publish.blocked")
            if valid_approval(s, job.package_id, job.package_hash, job.destination_id) is None:
                return stop("blocked", "approval missing or no longer valid", "publish.blocked")
            plan = pkg.content.destination(job.destination_id)
            if plan.expires_at is not None and now >= plan.expires_at:
                return stop("expired", "publication window has closed", "publish.expired")
            if plan.not_before is not None and now < plan.not_before:
                job.next_attempt_at = plan.not_before
                return "waiting"
            dest = s.get(Destination, job.destination_id)
            if dest is None or not dest.enabled or dest.org_id != job.org_id:
                return stop("failed", "destination missing, disabled or not owned by this organization", "publish.failed")

            attempt = job.attempts + 1  # read before the claim: the ORM syncs the UPDATE into `job`
            claimed = s.execute(
                update(PublishJob)
                .where(PublishJob.id == job_id, PublishJob.status.in_(("pending", "retry_wait")))
                .values(status="sending", attempts=PublishJob.attempts + 1, last_attempt_at=now)
            ).rowcount
            if claimed != 1:
                return "taken"
            s.expunge(dest)
            return Prepared(job_id=job.id, org_id=job.org_id, attempts=attempt, plan=plan,
                            content=pkg.content, destination=dest)

    def _load_media(self, p: Prepared) -> dict[str, Media]:
        needed = {a for step in p.plan.delivery_plan for a in step.asset_ids}
        out = {}
        for asset_id in needed:
            ref = p.content.asset(asset_id)
            data = self.assets.get(ref.sha256)
            if len(data) != ref.bytes:
                raise AssetIntegrityError(f"asset {asset_id} size differs from the package")
            out[asset_id] = Media(asset_id=asset_id, data=data, mime=ref.mime)
        return out

    def _finish(self, p: Prepared, status: str, action: str, error: str | None = None, mode: str | None = None,
                step_index: int | None = None, message_ids: list[int] | None = None,
                next_attempt_at: datetime | None = None) -> str:
        values: dict = {"status": status, "error": error[:1000] if error else None}
        if mode is not None:
            values.update(mode=mode, step_index=step_index, message_ids=message_ids)
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        with self.sessions.begin() as s:
            n = s.execute(update(PublishJob).where(PublishJob.id == p.job_id, PublishJob.status == "sending").values(**values)).rowcount
            if n != 1:  # someone (recover_sending) already moved it: keep their state, record ours
                audit.record(s, p.org_id, "system:publisher", "publish.late_result", f"job:{p.job_id}", {"would_be": status})
                return "conflict"
            audit.record(s, p.org_id, "system:publisher", action, f"job:{p.job_id}",
                         {"attempt": p.attempts, "mode": mode, "message_ids": message_ids, "error": values["error"]})
        return status
