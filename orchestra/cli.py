"""Command line for running ORCHESTRA on one computer, until the approval bot exists.

    python -m orchestra.cli init                       create tables, load destinations from orgs/<org>/destinations.yaml
    python -m orchestra.cli ask "گزارش پایان بازار امروز با تحلیل تکنیکال شاخص کل"
                                                       the Manager plans the request and the team carries it to approval
    python -m orchestra.cli import-prices --subject "شاخص کل" prices.csv     price history for technical analysis
    python -m orchestra.cli import-statements statements.yaml               financial statements for fundamental analysis
    python -m orchestra.cli run --org pouyan           run market_close_v1 (Telegram/Bale text only) for today
    python -m orchestra.cli show RUN_ID                the post exactly as it would be sent, with its sources and checks
    python -m orchestra.cli approve RUN_ID [--only tg_test]
    python -m orchestra.cli reject RUN_ID --reason "..."
    python -m orchestra.cli publish                    send every queued job that is due (once)
    python -m orchestra.cli jobs                       list publish jobs and their state
    python -m orchestra.cli confirm-sent JOB_ID        an "unknown" post is on the channel
    python -m orchestra.cli retry JOB_ID               send an "unknown"/"dead"/"failed" job again
    python -m orchestra.cli pause / resume --org pouyan
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select

from .assets import AssetStore
from .data.acquisition import http_fetcher
from .data.sources.bourse_trader import BourseTraderSource
from .db import Destination, PackageRow, PublishJob, Run, create_schema, make_engine, make_sessionmaker
from .llm import AnthropicProvider, Gateway, OpenAICompatibleProvider, Route
from .publishing.instagram import DirectoryHost, InstagramAdapter, NoHost
from .publishing.adapters import BotApiAdapter, MissingTokenAdapter
from .publishing.publisher import Publisher
from .runtime import Runtime, set_runtime
from .settings import Settings, load_settings

TEHRAN = ZoneInfo("Asia/Tehran")
APP_VERSION = "orchestra-1"
DECIDE_TIMEOUT_S = 180


def _sessions(cfg: Settings):
    return make_sessionmaker(make_engine(cfg.database_url))


def _runtime(cfg: Settings) -> Runtime:
    if not cfg.llm_api_key or not cfg.llm_model:
        sys.exit("ORCHESTRA_LLM_API_KEY و ORCHESTRA_LLM_MODEL را در فایل .env پر کنید.")
    from .llm import check_base_url
    try:
        if cfg.llm_provider == "anthropic":
            if cfg.llm_base_url:
                check_base_url(cfg.llm_base_url)
            provider = AnthropicProvider(cfg.llm_api_key, **({"base_url": cfg.llm_base_url} if cfg.llm_base_url else {}))
        else:
            check_base_url(cfg.llm_base_url)
            provider = OpenAICompatibleProvider(cfg.llm_api_key, cfg.llm_base_url)
    except ValueError as e:
        hint = ""
        if cfg.llm_provider != "anthropic" and "gemini" in cfg.llm_model.lower():
            hint = "\nبرای Gemini: ORCHESTRA_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/"
        sys.exit(f"{e}{hint}")
    if cfg.llm_provider == "anthropic" and not cfg.llm_model.lower().startswith("claude"):
        sys.exit("ORCHESTRA_LLM_PROVIDER روی anthropic است ولی مدل Claude نیست. برای Gemini و مدل‌های دیگر بنویسید: ORCHESTRA_LLM_PROVIDER=openai_compatible")

    def tier(model: str) -> list[Route]:
        """`model` may list several models separated by commas: they are tried in that order if one fails."""
        names = [m.strip() for m in (model or cfg.llm_model).split(",") if m.strip()]
        if cfg.llm_fallback_model:
            names += [m.strip() for m in cfg.llm_fallback_model.split(",") if m.strip()]
        return [Route(provider, m) for m in dict.fromkeys(names)]
    routes = tier(cfg.llm_model)
    sessions = _sessions(cfg)
    market = BourseTraderSource()
    if cfg.bourse_trader_url:
        market.url = cfg.bourse_trader_url
    from .data.sources.tgju import TgjuSource
    fx = TgjuSource()
    if cfg.tgju_url:
        fx.url = cfg.tgju_url
    return Runtime(sessions=sessions, assets=AssetStore(cfg.asset_dir),
                   gateway=Gateway(sessions, {"writer": routes, "manager": tier(cfg.manager_model), "reviewer": tier(cfg.reviewer_model)},
                                   max_calls_per_run=cfg.llm_max_calls_per_run),
                   fetcher=http_fetcher(), sources={"bourse_trader": market, "tgju": fx}, brand_root=cfg.orgs_dir,
                   tse_old_base=cfg.tse_old_base, tse_cdn_base=cfg.tse_cdn_base)


def _launch_dbos(cfg: Settings):
    from dbos import DBOS
    # A fixed application version: DBOS otherwise derives it from the code, and a run that waits for approval would
    # never resume after any code update (approve would hang). Bump it only when a workflow's step order changes.
    DBOS(config={"name": "orchestra", "system_database_url": cfg.system_database_url, "log_level": "ERROR",
                 "application_version": APP_VERSION})
    DBOS.launch()
    return DBOS


def leave_runs_waiting(code: int = 0) -> None:
    """Exit while a run waits for approval. DBOS.destroy() would cancel the waiting run and record it as failed,
    so the process ends without it: the run's state is already in the system database and the next process that
    launches DBOS (approve / reject) resumes it from where it stopped."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _check_llm_reachable(cfg: Settings) -> None:
    """Fail in seconds, with a clear reason, instead of hanging when the model service cannot be reached."""
    import httpx

    from .llm import UNREACHABLE_HINT
    url = cfg.llm_base_url or ("https://api.anthropic.com" if cfg.llm_provider == "anthropic" else "")
    if not url:
        return
    try:
        httpx.get(url, timeout=httpx.Timeout(10.0, connect=10.0), follow_redirects=True)  # any HTTP answer means reachable
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as e:
        sys.exit(f"{UNREACHABLE_HINT}.\n({url} — {type(e).__name__})")
    except httpx.HTTPError:
        pass


