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
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dbos import DBOS
from sqlalchemy import select

from ..agents.analysis import AnalysisReport, InsufficientData
from ..agents.knowledge import knowledge_part
from ..agents.manager import PROMPT_VERSION as MANAGER_PROMPT
from ..agents.manager import plan_request
from ..agents.multichannel import PROMPT_VERSION as WRITER_PROMPT
from ..agents.multichannel import (
    Caption, MultiChannelDraft, RenderedSlide, VerifiedContent, instagram_caption_text, verify_multichannel,
    write_multichannel,
)
from ..agents.reviewers import PROMPT_VERSION as REVIEW_PROMPT
from ..agents.reviewers import compliance_review, editorial_review
from ..agents.specialists import Part, compose, fundamental_part, fx_part, indices_part, market_part, technical_part
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
from ..data.acquisition import AcquisitionError, acquire
from ..data.imports import load_bars, record_import
from ..data.sources import tse
from ..db import DataPointRow, DataSnapshot, Run
from ..knowledge import KnowledgeError, load_knowledge
from ..llm import BudgetExceeded, LLMError
from ..package import AssetRef, DataPointRef
from ..registry import DEFAULT_ROOT, ExecutionPlan, OrgPolicy, Registry, RegistryError
from ..runtime import rt
from ..visual.charts import ChartSpec
from ..visual.render import MIME, TEMPLATE_VERSION, RenderError, render_carousel
from .market_close import TOPIC, s_acquire, s_decide, s_finish, s_note, s_request_approval

WORKFLOW = "handle_request@1"
MAX_WRITES = 2
ATTEMPTS_PER_VERSION = MAX_WRITES + 1  # + one polishing round on the editor's remarks
MAX_STALE_MESSAGES = 5
AUTO_TECHNICAL = "شاخص کل"  # the daily market report always carries the index's technical picture
AUTO_TECHNICAL_TEMPLATES = ("market_report",)
AUTO_FX_TEMPLATES = ("market_report",)  # the daily report also carries the dollar, gold and coin
FX_SOURCE = "tgju"
AUTO_INDICES_TEMPLATES = ("market_report",)  # ... and the other indices and the industries' moves, from TSE
INDICES_SOURCES = ("tse_indices", "tse_farabourse")
STALE_HISTORY_DAYS = 6
MAX_REVISIONS = 3  # how many times the owner may send one piece of content back
OWNER_NOTE_MAX = 500


def run_id_for(org_id: str, request: str, day: date) -> str:
    digest = hashlib.sha256(" ".join(request.split()).encode()).hexdigest()[:12]
    return f"{org_id}:req:{day.isoformat()}:{digest}"


RESUMABLE = ("running", "planning", "awaiting_approval", "approved", "rejected")


def fresh_run_id(sessions, base: str, *, new: bool = False) -> str:
    """The same request on the same day is one run (so a post is never made twice). But a run that failed, was
    blocked or needed input must not make a corrected re-run return the old failure: that one gets a new attempt id.
    new=True (testing) always starts another attempt, even if an earlier one is still waiting for approval."""
    with sessions() as s:
        n = 0
        while True:
            rid = base if n == 0 else f"{base}:r{n}"
            run = s.get(Run, rid)
            if run is None or (run.status in RESUMABLE and not new):
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


def _topics(org_id: str) -> list[tuple[str, str]]:
    try:
        got = load_knowledge(org_id, rt().brand_root)
    except KnowledgeError:
        return []
    return [(t.id, t.title) for t in got[0].topics] if got else []


@DBOS.step(retries_allowed=True, max_attempts=2, interval_seconds=30)
async def s_plan(org_id: str, run_id: str, request: str, day_iso: str, platforms: list[str], preset: dict | None = None) -> dict:
    """The Manager plans the request. A `preset` (an item of an approved content plan) is already a template and its
    parameters: it is checked by the registry exactly like the Manager's answer, with no model call."""
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    policy = OrgPolicy.from_brand(brand, tuple(platforms))
    if preset is not None:
        try:
            plan = _registry().instantiate(preset["template"], dict(preset.get("params") or {}), today=date.fromisoformat(day_iso),
                                           policy=policy, summary_fa=str(preset.get("summary_fa") or ""))
        except RegistryError as e:
            return {"action": "decline", "message": f"این مورد برنامه قابل اجرا نیست: {e}"}
    else:
        try:
            d = await plan_request(r.gateway, _registry(), request, org_id=org_id, run_id=run_id,
                                   today=date.fromisoformat(day_iso), policy=policy, topics=_topics(org_id))
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


