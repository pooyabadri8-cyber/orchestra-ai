"""Approvals are bound to the exact package hash and destination."""
import pytest
from sqlalchemy import select, update

from orchestra import audit
from orchestra.approvals import ApprovalError, decide, load_package, store_package, valid_approval
from orchestra.db import PackageRow, PublishJob
from orchestra.publishing.publisher import Publisher
from conftest import add_destinations, make_package


def _setup(sessions, assets, **kw):
    add_destinations(sessions)
    pkg = make_package(assets, **kw)
    with sessions.begin() as s:
        store_package(s, pkg)
    return pkg


def test_approval_for_the_shown_hash_only(sessions, assets):
    pkg = _setup(sessions, assets)
    with sessions.begin() as s:
        with pytest.raises(ApprovalError):
            decide(s, pkg.package_id, "f" * 64, "tg_test", "approved", "user:1")  # an old button
        with pytest.raises(ApprovalError):
            decide(s, pkg.package_id, pkg.package_hash, "instagram", "approved", "user:1")  # not in the package
        decide(s, pkg.package_id, pkg.package_hash, "tg_test", "approved", "user:1")
    with sessions() as s:
        assert valid_approval(s, pkg.package_id, pkg.package_hash, "tg_test") is not None
        assert valid_approval(s, pkg.package_id, pkg.package_hash, "bale_test") is None  # approved per destination


def test_latest_decision_wins(sessions, assets):
    pkg = _setup(sessions, assets)
    with sessions.begin() as s:
        decide(s, pkg.package_id, pkg.package_hash, "tg_test", "approved", "user:1")
        decide(s, pkg.package_id, pkg.package_hash, "tg_test", "rejected", "user:2", reason="عدد اشتباه")
    with sessions() as s:
        assert valid_approval(s, pkg.package_id, pkg.package_hash, "tg_test") is None


def test_new_version_invalidates_approvals_and_cancels_unsent_jobs(sessions, assets):
    v1 = _setup(sessions, assets)
    pub = Publisher(sessions, assets, adapter_factory=lambda d: None)
    with sessions.begin() as s:
        decide(s, v1.package_id, v1.package_hash, "tg_test", "approved", "user:1")
        job_id = pub.enqueue(s, v1.package_id, "tg_test").id
    v2 = make_package(assets, version=2, supersedes=v1.package_id, body="متن اصلاح‌شده")
    with sessions.begin() as s:
        store_package(s, v2, actor="user:1")
    with sessions() as s:
        assert valid_approval(s, v1.package_id, v1.package_hash, "tg_test") is None
        assert s.get(PublishJob, job_id).status == "cancelled"
        assert s.get(PackageRow, v1.package_id).status == "superseded"
        with pytest.raises(ApprovalError):
            decide(s, v1.package_id, v1.package_hash, "tg_test", "approved", "user:1")  # the old message cannot approve


def test_superseding_a_published_package_needs_explicit_intent(sessions, assets):
    v1 = _setup(sessions, assets)
    pub = Publisher(sessions, assets, adapter_factory=lambda d: None)
    with sessions.begin() as s:
        decide(s, v1.package_id, v1.package_hash, "tg_test", "approved", "user:1")
        job_id = pub.enqueue(s, v1.package_id, "tg_test").id
        s.execute(update(PublishJob).where(PublishJob.id == job_id).values(status="confirmed"))
    v2 = make_package(assets, version=2, supersedes=v1.package_id, body="اصلاحیه")
    with sessions.begin() as s:
        with pytest.raises(ApprovalError):
            store_package(s, v2)
    with sessions.begin() as s:
        store_package(s, v2, allow_after_publish=True)  # a deliberate correction post


def test_storage_tampering_is_detected(sessions, assets):
    pkg = _setup(sessions, assets)
    with sessions.begin() as s:
        row = s.get(PackageRow, pkg.package_id)
        row.content_json = row.content_json.replace("متن پست", "متن جعلی")
    with sessions() as s:
        with pytest.raises(ApprovalError):
            load_package(s, pkg.package_id)


def test_audit_chain_records_and_detects_edits(sessions, assets):
    pkg = _setup(sessions, assets)
    with sessions.begin() as s:
        decide(s, pkg.package_id, pkg.package_hash, "tg_test", "approved", "user:1")
    with sessions() as s:
        assert audit.verify_chain(s, "pouyan") == (True, None)
        actions = s.execute(select(audit.AuditEvent.action).order_by(audit.AuditEvent.id)).scalars().all()
        assert actions == ["package.created", "approval.approved"]
    with sessions.begin() as s:
        first = s.execute(select(audit.AuditEvent).order_by(audit.AuditEvent.id)).scalars().first()
        first.actor = "user:someone-else"
    with sessions() as s:
        ok, broken = audit.verify_chain(s, "pouyan")
        assert not ok and broken is not None