def _ensure_ready(cfg: Settings, org: str) -> None:
    """First run on a fresh database: create the tables and load destinations, so `init` is never a forgotten step."""
    from sqlalchemy import inspect
    engine = make_engine(cfg.database_url)
    if not inspect(engine).has_table("destinations") or not _has_destinations(cfg, org):
        cmd_init(cfg, argparse.Namespace(org=org))


def _has_destinations(cfg: Settings, org: str) -> bool:
    with _sessions(cfg)() as s:
        return s.execute(select(Destination.id).where(Destination.org_id == org).limit(1)).first() is not None


def _destinations(cfg: Settings, org: str) -> list[list[str]]:
    with _sessions(cfg)() as s:
        rows = s.execute(select(Destination).where(Destination.org_id == org, Destination.enabled.is_(True))).scalars().all()
    if not rows:
        sys.exit(f"هیچ مقصدی برای {org} نیست؛ اول `init` را بزنید.")
    return [[d.id, d.platform, d.content or "post"] for d in rows]


# ------------------------------------------------------------------ commands


def _load_destinations(cfg: Settings, org: str, session) -> int:
    path = cfg.orgs_dir / org / "destinations.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for d in data.get("destinations", []):
        content = d.get("content", "post")
        if content not in ("post", "carousel") or (content == "carousel" and d["platform"] not in ("telegram", "bale")):
            raise KeyError(f"{d['id']}: content باید post باشد، یا carousel برای telegram/bale")
        session.merge(Destination(id=d["id"], org_id=org, platform=d["platform"], chat_id=str(d["chat_id"]),
                                  secret_env=d["secret_env"], enabled=d.get("enabled", True),
                                  content="carousel" if d["platform"] == "instagram" else content))
    return len(data.get("destinations", []))