def _note(rule_id: str, message: str) -> dict:
    return {"severity": "warn", "rule_id": rule_id, "message": message, "field": "data"}


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


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=20)
async def s_fetch_history(subject: str) -> dict:
    """Fetch the daily history of a market index from TSE and keep it as a snapshot. Not an index we know, or TSE
    not reachable after the retries: {"error"}; the technical analyst then uses the last history it has, if any."""
    r = rt()
    name = tse.canonical_index(subject)
    if name is None:
        return {"error": "برای این موضوع منبع خودکار نداریم"}
    errors = []
    for ep in tse.endpoints(tse.INDEX_CODES[name], r.tse_old_base, r.tse_cdn_base):
        try:
            raw = await r.fetcher(ep.url)
            bars = tse.PARSERS[ep.parser](raw)
        except AcquisitionError as e:
            errors.append(f"{ep.parser}: {e}")
            continue
        except (tse.TseError, ValueError) as e:
            errors.append(f"{ep.parser}: {e}")
            continue
        with r.sessions.begin() as s:
            sid = record_import(s, r.assets, source_id=price_source(name), raw=raw, parser=ep.parser, now=r.clock(),
                                label=f"tse:{tse.INDEX_CODES[name]}")
        return {"snapshot_id": sid, "subject": name, "last_day": bars[-1].day.isoformat(), "days": len(bars)}
    if any("دریافت ناموفق" in e or "HTTP 5" in e or "HTTP 429" in e for e in errors):
        raise AcquisitionError("TSE: " + " | ".join(errors), retryable=True)  # the network: DBOS tries again
    return {"error": "TSE: " + " | ".join(errors)}


@DBOS.step()
def s_technical(subject: str, market_snapshot_id: str | None = None, day_iso: str | None = None) -> dict:
    r = rt()
    subject = tse.canonical_index(subject) or subject
    with r.sessions.begin() as s:
        sid = _latest_snapshot(s, price_source(subject))
        if sid is None:
            return {"missing": f"سابقهٔ قیمت «{subject}» در دسترس نیست (از TSE دریافت نشد و فایلی هم وارد نشده است)"}
        unit = "point" if "شاخص" in subject else "IRR"
        today = _today_close(s, subject, market_snapshot_id, day_iso)
        part = technical_part(s, r.assets, history_snapshot_id=sid, subject=subject, price_unit=unit, now=r.clock(), today=today)
        last_day = max(load_bars(s, r.assets, sid)[-1].day, today[0] if today and not part.notes else date.min)
    return {"part": _part_json(part), "last_day": last_day.isoformat(), "notes": part.notes}


TEHRAN = ZoneInfo("Asia/Tehran")
TODAY_METRIC = {"شاخص کل": "index.total.value"}  # the market page's closing value for an index we track


def _today_close(s, subject: str, market_snapshot_id: str | None, day_iso: str | None):
    """(day, value, point id) of today's closing value from today's market page, or None."""
    metric = TODAY_METRIC.get(subject)
    if not (metric and market_snapshot_id and day_iso):
        return None
    row = s.execute(select(DataPointRow).where(DataPointRow.snapshot_id == market_snapshot_id,
                                                DataPointRow.metric == metric)).scalars().first()
    if row is None or row.as_of.astimezone(TEHRAN).date().isoformat() != day_iso:
        return None
    return (date.fromisoformat(day_iso), row.value, row.id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=20, backoff_rate=2)
async def s_acquire_extra(source_id: str) -> dict:
    """A second source for the same report (it does not become the run's main snapshot)."""
    r = rt()
    if source_id not in r.sources:
        return {"error": f"منبع «{source_id}» تنظیم نشده است"}
    try:
        with r.sessions.begin() as s:
            got = await acquire(s, r.assets, r.sources[source_id], r.fetcher, r.clock())
    except AcquisitionError as e:
        if e.retryable:
            raise
        return {"error": str(e)}
    return {"snapshot_id": got.snapshot_id, "warnings": got.warnings}


@DBOS.step()
def s_fx(snapshot_id: str) -> dict:
    with rt().sessions.begin() as s:
        part = fx_part(s, snapshot_id=snapshot_id)
    return {"part": _part_json(part)} if part.points else {"missing": "قیمتی از tgju خوانده نشد"}


