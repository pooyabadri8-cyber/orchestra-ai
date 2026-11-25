"""Reporting agent (deterministic, no LLM): what the team did in a period, counted from the system's own records.

Every figure is counted in code from the database (runs, packages, approvals, publish jobs, model calls, content
plans); nothing is estimated and no model writes or interprets it. The observations at the end are fixed rules over
the same counts. The report is for the owner, in the control chat or on the command line; it is never published.

Audience figures (views, reactions) are not here: the Bot API does not give them for channel posts.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..content.calendar import FA, long_date
from ..db import Approval, ContentPlanRow, ControlRequest, LLMCall, OrgControl, PackageRow, PublishJob, Run

FORMULA_VERSION = "reporting@1"
TEHRAN = ZoneInfo("Asia/Tehran")
MAX_DAYS = 90

ORIGIN_FA = {"update": "درخواست شما", "schedule": "زمان‌بندی روزانه", "plan": "برنامهٔ محتوا", "cli": "خط فرمان"}
TEMPLATE_FA = {"market_report": "گزارش بازار", "topic_post": "پست موضوعی", "stock_report": "گزارش نماد",
               "market_close_v1": "گزارش پایان بازار (ساده)", "request": "بدون برنامه"}
RULE_FA = {
    "V-UNBOUND": "عدد بی‌منبع در متن", "V-BINDING": "ارجاع نادرست به داده", "R-SHAPE": "ساختار ناقص", "R-LENGTH": "متن بلند",
    "R-GUARD": "عبارت ممنوع", "R-EXCLUDED": "موضوع ممنوع (صندوق‌ها)", "R-LINK": "لینک در متن", "R-TYPO": "غلط تایپی",
    "R-LATIN": "متن انگلیسی", "R-LEAK": "خروجی خراب مدل", "R-MARKUP": "تگ HTML", "R-CHART": "نمودار نادرست",
    "R-STAT": "کاشی عدد نادرست", "R-HEADLINE": "تیتر بلند", "W-SKIP": "نویسنده ننوشت", "W-SHAPE": "ساختار نامفهوم",
    "E-EDITOR": "ایراد ویراستار", "E-POLISH": "بازنویسی ویراستار پذیرفته نشد", "C-COMPLIANCE": "ایراد انطباق",
    "D-FIT": "جملهٔ جانشده در اسلاید", "DATA": "مشکل داده",
}
DATA_FA = {"DATA-FX": "ارز و طلا (tgju)", "DATA-TSE": "سابقهٔ شاخص (TSE)", "DATA-INDICES": "شاخص‌ها و صنایع (TSE)",
           "DATA-TECHNICAL": "تحلیل تکنیکال", "DATA-NEWS": "منبع‌های خبری", "DATA-STALE": "سابقهٔ کهنهٔ شاخص", "DATA-TODAY": "عدد امروز شاخص"}
ROLE_FA = {"write": "نویسنده", "review": "بازبین‌ها", "manager": "مدیر", "marketing": "بازاریابی"}
DONE = ("awaiting_approval", "approved", "rejected", "expired")  # runs that produced content for the owner
PUBLISHED = "confirmed"
JOB_PROBLEM = ("failed", "dead", "unknown", "blocked")


def _rule_group(rule_id: str) -> str:
    if rule_id.startswith("DATA"):
        return "DATA"
    for prefix in ("E-EDITOR", "C-COMPLIANCE"):
        if rule_id.startswith(prefix):
            return prefix
    return rule_id


def period_bounds(end_day: date, days: int) -> tuple[date, date, datetime, datetime]:
    """[start_day, end_day] in Tehran days, and the same as UTC instants [from, to)."""
    days = max(1, min(int(days), MAX_DAYS))
    start_day = end_day - timedelta(days=days - 1)
    lo = datetime.combine(start_day, time(0), tzinfo=TEHRAN)
    hi = datetime.combine(end_day + timedelta(days=1), time(0), tzinfo=TEHRAN)
    return start_day, end_day, lo, hi


@dataclass
class PlanStats:
    planned: int = 0
    started: int = 0
    ready: int = 0  # reached the owner
    approved: int = 0
    published: int = 0
    missed: int = 0  # its time passed and it never started
    upcoming: int = 0
    by_pillar: dict[str, list[int]] = field(default_factory=dict)  # pillar -> [planned, published]


@dataclass
class Report:
    org_id: str
    start: date
    end: date
    runs: int = 0
    by_origin: Counter = field(default_factory=Counter)
    by_template: Counter = field(default_factory=Counter)
    by_status: Counter = field(default_factory=Counter)
    ready: int = 0
    approved: int = 0
    rejected: int = 0
    expired: int = 0
    waiting: int = 0
    first_version_approved: int = 0
    revisions: int = 0
    reject_reasons: list[str] = field(default_factory=list)
    stopped: list[tuple[str, str]] = field(default_factory=list)  # (status, why) of runs that produced nothing
    blocking_rules: Counter = field(default_factory=Counter)
    warning_rules: Counter = field(default_factory=Counter)
    data_notes: Counter = field(default_factory=Counter)
    jobs: Counter = field(default_factory=Counter)
    jobs_by_destination: dict[str, Counter] = field(default_factory=dict)
    unknown_jobs: list[int] = field(default_factory=list)
    median_ready_minutes: float | None = None
    median_decision_minutes: float | None = None
    llm_calls: int = 0
    llm_failed: int = 0
    llm_tokens: int = 0
    llm_cost: float | None = None
    llm_by_role: Counter = field(default_factory=Counter)
    plans: PlanStats = field(default_factory=PlanStats)
    pillar_names: dict[str, str] = field(default_factory=dict)
    paused: bool = False


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60


def _origin(origin: str | None) -> str:
    return (origin or "cli").split(":", 1)[0]


def build_report(session: Session, org_id: str, *, end_day: date, days: int, now: datetime, root="orgs") -> Report:
    start_day, end_day, lo, hi = period_bounds(end_day, days)
    rep = Report(org_id=org_id, start=start_day, end=end_day)
    runs = session.execute(select(Run).where(Run.org_id == org_id, Run.created_at >= lo, Run.created_at < hi)).scalars().all()
    run_ids = [r.id for r in runs]
    origins = {c.run_id: c.origin for c in session.execute(select(ControlRequest).where(ControlRequest.run_id.in_(run_ids))).scalars()} \
        if run_ids else {}
    packages: dict[str, list[PackageRow]] = {}
    if run_ids:
        for p in session.execute(select(PackageRow).where(PackageRow.run_id.in_(run_ids)).order_by(PackageRow.version)).scalars():
            packages.setdefault(p.run_id, []).append(p)
    ready_times, decision_times = [], []
    for r in runs:
        rep.runs += 1
        rep.by_origin[_origin(origins.get(r.id))] += 1
        rep.by_template[r.template] += 1
        rep.by_status[r.status] += 1
        for f in r.findings or []:
            rule = _rule_group(str(f.get("rule_id", "")))
            if rule == "DATA":
                rep.data_notes[str(f.get("rule_id"))] += 1
            elif f.get("severity") == "block":
                rep.blocking_rules[rule] += 1
            else:
                rep.warning_rules[rule] += 1
        pkgs = packages.get(r.id, [])
        if pkgs:
            rep.ready += 1
            rep.revisions += max(p.version for p in pkgs) - 1
            ready_times.append(_minutes(r.created_at, pkgs[0].created_at))
        elif r.status not in ("running", "planning"):
            d = r.detail or {}
            why = d.get("message") or d.get("reason") or d.get("error") or \
                "؛ ".join(f["message"] for f in (r.findings or []) if f.get("severity") == "block")[:200]
            rep.stopped.append((r.status, str(why)[:200]))
        if r.status == "approved":
            rep.approved += 1
            if pkgs and max(p.version for p in pkgs) == 1:
                rep.first_version_approved += 1
        elif r.status == "rejected":
            rep.rejected += 1
        elif r.status == "expired" and pkgs:
            rep.expired += 1
        elif r.status == "awaiting_approval":
            rep.waiting += 1
        if pkgs and r.status in ("approved", "rejected"):
            last = pkgs[-1]
            decided = session.execute(select(Approval.decided_at).where(Approval.package_id == last.package_id)
                                      .order_by(Approval.id).limit(1)).scalar_one_or_none()
            if decided is not None:
                decision_times.append(_minutes(last.created_at, decided))
                if r.status == "rejected":
                    reason = session.execute(select(Approval.reason).where(Approval.package_id == last.package_id,
                                                                           Approval.decision == "rejected", Approval.reason.is_not(None))
                                             .limit(1)).scalar_one_or_none()
                    if reason:
                        rep.reject_reasons.append(reason[:150])
    rep.median_ready_minutes = statistics.median(ready_times) if ready_times else None
    rep.median_decision_minutes = statistics.median(decision_times) if decision_times else None

    for j in session.execute(select(PublishJob).where(PublishJob.org_id == org_id, PublishJob.created_at >= lo,
                                                      PublishJob.created_at < hi)).scalars():
        rep.jobs[j.status] += 1
        rep.jobs_by_destination.setdefault(j.destination_id, Counter())[j.status] += 1
        if j.status == "unknown":
            rep.unknown_jobs.append(j.id)

    costs = []
    for c in session.execute(select(LLMCall).where(LLMCall.org_id == org_id, LLMCall.created_at >= lo, LLMCall.created_at < hi)).scalars():
        rep.llm_calls += 1
        rep.llm_failed += 0 if c.ok else 1
        rep.llm_tokens += c.input_tokens + c.output_tokens
        rep.llm_by_role[c.step_key.split(".")[0]] += 1
        if c.cost_usd is not None:
            costs.append(c.cost_usd)
    rep.llm_cost = sum(costs) if costs else None

    rep.plans = _plan_stats(session, org_id, start_day, end_day, now)
    if rep.plans.planned:
        from .marketing import PlanError, load_policy
        try:
            policy = load_policy(org_id, root)
            rep.pillar_names = {p.id: p.name_fa for p in policy.pillars} if policy else {}
        except PlanError:
            pass  # ids are shown instead
    control = session.get(OrgControl, org_id)
    rep.paused = bool(control and control.paused)
    return rep


def _plan_stats(session: Session, org_id: str, start_day: date, end_day: date, now: datetime) -> PlanStats:
    from .marketing import PLAN_LATE, load_plan, publish_at
    st = PlanStats()
    rows = session.execute(select(ContentPlanRow).where(ContentPlanRow.org_id == org_id,
                                                        ContentPlanRow.status.in_(("approved", "superseded")),
                                                        ContentPlanRow.decided_at.is_not(None))).scalars().all()
    for row in rows:
        plan = load_plan(row)
        for n, it in enumerate(plan.items, 1):
            if not start_day <= it.day <= end_day:
                continue
            req = session.execute(select(ControlRequest).where(ControlRequest.origin == f"plan:{row.id}:{n}")).scalar_one_or_none()
            if req is None and row.status == "superseded":
                continue  # a newer approved plan replaced this item before it ran
            st.planned += 1
            pillar = st.by_pillar.setdefault(it.pillar, [0, 0])
            pillar[0] += 1
            if req is None:
                if publish_at(it) + PLAN_LATE <= now:
                    st.missed += 1
                elif row.status == "approved":
                    st.upcoming += 1
                continue
            st.started += 1
            run = session.get(Run, req.run_id)
            if run is None:
                continue
            if run.package_id:
                st.ready += 1
            if run.status == "approved":
                st.approved += 1
                jobs = session.execute(select(PublishJob.status).where(PublishJob.package_id == run.package_id)).scalars().all()
                if PUBLISHED in jobs:
                    st.published += 1
                    pillar[1] += 1
    return st


# ------------------------------------------------------------------ what the owner reads


def _n(x: float | int, decimals: int = 1) -> str:
    text = f"{x:,.0f}" if isinstance(x, int) or float(x).is_integer() else f"{x:,.{decimals}f}"
    return text.replace(",", "٬").replace(".", "٫").translate(FA)


def _pct(part: int, whole: int) -> str:
    return f"{_n(round(100 * part / whole))}٪" if whole else "—"


def _duration(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    if minutes < 90:
        return f"{_n(round(minutes))} دقیقه"
    return f"{_n(round(minutes / 60, 1))} ساعت"


def observations(rep: Report) -> list[str]:
    """Fixed rules over the counts: what needs the owner's attention or is worth knowing."""
    out = []
    if rep.paused:
        out.append("⏸ انتشار الان متوقف است؛ برای ادامه /resume را بزنید.")
    if rep.unknown_jobs:
        out.append(f"❓ {_n(len(rep.unknown_jobs))} ارسال «نامعلوم» منتظر بررسی شماست: " +
                   "، ".join(f"/sent {j} یا /retry {j}" for j in rep.unknown_jobs[:3]))
    failed = sum(rep.jobs[s] for s in ("failed", "dead", "blocked"))
    if failed:
        out.append(f"❌ {_n(failed)} ارسال منتشر نشد؛ دلیلش را با /jobs ببینید (معمولاً توکن یا دسترسی ربات به کانال).")
    if rep.plans.missed:
        out.append(f"⏭ {_n(rep.plans.missed)} پست برنامه ساخته نشد چون ربات کنترل سر وقتش روشن نبود.")
    if rep.expired:
        out.append(f"⌛️ {_n(rep.expired)} محتوای آماده بدون تصمیم ماند و مهلتش گذشت.")
    if rep.waiting:
        out.append(f"⏳ {_n(rep.waiting)} محتوا هنوز منتظر تصمیم شماست.")
    if rep.data_notes:
        top, n = rep.data_notes.most_common(1)[0]
        out.append(f"📉 {_n(sum(rep.data_notes.values()))} بار بخشی از داده‌ها نیامد (بیشتر: {DATA_FA.get(top, top)}، {_n(n)} بار)؛ "
                   "آن بخش‌ها از گزارش‌ها حذف شدند.")
    decided = rep.approved + rep.rejected
    if decided >= 3 and rep.first_version_approved / decided < 0.5:
        out.append("✏️ بیشتر محتوا در نسخهٔ اول تأیید نشد؛ اگر ایرادها تکراری است، آن را در قواعد برند یا سیاست محتوا بنویسید.")
    if rep.blocking_rules:
        rule, n = rep.blocking_rules.most_common(1)[0]
        if n >= 3:
            out.append(f"🧪 رایج‌ترین ایراد کنترل کیفیت: «{RULE_FA.get(rule, rule)}» ({_n(n)} بار).")
    if rep.llm_failed and rep.llm_failed / max(rep.llm_calls, 1) > 0.2:
        out.append(f"🤖 {_n(rep.llm_failed)} از {_n(rep.llm_calls)} تماس با مدل ناموفق بود؛ اتصال یا سهمیهٔ سرویس مدل را بررسی کنید.")
    return out