def _sync_destinations(cfg: Settings) -> None:
    """The YAML file is the source of truth for where content goes. A broken file is reported, never half-applied."""
    if not cfg.orgs_dir.is_dir():
        return
    for org_dir in sorted(p for p in cfg.orgs_dir.iterdir() if (p / "destinations.yaml").is_file()):
        try:
            with _sessions(cfg).begin() as s:
                _load_destinations(cfg, org_dir.name, s)
        except (yaml.YAMLError, KeyError, TypeError) as e:
            sys.exit(f"فایل {org_dir / 'destinations.yaml'} خراب است: {e}")


def cmd_init(cfg: Settings, a) -> None:
    engine = make_engine(cfg.database_url)
    create_schema(engine)
    with make_sessionmaker(engine).begin() as s:
        n = _load_destinations(cfg, a.org, s)
    print(f"جدول‌ها ساخته شد و {n} مقصد برای {a.org} ثبت شد.")


def cmd_run(cfg: Settings, a) -> None:
    from dbos import SetWorkflowID

    from .workflows import market_close as wf
    _ensure_ready(cfg, a.org)
    rtm = _runtime(cfg)
    set_runtime(rtm)
    dbos = _launch_dbos(cfg)
    day = datetime.now(TEHRAN).date() if not a.day else datetime.fromisoformat(a.day).date()
    run_id = wf.run_id_for(a.org, day)
    with SetWorkflowID(run_id):
        text_only = [[d, p] for d, p, content in _destinations(cfg, a.org) if p != "instagram" and content == "post"]
        handle = dbos.start_workflow(wf.market_close, a.org, day.isoformat(), text_only,
                                     approval_timeout_s=a.timeout_hours * 3600)
    _wait_and_report(rtm, dbos, run_id, handle)


def _wait_and_report(rtm, dbos, run_id: str, handle) -> None:
    """Wait until the run needs the owner or has ended. The workflow engine's own status is watched too, so a run
    that stopped for any reason can never leave this loop waiting forever."""
    import time

    from .db import LLMCall
    started, last_note = time.monotonic(), 0.0
    while True:
        with rtm.sessions() as s:
            run = s.get(Run, run_id)
            status = run.status if run else "starting"
            last = s.execute(select(LLMCall.step_key).where(LLMCall.run_id == run_id).order_by(LLMCall.id.desc()).limit(1)).scalar_one_or_none()
        if status not in ("running", "starting", "planning"):
            break
        engine = dbos.get_workflow_status(run_id)
        if engine is not None and engine.status not in ("PENDING", "ENQUEUED"):
            time.sleep(1)  # the run row is written by the last step; give it a moment, then report what the engine says
            with rtm.sessions() as s:
                run = s.get(Run, run_id)
                status = run.status if run else "failed"
            if status in ("running", "starting", "planning"):
                status = "failed"
            break
        if time.monotonic() - last_note >= 15:
            last_note = time.monotonic()
            print(f"… {int(time.monotonic() - started)} ثانیه؛ وضعیت: {status}؛ آخرین مرحلهٔ مدل: {last or '-'}", flush=True)
        time.sleep(1)
    print(f"اجرا: {run_id} → {status}")
    if status == "awaiting_approval":
        print(f"محتوای هر کانال و اسلایدها را ببینید:  python -m orchestra.cli show {run_id}")
        leave_runs_waiting()
    try:
        result = handle.get_result()
    except Exception as e:  # noqa: BLE001  the engine recorded an error: show it plainly, no traceback
        result = {"run_id": run_id, "status": "failed", "error": f"{type(e).__name__}: {str(e)[:500]}"}
    if result.get("message"):
        print(result["message"])
    if result.get("error"):
        print(f"خطا: {result['error']}")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    dbos.destroy()


