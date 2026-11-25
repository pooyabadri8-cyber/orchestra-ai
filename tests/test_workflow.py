"""market_close_v1 end to end on DBOS (Postgres system database), with a scripted model and the real sample page."""
import json
import os
import subprocess
import sys
import time

import pytest
from dbos import DBOS, SetWorkflowID
from sqlalchemy import func, select

import orchestra.workflows.market_close as wf
from orchestra import audit
from orchestra.approvals import valid_approval
from orchestra.db import Approval, DataSnapshot, LLMCall, PackageRow, PublishJob, Run
from orchestra.runtime import set_runtime
from wf_support import GOOD, PAGE, ROOT, TYPED_NUMBER, fresh_system_db, make_runtime, postgres_available, wait_for_status

pytestmark = pytest.mark.skipif(not postgres_available(), reason="workflow tests need Postgres")

DEST = [["tg_test", "telegram"], ["bale_test", "bale"]]
DAY = "2026-11-11"
RUN = wf.run_id_for("pouyan", __import__("datetime").date(2026, 11, 11))


@pytest.fixture
def engine(tmp_path):
    url = fresh_system_db()
    DBOS(config={"name": "orchestra-test", "system_database_url": url, "log_level": "WARNING"})
    DBOS.launch()
    yield tmp_path
    DBOS.destroy(destroy_registry=False)


def launch(rtm, **kw):
    set_runtime(rtm)
    with SetWorkflowID(RUN):
        return DBOS.start_workflow(wf.market_close, "pouyan", DAY, DEST, **kw)


def package_of(sessions):
    with sessions() as s:
        run = s.get(Run, RUN)
        return run, (s.get(PackageRow, run.package_id) if run.package_id else None)


def test_happy_path_to_queued_jobs(engine):
    rtm, prov, notifier = make_runtime(engine, [GOOD])
    handle = launch(rtm)
    assert wait_for_status(rtm.sessions, RUN, {"awaiting_approval"}) == "awaiting_approval"
    run, pkg = package_of(rtm.sessions)
    assert notifier.requests == [{"org_id": "pouyan", "run_id": RUN, "package_id": pkg.package_id, "package_hash": pkg.package_hash}]
    content = json.loads(pkg.content_json)
    tg = content["destinations"][0]["delivery_plan"][0]["body"]
    assert "چهارشنبه ۲۰ آبان ۱۴۰۵" in tg and "۷٬۹۷۶٬۴۰۰" in tg and "{{" not in tg  # date and numbers bound by code

    wf.submit_decision(RUN, pkg.package_hash, "approved", "user:42")
    result = handle.get_result()
    assert result["status"] == "approved" and len(result["jobs"]) == 2
    with rtm.sessions() as s:
        jobs = s.execute(select(PublishJob)).scalars().all()
        assert {j.destination_id for j in jobs} == {"tg_test", "bale_test"} and all(j.status == "pending" for j in jobs)
        assert valid_approval(s, pkg.package_id, pkg.package_hash, "tg_test").approver == "user:42"
        assert s.execute(select(func.count(LLMCall.id))).scalar_one() == 1
        assert s.get(Run, RUN).status == "approved"
        assert audit.verify_chain(s, "pouyan") == (True, None)


def test_typed_number_gets_one_revision_with_feedback(engine):
    rtm, prov, _ = make_runtime(engine, [TYPED_NUMBER, GOOD])
    handle = launch(rtm)
    wait_for_status(rtm.sessions, RUN, {"awaiting_approval"})
    assert len(prov.calls) == 2 and "«۲»" in prov.calls[1] and "فقط توکن بنویس" in prov.calls[1]
    _, pkg = package_of(rtm.sessions)
    wf.submit_decision(RUN, pkg.package_hash, "rejected", "user:1")
    assert handle.get_result()["status"] == "rejected"


def test_two_blocked_drafts_stop_the_run(engine):
    rtm, prov, notifier = make_runtime(engine, [TYPED_NUMBER, TYPED_NUMBER])
    result = launch(rtm).get_result()
    run, pkg = package_of(rtm.sessions)
    assert result["status"] == "blocked" and pkg is None and notifier.requests == []
    assert run.findings and run.findings[0]["rule_id"] == "V-UNBOUND"


