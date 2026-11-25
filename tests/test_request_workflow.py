"""handle_request end to end on DBOS: Manager -> analysts -> writer -> checks -> reviewers -> designer -> package ->
approval -> publish queue, with a scripted model, the real market page and real slide rendering."""
import json
from datetime import date

import pytest
from dbos import DBOS, SetWorkflowID
from sqlalchemy import select

import orchestra.workflows.request as wf
from orchestra.db import DataPointRow, DataSnapshot, Destination, LLMCall, PackageRow, PublishJob, Run
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
    # the market report has a fixed slide layout: the writer only adds sentences per slide
    "slide_text": {"slide2": ["پول حقیقی وارد بازار شد."], "slide3": ["گروه {{P7|name}} بیشترین ورود را داشت."]},
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
    assert ig["mode"] == "carousel" and len(ig["asset_ids"]) == 6 and "#کارگزاری_پویان" in ig["body"] and "{{" not in ig["body"]
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
    import wf_support
    wf_support.TSE_DOWN = True  # not reachable and nothing imported
    try:
        rid, handle = start("گزارش بازار با تکنیکال شاخص کل.")
        out = handle.get_result()
    finally:
        wf_support.TSE_DOWN = False
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
    assert steps[4:] == ["write.4", "review.editor.4", "review.compliance.4"]  # step keys never repeat
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



# ------------------------------------------------------------------ the index history comes from TSE by itself


def test_daily_report_carries_the_index_technical_picture_fetched_from_tse(engine):
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    writer_prompt = prov.calls[1]
    assert "تحلیل تکنیکال شاخص کل" in writer_prompt and "RSI" in writer_prompt
    with rtm.sessions() as s:
        snaps = s.execute(select(DataSnapshot).where(DataSnapshot.source_id == wf.price_source("شاخص کل"))).scalars().all()
        run = s.get(Run, rid)
    assert [x.parser for x in snaps] == ["tse_index_financial@1"]
    assert not any(f["rule_id"].startswith("DATA-") for f in run.findings)


def test_without_tse_the_daily_report_still_comes_with_a_note(engine):
    import wf_support
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    wf_support.TSE_DOWN = True
    try:
        rid, handle = start()
        assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    finally:
        wf_support.TSE_DOWN = False
    with rtm.sessions() as s:
        notes = [f["message"] for f in s.get(Run, rid).findings if f["rule_id"].startswith("DATA-")]
    assert any("TSE" in n for n in notes) and any("تکنیکال اضافه نشد" in n for n in notes)
    assert "تحلیل تکنیکال" not in prov.calls[1]


def test_an_old_history_is_used_but_flagged(engine):
    import wf_support
    from tse_samples import index_financial
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    with rtm.sessions.begin() as s:
        record_import(s, rtm.assets, source_id=wf.price_source("شاخص کل"), raw=index_financial(end=date(2026, 10, 20)),
                      parser="tse_index_financial@1", now=rtm.clock(), label="old")
    wf_support.TSE_DOWN = True
    try:
        rid, handle = start()
        assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    finally:
        wf_support.TSE_DOWN = False
    with rtm.sessions() as s:
        notes = [f["message"] for f in s.get(Run, rid).findings if f["rule_id"].startswith("DATA-")]
    assert any("2026-10-20" in n and "به‌روز نیست" in n for n in notes)
    assert "تحلیل تکنیکال شاخص کل" in prov.calls[1]


def _run_with_history(engine, history: bytes):
    import wf_support
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    with rtm.sessions.begin() as s:
        record_import(s, rtm.assets, source_id=wf.price_source("شاخص کل"), raw=history,
                      parser="tse_index_financial@1", now=rtm.clock(), label="test")
    wf_support.TSE_DOWN = True
    try:
        rid, handle = start()
        assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    finally:
        wf_support.TSE_DOWN = False
    return rtm, prov, rid