def cmd_ask(cfg: Settings, a) -> None:
    from dbos import SetWorkflowID

    from .workflows import request as wf
    _ensure_ready(cfg, a.org)
    rtm = _runtime(cfg)
    _check_llm_reachable(cfg)
    set_runtime(rtm)
    dbos = _launch_dbos(cfg)
    day = datetime.now(TEHRAN).date() if not a.day else datetime.fromisoformat(a.day).date()
    run_id = wf.fresh_run_id(rtm.sessions, wf.run_id_for(a.org, a.request, day), new=a.new)
    with SetWorkflowID(run_id):
        handle = dbos.start_workflow(wf.handle_request, a.org, a.request, day.isoformat(), _destinations(cfg, a.org),
                                     approval_timeout_s=a.timeout_hours * 3600)
    _wait_and_report(rtm, dbos, run_id, handle)


def cmd_import(cfg: Settings, a, kind: str) -> None:
    from .data.imports import parse_price_csv, parse_statements_yaml, record_import
    from .db import utcnow
    from .workflows.request import price_source, statements_source
    raw = open(a.file, "rb").read()
    if kind == "prices":
        bars = parse_price_csv(raw)  # refuse a broken file before storing it
        source, parser, note = price_source(a.subject), "price_csv@1", f"{len(bars)} روز، از {bars[0].day} تا {bars[-1].day}"
    else:
        statements = parse_statements_yaml(raw)
        source, parser, note = statements_source(statements[0].symbol), "statements_yaml@1", f"{len(statements)} دوره برای {statements[0].symbol}"
    with _sessions(cfg).begin() as s:
        sid = record_import(s, AssetStore(cfg.asset_dir), source_id=source, raw=raw, parser=parser, now=utcnow(), label=os.path.basename(a.file))
    print(f"وارد شد: {note}  ({sid})")


def cmd_show(cfg: Settings, a) -> None:
    with _sessions(cfg)() as s:
        run = s.get(Run, a.run_id)
        if run is None:
            sys.exit("این اجرا پیدا نشد.")
        print(f"وضعیت: {run.status}")
        for f in run.findings or []:
            print(f"  [{f['severity']}] {f['rule_id']}: {f['message']}")
        if not run.package_id:
            return
        pkg = s.get(PackageRow, run.package_id)
    content = json.loads(pkg.content_json)
    print(f"بسته: {pkg.package_id}  hash: {pkg.package_hash[:16]}…  نسخه: {pkg.version}")
    for d in content["destinations"]:
        print(f"\n===== {d['destination_id']} ({d['platform']}) =====")
        print(d["delivery_plan"][0]["body"])
    if content.get("assets"):
        out = os.path.join("previews", a.run_id.replace(":", "_"))
        os.makedirs(out, exist_ok=True)
        store = AssetStore(cfg.asset_dir)
        for ref in content["assets"]:
            with open(os.path.join(out, f"{ref['order']:02d}.jpg"), "wb") as f:
                f.write(store.get(ref["sha256"]))
        print(f"\n🖼  {len(content['assets'])} اسلاید اینستاگرام در پوشهٔ {out}")
    print("\n===== منبع هر عدد =====")
    for p in content["sources"]["data_points"]:
        print(f"  {p['metric']}{' / ' + p['subject'] if p.get('subject') else ''}: «{p['source_text']}» از {p['source_id']} (پاسخ خام {p['raw_sha256'][:12]})")


