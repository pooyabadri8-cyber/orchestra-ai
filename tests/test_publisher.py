"""The publisher's guarantees, one test each, plus a randomized fault-injection run."""
import asyncio
import random

import pytest
from sqlalchemy import update

from orchestra import audit
from orchestra.approvals import decide, store_package
from orchestra.db import PackageRow, PublishJob
from orchestra.publishing.adapters import Delivered, Permanent, Rejected, Transient, Unknown
from orchestra.publishing.publisher import PublishError, Publisher
from conftest import add_destinations, make_package


class FakeAdapter:
    """Plays back scripted outcomes and records every send attempt (shared across instances via `log`)."""

    def __init__(self, script, log, delay=0.0):
        self.script, self.log, self.delay = script, log, delay

    async def send(self, step, media):
        self.log.append((step.mode, [m.asset_id for m in media]))
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.script.pop(0) if self.script else Delivered([len(self.log)])
        if isinstance(out, BaseException):
            raise out
        return out


def setup(sessions, assets, clock, script=None, approve=("tg_test",), delay=0.0, **pkg_kw):
    add_destinations(sessions)
    pkg = make_package(assets, **pkg_kw)
    log = []
    script = list(script or [])
    pub = Publisher(sessions, assets, adapter_factory=lambda d: FakeAdapter(script, log, delay),
                    max_attempts=3, retry_delays=[30, 120], clock=clock)
    with sessions.begin() as s:
        store_package(s, pkg)
        for dest in approve:
            decide(s, pkg.package_id, pkg.package_hash, dest, "approved", "user:1")
    return pkg, pub, log, script


def enqueue(pub, sessions, pkg, dest="tg_test"):
    with sessions.begin() as s:
        return pub.enqueue(s, pkg.package_id, dest).id


def job(sessions, job_id) -> PublishJob:
    with sessions() as s:
        return s.get(PublishJob, job_id)


# ---------------------------------------------------------------- 1. approval is required


def test_no_job_without_approval(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, approve=())
    with sessions.begin() as s, pytest.raises(PublishError):
        pub.enqueue(s, pkg.package_id, "tg_test")


def test_enqueue_is_idempotent(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock)
    assert enqueue(pub, sessions, pkg) == enqueue(pub, sessions, pkg)


def test_rejection_after_enqueue_blocks_the_send(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock)
    jid = enqueue(pub, sessions, pkg)
    with sessions.begin() as s:
        decide(s, pkg.package_id, pkg.package_hash, "tg_test", "rejected", "user:2")
    assert asyncio.run(pub.process(jid)) == "blocked" and log == []


# ---------------------------------------------------------------- delivery plan


def test_album_is_sent_and_receipt_kept(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Delivered([501, 502, 503])])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "confirmed"
    j = job(sessions, jid)
    assert (j.mode, j.step_index, j.message_ids, j.attempts) == ("album", 0, [501, 502, 503], 1)
    assert log == [("album", ["s0", "s1", "s2"])]


def test_refused_album_falls_back_to_the_next_approved_step(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Rejected("method not supported"), Delivered([9])])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "confirmed"
    assert job(sessions, jid).mode == "photo" and [m for m, _ in log] == ["album", "photo"]


def test_every_step_refused_is_a_failure(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Rejected("a"), Rejected("b"), Rejected("c")])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "failed" and len(log) == 3
    assert "album: a" in job(sessions, jid).error


# ---------------------------------------------------------------- 4-5. outcomes


def test_unclear_outcome_is_never_resent(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Unknown("read timeout")])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "unknown"
    clock.advance(3600)
    assert asyncio.run(pub.run_due()) == {} and len(log) == 1


def test_adapter_exception_is_treated_as_unclear(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[RuntimeError("library bug")])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "unknown"


def test_transient_errors_back_off_then_dead_letter(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Transient("net"), Transient("429", retry_after=300), Transient("net")])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "retry_wait"
    assert (job(sessions, jid).next_attempt_at - clock.now).total_seconds() == 30
    assert asyncio.run(pub.process(jid)) == "retry_wait" and len(log) == 1  # not due yet: nothing sent
    clock.advance(30)
    assert asyncio.run(pub.process(jid)) == "retry_wait"
    assert (job(sessions, jid).next_attempt_at - clock.now).total_seconds() == 300  # the platform's wait wins
    clock.advance(300)
    assert asyncio.run(pub.process(jid)) == "dead" and len(log) == 3


def test_permanent_error_fails_without_retry(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Permanent("bot is not an admin")])
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.process(jid)) == "failed"
    clock.advance(3600)
    assert asyncio.run(pub.run_due()) == {}


def test_human_actions_on_parked_jobs(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Unknown("timeout"), Delivered([7])])
    jid = enqueue(pub, sessions, pkg)
    asyncio.run(pub.process(jid))
    assert pub.requeue(jid, "user:1") and asyncio.run(pub.process(jid)) == "confirmed"
    assert not pub.requeue(jid, "user:1")  # a confirmed job cannot be sent again
    pkg2, pub2, _, _ = setup(sessions, assets, clock, script=[Unknown("timeout")], approve=("bale_test",), body="دوم")
    j2 = enqueue(pub2, sessions, pkg2, "bale_test")
    asyncio.run(pub2.process(j2))
    assert pub2.confirm_sent(j2, "user:1", [42]) and job(sessions, j2).status == "confirmed"


# ---------------------------------------------------------------- 2, 6. claims and crashes


