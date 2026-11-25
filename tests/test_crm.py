"""CRM: leads from the public intake bot, followed by the owner in the control chat, counted in the report.
The chat platforms are stand-ins; the database, the rules and both bots' logic are real."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import Clock, add_destinations
from orchestra.agents.reporting import build_report, report_text
from orchestra.control.service import ControlService
from orchestra.crm.intake import GO, OK, REDO, IntakeBot
from orchestra.crm.store import CrmError, clean_answer, load_crm, normalize_phone, set_stage
from orchestra.db import AuditEvent, Lead, LeadEvent
from test_control import OWNER, FakeApi, FakeEngine

CFG = load_crm("pouyan")
APPLICANT = "5005"


@pytest.fixture
def crm(sessions, assets, clock):
    add_destinations(sessions)
    owner_api, public_api = FakeApi(), FakeApi()
    svc = ControlService(org_id="pouyan", api=owner_api, engine=FakeEngine(sessions, assets, clock), sessions=sessions,
                         assets=assets, owners=(OWNER,), clock=clock, crm_cfg=CFG)
    intake = IntakeBot(CFG, public_api, sessions, notify=svc.notify_lead, clock=clock)
    svc.extra_ticks.append(intake.tick)
    return svc, owner_api, public_api, clock


def fill(public_api, svc, user=APPLICANT, phone="۰۹۱۲ ۳۴۵ ۶۷۸۹", start="/start channel"):
    public_api.say(start, user=user)
    svc.tick()
    public_api.press(GO, user=user)
    svc.tick()
    for answer in ("صنایع نمونه", "علی رضایی", phone, "۲۰ میلیارد تومان", "تولید قطعات خودرو"):
        public_api.say(answer, user=user)
        svc.tick()


def leads(sessions):
    with sessions() as s:
        return s.query(Lead).order_by(Lead.id).all()


def test_config_and_answers():
    assert [q.id for q in CFG.intake.questions] == ["company", "contact_name", "phone", "amount", "activity"]
    assert CFG.first == "new" and CFG.stage("funded").final
    for text in ("09123456789", "۰۹۱۲-۳۴۵-۶۷۸۹", "+98 912 345 6789", "00989123456789", "021 9100 5777"):
        assert normalize_phone(text) in ("09123456789", "02191005777")
    assert normalize_phone("12345") is None and normalize_phone("0912345678") is None
    phone = next(q for q in CFG.intake.questions if q.kind == "phone")
    assert clean_answer(phone, "abc")[0] is None and clean_answer(CFG.intake.questions[0], "x" * 400)[0] is None
    assert clean_answer(CFG.intake.questions[0], "  صنایع   نمونه ") == ("صنایع نمونه", "")


def test_broken_config(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "crm.yaml").write_text("org_id: x\nintake: {welcome: a, consent: b, done: c, questions: []}\n"
                                             "stages: [{id: new, name_fa: n}]\n", encoding="utf-8")
    with pytest.raises(CrmError):
        load_crm("x", tmp_path)


def test_the_form_becomes_one_lead_and_the_owner_is_told(crm, sessions):
    svc, owner_api, public_api, clock = crm
    fill(public_api, svc, phone="12")  # a bad phone is asked again
    told = public_api.texts(APPLICANT)
    assert "پویان کراد" in told[0] and "شماره درست" in told[-1]  # still on the phone question
    public_api.say("۰۹۱۲ ۳۴۵ ۶۷۸۹", user=APPLICANT)
    svc.tick()
    for answer in ("۲۰ میلیارد تومان", "تولید قطعات خودرو"):
        public_api.say(answer, user=APPLICANT)
        svc.tick()
    assert "لطفاً بررسی کنید" in public_api.texts(APPLICANT)[-1] and "09123456789" in public_api.texts(APPLICANT)[-1]
    assert [b[1] for b in public_api.out[-1]["buttons"][0]] == [OK, REDO]
    public_api.press(OK, user=APPLICANT)
    svc.tick()
    assert "ثبت شد" in public_api.texts(APPLICANT)[-1]
    (lead,) = leads(sessions)
    assert lead.fields == {"company": "صنایع نمونه", "contact_name": "علی رضایی", "phone": "09123456789",
                           "amount": "۲۰ میلیارد تومان", "activity": "تولید قطعات خودرو"}
    assert (lead.stage, lead.ref, lead.channel, lead.user_id) == ("new", "channel", "bale", APPLICANT)
    card = owner_api.out[-1]
    assert card["chat"] == OWNER and "سرنخ تازه" in card["text"] and "09123456789" in card["text"]
    assert ("تماس گرفته شد", f"ld:{lead.id}:contacted") in card["buttons"][0]
    public_api.press(OK, user=APPLICANT)  # pressed again
    svc.tick()
    assert len(leads(sessions)) == 1 and "چیزی برای ثبت نیست" in public_api.texts(APPLICANT)[-1]


def test_same_person_updates_their_open_lead_and_is_limited(crm, sessions):
    svc, owner_api, public_api, clock = crm
    for _ in range(4):
        fill(public_api, svc)
        public_api.press(OK, user=APPLICANT)
        svc.tick()
    assert len(leads(sessions)) == 1
    assert "به‌روز کرد" in owner_api.texts()[-1]
    assert "پیش‌تر ثبت شده" in public_api.texts(APPLICANT)[-1]  # the fourth within a day


def test_groups_are_not_answered(crm, sessions):
    svc, owner_api, public_api, clock = crm
    public_api._uid += 1
    public_api.updates.append({"update_id": public_api._uid, "message": {"text": "/start", "chat": {"id": -100, "type": "group"},
                                                                         "from": {"id": 7}}})
    svc.tick()
    assert public_api.out == []


def test_owner_moves_notes_and_lists_leads(crm, sessions):
    svc, owner_api, public_api, clock = crm
    fill(public_api, svc)
    public_api.press(OK, user=APPLICANT)
    svc.tick()
    clock.advance(3 * 3600)
    owner_api.press("ld:1:contacted", user="999")  # a stranger
    svc.tick()
    assert leads(sessions)[0].stage == "new"
    owner_api.press("ld:1:contacted")
    svc.tick()
    lead = leads(sessions)[0]
    assert lead.stage == "contacted" and lead.first_contact_at == clock.now
    owner_api.say("/note 1 جلسه با مدیرعامل هفتهٔ بعد")
    owner_api.say("/leads")
    owner_api.say("/lead ۱")
    svc.tick()
    texts = owner_api.texts()
    assert "یادداشت روی سرنخ 1 ثبت شد" in texts[-3]
    assert "تماس گرفته شد:" in texts[-2] and "صنایع نمونه" in texts[-2]
    assert "جلسه با مدیرعامل" in texts[-1] and "جدید ← تماس گرفته شد" in texts[-1]
    owner_api.press("ld:1:lost")
    svc.tick()
    assert leads(sessions)[0].closed_at is not None and owner_api.out[-1]["buttons"] is None
    with sessions() as s:
        audits = [(e.action, e.data) for e in s.query(AuditEvent).order_by(AuditEvent.id) if e.action.startswith("lead.")]
    assert [a for a, _ in audits] == ["lead.created", "lead.stage", "lead.stage"]
    assert "0912" not in str(audits) and "صنایع" not in str(audits)  # no personal details in the audit chain


def test_a_lead_nobody_called_is_reminded_once(crm, sessions):
    svc, owner_api, public_api, clock = crm
    fill(public_api, svc)
    public_api.press(OK, user=APPLICANT)
    svc.tick()
    clock.advance(23 * 3600)
    svc.tick()
    assert not any("منتظر تماس" in t for t in owner_api.texts())
    clock.advance(2 * 3600)
    svc.tick()
    svc.tick()
    assert sum("منتظر تماس" in t for t in owner_api.texts()) == 1


def test_crm_in_the_report(crm, sessions):
    svc, owner_api, public_api, clock = crm
    fill(public_api, svc)
    public_api.press(OK, user=APPLICANT)
    fill(public_api, svc, user="6006", start="/start")
    public_api.press(OK, user="6006")
    svc.tick()
    clock.advance(30 * 60)
    with sessions.begin() as s:
        set_stage(s, CFG, 1, "contacted", "u", clock())
    clock.advance(25 * 3600)
    with sessions() as s:
        rep = build_report(s, "pouyan", end_day=clock().date(), days=7, now=clock())
    assert rep.leads_new == 2 and rep.leads_by_ref == {"channel": 1, "مستقیم": 1}
    assert rep.leads_by_stage == {"contacted": 1, "new": 1} and rep.median_first_contact_minutes == 30
    assert rep.leads_overdue == [2]
    text = report_text(rep)
    assert "۲ سرنخ تازه" in text and "تماس گرفته شد ۱" in text and "/lead 2" in text


def test_serve_attaches_the_intake_bot_only_with_its_own_token(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from orchestra.cli import _attach_intake
    from orchestra.settings import Settings
    cfg = Settings(control_bot_token="1:control", orgs_dir=Path("orgs"))
    svc = SimpleNamespace(crm_cfg=None, extra_ticks=[], sessions=None, notify_lead=lambda *a: None)
    monkeypatch.delenv("ORCHESTRA_INTAKE_BOT_TOKEN", raising=False)
    _attach_intake(cfg, "pouyan", svc, {})
    assert svc.crm_cfg is not None and svc.extra_ticks == []  # no token: /leads works, the public bot is off
    monkeypatch.setenv("ORCHESTRA_INTAKE_BOT_TOKEN", "1:control")
    with pytest.raises(SystemExit):
        _attach_intake(cfg, "pouyan", svc, {})
    monkeypatch.setenv("ORCHESTRA_INTAKE_BOT_TOKEN", "2:intake")
    _attach_intake(cfg, "pouyan", svc, {})
    assert len(svc.extra_ticks) == 1