def report_text(rep: Report) -> str:
    head = f"📊 گزارش عملکرد تیم — {long_date(rep.start)}" + ("" if rep.start == rep.end else f" تا {long_date(rep.end)}")
    lines = [head]
    if not rep.runs and not sum(rep.jobs.values()) and not rep.plans.planned:
        return head + "\nدر این دوره کاری انجام نشده است."

    lines += ["", "🧾 تولید محتوا",
              f"• {_n(rep.runs)} درخواست: " + "، ".join(f"{ORIGIN_FA.get(k, k)} {_n(v)}" for k, v in rep.by_origin.most_common()),
              "• نوع: " + "، ".join(f"{TEMPLATE_FA.get(k, k)} {_n(v)}" for k, v in rep.by_template.most_common()),
              f"• {_n(rep.ready)} محتوای آماده برای تأیید ({_pct(rep.ready, rep.runs)} درخواست‌ها)؛ زمان آماده‌سازی (میانه): {_duration(rep.median_ready_minutes)}"]
    if rep.stopped:
        lines.append(f"• {_n(len(rep.stopped))} درخواست به محتوا نرسید:")
        for status, why in rep.stopped[:4]:
            from ..control.service import STATUS_FA
            lines.append(f"   – {STATUS_FA.get(status, status)}: {why}")

    decided = rep.approved + rep.rejected
    lines += ["", "✅ تصمیم‌های شما",
              f"• تأیید {_n(rep.approved)}، رد {_n(rep.rejected)}، بی‌تصمیم و منقضی {_n(rep.expired)}، منتظر {_n(rep.waiting)}",
              f"• تأیید در نسخهٔ اول: {_pct(rep.first_version_approved, decided)}؛ درخواست اصلاح: {_n(rep.revisions)} بار",
              f"• فاصلهٔ آماده شدن تا تصمیم شما (میانه): {_duration(rep.median_decision_minutes)}"]
    for reason in rep.reject_reasons[:3]:
        lines.append(f"   – دلیل رد: {reason}")

    lines += ["", "📣 انتشار"]
    if rep.jobs:
        lines.append(f"• منتشر شد {_n(rep.jobs[PUBLISHED])} از {_n(sum(rep.jobs.values()))} ارسال؛ " +
                     "، ".join(f"{d}: {_n(c[PUBLISHED])}/{_n(sum(c.values()))}" for d, c in sorted(rep.jobs_by_destination.items())))
        problems = {s: rep.jobs[s] for s in JOB_PROBLEM if rep.jobs[s]}
        if problems:
            from ..control.service import JOB_FA
            lines.append("• " + "، ".join(f"{JOB_FA.get(s, s)}: {_n(n)}" for s, n in problems.items()))
    else:
        lines.append("• در این دوره چیزی برای ارسال در صف قرار نگرفت.")

    p = rep.plans
    if p.planned:
        lines += ["", "🗓 برنامهٔ محتوا",
                  f"• {_n(p.planned)} پست برنامه‌ریزی‌شده: شروع {_n(p.started)}، آماده {_n(p.ready)}، تأیید {_n(p.approved)}، "
                  f"منتشر {_n(p.published)}، جامانده {_n(p.missed)}، پیش رو {_n(p.upcoming)}",
                  f"• پایبندی به برنامه (منتشرشده از برنامه‌ریزی‌شدهٔ گذشته): {_pct(p.published, p.planned - p.upcoming)}"]
        lines.append("• منتشرشده/برنامه‌ریزی‌شدهٔ هر محور: " + "، ".join(f"{rep.pillar_names.get(k, k)} {_n(v[1])}/{_n(v[0])}"
                                                                     for k, v in sorted(p.by_pillar.items())))

    if rep.blocking_rules or rep.warning_rules:
        lines += ["", "🧪 کنترل کیفیت (ایرادهای ثبت‌شده روی محتوای این دوره)"]
        if rep.blocking_rules:
            lines.append("• متوقف‌کننده: " + "، ".join(f"{RULE_FA.get(k, k)} {_n(v)}" for k, v in rep.blocking_rules.most_common(4)))
        if rep.warning_rules:
            lines.append("• هشدار: " + "، ".join(f"{RULE_FA.get(k, k)} {_n(v)}" for k, v in rep.warning_rules.most_common(4)))

    if rep.llm_calls:
        cost = f"؛ هزینه {_n(rep.llm_cost, 2)} دلار" if rep.llm_cost is not None else ""
        lines += ["", "🤖 مدل زبانی",
                  f"• {_n(rep.llm_calls)} تماس ({_n(rep.llm_failed)} ناموفق)، {_n(rep.llm_tokens)} توکن{cost}",
                  "• " + "، ".join(f"{ROLE_FA.get(k, k)} {_n(v)}" for k, v in rep.llm_by_role.most_common())]

    obs = observations(rep)
    if obs:
        lines += ["", "🔎 نیاز به توجه", *obs]
    return "\n".join(lines)


# ------------------------------------------------------------------ the weekly report's settings


@dataclass(frozen=True)
class Weekly:
    day: str
    at: tuple[int, int]
    days: int


def load_weekly(org_id: str, root="orgs") -> Weekly | None:
    """orgs/<org>/reporting.yaml `weekly`, or None. A broken file is an error, never a silently missing report."""
    import re
    from pathlib import Path

    import yaml
    path = Path(root) / org_id / "reporting.yaml"
    if not path.is_file():
        return None
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("weekly")
    if not data:
        return None
    from .marketing import DAYS
    day = str(data.get("day", "")).lower()[:3]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(data.get("time", "")))
    days = data.get("days", 7)
    if day not in DAYS or not m or int(m.group(1)) > 23 or int(m.group(2)) > 59 or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"{path}: weekly باید day (مثل fri)، time (مثل \"20:00\") و days (۱ تا ۹۰) داشته باشد")
    return Weekly(day, (int(m.group(1)), int(m.group(2))), days)