def _decide(cfg: Settings, a, decision: str) -> None:
    from .workflows import market_close as wf
    from .workflows import request as _request  # noqa: F401  (registers handle_request so a waiting run can resume)
    with _sessions(cfg)() as s:
        run = s.get(Run, a.run_id)
        if run is None or run.status != "awaiting_approval":
            sys.exit("این اجرا منتظر تأیید نیست.")
        pkg = s.get(PackageRow, run.package_id)
    set_runtime(_runtime(cfg))
    dbos = _launch_dbos(cfg)  # recovers the waiting run in this process
    status = dbos.get_workflow_status(a.run_id)
    if status is None or status.app_version != APP_VERSION:
        dbos.destroy()
        sys.exit("این اجرا با نسخهٔ قدیمی‌تر برنامه ساخته شده و دیگر قابل ادامه نیست. همان درخواست را دوباره اجرا کنید (run_ask.bat).")
    wf.submit_decision(a.run_id, pkg.package_hash, decision, f"user:{os.environ.get('USERNAME') or os.environ.get('USER') or 'owner'}",
                       destinations=a.only or None, reason=getattr(a, "reason", None))
    import time
    deadline = time.monotonic() + DECIDE_TIMEOUT_S
    while time.monotonic() < deadline:
        st = dbos.get_workflow_status(a.run_id)
        if st is not None and st.status not in ("PENDING", "ENQUEUED"):
            break
        time.sleep(0.5)
    else:
        print("تصمیم ثبت شد ولی اجرا در زمان معقول تمام نشد؛ وضعیت را با `show` ببینید.")
        leave_runs_waiting(1)
    try:
        print(json.dumps(dbos.retrieve_workflow(a.run_id).get_result(), ensure_ascii=False, indent=1))
    except Exception as e:  # the run itself failed after the decision; show why instead of a traceback
        print(f"اجرا پس از تصمیم با خطا تمام شد: {type(e).__name__}: {str(e)[:300]}")
    dbos.destroy()


def cmd_revise(cfg: Settings, a) -> None:
    """Send waiting content back to the team with a note; wait for the new version (or for why there is none)."""
    import time

    from .workflows import market_close as wf
    from .workflows import request as _request  # noqa: F401
    note = " ".join(a.note.split())
    with _sessions(cfg)() as s:
        run = s.get(Run, a.run_id)
        if run is None or run.status != "awaiting_approval":
            sys.exit("این اجرا منتظر تأیید نیست.")
        if not note or not (run.detail or {}).get("can_revise"):
            sys.exit("برای این اجرا اصلاح ممکن نیست (توضیح خالی است، یا دیگر نوبت اصلاحی نمانده).")
        pkg = s.get(PackageRow, run.package_id)
        before = int((run.detail or {}).get("approval_round") or 0)
    rtm = _runtime(cfg)
    set_runtime(rtm)
    dbos = _launch_dbos(cfg)
    status = dbos.get_workflow_status(a.run_id)
    if status is None or status.app_version != APP_VERSION:
        dbos.destroy()
        sys.exit("این اجرا با نسخهٔ قدیمی‌تر برنامه ساخته شده و دیگر قابل ادامه نیست. همان درخواست را دوباره اجرا کنید.")
    wf.submit_decision(a.run_id, pkg.package_hash, "revise", f"user:{os.environ.get('USERNAME') or os.environ.get('USER') or 'owner'}", reason=note)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        with rtm.sessions() as s:
            run = s.get(Run, a.run_id)
        d = run.detail or {}
        if run.status == "awaiting_approval" and int(d.get("approval_round") or 0) > before:
            if d.get("revision_error"):
                print(f"نسخهٔ جدید ساخته نشد: {d['revision_error']}\nنسخهٔ قبلی همچنان منتظر تأیید است.")
            else:
                print(f"نسخهٔ جدید آماده است:  python -m orchestra.cli show {a.run_id}")
            leave_runs_waiting()
        if run.status not in ("awaiting_approval", "running"):
            print(f"اجرا: {a.run_id} → {run.status}")
            leave_runs_waiting(1)
        time.sleep(0.5)
    print("اصلاح ثبت شد ولی نسخهٔ جدید هنوز آماده نیست؛ بعداً `show` را بزنید.")
    leave_runs_waiting(1)