def test_two_workers_send_once(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, delay=0.05)
    jid = enqueue(pub, sessions, pkg)

    async def both():
        return await asyncio.gather(pub.process(jid), pub.process(jid))
    results = asyncio.run(both())
    assert sorted(results) == ["confirmed", "sending"] and len(log) == 1


class Crash(BaseException):
    """Stands for the process being killed in the middle of a send."""


def test_crash_mid_send_becomes_unknown_at_startup(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Crash()])
    jid = enqueue(pub, sessions, pkg)
    with pytest.raises(Crash):
        asyncio.run(pub.process(jid))
    assert job(sessions, jid).status == "sending"
    assert pub.recover_sending() == [jid] and job(sessions, jid).status == "unknown"
    clock.advance(3600)
    assert asyncio.run(pub.run_due()) == {} and len(log) == 1


# ---------------------------------------------------------------- 3. integrity checks right before sending


def test_changed_image_on_disk_blocks_the_send(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock)
    jid = enqueue(pub, sessions, pkg)
    path = assets._path(pkg.content.assets[1].sha256)
    path.write_bytes(path.read_bytes() + b"edited")
    assert asyncio.run(pub.process(jid)) == "blocked" and log == []


def test_changed_package_in_storage_blocks_the_send(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock)
    jid = enqueue(pub, sessions, pkg)
    with sessions.begin() as s:
        row = s.get(PackageRow, pkg.package_id)
        row.content_json = row.content_json.replace("متن پست", "متن عوض‌شده")
    assert asyncio.run(pub.process(jid)) == "blocked" and log == []


# ---------------------------------------------------------------- windows and pause


def test_publication_window(sessions, assets, clock):
    from datetime import timedelta
    pkg, pub, log, _ = setup(sessions, assets, clock, not_before=clock.now + timedelta(minutes=10),
                             expires_at=clock.now + timedelta(minutes=40))
    jid = enqueue(pub, sessions, pkg)
    assert asyncio.run(pub.run_due()) == {}
    clock.advance(600)
    assert asyncio.run(pub.process(jid)) == "confirmed"

    pkg2, pub2, log2, _ = setup(sessions, assets, clock, approve=("bale_test",), body="دیرشده",
                               expires_at=clock.now + timedelta(minutes=5))
    j2 = enqueue(pub2, sessions, pkg2, "bale_test")
    clock.advance(600)
    assert asyncio.run(pub2.process(j2)) == "expired" and log2 == []


def test_pause_holds_everything_and_resume_continues(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock)
    jid = enqueue(pub, sessions, pkg)
    pub.set_paused("pouyan", True, "user:1")
    assert asyncio.run(pub.process(jid)) == "paused" and log == []
    pub.set_paused("pouyan", False, "user:1")
    assert asyncio.run(pub.process(jid)) == "confirmed"


# ---------------------------------------------------------------- 8. audit


def test_every_transition_is_audited_and_chain_holds(sessions, assets, clock):
    pkg, pub, log, _ = setup(sessions, assets, clock, script=[Transient("net"), Delivered([1])])
    jid = enqueue(pub, sessions, pkg)
    asyncio.run(pub.process(jid))
    clock.advance(30)
    asyncio.run(pub.process(jid))
    with sessions() as s:
        from sqlalchemy import select
        acts = s.execute(select(audit.AuditEvent.action).where(audit.AuditEvent.subject == f"job:{jid}")
                         .order_by(audit.AuditEvent.id)).scalars().all()
        assert acts == ["publish.enqueued", "publish.retry", "publish.confirmed"]
        assert audit.verify_chain(s, "pouyan") == (True, None)


# ---------------------------------------------------------------- fault injection


@pytest.mark.parametrize("seed", range(4))
def test_randomized_faults_never_send_twice(sessions, assets, clock, seed):
    """50 packages per seed (200 runs per backend), random outcomes and crashes, a restart after every crash.
    Invariant: a job is delivered at most once, and only `confirmed` jobs were delivered."""
    rng = random.Random(seed)
    add_destinations(sessions)
    delivered: dict[int, int] = {}
    current = {"job": None}

    class Chaos:
        async def send(self, step, media):
            r = rng.random()
            if r < 0.10:
                raise Crash()
            if r < 0.25:
                return Unknown("timeout")
            if r < 0.40:
                return Transient("net")
            if r < 0.50 and step.mode != "text":
                return Rejected("unsupported")
            delivered[current["job"]] = delivered.get(current["job"], 0) + 1
            return Delivered([1])

    pub = Publisher(sessions, assets, adapter_factory=lambda d: Chaos(), max_attempts=3, retry_delays=[1], clock=clock)
    ids = []
    for i in range(50):
        pkg = make_package(assets, body=f"پست {seed}-{i}")
        with sessions.begin() as s:
            store_package(s, pkg)
            decide(s, pkg.package_id, pkg.package_hash, "tg_test", "approved", "user:1")
            ids.append(pub.enqueue(s, pkg.package_id, "tg_test").id)
    for _ in range(12):
        for jid in ids:
            current["job"] = jid
            try:
                asyncio.run(pub.process(jid))
            except Crash:
                pub.recover_sending()
        clock.advance(5)
    with sessions() as s:
        status = {jid: s.get(PublishJob, jid).status for jid in ids}
    assert all(n == 1 for n in delivered.values())
    assert {j for j, st in status.items() if st == "confirmed"} == set(delivered)
    assert all(st in ("confirmed", "unknown", "dead", "failed") for st in status.values())
