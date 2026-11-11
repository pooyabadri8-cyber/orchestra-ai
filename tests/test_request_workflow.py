"""handle_request end to end on DBOS: Manager -> analysts -> writer -> checks -> reviewers -> designer -> package ->
approval -> publish queue, with a scripted model, the real market page and real slide rendering."""
import json
from datetime import date

import pytest
from dbos import DBOS, SetWorkflowID
from sqlalchemy import select

import orchestra.workflows.request as wf
from orchestra.db import Destination, LLMCall, PackageRow, PublishJob, Run
from orchestra.data.imports import record_import
from orchestra.llm import Gateway, Route
from orchestra.runtime import set_runtime
from orchestra.workflows.market_close import submit_decision
from test_specialists import synthetic_csv
from wf_support import ScriptedProvider, fresh_system_db, make_runtime, postgres_available, wait_for_status

pytestmark = pytest.mark.skipif(not postgres_available(), reason="workflow tests need Postgres")

DAY = "2026-11-11"
DEST = [["tg_test", "telegram"], ["bale_test", "bale"], ["ig_test", "instagram"]]
REQUEST = "گزارش پایان بازار امروز رو برای همهٔ کانال‌ها آماده کن"

PLAN = {"action": "run", "template": "market_report", "params": {}, "summary_fa": "گزارش پایان بازار برای همهٔ کانال‌ها"}
DRAFT = {
    "post": {"hashtag": "#تحلیل_بازار", "headline": "رشد {{P3|abs_pct}} شاخص کل در {{D1|name}}",
             "paragraphs": ["شاخص کل با رشد {{P3|abs_pct}} به {{P1|int}} واحد رسید و ارزش معاملات خرد {{P4|money}} بود.",
                            "در معاملات خرد {{P5|abs_money}} پول حقیقی وارد شد و گروه {{P7|name}} بیشترین ورود را داشت."],
             "cta": "نظرتان را بنویسید."},
    "instagram": {"paragraphs": ["شاخص کل امروز {{P3|abs_pct}} رشد کرد.", "خلاصهٔ بازار در اسلایدها."], "hashtags": ["#بورس", "#شاخص_کل"]},
    "slides": [{"title": "بازار امروز", "bullets": ["{{D1|name}}"]},
               {"title": "شاخص و ارزش معاملات", "bullets": ["پول حقیقی وارد بازار شد."],
                "stats": [{"label": "شاخص کل", "value": "{{P1|int}}"}, {"label": "تغییر روزانه", "value": "{{P3|signed_pct}}"},
                          {"label": "ارزش معاملات خرد", "value": "{{P4|money}}"}, {"label": "ورود پول حقیقی", "value": "{{P5|abs_money}}"}]},
               {"title": "پول حقیقی کجا رفت؟", "bullets": ["گروه {{P7|name}} بیشترین ورود را داشت."], "chart": "groups_flow"}],
}
CLEAN = {"findings": []}


@pytest.fixture
def engine(tmp_path):
    DBOS(config={"name": "orchestra-test", "system_database_url": fresh_system_db(), "log_level": "WARNING"})
    DBOS.launch()
    yield tmp_path
    DBOS.destroy(destroy_registry=False)


def runtime(tmp_path, replies):
    rtm, _, notifier = make_runtime(tmp_path, [])
    prov = ScriptedProvider(replies)
    route = [Route(prov, "scripted")]
    rtm.gateway = Gateway(rtm.sessions, {"manager": route, "writer": route, "reviewer": route}, max_calls_per_run=20)
    with rtm.sessions.begin() as s:
        s.merge(Destination(id="ig_test", org_id="pouyan", platform="instagram", chat_id="1784", secret_env="Z"))
    set_runtime(rtm)
    return rtm, prov, notifier


def start(request=REQUEST):
    rid = wf.run_id_for("pouyan", request, date(2026, 11, 11))
    with SetWorkflowID(rid):
        return rid, DBOS.start_workflow(wf.handle_request, "pouyan", request, DAY, DEST)


