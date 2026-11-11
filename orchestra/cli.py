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
    return Runtime(sessions=sessions, assets=AssetStore(cfg.asset_dir),
                   gateway=Gateway(sessions, {"writer": routes, "manager": tier(cfg.manager_model), "reviewer": tier(cfg.reviewer_model)},
                                   max_calls_per_run=cfg.llm_max_calls_per_run),
                   fetcher=http_fetcher(), sources={"bourse_trader": BourseTraderSource()}, brand_root=cfg.orgs_dir)


def _launch_dbos(cfg: Settings):
    from dbos import DBOS
    DBOS(config={"name": "orchestra", "system_database_url": cfg.system_database_url, "log_level": "WARNING"})
    DBOS.launch()
    return DBOS


def leave_runs_waiting(code: int = 0) -> None:
    """Exit while a run waits for approval. DBOS.destroy() would cancel the waiting run and record it as failed,
    so the process ends without it: the run's state is already in the system database and the next process that
    launches DBOS (approve / reject) resumes it from where it stopped."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


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
    return [[d.id, d.platform] for d in rows]


# ------------------------------------------------------------------ commands


def cmd_init(cfg: Settings, a) -> None:
    engine = make_engine(cfg.database_url)
    create_schema(engine)
    path = cfg.orgs_dir / a.org / "destinations.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    with make_sessionmaker(engine).begin() as s:
        for d in data.get("destinations", []):
            s.merge(Destination(id=d["id"], org_id=a.org, platform=d["platform"], chat_id=str(d["chat_id"]),
                                secret_env=d["secret_env"], enabled=d.get("enabled", True)))
    print(f"جدول‌ها ساخته شد و {len(data.get('destinations', []))} مقصد برای {a.org} ثبت شد.")


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
        handle = dbos.start_workflow(wf.market_close, a.org, day.isoformat(), _destinations(cfg, a.org),
                                     approval_timeout_s=a.timeout_hours * 3600)
    import time
    while True:
        with rtm.sessions() as s:
            run = s.get(Run, run_id)
            status = run.status if run else "starting"
        if status != "running" and status != "starting":
            break
        time.sleep(1)
    print(f"اجرا: {run_id} → {status}")
    if status == "awaiting_approval":
        print(f"متن را ببینید:  python -m orchestra.cli show {run_id}")
        leave_runs_waiting()
    print(json.dumps(handle.get_result(), ensure_ascii=False, indent=1))
    dbos.destroy()


def _wait_and_report(rtm, dbos, run_id: str, handle) -> None:
    import time
    while True:
        with rtm.sessions() as s:
            run = s.get(Run, run_id)
            status = run.status if run else "starting"
        if status not in ("running", "starting", "planning"):
            break
        time.sleep(1)
    print(f"اجرا: {run_id} → {status}")
    if status == "awaiting_approval":
        print(f"محتوای هر کانال و اسلایدها را ببینید:  python -m orchestra.cli show {run_id}")
        leave_runs_waiting()
    result = handle.get_result()
    if result.get("message"):
        print(result["message"])
    print(json.dumps(result, ensure_ascii=False, indent=1))
    dbos.destroy()


def cmd_ask(cfg: Settings, a) -> None:
    from dbos import SetWorkflowID

    from .workflows import request as wf
    _ensure_ready(cfg, a.org)
    rtm = _runtime(cfg)
    set_runtime(rtm)
    dbos = _launch_dbos(cfg)
    day = datetime.now(TEHRAN).date() if not a.day else datetime.fromisoformat(a.day).date()
    run_id = wf.fresh_run_id(rtm.sessions, wf.run_id_for(a.org, a.request, day))
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
    wf.submit_decision(a.run_id, pkg.package_hash, decision, f"user:{os.environ.get('USERNAME') or os.environ.get('USER') or 'owner'}",
                       destinations=a.only or None, reason=getattr(a, "reason", None))
    print(json.dumps(dbos.retrieve_workflow(a.run_id).get_result(), ensure_ascii=False, indent=1))
    dbos.destroy()


def _publisher(cfg: Settings) -> Publisher:
    def adapter(dest: Destination):
        token = os.environ.get(dest.secret_env, "")
        if not token:
            return MissingTokenAdapter(dest.secret_env)
        if dest.platform == "instagram":
            host = DirectoryHost(cfg.public_asset_dir, cfg.public_asset_url) if cfg.public_asset_dir and cfg.public_asset_url else NoHost()
            return InstagramAdapter(ig_user_id=dest.chat_id, token=token, host=host, timeout=cfg.send_timeout_seconds)
        return BotApiAdapter(platform=dest.platform, token=token, chat_id=dest.chat_id, timeout=cfg.send_timeout_seconds)
    return Publisher(_sessions(cfg), AssetStore(cfg.asset_dir), adapter_factory=adapter,
                     max_attempts=cfg.max_publish_attempts, retry_delays=cfg.retry_delays_seconds)


def cmd_publish(cfg: Settings, a) -> None:
    pub = _publisher(cfg)
    recovered = pub.recover_sending()
    if recovered:
        print(f"⚠️ این کارها وسط ارسال قطع شده بودند و «نامعلوم» شدند: {recovered}")
    results = asyncio.run(pub.run_due())
    print(json.dumps(results, ensure_ascii=False) if results else "کاری برای ارسال نیست.")


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
    p = sub.add_parser("import-prices"); p.add_argument("file"); p.add_argument("--subject", required=True)
    p = sub.add_parser("import-statements"); p.add_argument("file")
    p = sub.add_parser("show"); p.add_argument("run_id")
    p = sub.add_parser("approve"); p.add_argument("run_id"); p.add_argument("--only", nargs="*")
    p = sub.add_parser("reject"); p.add_argument("run_id"); p.add_argument("--reason", default=None); p.set_defaults(only=None)
    sub.add_parser("publish")
    sub.add_parser("jobs")
    p = sub.add_parser("confirm-sent"); p.add_argument("job_id", type=int)
    p = sub.add_parser("retry"); p.add_argument("job_id", type=int)
    p = sub.add_parser("pause"); p.add_argument("--org", default="pouyan")
    p = sub.add_parser("resume"); p.add_argument("--org", default="pouyan")
    a = ap.parse_args(argv)
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
    elif a.cmd == "publish":
        cmd_publish(cfg, a)
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
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if __name__ == "__main__":
    sys.exit(main())