def test_decision_for_another_version_is_ignored(engine):
    rtm, _, _ = make_runtime(engine, [GOOD])
    handle = launch(rtm)
    wait_for_status(rtm.sessions, RUN, {"awaiting_approval"})
    _, pkg = package_of(rtm.sessions)
    wf.submit_decision(RUN, "0" * 64, "approved", "user:1")  # an old button
    time.sleep(1)
    with rtm.sessions() as s:
        assert s.execute(select(func.count(Approval.id))).scalar_one() == 0
    wf.submit_decision(RUN, pkg.package_hash, "rejected", "user:1", reason="لحن مناسب نیست")
    assert handle.get_result()["status"] == "rejected"
    with rtm.sessions() as s:
        assert s.execute(select(func.count(PublishJob.id))).scalar_one() == 0
        assert s.get(Run, RUN).detail["notes"][0].startswith("ignored a decision")


def test_approval_for_one_destination_only(engine):
    rtm, _, _ = make_runtime(engine, [GOOD])
    handle = launch(rtm)
    wait_for_status(rtm.sessions, RUN, {"awaiting_approval"})
    _, pkg = package_of(rtm.sessions)
    wf.submit_decision(RUN, pkg.package_hash, "approved", "user:1", destinations=["bale_test"])
    assert len(handle.get_result()["jobs"]) == 1
    with rtm.sessions() as s:
        assert [j.destination_id for j in s.execute(select(PublishJob)).scalars()] == ["bale_test"]
        assert valid_approval(s, pkg.package_id, pkg.package_hash, "tg_test") is None


def test_no_answer_expires(engine):
    rtm, _, _ = make_runtime(engine, [GOOD])
    assert launch(rtm, approval_timeout_s=2).get_result()["status"] == "expired"


def test_same_run_cannot_start_twice(engine):
    rtm, prov, _ = make_runtime(engine, [GOOD, GOOD])
    h1 = launch(rtm)
    wait_for_status(rtm.sessions, RUN, {"awaiting_approval"})
    h2 = launch(rtm)
    _, pkg = package_of(rtm.sessions)
    wf.submit_decision(RUN, pkg.package_hash, "approved", "user:1")
    assert h1.get_result() == h2.get_result() and len(prov.calls) == 1
    with rtm.sessions() as s:  # each source (TSE's overview, the market page) fetched once
        per_source = s.execute(select(DataSnapshot.source_id, func.count(DataSnapshot.id)).group_by(DataSnapshot.source_id)).all()
        assert len(per_source) == 2 and all(n == 1 for _, n in per_source)


def test_closed_market_is_skipped(engine):
    """Closed means TSE's last trading day is not today (agents/freshness.py); the market page alone cannot say it."""
    import wf_support
    rtm, prov, _ = make_runtime(engine, [GOOD])
    wf_support.TSE_DAY = "20261110"
    try:
        assert launch(rtm).get_result()["status"] == "skipped" and prov.calls == []
    finally:
        wf_support.TSE_DAY = "20261111"


def test_a_market_page_with_no_trading_is_left_out_when_tse_confirms_the_day(engine):
    rtm, prov, _ = make_runtime(engine, [{**GOOD, "paragraphs": ["شاخص کل امروز {{P3|abs_pct}} رشد کرد.",
                                                                  "شاخص کل به {{P1|int}} واحد رسید."]}],
                                page=PAGE.replace(b">18.6T<", b">0<"))
    launch(rtm)
    assert wait_for_status(rtm.sessions, RUN, {"awaiting_approval", "failed", "skipped"}) == "awaiting_approval"
    assert ": ارزش معاملات خرد |" not in prov.calls[0] and ": شاخص کل بورس |" in prov.calls[0]  # no page figures in the table
    with rtm.sessions() as s:
        assert any("صفر" in f["message"] for f in s.get(Run, RUN).findings or [])


@pytest.mark.parametrize("how", ["start", "start_cli_exit"])
def test_process_stopped_while_waiting_resumes_without_redoing_steps(tmp_path, how):
    """`start`: a child process starts the run and is killed with os._exit while waiting for approval (power loss).
    `start_cli_exit`: it leaves the way the command line does. (A normal DBOS.destroy() would cancel the waiting run
    for good; that is why the command line does not call it there.) Either way a second process recovers
    the run; the approval is delivered and the run finishes. The model was called once in total."""
    url = fresh_system_db()
    env = {**os.environ, "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'tests'}"}
    script = ROOT / "tests" / "wf_crash_child.py"
    first = subprocess.run([sys.executable, str(script), how, str(tmp_path), url], env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr[-2000:]
    assert "WAITING" in first.stdout
    second = subprocess.run([sys.executable, str(script), "resume", str(tmp_path), url], env=env, capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr[-2000:]
    report = json.loads(second.stdout.strip().splitlines()[-1])
    assert report["status"] == "approved" and report["llm_calls"] == 1 and report["snapshots"] == 1 and report["jobs"] == 2