def test_whole_team_to_queued_jobs_on_three_channels(engine):
    rtm, prov, notifier = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked"}, timeout=90) == "awaiting_approval"
    with rtm.sessions() as s:
        run = s.get(Run, rid)
        pkg = s.get(PackageRow, run.package_id)
        assert run.template == "market_report" and run.detail["summary"].startswith("گزارش")
        steps = [c.step_key for c in s.execute(select(LLMCall).order_by(LLMCall.id)).scalars()]
    assert steps == ["manager.plan.0", "write.1", "review.editor.1", "review.compliance.1"]
    content = json.loads(pkg.content_json)
    plans = {d["platform"]: d["delivery_plan"][0] for d in content["destinations"]}
    assert plans["telegram"]["mode"] == "text" and "۷٬۹۷۶٬۴۰۰" in plans["telegram"]["body"]
    ig = plans["instagram"]
    assert ig["mode"] == "carousel" and len(ig["asset_ids"]) == 4 and "#کارگزاری_پویان" in ig["body"] and "{{" not in ig["body"]
    assert all(a["mime"] == "image/jpeg" and (a["width"], a["height"]) == (1080, 1350) for a in content["assets"])
    assert any(a["chart_spec_id"] == "groups_flow" for a in content["assets"])
    metrics = {p["metric"] for p in content["sources"]["data_points"]}
    assert "real_money_net.group.top_out" in metrics  # chart bars are in the sources too, not only the text's figures
    assert content["versions"]["plan"] == "market_report@1.0.0"

    submit_decision(rid, pkg.package_hash, "approved", "user:1")
    result = handle.get_result()
    assert result["status"] == "approved" and len(result["jobs"]) == 3
    with rtm.sessions() as s:
        assert {j.destination_id for j in s.execute(select(PublishJob)).scalars()} == {"tg_test", "bale_test", "ig_test"}


def test_compliance_block_goes_back_to_writer_once(engine):
    block = {"findings": [{"severity": "block", "quote": "پول حقیقی وارد بازار شد", "problem": "توصیهٔ ضمنی"}]}
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, block, DRAFT, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "blocked", "failed"}, timeout=90) == "awaiting_approval"
    assert "توصیهٔ ضمنی" in prov.calls[4]  # the second writer call carries the compliance finding


def test_manager_question_and_missing_data_stop_cleanly(engine):
    rtm, _, _ = runtime(engine, [{"action": "clarify", "question": "برای کدام روز؟"}])
    rid, handle = start("گزارش")
    assert handle.get_result()["status"] == "needs_input"
    with rtm.sessions() as s:
        assert s.get(Run, rid).detail["message"] == "برای کدام روز؟"

    plan = {**PLAN, "params": {"technical_subject": "شاخص کل", "platforms": ["telegram"]}}
    rtm, _, _ = runtime(engine, [plan])
    rid, handle = start("گزارش بازار با تکنیکال شاخص کل")
    out = handle.get_result()
    assert out["status"] == "needs_data" and "سابقهٔ قیمت" in out["message"]


def test_technical_section_uses_imported_history(engine):
    plan = {**PLAN, "params": {"technical_subject": "شاخص کل", "platforms": ["telegram"]}}
    draft = {"post": {**DRAFT["post"], "paragraphs": [*DRAFT["post"]["paragraphs"], "از نگاه تکنیکال، روند شاخص بر پایهٔ میانگین‌های متحرک بررسی شد."]}}
    rtm, prov, _ = runtime(engine, [plan, draft, CLEAN, CLEAN])
    with rtm.sessions.begin() as s:
        record_import(s, rtm.assets, source_id=wf.price_source("شاخص کل"), raw=synthetic_csv(), parser="price_csv@1",
                      now=rtm.clock(), label="index.csv")
    rid, handle = start("گزارش بازار با تکنیکال شاخص کل")
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked"}, timeout=60) == "awaiting_approval"
    writer_prompt = prov.calls[1]
    assert "[تحلیل تکنیکال شاخص کل" in writer_prompt and "RSI" in writer_prompt and "price_trend" not in writer_prompt