@DBOS.step()
def s_knowledge(org_id: str, topic: str) -> dict:
    r = rt()
    try:
        got = load_knowledge(org_id, r.brand_root)
        if got is None:
            return {"missing": "این سازمان پایگاه دانش ندارد (orgs/<org>/knowledge.yaml)"}
        kb, raw = got
        t = kb.topic(topic)
        with r.sessions.begin() as s:
            part = knowledge_part(s, r.assets, kb, raw, topic_id=topic, now=r.clock())
    except KnowledgeError as e:
        return {"missing": str(e)}
    task = f"یک پست دربارهٔ «{t.title}» بنویس: {t.brief} فقط از جدول داده و واقعیت‌ها استفاده کن."
    return {"part": _part_json(part), "task": task}


@DBOS.step()
def s_indices(org_id: str, indices_snapshot_id: str | None, fara_snapshot_id: str | None, day_iso: str) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    with r.sessions.begin() as s:
        part = indices_part(s, indices_snapshot_id=indices_snapshot_id, fara_snapshot_id=fara_snapshot_id, brand=brand,
                            run_date=date.fromisoformat(day_iso))
    if not part.points:
        return {"missing": "؛ ".join(part.notes) or "داده‌ای از TSE خوانده نشد"}
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
        bars = load_bars(s, r.assets, px)
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
                  feedback: list[str] | None, attempt: int, task: str = "") -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    try:
        draft = await write_multichannel(r.gateway, brand, AnalysisReport.model_validate(report), org_id=org_id, run_id=run_id,
                                         date_text=long_date(date.fromisoformat(day_iso)), platforms=tuple(platforms),
                                         focus=focus, feedback=feedback, step_key=f"write.{attempt}", task=task)
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
    try:
        vc = verify_multichannel(MultiChannelDraft.model_validate(draft), AnalysisReport.model_validate(report), brand, tuple(platforms))
    except (ValueError, TypeError) as e:  # pydantic's ValidationError is a ValueError
        # A draft the checker cannot even read is the writer's to fix, never a reason to stop the run.
        vc = VerifiedContent(platforms=tuple(platforms), findings=[Finding(
            rule_id="W-SHAPE", severity="block", field="draft",
            message=f"ساختار متن قابل بررسی نبود؛ همان شکل خواسته‌شده را رعایت کن ({str(e).splitlines()[0][:160]})")])
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
    cut = [(im.order, len(im.manifest.get("dropped", []))) for im in images if im.manifest.get("dropped")]
    notes = [("اسلاید " + " و ".join(str(o) for o, _ in cut) + f": {sum(n for _, n in cut)} جملهٔ کوتاه برای جا شدن در تصویر "
              "نیامد (همه در پست و کپشن هست).")] if cut else []
    return {"assets": refs, "chart_points": sorted({pid for s in vc.slides if s.chart for pid in s.chart.sources}),
            "notes": notes}


@DBOS.step()
def q_package(org_id: str, run_id: str, verified: dict, assets: list[dict], chart_points: list[str], destinations: list[list[str]],
              window_minutes: int, risk: str, versions: dict, version: int = 1, supersedes: str | None = None,
              content_class: str = "market_report", not_before_iso: str | None = None) -> dict:
    r = rt()
    brand = load_brand(org_id, r.brand_root)
    vc = _vc(verified)
    used = {c.point_id for c in vc.claims} | set(chart_points)
    now = r.clock()
    not_before = datetime.fromisoformat(not_before_iso) if not_before_iso else None
    if not_before is not None and not_before <= now:
        not_before = None  # the planned time has passed: publish as soon as it is approved
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
            brand=brand, run_id=run_id, content_class=content_class, post=vc.post, instagram_caption=caption,
            slides=[AssetRef.model_validate(a) for a in assets], claims=vc.claims, data_points=refs,
            destinations=[tuple(d) for d in destinations], versions=versions, not_before=not_before,
            expires_at=(not_before or now) + timedelta(minutes=window_minutes),
            risk_level=risk, extra_point_ids=frozenset(chart_points), version=version, supersedes=supersedes)
        store_package(s, pkg, actor=f"workflow:{run_id}")
        run = s.get(Run, run_id)
        run.package_id = pkg.package_id  # the workflow marks it as waiting, together with its approval round
    return {"package_id": pkg.package_id, "package_hash": pkg.package_hash}