def _publisher(cfg: Settings) -> Publisher:
    def adapter(dest: Destination):
        token = os.environ.get(dest.secret_env, "")
        if not token:
            return MissingTokenAdapter(dest.secret_env)
        if dest.platform == "instagram":
            host = DirectoryHost(cfg.public_asset_dir, cfg.public_asset_url) if cfg.public_asset_dir and cfg.public_asset_url else NoHost()
            return InstagramAdapter(ig_user_id=dest.chat_id, token=token, host=host, timeout=cfg.send_timeout_seconds)
        base = {"telegram": cfg.telegram_api_base, "bale": cfg.bale_api_base}.get(dest.platform) or None
        return BotApiAdapter(platform=dest.platform, token=token, chat_id=dest.chat_id, timeout=cfg.send_timeout_seconds, base_url=base)
    return Publisher(_sessions(cfg), AssetStore(cfg.asset_dir), adapter_factory=adapter,
                     max_attempts=cfg.max_publish_attempts, retry_delays=cfg.retry_delays_seconds)


def cmd_publish(cfg: Settings, a) -> None:
    pub = _publisher(cfg)
    recovered = pub.recover_sending()
    if recovered:
        print(f"⚠️ این کارها وسط ارسال قطع شده بودند و «نامعلوم» شدند: {recovered}")
    results = asyncio.run(pub.run_due())
    print(json.dumps(results, ensure_ascii=False) if results else "کاری برای ارسال نیست.")


class DbosEngine:
    """The control bot's way to the workflow engine and the publisher (see orchestra.control.service.Engine)."""

    def __init__(self, cfg: Settings, rtm: Runtime, dbos, publisher: Publisher):
        self.cfg, self.rtm, self.dbos, self.publisher = cfg, rtm, dbos, publisher

    def start_request(self, org_id: str, text: str, *, new: bool) -> str:
        from dbos import SetWorkflowID

        from .workflows import request as wf
        day = datetime.now(TEHRAN).date()
        run_id = wf.fresh_run_id(self.rtm.sessions, wf.run_id_for(org_id, text, day), new=new)
        with SetWorkflowID(run_id):
            self.dbos.start_workflow(wf.handle_request, org_id, text, day.isoformat(), _destinations(self.cfg, org_id))
        return run_id

    def can_resume(self, run_id: str) -> bool:
        st = self.dbos.get_workflow_status(run_id)
        return st is not None and st.app_version == APP_VERSION and st.status in ("PENDING", "ENQUEUED")

    def is_alive(self, run_id: str) -> bool:
        st = self.dbos.get_workflow_status(run_id)
        return st is None or st.status in ("PENDING", "ENQUEUED")  # None: not recorded yet, it has just been started

    def decide(self, run_id: str, package_hash: str, decision: str, actor: str, reason: str | None) -> dict:
        import time

        from .workflows import market_close as wf
        wf.submit_decision(run_id, package_hash, decision, actor, reason=reason)
        if decision == "revise":
            return {"status": "revising"}  # the run keeps going; the new version arrives as a new approval round
        deadline = time.monotonic() + DECIDE_TIMEOUT_S
        while time.monotonic() < deadline:
            st = self.dbos.get_workflow_status(run_id)
            if st is not None and st.status not in ("PENDING", "ENQUEUED"):
                return self.dbos.retrieve_workflow(run_id).get_result()
            time.sleep(0.3)
        raise TimeoutError("the run did not finish after the decision")

    def publish_due(self) -> dict[int, str]:
        return asyncio.run(self.publisher.run_due())

    def set_paused(self, org_id: str, paused: bool, actor: str) -> None:
        self.publisher.set_paused(org_id, paused, actor)

    def confirm_sent(self, job_id: int, actor: str) -> bool:
        return self.publisher.confirm_sent(job_id, actor)

    def requeue(self, job_id: int, actor: str) -> bool:
        return self.publisher.requeue(job_id, actor)