def test_the_technical_picture_includes_today_when_tse_has_not_caught_up(engine):
    from tse_samples import index_financial
    rtm, prov, rid = _run_with_history(engine, index_financial(end=date(2026, 11, 10), last_close=7_900_000))
    prompt = prov.calls[1]
    assert "عدد پایانی امروز" in prompt and "تا چهارشنبه ۲۰ آبان ۱۴۰۵" in prompt
    with rtm.sessions() as s:
        today = s.execute(select(DataPointRow).where(DataPointRow.metric == "index.total.value")).scalars().first()
        close = s.execute(select(DataPointRow).where(DataPointRow.metric == "technical.price.close")).scalars().first()
        sma = s.execute(select(DataPointRow).where(DataPointRow.metric == "technical.sma.20")).scalars().first()
        notes = [f["rule_id"] + ": " + f["message"] for f in s.get(Run, rid).findings]
    assert close is None or close.value == today.value
    assert today.id in sma.derivation["inputs"]  # every technical figure names today's value as a source
    assert not any(n.startswith(("DATA-TODAY", "DATA-STALE", "DATA-TECHNICAL")) for n in notes), notes


def test_today_is_not_merged_when_the_two_sources_disagree(engine):
    from tse_samples import index_financial
    rtm, prov, rid = _run_with_history(engine, index_financial(end=date(2026, 11, 10), last_close=3_000_000))
    assert "عدد پایانی امروز" not in prov.calls[1] and "تا سه‌شنبه ۱۹ آبان ۱۴۰۵" in prov.calls[1]
    with rtm.sessions() as s:
        notes = [f["message"] for f in s.get(Run, rid).findings if f["rule_id"] == "DATA-TODAY"]
    assert len(notes) == 1 and "نمی‌خواند" in notes[0]