async def _produce(org_id: str, run_id: str, plan: ExecutionPlan, report: dict, day: str, platforms: list[str], focus: str,
                   dests: list[list[str]], window_minutes: int, owner_note: str | None, attempts_used: int, *,
                   version: int, supersedes: str | None, task: str = "", publish_at_iso: str | None = None) -> dict:
    """One version of the content: write (with a retry on blocking findings), check, review, design, package.
    Returns {"pkg", "findings"} or, when no publishable version came out, {"status", "findings", "detail", "why"}."""
    note = [f"درخواست صاحب رسانه برای نسخهٔ جدید: {owner_note}"] if owner_note else []
    feedback: list[str] | None = note or None
    verified: dict = {}
    findings: list[dict] = []
    good: tuple[dict, list[dict]] | None = None  # an acceptable draft kept while a polished one is tried
    for attempt in range(attempts_used + 1, attempts_used + ATTEMPTS_PER_VERSION + 1):
        try:
            written = await q_write(org_id, run_id, report, day, platforms, focus, feedback, attempt, task)
        except Exception as e:
            return {"status": "failed", "detail": {"error": f"write: {e}"}, "why": f"نویسنده: {e}"}
        if "error" in written:
            return {"status": "failed", "detail": {"error": written["error"]}, "why": written["error"]}
        verified = s_check(org_id, report, written["draft"], platforms)
        findings = list(verified["findings"])
        if not verified["blocked"]:
            if plan.has("edit"):
                findings += await s_review(org_id, run_id, report, verified, attempt, "editor")
            if plan.has("comply"):
                findings += await s_review(org_id, run_id, report, verified, attempt, "compliance")
        blocking = [f["message"] for f in findings if f["severity"] == "block"]
        if not blocking and not verified["blocked"]:
            # Acceptable. The editor's remarks (vague or illogical sentences, typos) are handed back once, so the
            # owner gets the polished text instead of a list of warnings; the acceptable draft is kept meanwhile.
            remarks = [f["message"] for f in findings if f["rule_id"] == "E-EDITOR" and f["severity"] == "warn"]
            if remarks and good is None and attempt < attempts_used + ATTEMPTS_PER_VERSION:
                good = (verified, findings)
                feedback = [*note, *(f"پیش از انتشار این را بهتر کن: {r}" for r in remarks[:6])]
                continue
            good = None
            break
        if good is not None:  # the polished draft is worse: keep the acceptable one
            verified, findings = good
            findings = [*findings, _note("E-POLISH", "بازنویسی برای رفع ایرادهای ویراستار پذیرفته نشد؛ همان متن قبلی آماده است.")]
            break
        if attempt - attempts_used >= MAX_WRITES:  # rewrites for blocking problems are limited; the extra slot is for polishing
            return {"status": "blocked", "findings": findings, "why": "کنترل کیفیت نسخهٔ جدید را نپذیرفت: " + "؛ ".join(blocking[:3])}
        feedback = [*note, *(blocking or ["متن همهٔ کانال‌ها کامل نبود"])]
    else:
        if good is not None:
            verified, findings = good
        else:
            blocking = [f["message"] for f in findings if f["severity"] == "block"]
            return {"status": "blocked", "findings": findings, "why": "کنترل کیفیت نسخهٔ جدید را نپذیرفت: " + "؛ ".join(blocking[:3])}

    assets: list[dict] = []
    chart_points: list[str] = []
    if plan.has("design"):
        designed = await s_design(org_id, run_id, verified)
        if "error" in designed:
            return {"status": "blocked", "findings": findings, "detail": {"error": f"design: {designed['error']}"},
                    "why": f"طراح: {designed['error']}"}
        assets, chart_points = designed["assets"], designed["chart_points"]
        findings += [{"severity": "warn", "rule_id": "D-FIT", "message": m, "field": "slides"} for m in designed.get("notes", [])]

    risk = "medium" if any(f["severity"] == "warn" for f in findings) else "low"
    versions = {"workflow": WORKFLOW, "plan": f"{plan.template_id}@{plan.template_version}", "manager_prompt": MANAGER_PROMPT,
                "writer_prompt": WRITER_PROMPT, "review_prompt": REVIEW_PROMPT, "carousel": TEMPLATE_VERSION,
                "technical": TECH_V, "fundamental": FUND_V}
    try:
        pkg = q_package(org_id, run_id, verified, assets, chart_points, dests, window_minutes, risk, versions, version, supersedes,
                        plan.template_id, publish_at_iso)
    except Exception as e:  # noqa: BLE001  e.g. the earlier version is already on a channel
        if supersedes is None:
            raise
        return {"status": "blocked", "findings": findings, "why": f"بسته‌بندی: {e}"}
    return {"pkg": {**pkg, "version": version}, "findings": findings}


