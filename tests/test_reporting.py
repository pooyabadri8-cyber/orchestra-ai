"""The reporting agent: every figure counted from the system's own records, and delivered only to the owner."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import Clock
from orchestra.agents.marketing import ContentPlan, PlanItem, store_plan, decide_plan
from orchestra.agents.reporting import build_report, load_weekly, observations, period_bounds, report_text
from orchestra.control.service import ControlService
from orchestra.db import (
    Approval, ContentPlanRow, ControlRequest, Destination, LLMCall, OrgControl, PackageRow, PublishJob, Run,
)
from test_control import OWNER, FakeApi, FakeEngine

NOW = datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)  # Friday 20:30 in Tehran
END = date(2026, 11, 27)
T0 = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)  # Tuesday 09:30 in Tehran


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def add_run(s, rid, *, status, template="market_report", origin=None, created=None, findings=(), versions=0, detail=None):
    s.add(Run(id=rid, org_id="pouyan", template=template, status=status, findings=list(findings), detail=detail or {},
              created_at=created or at(0)))
    if origin:
        s.add(ControlRequest(org_id="pouyan", origin=origin, run_id=rid, text="x", reported=True))
    pkgs = []
    for v in range(1, versions + 1):
        pid = f"pkg_{rid}_{v}"
        s.add(PackageRow(package_id=pid, org_id="pouyan", package_hash=(pid * 8)[:64].ljust(64, "0"), run_id=rid, version=v,
                         content_class=template, content_json="{}", status="active" if v == versions else "superseded",
                         created_at=(created or at(0)) + timedelta(minutes=20 * v)))
        pkgs.append(pid)
    s.flush()
    if pkgs:
        s.get(Run, rid).package_id = pkgs[-1]
    return pkgs


def decide(s, pid, decision, minutes_after, reason=None):
    pkg = s.get(PackageRow, pid)
    a = Approval(org_id="pouyan", package_id=pid, package_hash=pkg.package_hash, destination_id="tg_test", decision=decision,
                 approver="user:1", reason=reason, decided_at=pkg.created_at + timedelta(minutes=minutes_after))
    s.add(a)
    s.flush()
    return a


def job(s, pid, approval, dest, status, created):
    s.add(PublishJob(org_id="pouyan", package_id=pid, package_hash=s.get(PackageRow, pid).package_hash, destination_id=dest,
                     approval_id=approval.id, idempotency_key=f"{pid}{dest}", status=status, created_at=created))


@pytest.fixture
def week(sessions):
    with sessions.begin() as s:
        s.add(Destination(id="tg_test", org_id="pouyan", platform="telegram", chat_id="@t", secret_env="X"))
        a = add_run(s, "r_asked", status="approved", origin="update:5", versions=1, created=at(0))
        ap = decide(s, a[0], "approved", 30)
        job(s, a[0], ap, "tg_test", "confirmed", at(1))
        job(s, a[0], ap, "bale_test", "unknown", at(1))
        b = add_run(s, "r_plan", status="rejected", template="topic_post", origin="plan:1:1", versions=2, created=at(24),
                    findings=[{"severity": "warn", "rule_id": "E-EDITOR", "message": "m"}])
        decide(s, b[-1], "rejected", 90, reason="لحن تبلیغی‌تر از حد")
        add_run(s, "r_sched", status="blocked", origin="schedule:market_close:2026-11-25", created=at(28),
                findings=[{"severity": "block", "rule_id": "V-UNBOUND", "message": "عدد"},
                          {"severity": "warn", "rule_id": "DATA-FX", "message": "tgju"}])
        add_run(s, "r_wait", status="awaiting_approval", versions=1, created=at(50))
        add_run(s, "r_old", status="approved", versions=1, created=at(-24 * 10))  # outside the period
        for i, ok in enumerate((True, True, True, False)):
            s.add(LLMCall(org_id="pouyan", run_id="r_asked", step_key=("write.1", "review.editor.1", "manager.plan.0", "write.2")[i],
                          provider="p", model="m", prompt_sha256="0" * 64, system="", prompt="", input_tokens=100,
                          output_tokens=50, cost_usd=0.01 if ok else None, ok=ok, created_at=at(i)))
        items = (PlanItem(day=date(2026, 11, 25), time="18:00", pillar="proof", topic="crowd_track_record", angle="الف"),
                 PlanItem(day=date(2026, 11, 26), time="10:00", pillar="concerns", topic="crowd_concerns", angle="ب"),
                 PlanItem(day=date(2026, 11, 29), time="10:00", pillar="path", topic="crowd_path", angle="ج"))
        row = store_plan(s, ContentPlan(org_id="pouyan", start=date(2026, 11, 22), days=14, items=items), actor="t")
        decide_plan(s, row.id, row.plan_hash, "approved", "u", date(2026, 11, 21))
    return sessions


def test_period_is_in_tehran_days():
    start, end, lo, hi = period_bounds(END, 7)
    assert (start, end) == (date(2026, 11, 21), END)
    assert lo.isoformat() == "2026-11-21T00:00:00+03:30" and hi - lo == timedelta(days=7)
    assert period_bounds(END, 1000)[0] == END - timedelta(days=89)


def test_counts_come_from_the_records(week):
    with week() as s:
        rep = build_report(s, "pouyan", end_day=END, days=7, now=NOW)
    assert rep.runs == 4 and rep.by_origin == {"update": 1, "plan": 1, "schedule": 1, "cli": 1}
    assert rep.ready == 3 and rep.approved == 1 and rep.rejected == 1 and rep.waiting == 1
    assert rep.first_version_approved == 1 and rep.revisions == 1 and rep.reject_reasons == ["لحن تبلیغی‌تر از حد"]
    assert rep.stopped and rep.stopped[0][0] == "blocked" and "عدد" in rep.stopped[0][1]
    assert rep.blocking_rules == {"V-UNBOUND": 1} and rep.warning_rules == {"E-EDITOR": 1} and rep.data_notes == {"DATA-FX": 1}
    assert rep.jobs == {"confirmed": 1, "unknown": 1} and rep.unknown_jobs == [2]
    assert rep.median_ready_minutes == 20 and rep.median_decision_minutes == 60  # 30 and 90 minutes
    assert (rep.llm_calls, rep.llm_failed, rep.llm_tokens) == (4, 1, 600) and rep.llm_cost == pytest.approx(0.03)
    assert rep.llm_by_role == {"write": 2, "review": 1, "manager": 1}
    p = rep.plans
    assert (p.planned, p.started, p.ready, p.approved, p.published, p.missed, p.upcoming) == (2, 1, 1, 0, 0, 1, 0)


def test_the_text_and_what_needs_attention(week):
    with week() as s:
        rep = build_report(s, "pouyan", end_day=END, days=7, now=NOW)
    text = report_text(rep)
    assert text.splitlines()[0].startswith("📊 گزارش عملکرد تیم — ") and text.splitlines()[0].endswith("تا جمعه ۶ آذر ۱۴۰۵")
    assert "۴ درخواست" in text and "منتشر شد ۱ از ۲ ارسال" in text and "لحن تبلیغی‌تر از حد" in text
    assert "عدد بی‌منبع در متن ۱" in text and "۰٫۰۳ دلار" in text and "جامانده ۱" in text
    obs = "\n".join(observations(rep))
    assert "/sent 2" in obs and "ساخته نشد چون ربات" in obs and "منتظر تصمیم شماست" in obs
    assert "ارز و طلا (tgju)" in obs and "اثبات و کارنامه ۰/۱" in text and "نویسنده ۲" in text


def test_an_empty_period():
    from orchestra.agents.reporting import Report
    assert "کاری انجام نشده" in report_text(Report(org_id="pouyan", start=END, end=END))


def test_weekly_settings(tmp_path):
    assert load_weekly("pouyan").day == "fri"
    assert load_weekly("nobody", tmp_path) is None
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "reporting.yaml").write_text('weekly: {day: someday, time: "20:00"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_weekly("x", tmp_path)


def test_report_in_the_control_chat_and_once_a_week(week, assets):
    clock = Clock(NOW - timedelta(hours=1))  # Friday 19:30: before the weekly report
    api = FakeApi()
    engine = FakeEngine(week, assets, clock)
    engine.resumable = False  # the waiting run here is a bare record, not content to show
    svc = ControlService(org_id="pouyan", api=api, engine=engine, sessions=week, assets=assets, owners=(OWNER,), clock=clock)
    svc.tick()
    assert not any("گزارش عملکرد" in t for t in api.texts())
    clock.now = NOW
    svc.tick()
    svc.tick()
    assert sum("📊 گزارش عملکرد" in t for t in api.texts()) == 1
    api.say("/report امروز")
    svc.tick()
    assert "جمعه ۶ آذر ۱۴۰۵" in api.texts()[-1] and "تا" not in api.texts()[-1].splitlines()[0]
    api.say("/report ۳۰")
    svc.tick()
    assert "۴ درخواست" in "\n".join(api.texts()[-3:])