def test_a_post_with_five_paragraphs_is_checked_not_crashed(engine):
    """Real failure on 2026-11-17: Gemini wrote 5 paragraphs; the writer allowed 5, the fact checker only 4."""
    five = {**DRAFT, "post": {**DRAFT["post"], "paragraphs": [*DRAFT["post"]["paragraphs"], "پاراگراف پنجم بدون عدد."][:5]}}
    while len(five["post"]["paragraphs"]) < 5:
        five["post"]["paragraphs"].append("یک جملهٔ دیگر بدون عدد.")
    rtm, prov, _ = runtime(engine, [PLAN, five, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"


def test_a_draft_the_checker_cannot_read_is_sent_back_to_the_writer(engine, monkeypatch):
    calls = {"n": 0}
    real = wf.verify_multichannel

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("unexpected shape")
        return real(*a, **kw)
    monkeypatch.setattr(wf, "verify_multichannel", flaky)
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, DRAFT, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=150) == "awaiting_approval"
    assert "ساختار متن قابل بررسی نبود" in prov.calls[2]  # the second writing got the reason


# ------------------------------------------------------------------ the editor's remarks are fixed before the owner sees them

WARN = {"findings": [{"severity": "warn", "quote": "پول حقیقی وارد بازار شد", "problem": "جملهٔ کلی؛ بگو چقدر و به کدام گروه‌ها"}]}


def test_editor_remarks_get_one_polishing_round(engine):
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, WARN, CLEAN, DRAFT2, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    assert "پیش از انتشار این را بهتر کن" in prov.calls[4] and "کدام گروه‌ها" in prov.calls[4]
    with rtm.sessions() as s:
        run = s.get(Run, rid)
        body = json.loads(s.get(PackageRow, run.package_id).content_json)["destinations"][0]["delivery_plan"][0]["body"]
    assert not [f for f in run.findings if f["rule_id"] == "E-EDITOR"] and "شاخص کل ۲٫۳۹٪ بالا رفت" in body
    assert prov.replies == []


def test_if_polishing_makes_it_worse_the_acceptable_text_is_kept(engine):
    block = {"findings": [{"severity": "block", "quote": "پول حقیقی وارد بازار شد", "problem": "تناقض با داده"}]}
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, WARN, CLEAN, DRAFT2, block, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    with rtm.sessions() as s:
        run = s.get(Run, rid)
        body = json.loads(s.get(PackageRow, run.package_id).content_json)["destinations"][0]["delivery_plan"][0]["body"]
    rules = [f["rule_id"] for f in run.findings]
    assert "E-POLISH" in rules and "E-EDITOR" in rules and "شاخص کل ۲٫۳۹٪ بالا رفت" not in body



def test_daily_report_has_currency_and_gold_and_survives_without_tgju(engine):
    import wf_support
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    rid, handle = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    assert "بازار ارز و طلا" in prov.calls[1] and "دلار آمریکا (بازار آزاد) امروز پایین آمد" in prov.calls[1]


def test_without_tgju_the_report_still_comes_with_a_note(engine):
    import wf_support
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    wf_support.TGJU_DOWN = True
    try:
        rid, handle = start()
        assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_data"}, timeout=90) == "awaiting_approval"
    finally:
        wf_support.TGJU_DOWN = False
    with rtm.sessions() as s:
        notes = [f["message"] for f in s.get(Run, rid).findings if f["rule_id"] == "DATA-FX"]
    assert len(notes) == 1 and "بخش ارز و طلا اضافه نشد" in notes[0] and "بازار ارز و طلا" not in prov.calls[1]


TOPIC_DRAFT = {
    "post": {"hashtag": "#تامین_مالی_جمعی", "headline": "کارنامهٔ پویان کراد",
             "paragraphs": ["تاکنون {{P1|money}} برای {{P2|int}} طرح در پویان کراد تامین شده است.",
                            "نرخ تکمیل تامین مالی طرح‌ها {{P3|pct}} بوده است."],
             "cta": "برای ثبت درخواست تامین مالی با کارشناس پذیرش گفت‌وگو کنید."},
    "instagram": {"paragraphs": ["هر طرحی که روی پویان کراد عرضه شد، کامل تامین شد."], "hashtags": ["#تامین_مالی_جمعی"]},
    "slides": [{"title": "کارنامهٔ پویان کراد", "bullets": ["هر طرح عرضه‌شده کامل تامین شد."],
                "stats": [{"label": "مبلغ تامین‌شده", "value": "{{P1|money}}"}, {"label": "نرخ تکمیل", "value": "{{P3|pct}}"}]},
               {"title": "طرح‌ها", "bullets": ["طرح {{P6|name}} با مبلغ {{P6|money}} عرضه شد."]}],
}


def test_a_content_plan_item_runs_without_the_manager_and_waits_for_its_time(engine):
    rtm, prov, _ = runtime(engine, [TOPIC_DRAFT, CLEAN, CLEAN])
    preset = {"template": "topic_post", "params": {"topic": "crowd_track_record"}, "summary_fa": "کارنامهٔ پویان کراد"}
    at = "2026-11-11T15:00:00+00:00"
    request = "برنامهٔ محتوا: کارنامهٔ پویان کراد"
    rid = wf.run_id_for("pouyan", request, date(2026, 11, 11))
    with SetWorkflowID(rid):
        handle = DBOS.start_workflow(wf.handle_request, "pouyan", request, DAY, DEST, preset=preset, publish_at_iso=at)
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "needs_input"}, timeout=90) == "awaiting_approval"
    with rtm.sessions() as s:
        run = s.get(Run, rid)
        pkg = s.get(PackageRow, run.package_id)
        steps = [c.step_key for c in s.execute(select(LLMCall).order_by(LLMCall.id)).scalars()]
    assert steps == ["write.1", "review.editor.1", "review.compliance.1"] and run.template == "topic_post"
    content = json.loads(pkg.content_json)
    assert pkg.content_class == "topic_post"
    assert {d["not_before"] for d in content["destinations"]} == {"2026-11-11T15:00:00Z"}
    tg = next(d for d in content["destinations"] if d["platform"] == "telegram")["delivery_plan"][0]["body"]
    assert "۵۵ میلیارد تومان" in tg and "۱۰۰٪" in tg
    assert "knowledge.crowd_funded_total" in {p["metric"] for p in content["sources"]["data_points"]}
    submit_decision(rid, pkg.package_hash, "approved", "user:1")
    assert handle.get_result()["status"] == "approved"
    with rtm.sessions() as s:
        jobs = s.execute(select(PublishJob)).scalars().all()
    assert jobs and all(j.next_attempt_at.isoformat().startswith("2026-11-11T15:00") for j in jobs)  # waits for its time


def test_an_unknown_topic_asks_instead_of_writing(engine):
    rtm, prov, _ = runtime(engine, [])
    rid = wf.run_id_for("pouyan", "x", date(2026, 11, 11))
    with SetWorkflowID(rid):
        DBOS.start_workflow(wf.handle_request, "pouyan", "x", DAY, DEST, preset={"template": "topic_post", "params": {"topic": "nope"}})
    assert wait_for_status(rtm.sessions, rid, {"needs_input", "failed"}, timeout=60) == "needs_input"
    assert prov.calls == []


def news_runtime(engine, replies):
    from test_research import OIL, RATE, rss
    rtm, prov, notifier = runtime(engine, replies)

    async def fetch(url, foreign):
        if "t.me" in url:
            from orchestra.data.acquisition import AcquisitionError
            raise AcquisitionError("blocked", retryable=False)
        return rss(OIL, RATE)
    rtm.news_fetcher = fetch
    return rtm, prov


