"""handle_request: a Persian request from the owner, carried by the whole team to an approved, queued publication.

    Manager (plan / ask / decline)
      -> data acquisition -> market, technical and fundamental analysts (as the plan says)
      -> writer (all channels at once) -> fact checker -> editor + compliance officer
         (blocking findings go back to the writer once)
      -> designer (Instagram carousel)
      -> one publication package -> human approval (durable wait) -> publish queue

Every box is a DBOS step: after a crash the run continues from the last finished step, with no second model call,
fetch or render. The run id is the workflow id. Sending happens in the publisher, with its own at-most-once rules.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from dbos import DBOS
from sqlalchemy import select

from ..agents.analysis import AnalysisReport, InsufficientData
from ..agents.manager import PROMPT_VERSION as MANAGER_PROMPT
from ..agents.manager import plan_request
from ..agents.multichannel import PROMPT_VERSION as WRITER_PROMPT
from ..agents.multichannel import (
    Caption, MultiChannelDraft, RenderedSlide, VerifiedContent, instagram_caption_text, verify_multichannel,
    write_multichannel,
)
from ..agents.reviewers import PROMPT_VERSION as REVIEW_PROMPT
from ..agents.reviewers import compliance_review, editorial_review
from ..agents.specialists import Part, compose, fundamental_part, market_part, technical_part
from ..agents.verification import Finding
from ..agents.writer import WriterError
from ..analysis.derived import Spec, store_derived
from ..analysis.fundamental import FORMULA_VERSION as FUND_V
from ..analysis.technical import FORMULA_VERSION as TECH_V
from ..approvals import store_package
from ..brand import load_brand
from ..content.calendar import long_date
from ..content.numbers import BoundPoint, Claim
from ..content.package_builder import build_multichannel_package
from ..content.template import PostParts
from ..data.imports import load_snapshot_bytes, parse_price_csv
from ..db import DataPointRow, DataSnapshot, Run
from ..llm import BudgetExceeded, LLMError
from ..package import AssetRef, DataPointRef
from ..registry import DEFAULT_ROOT, ExecutionPlan, OrgPolicy, Registry
from ..runtime import rt
from ..visual.charts import ChartSpec
from ..visual.render import MIME, TEMPLATE_VERSION, RenderError, render_carousel
from .market_close import TOPIC, s_acquire, s_decide, s_finish, s_note, s_request_approval

WORKFLOW = "handle_request@1"
MAX_WRITES = 2
MAX_STALE_MESSAGES = 5


def run_id_for(org_id: str, request: str, day: date) -> str:
    digest = hashlib.sha256(" ".join(request.split()).encode()).hexdigest()[:12]
    return f"{org_id}:req:{day.isoformat()}:{digest}"


RESUMABLE = ("running", "planning", "awaiting_approval", "approved", "rejected")


def fresh_run_id(sessions, base: str) -> str:
    """The same request on the same day is one run (so a post is never made twice). But a run that failed, was
    blocked or needed input must not make a corrected re-run return the old failure: that one gets a new attempt id."""
    with sessions() as s:
        n = 0
        while True:
            rid = base if n == 0 else f"{base}:r{n}"
            run = s.get(Run, rid)
            if run is None or run.status in RESUMABLE:
                return rid
            n += 1


def price_source(subject: str) -> str:
    return f"import:prices:{subject}"


def statements_source(subject: str) -> str:
    return f"import:statements:{subject}"


def _registry() -> Registry:
    return Registry.load(getattr(rt(), "registry_root", None) or DEFAULT_ROOT, check_entrypoints=False)


# ------------------------------------------------------------------ steps


@DBOS.step()
def s_open(run_id: str, org_id: str, request: str) -> None:
    with rt().sessions.begin() as s:
        if s.get(Run, run_id) is None:
            s.add(Run(id=run_id, org_id=org_id, template="request", status="planning", detail={"request": request}))


@DBOS.step(retries_allowed=True, max_attempts=2, interval_seconds=30)
async def s_plan(org_id: str, run_id: str, request: str, day_iso: str, platforms: list[str]) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    policy = OrgPolicy.from_brand(brand, tuple(platforms))
    try:
        d = await plan_request(r.gateway, _registry(), request, org_id=org_id, run_id=run_id,
                               today=date.fromisoformat(day_iso), policy=policy)
    except LLMError as e:
        if e.retryable and not isinstance(e, BudgetExceeded):
            raise
        return {"action": "failed", "message": f"مدیر نتوانست برنامه بریزد: {e}"}
    if d.action != "run":
        return {"action": d.action, "message": d.message}
    plan = d.plan
    with r.sessions.begin() as s:
        run = s.get(Run, run_id)
        run.template = plan.template_id
        run.status = "running"
        run.detail = {**(run.detail or {}), "plan": json.loads(plan.model_dump_json()), "summary": plan.summary_fa}
    return {"action": "run", "plan": json.loads(plan.model_dump_json())}


def _latest_snapshot(s, source_id: str) -> str | None:
    return s.execute(select(DataSnapshot.id).where(DataSnapshot.source_id == source_id)
                     .order_by(DataSnapshot.fetched_at.desc()).limit(1)).scalar_one_or_none()


def _part_json(part: Part) -> dict:
    return {"title": part.title, "points": [p.model_dump(mode="json") for p in part.points], "facts": list(part.facts),
            "charts": [c.model_dump(mode="json") for c in part.charts]}


def _part(d: dict) -> Part:
    return Part(title=d["title"], points=[BoundPoint.model_validate(p) for p in d["points"]], facts=list(d["facts"]),
                charts=[ChartSpec.model_validate(c) for c in d["charts"]])


@DBOS.step()
def s_market(org_id: str, snapshot_id: str, day_iso: str) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    try:
        with r.sessions.begin() as s:
            part, excluded = market_part(s, snapshot_id=snapshot_id, brand=brand, run_date=date.fromisoformat(day_iso), now=r.clock())
    except InsufficientData as e:
        return {"skip": str(e)}
    return {"part": _part_json(part), "excluded": list(excluded)}


@DBOS.step()
def s_technical(subject: str) -> dict:
    r = rt()
    with r.sessions.begin() as s:
        sid = _latest_snapshot(s, price_source(subject))
        if sid is None:
            return {"missing": f"سابقهٔ قیمت «{subject}» وارد نشده است (orchestra import-prices)"}
        unit = "point" if "شاخص" in subject else "IRR"
        part = technical_part(s, r.assets, history_snapshot_id=sid, subject=subject, price_unit=unit, now=r.clock())
    return {"part": _part_json(part)}


@DBOS.step()
def s_fundamental(subject: str) -> dict:
    r = rt()
    with r.sessions.begin() as s:
        st = _latest_snapshot(s, statements_source(subject))
        px = _latest_snapshot(s, price_source(subject))
        if st is None or px is None:
            need = "صورت‌های مالی" if st is None else "سابقهٔ قیمت"
            return {"missing": f"{need} «{subject}» وارد نشده است"}
        bars = parse_price_csv(load_snapshot_bytes(s, r.assets, px))
        last = bars[-1]
        price = store_derived(s, metric="price.last_close", subject=subject, value=last.close,
                              spec=Spec("آخرین قیمت پایانی", "IRR", 0, ("rial",)), formula=f"{TECH_V}:last_close@{last.day.isoformat()}",
                              inputs=[px], as_of=r.clock())
        part = fundamental_part(s, r.assets, statements_snapshot_id=st, price=last.close, price_day=last.day,
                                price_point_id=price.point_id, now=r.clock())
    return {"part": _part_json(part)}


@DBOS.step()
def s_compose(snapshot_id: str, parts: list[dict], excluded: list[str], day_iso: str) -> dict:
    ps = [_part(p) for p in parts]
    day = date.fromisoformat(day_iso)
    has_date = any(p.key == "D1" for part in ps for p in part.points)
    extra = [] if has_date else [BoundPoint(key="D1", point_id=f"date:{day_iso}", metric="run.date", label="تاریخ امروز",
                                            value=0, unit="date", decimals=0, subject=long_date(day), formats=("name",))]
    rep = compose(snapshot_id, ps, tuple(excluded), extra=extra)
    return {"report": rep.model_dump(mode="json")}


@DBOS.step(retries_allowed=True, max_attempts=2, interval_seconds=60)
async def q_write(org_id: str, run_id: str, report: dict, day_iso: str, platforms: list[str], focus: str,
                  feedback: list[str] | None, attempt: int) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    try:
        draft = await write_multichannel(r.gateway, brand, AnalysisReport.model_validate(report), org_id=org_id, run_id=run_id,
                                         date_text=long_date(date.fromisoformat(day_iso)), platforms=tuple(platforms),
                                         focus=focus, feedback=feedback, step_key=f"write.{attempt}")
    except BudgetExceeded as e:
        return {"error": f"budget: {e}"}
    except WriterError as e:
        return {"error": f"writer: {e}"}
    except LLMError as e:
        if e.retryable:
            raise
        return {"error": f"model: {e}"}
    return {"draft": draft.model_dump(mode="json")}


def _vc_json(vc: VerifiedContent) -> dict:
    return {
        "post": None if vc.post is None else {"hashtag": vc.post.hashtag, "headline": vc.post.headline,
                                              "paragraphs": list(vc.post.paragraphs), "cta": vc.post.cta},
        "caption": None if vc.caption is None else {"paragraphs": list(vc.caption.paragraphs), "hashtags": list(vc.caption.hashtags)},
        "slides": [{"title": s.title, "bullets": list(s.bullets), "stats": [list(x) for x in s.stats], "kind": s.kind,
                    "chart": None if s.chart is None else s.chart.model_dump(mode="json")} for s in vc.slides],
        "claims": [c.__dict__ for c in vc.claims],
        "findings": [f.__dict__ for f in vc.findings],
        "platforms": list(vc.platforms),
        "blocked": vc.blocked,
    }


def _vc(d: dict) -> VerifiedContent:
    return VerifiedContent(
        post=None if d["post"] is None else PostParts(d["post"]["hashtag"], d["post"]["headline"], tuple(d["post"]["paragraphs"]), d["post"]["cta"]),
        caption=None if d["caption"] is None else Caption(tuple(d["caption"]["paragraphs"]), tuple(d["caption"]["hashtags"])),
        slides=tuple(RenderedSlide(title=s["title"], bullets=tuple(s["bullets"]), stats=tuple(tuple(x) for x in s["stats"]), kind=s["kind"],
                                   chart=None if s["chart"] is None else ChartSpec.model_validate(s["chart"])) for s in d["slides"]),
        claims=[Claim(**c) for c in d["claims"]], findings=[Finding(**f) for f in d["findings"]], platforms=tuple(d["platforms"]))


@DBOS.step()
def s_check(org_id: str, report: dict, draft: dict, platforms: list[str]) -> dict:
    brand = load_brand(org_id, rt().brand_root)
    vc = verify_multichannel(MultiChannelDraft.model_validate(draft), AnalysisReport.model_validate(report), brand, tuple(platforms))
    return _vc_json(vc)


@DBOS.step()
async def s_review(org_id: str, run_id: str, report: dict, verified: dict, attempt: int, which: str) -> list[dict]:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    fn = editorial_review if which == "editor" else compliance_review
    found = await fn(r.gateway, brand, _vc(verified), AnalysisReport.model_validate(report), org_id=org_id, run_id=run_id,
                     step_key=f"review.{which}.{attempt}")
    return [f.__dict__ for f in found]


@DBOS.step()
async def s_design(org_id: str, run_id: str, verified: dict) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    vc = _vc(verified)
    try:
        images = await render_carousel(brand, list(vc.slides), vc.claims)
    except RenderError as e:
        return {"error": str(e)}
    refs = []
    for im in images:
        sha = r.assets.put(im.data)
        refs.append(AssetRef(asset_id=f"slide{im.order}_{sha[:12]}", order=im.order, role="slide", sha256=sha, mime=MIME,
                             bytes=len(im.data), width=im.width, height=im.height, template=TEMPLATE_VERSION,
                             chart_spec_id=im.chart_id).model_dump(mode="json"))
    return {"assets": refs, "chart_points": sorted({pid for s in vc.slides if s.chart for pid in s.chart.sources})}


@DBOS.step()
def q_package(org_id: str, run_id: str, verified: dict, assets: list[dict], chart_points: list[str], destinations: list[list[str]],
              window_minutes: int, risk: str, versions: dict) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    vc = _vc(verified)
    used = {c.point_id for c in vc.claims} | set(chart_points)
    now = r.clock()
    with r.sessions.begin() as s:
        rows = s.execute(select(DataPointRow, DataSnapshot.raw_sha256).outerjoin(DataSnapshot, DataSnapshot.id == DataPointRow.snapshot_id)
                         .where(DataPointRow.id.in_(used))).all()
        refs = []
        for p, raw in rows:
            if raw is None:  # a derived point: its "raw" is its formula and inputs
                raw = hashlib.sha256(json.dumps(p.derivation, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            refs.append(DataPointRef(id=p.id, metric=p.metric, subject=p.subject, source_text=p.source_text, value=p.value,
                                     unit=p.unit, as_of=p.as_of, source_id=p.source_id, raw_sha256=raw))
        caption = instagram_caption_text(brand, vc.caption) if vc.caption else None
        pkg = build_multichannel_package(
            brand=brand, run_id=run_id, content_class="market_report", post=vc.post, instagram_caption=caption,
            slides=[AssetRef.model_validate(a) for a in assets], claims=vc.claims, data_points=refs,
            destinations=[(d, p) for d, p in destinations], versions=versions, expires_at=now + timedelta(minutes=window_minutes),
            risk_level=risk, extra_point_ids=frozenset(chart_points))
        store_package(s, pkg, actor=f"workflow:{run_id}")
        run = s.get(Run, run_id)
        run.package_id, run.status = pkg.package_id, "awaiting_approval"
    return {"package_id": pkg.package_id, "package_hash": pkg.package_hash}


# ------------------------------------------------------------------ the workflow


@DBOS.workflow()
async def handle_request(org_id: str, request: str, day_iso: str, destinations: list[list[str]], source_id: str = "bourse_trader",
                         approval_timeout_s: int = 3 * 3600, window_minutes: int = 240) -> dict[str, Any]:
    run_id = DBOS.workflow_id
    s_open(run_id, org_id, request)
    available = sorted({p for _, p in destinations})
    try:
        planned = await s_plan(org_id, run_id, request, day_iso, available)
    except Exception as e:
        return s_finish(run_id, "failed", detail={"error": f"manager: {e}"})
    if planned["action"] != "run":
        status = {"clarify": "needs_input", "decline": "declined"}.get(planned["action"], "failed")
        return s_finish(run_id, status, detail={"message": planned["message"]})
    plan = ExecutionPlan.model_validate(planned["plan"])
    params = plan.params
    day = str(params.get("day") or day_iso)
    platforms = list(params.get("platforms") or available)
    dests = [[d, p] for d, p in destinations if p in platforms]

    # ---- data and analysts
    parts: list[dict] = []
    excluded: list[str] = []
    snapshot_id = ""
    if plan.has("acquire"):
        try:
            got = await s_acquire(run_id, source_id)
        except Exception as e:
            return s_finish(run_id, "failed", detail={"error": f"acquire: {e}"})
        if "error" in got:
            return s_finish(run_id, "failed", detail={"error": got["error"]})
        snapshot_id = got["snapshot_id"]
    if plan.has("market"):
        m = s_market(org_id, snapshot_id, day)
        if "skip" in m:
            return s_finish(run_id, "skipped", detail={"reason": m["skip"]})
        parts.append(m["part"])
        excluded = m["excluded"]
    for step_id, subject_key, fn in (("technical", "technical_subject", s_technical), ("fundamental", "fundamental_subject", s_fundamental)):
        if plan.has(step_id):
            subject = params.get(subject_key) or params.get("subject")
            out = fn(subject)
            if "missing" in out:
                return s_finish(run_id, "needs_data", detail={"message": out["missing"]})
            parts.append(out["part"])
    report = s_compose(snapshot_id or "imports", parts, excluded, day)["report"]

    # ---- writer, fact checker, editor, compliance officer
    feedback: list[str] | None = None
    verified: dict = {}
    findings: list[dict] = []
    for attempt in range(1, MAX_WRITES + 1):
        try:
            written = await q_write(org_id, run_id, report, day, platforms, str(params.get("focus") or ""), feedback, attempt)
        except Exception as e:
            return s_finish(run_id, "failed", detail={"error": f"write: {e}"})
        if "error" in written:
            return s_finish(run_id, "failed", detail={"error": written["error"]})
        verified = s_check(org_id, report, written["draft"], platforms)
        findings = list(verified["findings"])
        if not verified["blocked"]:
            if plan.has("edit"):
                findings += await s_review(org_id, run_id, report, verified, attempt, "editor")
            if plan.has("comply"):
                findings += await s_review(org_id, run_id, report, verified, attempt, "compliance")
        blocking = [f["message"] for f in findings if f["severity"] == "block"]
        if not blocking and not verified["blocked"]:
            break
        feedback = blocking or ["متن همهٔ کانال‌ها کامل نبود"]
    else:
        return s_finish(run_id, "blocked", findings=findings)

    # ---- designer
    assets: list[dict] = []
    chart_points: list[str] = []
    if plan.has("design"):
        designed = await s_design(org_id, run_id, verified)
        if "error" in designed:
            return s_finish(run_id, "blocked", findings=findings, detail={"error": f"design: {designed['error']}"})
        assets, chart_points = designed["assets"], designed["chart_points"]

    # ---- package, approval, publish queue
    risk = "medium" if any(f["severity"] == "warn" for f in findings) else "low"
    versions = {"workflow": WORKFLOW, "plan": f"{plan.template_id}@{plan.template_version}", "manager_prompt": MANAGER_PROMPT,
                "writer_prompt": WRITER_PROMPT, "review_prompt": REVIEW_PROMPT, "carousel": TEMPLATE_VERSION,
                "technical": TECH_V, "fundamental": FUND_V}
    pkg = q_package(org_id, run_id, verified, assets, chart_points, dests, window_minutes, risk, versions)
    s_finish(run_id, "awaiting_approval", findings=findings, detail={"package_id": pkg["package_id"]})
    await s_request_approval(org_id, run_id, pkg["package_id"], pkg["package_hash"])

    dest_ids = [d for d, _ in dests]
    for _ in range(MAX_STALE_MESSAGES):
        message = await DBOS.recv_async(TOPIC, timeout_seconds=approval_timeout_s)
        if message is None:
            return s_finish(run_id, "expired", detail={"package_id": pkg["package_id"]})
        if message.get("package_hash") != pkg["package_hash"]:
            s_note(run_id, f"ignored a decision for another version ({str(message.get('package_hash'))[:12]})")
            continue
        result = s_decide(org_id, run_id, pkg["package_id"], pkg["package_hash"], message, dest_ids)
        return {"run_id": run_id, **result, "package_id": pkg["package_id"]}
    return s_finish(run_id, "expired", detail={"reason": "too many decisions for other versions"})