def cmd_serve(cfg: Settings, a) -> None:
    """The always-on mode: control bot + daily schedule + publisher, in one process."""
    import logging

    from .control.botapi import BotApi
    from .control.service import ControlService, load_schedule
    from .workflows import market_close as _mc  # noqa: F401  (both workflows registered: waiting runs resume here)
    from .workflows import request as _rq  # noqa: F401
    if not cfg.control_bot_token:
        sys.exit("توکن ربات کنترل تنظیم نشده است. در .env بنویسید: ORCHESTRA_CONTROL_BOT_TOKEN=...  (راهنما در README، بخش «ربات کنترل»)")
    if cfg.control_platform not in ("telegram", "bale"):
        sys.exit("ORCHESTRA_CONTROL_PLATFORM باید telegram یا bale باشد.")
    owners = tuple(x.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")) for x in cfg.control_chat_ids.split(",") if x.strip())
    if any(not re.fullmatch(r"-?\d+", o) for o in owners):
        sys.exit("ORCHESTRA_CONTROL_CHAT_IDS باید فقط عدد باشد (چند شناسه با کاما).")
    try:
        schedule = load_schedule(cfg.orgs_dir / a.org / "schedule.yaml")
    except (ValueError, yaml.YAMLError) as e:
        sys.exit(str(e))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore"):  # their INFO lines print full request addresses, which contain bot tokens
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _ensure_ready(cfg, a.org)
    rtm = _runtime(cfg)
    set_runtime(rtm)
    dbos = _launch_dbos(cfg)
    pub = _publisher(cfg)
    recovered = pub.recover_sending()
    base = {"telegram": cfg.telegram_api_base, "bale": cfg.bale_api_base}[cfg.control_platform] or None
    api = BotApi(cfg.control_platform, cfg.control_bot_token, base_url=base)
    from .control.botapi import BotApiError
    try:
        api.call("getMe")
    except BotApiError as e:
        if not e.retryable:
            sys.exit(f"ربات کنترل توکن را نپذیرفت ({e}). ORCHESTRA_CONTROL_BOT_TOKEN و ORCHESTRA_CONTROL_PLATFORM را در .env بررسی کنید.")
        hint = " تلگرام از ایران بدون VPN در دسترس نیست؛ یا VPN را روشن کنید یا ربات را در بله بسازید." if cfg.control_platform == "telegram" else ""
        print(f"⚠️ فعلاً به {cfg.control_platform} وصل نشدم ({e}).{hint} به تلاش ادامه می‌دهم…", flush=True)
    svc = ControlService(org_id=a.org, api=api, engine=DbosEngine(cfg, rtm, dbos, pub), sessions=rtm.sessions,
                         assets=rtm.assets, owners=owners, schedule=schedule, poll_seconds=cfg.control_poll_seconds)
    print(f"ربات کنترل روشن است ({cfg.control_platform}). برای خاموش کردن، همین پنجره را ببندید.", flush=True)
    if not owners:
        print("هنوز هیچ شناسه‌ای مجاز نیست: به ربات یک پیام بدهید تا شناسه‌تان را بگوید، بعد آن را در ORCHESTRA_CONTROL_CHAT_IDS بنویسید.", flush=True)
    for e in schedule:
        print(f"زمان‌بندی: {e.name} ساعت {e.at[0]:02d}:{e.at[1]:02d} — {e.request[:60]}", flush=True)
    try:
        if recovered and owners:
            svc._say(None, f"⚠️ این ارسال‌ها وسط کار قطع شده بودند و «نامعلوم» شدند: {recovered}\n/jobs را ببینید.")
        svc.run_forever()
    except KeyboardInterrupt:
        pass
    leave_runs_waiting()


def cmd_jobs(cfg: Settings, a) -> None:
    with _sessions(cfg)() as s:
        for j in s.execute(select(PublishJob).order_by(PublishJob.id.desc()).limit(30)).scalars():
            print(f"#{j.id} {j.destination_id:<10} {j.status:<10} تلاش {j.attempts} {j.mode or ''} {j.message_ids or ''} {j.error or ''}")


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    cfg = load_settings()
    ap = argparse.ArgumentParser(prog="orchestra", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--org", default="pouyan")
    p = sub.add_parser("run"); p.add_argument("--org", default="pouyan"); p.add_argument("--day"); p.add_argument("--timeout-hours", type=int, default=3)
    p = sub.add_parser("ask"); p.add_argument("request"); p.add_argument("--org", default="pouyan"); p.add_argument("--day")
    p.add_argument("--timeout-hours", type=int, default=3)
    p.add_argument("--new", action="store_true", help="start a new attempt even if the same request is already waiting")
    p = sub.add_parser("import-prices"); p.add_argument("file"); p.add_argument("--subject", required=True)
    p = sub.add_parser("import-statements"); p.add_argument("file")
    p = sub.add_parser("show"); p.add_argument("run_id")
    p = sub.add_parser("approve"); p.add_argument("run_id"); p.add_argument("--only", nargs="*")
    p = sub.add_parser("reject"); p.add_argument("run_id"); p.add_argument("--reason", default=None); p.set_defaults(only=None)
    p = sub.add_parser("revise"); p.add_argument("run_id"); p.add_argument("--note", required=True)
    sub.add_parser("publish")
    p = sub.add_parser("serve"); p.add_argument("--org", default="pouyan")
    sub.add_parser("jobs")
    p = sub.add_parser("confirm-sent"); p.add_argument("job_id", type=int)
    p = sub.add_parser("retry"); p.add_argument("job_id", type=int)
    p = sub.add_parser("pause"); p.add_argument("--org", default="pouyan")
    p = sub.add_parser("resume"); p.add_argument("--org", default="pouyan")
    a = ap.parse_args(argv)
    if a.cmd != "init":
        create_schema(make_engine(cfg.database_url))  # every command works on a fresh database (idempotent)
        _sync_destinations(cfg)  # edits to orgs/<org>/destinations.yaml take effect on the next command
    actor = f"user:{os.environ.get('USERNAME') or os.environ.get('USER') or 'owner'}"
    if a.cmd == "init":
        cmd_init(cfg, a)
    elif a.cmd == "run":
        cmd_run(cfg, a)
    elif a.cmd == "ask":
        cmd_ask(cfg, a)
    elif a.cmd == "import-prices":
        cmd_import(cfg, a, "prices")
    elif a.cmd == "import-statements":
        cmd_import(cfg, a, "statements")
    elif a.cmd == "show":
        cmd_show(cfg, a)
    elif a.cmd == "approve":
        _decide(cfg, a, "approved")
    elif a.cmd == "reject":
        _decide(cfg, a, "rejected")
    elif a.cmd == "revise":
        cmd_revise(cfg, a)
    elif a.cmd == "publish":
        cmd_publish(cfg, a)
    elif a.cmd == "serve":
        cmd_serve(cfg, a)
    elif a.cmd == "jobs":
        cmd_jobs(cfg, a)
    elif a.cmd == "confirm-sent":
        print("ثبت شد." if _publisher(cfg).confirm_sent(a.job_id, actor) else "این کار در وضعیت «نامعلوم» نیست.")
    elif a.cmd == "retry":
        print("دوباره در صف قرار گرفت." if _publisher(cfg).requeue(a.job_id, actor) else "این کار قابل ارسال دوباره نیست.")
    elif a.cmd in ("pause", "resume"):
        _publisher(cfg).set_paused(a.org, a.cmd == "pause", actor)
        print("⏸ همهٔ انتشارها متوقف شد." if a.cmd == "pause" else "▶️ انتشار ادامه پیدا کرد.")
    return 0


def _load_dotenv() -> None:
    """Put .env values (tokens) into the environment without overriding real environment variables."""
    path = ".env"
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8-sig"):  # Notepad may save a BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if __name__ == "__main__":
    sys.exit(main())
