"""market_close_v1: the end-of-day market analysis post, from data to an approved, queued publication.

    acquire -> analyze -> write -> verify (one revision with feedback if blocked) -> package
            -> ask for approval -> wait (durable; survives restarts) -> record decision -> queue publish jobs

Every step is a DBOS step: once it has finished, a restart never runs it again (no second model call, no second
fetch). The workflow id is the run id, so the same post cannot be started twice. Sending is not done here; the
publisher picks up the queued jobs and applies its own at-most-once rules.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from dbos import DBOS
from sqlalchemy import select

from ..agents.analysis import AnalysisReport, InsufficientData, analyze_market_close
from ..agents.verification import verify
from ..agents.writer import PROMPT_VERSION, Draft, WriterError, write_market_close
from ..approvals import ApprovalError, decide, store_package
from ..brand import load_brand
from ..content.calendar import long_date
from ..content.numbers import Claim
from ..content.package_builder import build_text_package
from ..content.template import PostParts
from ..data.acquisition import AcquisitionError, acquire
from ..db import DataPointRow, DataSnapshot, Run
from ..llm import BudgetExceeded, LLMError
from ..package import DataPointRef
from ..publishing.publisher import Publisher
from ..runtime import rt

TEMPLATE = "market_close_v1"
TOPIC = "approval"
MAX_WRITES = 2
MAX_STALE_MESSAGES = 5


def run_id_for(org_id: str, day: date) -> str:
    return f"{org_id}:{TEMPLATE}:{day.isoformat()}"


# ------------------------------------------------------------------ steps


@DBOS.step()
def s_start(run_id: str, org_id: str) -> None:
    with rt().sessions.begin() as s:
        if s.get(Run, run_id) is None:
            s.add(Run(id=run_id, org_id=org_id, template=TEMPLATE, status="running"))


@DBOS.step()
def s_finish(run_id: str, status: str, findings: list | None = None, detail: dict | None = None) -> dict:
    with rt().sessions.begin() as s:
        run = s.get(Run, run_id)
        run.status = status
        if findings is not None:
            run.findings = findings
        if detail:
            run.detail = {**(run.detail or {}), **detail}
    return {"run_id": run_id, "status": status, **(detail or {})}


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=20, backoff_rate=2)
async def s_acquire(run_id: str, source_id: str) -> dict:
    r = rt()
    try:
        with r.sessions.begin() as s:
            got = await acquire(s, r.assets, r.sources[source_id], r.fetcher, r.clock())
            s.get(Run, run_id).snapshot_id = got.snapshot_id
    except AcquisitionError as e:
        if e.retryable:
            raise  # DBOS retries this step
        return {"error": str(e)}
    return {"snapshot_id": got.snapshot_id, "warnings": got.warnings}


@DBOS.step()
def s_analyze(org_id: str, snapshot_id: str, day_iso: str) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    try:
        with r.sessions() as s:
            rep = analyze_market_close(s, snapshot_id, brand, date.fromisoformat(day_iso))
    except InsufficientData as e:
        return {"skip": str(e)}
    return {"report": rep.model_dump(mode="json")}


@DBOS.step(retries_allowed=True, max_attempts=2, interval_seconds=60)
async def s_write(org_id: str, run_id: str, report: dict, day_iso: str, feedback: list[str] | None, attempt: int) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    rep = AnalysisReport.model_validate(report)
    try:
        draft = await write_market_close(r.gateway, brand, rep, org_id=org_id, run_id=run_id,
                                         date_text=long_date(date.fromisoformat(day_iso)), feedback=feedback,
                                         step_key=f"write.{attempt}")
    except BudgetExceeded as e:
        return {"error": f"budget: {e}"}
    except WriterError as e:
        return {"error": f"writer: {e}"}
    except LLMError as e:
        if e.retryable:
            raise
        return {"error": f"model: {e}"}
    return {"draft": draft.model_dump(mode="json")}


@DBOS.step()
def s_verify(org_id: str, report: dict, draft: dict) -> dict:
    brand = load_brand(org_id, rt().brand_root)
    v = verify(Draft.model_validate(draft), AnalysisReport.model_validate(report), brand)
    return {
        "blocked": v.blocked,
        "feedback": v.feedback(),
        "findings": [f.__dict__ for f in v.findings],
        "parts": None if v.parts is None else {"hashtag": v.parts.hashtag, "headline": v.parts.headline,
                                               "paragraphs": list(v.parts.paragraphs), "cta": v.parts.cta},
        "claims": [c.__dict__ for c in v.claims],
    }


@DBOS.step()
def s_package(org_id: str, run_id: str, verified: dict, destinations: list[list[str]], window_minutes: int) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    parts = PostParts(hashtag=verified["parts"]["hashtag"], headline=verified["parts"]["headline"],
                      paragraphs=tuple(verified["parts"]["paragraphs"]), cta=verified["parts"]["cta"])
    claims = [Claim(**c) for c in verified["claims"]]
    used = {c.point_id for c in claims}
    now = r.clock()
    with r.sessions.begin() as s:
        rows = s.execute(select(DataPointRow, DataSnapshot.raw_sha256)
                         .join(DataSnapshot, DataSnapshot.id == DataPointRow.snapshot_id)
                         .where(DataPointRow.id.in_(used))).all()
        refs = [DataPointRef(id=p.id, metric=p.metric, subject=p.subject, source_text=p.source_text, value=p.value,
                             unit=p.unit, as_of=p.as_of, source_id=p.source_id, raw_sha256=raw) for p, raw in rows]
        pkg = build_text_package(
            brand=brand, run_id=run_id, content_class="market_close", parts=parts, claims=claims, data_points=refs,
            destinations=[(d, p) for d, p in destinations], versions={"workflow": TEMPLATE, "writer_prompt": PROMPT_VERSION},
            expires_at=now + timedelta(minutes=window_minutes), risk_level="low",
        )
        store_package(s, pkg, actor=f"workflow:{run_id}")
        run = s.get(Run, run_id)
        run.package_id, run.status = pkg.package_id, "awaiting_approval"
    return {"package_id": pkg.package_id, "package_hash": pkg.package_hash}


@DBOS.step()
async def s_request_approval(org_id: str, run_id: str, package_id: str, package_hash: str) -> None:
    await rt().notifier.request(org_id=org_id, run_id=run_id, package_id=package_id, package_hash=package_hash)


@DBOS.step()
def s_decide(org_id: str, run_id: str, package_id: str, package_hash: str, message: dict, destinations: list[str]) -> dict:
    r = rt()
    decision = message.get("decision")
    approver = str(message.get("approver") or "unknown")
    chosen = [d for d in (message.get("destinations") or destinations) if d in destinations]
    jobs: list[int] = []
    try:
        with r.sessions.begin() as s:
            for dest in destinations:
                verdict = decision if dest in chosen else "rejected"
                decide(s, package_id, package_hash, dest, verdict, approver, reason=message.get("reason"))
            if decision == "approved":
                pub = Publisher(r.sessions, r.assets, adapter_factory=lambda d: None, clock=r.clock)
                jobs = [pub.enqueue(s, package_id, dest, actor=approver).id for dest in chosen]
            run = s.get(Run, run_id)
            run.status = "approved" if decision == "approved" else "rejected"
            run.detail = {**(run.detail or {}), "approver": approver, "jobs": jobs}
    except ApprovalError as e:
        return {"status": "blocked", "error": str(e)}
    return {"status": "approved" if decision == "approved" else "rejected", "jobs": jobs}


@DBOS.step()
def s_note(run_id: str, note: str) -> None:
    with rt().sessions.begin() as s:
        run = s.get(Run, run_id)
        run.detail = {**(run.detail or {}), "notes": [*(run.detail or {}).get("notes", []), note]}


# ------------------------------------------------------------------ the workflow


@DBOS.workflow()
async def market_close(org_id: str, day_iso: str, destinations: list[list[str]], source_id: str = "bourse_trader",
                       approval_timeout_s: int = 3 * 3600, window_minutes: int = 240) -> dict[str, Any]:
    run_id = DBOS.workflow_id
    s_start(run_id, org_id)
    try:
        got = await s_acquire(run_id, source_id)
    except Exception as e:  # retries exhausted
        return s_finish(run_id, "failed", detail={"error": f"acquire: {e}"})
    if "error" in got:
        return s_finish(run_id, "failed", detail={"error": got["error"]})

    analysed = s_analyze(org_id, got["snapshot_id"], day_iso)
    if "skip" in analysed:
        return s_finish(run_id, "skipped", detail={"reason": analysed["skip"]})

    feedback: list[str] | None = None
    verified: dict = {}
    for attempt in range(1, MAX_WRITES + 1):
        try:
            written = await s_write(org_id, run_id, analysed["report"], day_iso, feedback, attempt)
        except Exception as e:
            return s_finish(run_id, "failed", detail={"error": f"write: {e}"})
        if "error" in written:
            return s_finish(run_id, "failed", detail={"error": written["error"]})
        verified = s_verify(org_id, analysed["report"], written["draft"])
        if not verified["blocked"]:
            break
        feedback = verified["feedback"]
    if verified["blocked"]:
        return s_finish(run_id, "blocked", findings=verified["findings"])

    pkg = s_package(org_id, run_id, verified, destinations, window_minutes)
    await s_request_approval(org_id, run_id, pkg["package_id"], pkg["package_hash"])

    dest_ids = [d for d, _ in destinations]
    for _ in range(MAX_STALE_MESSAGES):
        message = await DBOS.recv_async(TOPIC, timeout_seconds=approval_timeout_s)
        if message is None:
            return s_finish(run_id, "expired", findings=verified["findings"], detail={"package_id": pkg["package_id"]})
        if message.get("package_hash") != pkg["package_hash"]:
            s_note(run_id, f"ignored a decision for another version ({str(message.get('package_hash'))[:12]})")
            continue
        result = s_decide(org_id, run_id, pkg["package_id"], pkg["package_hash"], message, dest_ids)
        return {"run_id": run_id, **result, "package_id": pkg["package_id"]}
    return s_finish(run_id, "expired", detail={"reason": "too many decisions for other versions"})


def submit_decision(run_id: str, package_hash: str, decision: str, approver: str,
                    destinations: list[str] | None = None, reason: str | None = None) -> None:
    """Called by the approval bot (or a test): deliver a human decision to the waiting run."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    DBOS.send(run_id, {"package_hash": package_hash, "decision": decision, "approver": approver,
                       "destinations": destinations, "reason": reason}, TOPIC)
