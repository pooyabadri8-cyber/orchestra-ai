"""Storing packages and recording approval decisions bound to `package_hash`.

Rules
- A decision is for one package hash and one destination.
- A new version of a package supersedes the old one: the old approvals are invalidated and its unsent jobs cancelled.
- Only an `approved`, not invalidated decision for an `active` package lets a publish job be created or sent.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import audit
from .db import Approval, PackageRow, PublishJob, utcnow
from .package import PackageContent, PublicationPackage, canonical_json


class ApprovalError(Exception):
    pass


def store_package(session: Session, pkg: PublicationPackage, actor: str = "system", allow_after_publish: bool = False) -> PackageRow:
    """Save a package. Saving the same content twice returns the existing row (the id is derived from the hash).
    A package with `supersedes` set replaces that one. Replacing a package that is already on a channel (or may be)
    would put a second post there, so it is refused unless the caller says that is intended (a correction post)."""
    if not pkg.verify():
        raise ApprovalError("package hash does not match its content")
    existing = session.get(PackageRow, pkg.package_id)
    if existing is not None:
        return existing
    c = pkg.content
    if c.supersedes:
        old = session.get(PackageRow, c.supersedes)
        if old is None:
            raise ApprovalError(f"superseded package {c.supersedes} not found")
        if old.org_id != c.org_id:
            raise ApprovalError("a package cannot supersede another organization's package")
        if not allow_after_publish:
            out = session.execute(
                select(PublishJob.id).where(PublishJob.package_id == old.package_id,
                                            PublishJob.status.in_(("sending", "confirmed", "unknown")))
            ).first()
            if out is not None:
                raise ApprovalError("the previous version is already on a channel (or may be); a new version would be a second post")
        _retire(session, old, reason=f"superseded by {pkg.package_id}", actor=actor)
    row = PackageRow(
        package_id=pkg.package_id, org_id=c.org_id, package_hash=pkg.package_hash, run_id=c.run_id, version=c.version,
        supersedes=c.supersedes, content_class=c.content_class, content_json=canonical_json(c), status="active",
    )
    session.add(row)
    session.flush()
    audit.record(session, c.org_id, actor, "package.created", pkg.package_id,
                 {"hash": pkg.package_hash, "version": c.version, "supersedes": c.supersedes})
    return row


def _retire(session: Session, old: PackageRow, reason: str, actor: str) -> None:
    now = utcnow()
    old.status = "superseded"
    n_appr = session.execute(
        update(Approval).where(Approval.package_id == old.package_id, Approval.invalidated_at.is_(None)).values(invalidated_at=now)
    ).rowcount
    n_jobs = session.execute(
        update(PublishJob)
        .where(PublishJob.package_id == old.package_id, PublishJob.status.in_(("pending", "retry_wait")))
        .values(status="cancelled", error=reason, updated_at=now)
    ).rowcount
    audit.record(session, old.org_id, actor, "package.superseded", old.package_id,
                 {"reason": reason, "approvals_invalidated": n_appr, "jobs_cancelled": n_jobs})


def load_package(session: Session, package_id: str) -> PublicationPackage:
    """Rebuild the package from storage and check that the stored content still produces the stored hash."""
    row = session.get(PackageRow, package_id)
    if row is None:
        raise ApprovalError(f"package {package_id} not found")
    pkg = PublicationPackage.build(PackageContent.model_validate_json(row.content_json))
    if pkg.package_hash != row.package_hash or pkg.package_id != row.package_id:
        raise ApprovalError(f"package {package_id} was changed in storage")
    return pkg


def decide(
    session: Session, package_id: str, package_hash: str, destination_id: str, decision: str, approver: str,
    policy_rule: str | None = None, reason: str | None = None,
) -> Approval:
    """Record a decision. `package_hash` is what the approver saw (it travels in the approval button); a stale hash is
    refused, so an old message can never approve a newer version."""
    if decision not in ("approved", "rejected"):
        raise ApprovalError("decision must be approved or rejected")
    row = session.get(PackageRow, package_id)
    if row is None:
        raise ApprovalError(f"package {package_id} not found")
    if row.status != "active":
        raise ApprovalError("this version is no longer valid; a newer version exists")
    if row.package_hash != package_hash:
        raise ApprovalError("the approval does not match the current content of this package")
    content = load_package(session, package_id).content
    if destination_id not in {d.destination_id for d in content.destinations}:
        raise ApprovalError(f"destination {destination_id} is not part of this package")
    appr = Approval(
        org_id=row.org_id, package_id=package_id, package_hash=package_hash, destination_id=destination_id,
        decision=decision, approver=approver, policy_rule=policy_rule, reason=reason,
    )
    session.add(appr)
    session.flush()
    audit.record(session, row.org_id, approver, f"approval.{decision}", package_id,
                 {"hash": package_hash, "destination": destination_id, "rule": policy_rule, "approval_id": appr.id})
    return appr


def valid_approval(session: Session, package_id: str, package_hash: str, destination_id: str) -> Approval | None:
    """The newest decision for this exact content and destination, if it is a valid approval."""
    pkg = session.get(PackageRow, package_id)
    if pkg is None or pkg.status != "active" or pkg.package_hash != package_hash:
        return None
    latest = session.execute(
        select(Approval)
        .where(Approval.package_id == package_id, Approval.package_hash == package_hash,
               Approval.destination_id == destination_id, Approval.invalidated_at.is_(None))
        .order_by(Approval.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest if latest is not None and latest.decision == "approved" else None