NEWS_DRAFT = {"post": {"hashtag": "#خبر_بازار", "headline": "نفت برنت گران شد",
                       "paragraphs": ["به گزارش ایسنا، نفت برنت با {{P2|as_is}} افزایش به {{P3|as_is}} رسید.",
                                      "بانک مرکزی نرخ سود سپرده را {{P4|as_is}} نگه داشت."], "cta": "نظرتان را بنویسید."}}


def start_news(request="خلاصهٔ خبرهای موثر بر بازار"):
    rid = wf.run_id_for("pouyan", request, date(2026, 11, 11))
    with SetWorkflowID(rid):
        DBOS.start_workflow(wf.handle_request, "pouyan", request, DAY, DEST,
                            preset={"template": "news_digest", "params": {"platforms": ["telegram", "bale"]}})
    return rid


def test_news_digest_carries_its_evidence_and_bound_figures(engine):
    rtm, prov = news_runtime(engine, [{"picks": [{"n": 2, "why": "انرژی"}, {"n": 1, "why": "نرخ سود"}]}, NEWS_DRAFT, CLEAN, CLEAN])
    rid = start_news()
    status = wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked", "skipped"}, timeout=90)
    with rtm.sessions() as s:
        assert status == "awaiting_approval", [(c.step_key, (c.response or "")[:60]) for c in s.execute(select(LLMCall).order_by(LLMCall.id)).scalars()]
    with rtm.sessions() as s:
        run = s.get(Run, rid)
        content = json.loads(s.get(PackageRow, run.package_id).content_json)
        steps = [c.step_key for c in s.execute(select(LLMCall).order_by(LLMCall.id)).scalars()]
    assert steps == ["research.pick.0", "write.1", "review.editor.1", "review.compliance.1"]
    assert {e["url"] for e in content["sources"]["evidence"]} == {"https://n.test/0", "https://n.test/1"}
    figs = {p["source_text"] for p in content["sources"]["data_points"] if p["metric"] == "news.figure"}
    assert figs == {"۲٫۵ درصد", "۸۰ دلار", "۲۳ درصد"}
    tg = next(d for d in content["destinations"] if d["platform"] == "telegram")["delivery_plan"][0]["body"]
    assert "۸۰ دلار" in tg and "۲۳ درصد" in tg
    assert any(f["rule_id"] == "DATA-NEWS" for f in run.findings)  # the Telegram source did not answer: the owner is told
    assert "انرژی" in prov.calls[1]  # the researcher's reason reaches the writer


def test_no_important_news_is_skipped_without_writing(engine):
    rtm, prov = news_runtime(engine, [{"picks": []}])
    rid = start_news("خبرهای امروز")
    assert wait_for_status(rtm.sessions, rid, {"skipped", "failed", "awaiting_approval"}, timeout=60) == "skipped"
    with rtm.sessions() as s:
        assert "خبر مهمی" in s.get(Run, rid).detail["reason"]
    assert len(prov.calls) == 1


def test_active_lessons_reach_the_writer_and_editor_and_are_recorded(engine):
    from orchestra import lessons as memory
    from orchestra.brand import load_brand
    rtm, prov, _ = runtime(engine, [PLAN, DRAFT, CLEAN, CLEAN])
    with rtm.sessions.begin() as s:
        lid = memory.offer(s, "pouyan", "writing", "در پایان هر پست یک جملهٔ جمع‌بندی بیاید", "typed").id
        memory.activate(s, load_brand("pouyan"), lid, "u", rtm.clock())
    rid, _ = start()
    assert wait_for_status(rtm.sessions, rid, {"awaiting_approval", "failed", "blocked"}, timeout=90) == "awaiting_approval"
    with rtm.sessions() as s:
        calls = {c.step_key: c.system for c in s.execute(select(LLMCall)).scalars()}
        content = json.loads(s.get(PackageRow, s.get(Run, rid).package_id).content_json)
    assert "یک جملهٔ جمع‌بندی" in calls["write.1"] and "یک جملهٔ جمع‌بندی" in calls["review.editor.1"]
    assert "یک جملهٔ جمع‌بندی" not in calls["review.compliance.1"] and "یک جملهٔ جمع‌بندی" not in calls["manager.plan.0"]
    assert content["versions"]["lessons"] == memory.fingerprint(("در پایان هر پست یک جملهٔ جمع‌بندی بیاید",))