# ------------------------------------------------------------------ the workflow


@DBOS.workflow()
async def handle_request(org_id: str, request: str, day_iso: str, destinations: list[list[str]], source_id: str = "bourse_trader",
                         approval_timeout_s: int = 3 * 3600, window_minutes: int = 240, preset: dict | None = None,
                         publish_at_iso: str | None = None) -> dict[str, Any]:
    """Any unexpected error ends the run as failed with its reason, so nothing waits on a run that stopped.
    `preset`: an approved content-plan item (template and params; no Manager call). `publish_at_iso`: the planned
    publication time; the approved post waits for it."""
    try:
        return await _handle_request(org_id, request, day_iso, destinations, source_id, approval_timeout_s, window_minutes,
                                     preset, publish_at_iso)
    except Exception as e:  # noqa: BLE001
        return s_finish(DBOS.workflow_id, "failed", detail={"error": f"{type(e).__name__}: {e}"[:600]})


async def _handle_request(org_id: str, request: str, day_iso: str, destinations: list[list[str]], source_id: str = "bourse_trader",
                         approval_timeout_s: int = 3 * 3600, window_minutes: int = 240, preset: dict | None = None,
                         publish_at_iso: str | None = None) -> dict[str, Any]:
    run_id = DBOS.workflow_id
    s_open(run_id, org_id, request)
    destinations = [[*d, "carousel" if d[1] == "instagram" else "post"][:3] if len(d) == 2 else list(d) for d in destinations]
    available = sorted({d[1] for d in destinations})
    try:
        planned = await s_plan(org_id, run_id, request, day_iso, available, preset)
    except Exception as e:
        return s_finish(run_id, "failed", detail={"error": f"manager: {e}"})
    if planned["action"] != "run":
        status = {"clarify": "needs_input", "decline": "declined"}.get(planned["action"], "failed")
        return s_finish(run_id, status, detail={"message": planned["message"]})
    plan = ExecutionPlan.model_validate(planned["plan"])
    params = plan.params
    day = str(params.get("day") or day_iso)
    platforms = list(params.get("platforms") or available)
    dests = [d for d in destinations if d[1] in platforms]
    if any(d[2] == "carousel" for d in dests) and "instagram" not in platforms:
        platforms = [*platforms, "instagram"]  # the slides (and their short caption) are needed for an album

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
    data_notes: list[dict] = []  # told to the owner with the content (not written into it)
    asked = plan.has("technical")
    tech_subject = (params.get("technical_subject") or params.get("subject")) if asked else \
        (AUTO_TECHNICAL if plan.template_id in AUTO_TECHNICAL_TEMPLATES else None)
    if tech_subject:
        if tse.canonical_index(tech_subject):
            try:
                fetched = await s_fetch_history(tech_subject)
            except Exception as e:  # noqa: BLE001  TSE unreachable after retries
                fetched = {"error": f"TSE در دسترس نبود ({type(e).__name__})"}
            if "error" in fetched:
                data_notes.append(_note("DATA-TSE", f"سابقهٔ {tech_subject} از TSE تازه نشد: {fetched['error'][:200]}"))
        out = s_technical(tech_subject, snapshot_id if plan.has("market") else None, day)
        if "missing" in out:
            if asked:
                return s_finish(run_id, "needs_data", detail={"message": out["missing"]})
            data_notes.append(_note("DATA-TECHNICAL", f"بخش تحلیل تکنیکال اضافه نشد: {out['missing']}"))
        else:
            parts.append(out["part"])
            data_notes += [_note("DATA-TODAY", f"عدد امروز {tech_subject} به سابقه اضافه نشد: {n}") for n in out.get("notes", [])]
            if out.get("last_day") and (date.fromisoformat(day) - date.fromisoformat(out["last_day"])).days > STALE_HISTORY_DAYS:
                data_notes.append(_note("DATA-STALE", f"آخرین روز سابقهٔ {tech_subject} {out['last_day']} است؛ تحلیل تکنیکال به‌روز نیست."))
    if plan.template_id in AUTO_FX_TEMPLATES:
        try:
            got_fx = await s_acquire_extra(FX_SOURCE)
        except Exception as e:  # noqa: BLE001  tgju unreachable after the retries
            got_fx = {"error": f"tgju در دسترس نبود ({type(e).__name__})"}
        fx = s_fx(got_fx["snapshot_id"]) if "snapshot_id" in got_fx else {"missing": got_fx["error"][:200]}
        if "part" in fx:
            parts.append(fx["part"])
        else:
            data_notes.append(_note("DATA-FX", f"بخش ارز و طلا اضافه نشد: {fx['missing']}"))
    if plan.template_id in AUTO_INDICES_TEMPLATES:
        got: dict[str, str | None] = {}
        errors = []
        for source_id in INDICES_SOURCES:
            try:
                res = await s_acquire_extra(source_id)
            except Exception as e:  # noqa: BLE001  TSE unreachable after the retries
                res = {"error": f"{source_id} در دسترس نبود ({type(e).__name__})"}
            got[source_id] = res.get("snapshot_id")
            if "error" in res:
                errors.append(res["error"][:150])
        out = s_indices(org_id, got["tse_indices"], got["tse_farabourse"], day) if any(got.values()) else \
            {"missing": "؛ ".join(errors)}
        if "part" in out:
            parts.append(out["part"])
            if errors:
                data_notes.append(_note("DATA-INDICES", f"بخشی از شاخص‌های TSE خوانده نشد: {'؛ '.join(errors)}"))
        else:
            data_notes.append(_note("DATA-INDICES", f"شاخص‌های فرعی و صنایع اضافه نشدند: {out['missing']}"))
    task = ""
    if plan.has("knowledge"):
        out = s_knowledge(org_id, str(params.get("topic") or ""))
        if "missing" in out:
            return s_finish(run_id, "needs_input", detail={"message": out["missing"]})
        parts.append(out["part"])
        task = out["task"]
    if plan.has("fundamental"):
        out = s_fundamental(params.get("fundamental_subject") or params.get("subject"))
        if "missing" in out:
            return s_finish(run_id, "needs_data", detail={"message": out["missing"]})
        parts.append(out["part"])
    report = s_compose(snapshot_id or "imports", parts, excluded, day)["report"]

    # ---- writer -> checks -> designer -> package -> the owner. The owner may send it back with a note; the team then
    # makes a new version from the same data. If a new version cannot be made, the last good one keeps waiting.
    dest_ids = [d[0] for d in dests]
    current: dict | None = None  # the package waiting for a decision
    owner_note: str | None = None
    attempts_used = 0
    for round_no in range(1, MAX_REVISIONS + 2):
        made = await _produce(org_id, run_id, plan, report, day, platforms, str(params.get("focus") or ""), dests,
                              window_minutes, owner_note, attempts_used, version=round_no if current is None else current["version"] + 1,
                              supersedes=None if current is None else current["package_id"], task=task, publish_at_iso=publish_at_iso)
        if "findings" in made:
            made["findings"] = [*data_notes, *(made["findings"] or [])]
        attempts_used += ATTEMPTS_PER_VERSION
        can_revise = round_no <= MAX_REVISIONS
        if "pkg" in made:
            current = made["pkg"]
            s_finish(run_id, "awaiting_approval", findings=made["findings"],
                     detail={"package_id": current["package_id"], "approval_round": round_no, "can_revise": can_revise, "revision_error": None})
        elif current is None:
            return s_finish(run_id, made["status"], findings=made.get("findings"), detail=made.get("detail"))
        else:
            s_finish(run_id, "awaiting_approval",
                     detail={"approval_round": round_no, "can_revise": can_revise, "revision_error": made["why"]})
        await s_request_approval(org_id, run_id, current["package_id"], current["package_hash"])

        for _ in range(MAX_STALE_MESSAGES):
            message = await DBOS.recv_async(TOPIC, timeout_seconds=approval_timeout_s)
            if message is None:
                return s_finish(run_id, "expired", detail={"package_id": current["package_id"]})
            if message.get("package_hash") != current["package_hash"]:
                s_note(run_id, f"ignored a decision for another version ({str(message.get('package_hash'))[:12]})")
                continue
            if message.get("decision") == "revise":
                owner_note = " ".join(str(message.get("reason") or "").split())[:OWNER_NOTE_MAX]
                if can_revise and owner_note:
                    break
                s_note(run_id, "ignored a revision request (no note, or no revisions left)")
                continue
            result = s_decide(org_id, run_id, current["package_id"], current["package_hash"], message, dest_ids)
            return {"run_id": run_id, **result, "package_id": current["package_id"]}
        else:
            return s_finish(run_id, "expired", detail={"reason": "too many decisions for other versions"})
        s_finish(run_id, "running", detail={"revising": owner_note})
    return s_finish(run_id, "expired", detail={"reason": "too many decisions for other versions"})
