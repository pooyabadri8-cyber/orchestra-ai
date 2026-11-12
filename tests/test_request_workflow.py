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


# ------------------------------------------------------------------ the owner sends content back with a note

DRAFT2 = {**DRAFT, "post": {**DRAFT["post"], "headline": "شاخص کل {{P3|abs_pct}} بالا رفت"}}


def waiting(rtm, rid, round_no, timeout=90):
    """Wait until the run waits for a decision in the given approval round; return (run, package row)."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with rtm.sessions() as s:
            run = s.get(Run, rid)
            if run is not None and run.status not in ("planning", "running", "awaiting_approval"):
                raise AssertionError(f"run ended as {run.status}: {run.detail}")
            if run is not None and run.status == "awaiting_approval" and run.detail.get("approval_round") == round_no:
                return run, s.get(PackageRow, run.package_id)
        time.sleep(0.1)
    raise AssertionError("timed out waiting for the approval round")


def test_owner_note_makes_a_new_version_and_only_that_one_can_be_approved(engine):
    rtm, prov, notifier = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN, DRAFT2, CLEAN, CLEAN])
    rid, handle = start()
    run, v1 = waiting(rtm, rid, 1)
    assert run.detail["can_revise"] is True and v1.version == 1

    submit_decision(rid, v1.package_hash, "revise", "user:1", reason="تیتر را   کوتاه‌تر کن")
    run, v2 = waiting(rtm, rid, 2)
    assert v2.version == 2 and v2.supersedes == v1.package_id and v2.package_hash != v1.package_hash
    assert "تیتر را کوتاه‌تر کن" in prov.calls[4] and "صاحب رسانه" in prov.calls[4]  # the writer was given the note
    assert "شاخص کل ۲٫۳۹٪ بالا رفت" in json.loads(v2.content_json)["destinations"][0]["delivery_plan"][0]["body"]
    with rtm.sessions() as s:
        assert s.get(PackageRow, v1.package_id).status == "superseded"
        steps = [c.step_key for c in s.execute(select(LLMCall).order_by(LLMCall.id)).scalars()]
    assert steps[4:] == ["write.3", "review.editor.3", "review.compliance.3"]  # step keys never repeat
    import time
    for _ in range(100):  # the approval request is sent right after the round becomes visible
        if len(notifier.requests) == 2:
            break
        time.sleep(0.05)
    assert len(notifier.requests) == 2

    submit_decision(rid, v1.package_hash, "approved", "user:1")  # a decision for the old version changes nothing
    submit_decision(rid, v2.package_hash, "approved", "user:1")
    result = handle.get_result()
    assert result["status"] == "approved" and result["package_id"] == v2.package_id
    with rtm.sessions() as s:
        assert {j.package_id for j in s.execute(select(PublishJob)).scalars()} == {v2.package_id}


def test_if_the_new_version_cannot_be_made_the_last_good_one_keeps_waiting(engine):
    block = {"findings": [{"severity": "block", "quote": "پول حقیقی وارد بازار شد", "problem": "توصیهٔ ضمنی"}]}
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN, DRAFT, CLEAN, block, DRAFT, CLEAN, block])
    rid, handle = start()
    _, v1 = waiting(rtm, rid, 1)
    submit_decision(rid, v1.package_hash, "revise", "user:1", reason="لحن را هیجانی کن")
    run, still = waiting(rtm, rid, 2)
    assert still.package_id == v1.package_id and still.status == "active"
    assert "کنترل کیفیت" in run.detail["revision_error"] and "توصیهٔ ضمنی" in run.detail["revision_error"]
    submit_decision(rid, v1.package_hash, "approved", "user:1")
    assert handle.get_result()["status"] == "approved"


def test_revisions_are_limited_and_a_note_is_required(engine):
    replies = [PLAN, DRAFT, CLEAN, CLEAN] + [DRAFT2, CLEAN, CLEAN, DRAFT, CLEAN, CLEAN, DRAFT2, CLEAN, CLEAN]
    rtm, prov, _ = runtime(engine, replies)
    rtm.gateway.max_calls_per_run = 40
    rid, handle = start()
    _, pkg = waiting(rtm, rid, 1)
    with pytest.raises(ValueError):
        submit_decision(rid, pkg.package_hash, "revise", "user:1", reason="  ")
    for round_no in range(2, wf.MAX_REVISIONS + 2):
        submit_decision(rid, pkg.package_hash, "revise", "user:1", reason=f"تغییر {round_no}")
        run, pkg = waiting(rtm, rid, round_no)
    assert run.detail["can_revise"] is False and pkg.version == wf.MAX_REVISIONS + 1
    submit_decision(rid, pkg.package_hash, "revise", "user:1", reason="یک بار دیگر")  # ignored: none left
    submit_decision(rid, pkg.package_hash, "rejected", "user:1", reason="کافی است")
    assert handle.get_result()["status"] == "rejected"
    assert prov.replies == []
