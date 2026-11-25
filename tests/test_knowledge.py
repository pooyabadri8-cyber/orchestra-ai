"""The knowledge base and the topic post: every figure in marketing content is a stored, traceable data point."""
from datetime import date, datetime, timezone

import pytest

from orchestra.agents.knowledge import knowledge_part, store_knowledge
from orchestra.agents.manager import system_prompt
from orchestra.agents.multichannel import MultiChannelDraft, verify_multichannel
from orchestra.agents.specialists import compose
from orchestra.brand import load_brand
from orchestra.content.numbers import format_point
from orchestra.db import DataPointRow, DataSnapshot
from orchestra.knowledge import KnowledgeError, load_knowledge, load_knowledge_bytes
from orchestra.registry import DEFAULT_ROOT, OrgPolicy, Registry, RegistryError

NOW = datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)
POLICY = OrgPolicy(platforms=("telegram", "bale", "instagram"))


def test_pouyan_knowledge_loads():
    kb, raw = load_knowledge("pouyan")
    assert {t.id for t in kb.topics} >= {"crowd_intro", "crowd_track_record", "crowd_concerns", "crowd_path", "crowd_cta"}
    assert {f.metric for f in kb.facts_for("crowd_path")} == {
        "knowledge.crowd_path_steps", "knowledge.crowd_path_phases", "knowledge.crowd_path_control_step"}


@pytest.mark.parametrize("bad", [
    "org_id: pouyan\ntopics:\n  - {id: a1, title: عنوان, brief: سه طرح در ۲ سال}\n",  # a figure in the text
    "org_id: pouyan\ntopics:\n  - {id: a1, title: عنوان, brief: شرح}\nfacts:\n  - {id: f1, label: x, value: 1, unit: count, topics: [nope]}\n",
    "org_id: pouyan\ntopics:\n  - {id: a1, title: عنوان, brief: شرح}\nfacts:\n  - {id: f1, label: x, value: 1, unit: meter}\n",
    "org_id: pouyan\ntopics:\n  - {id: a1, title: عنوان, brief: شرح}\nfacts:\n  - {id: f1, label: x, value: 1, unit: count, scale: B}\n",
    "org_id: other\ntopics:\n  - {id: a1, title: عنوان, brief: شرح}\n",
])
def test_a_broken_knowledge_file_is_refused(bad):
    with pytest.raises(KnowledgeError):
        load_knowledge_bytes(bad.encode(), "pouyan")


def test_topic_part_gives_bound_figures_traced_to_the_file(sessions, assets):
    kb, raw = load_knowledge("pouyan")
    with sessions.begin() as s:
        part = knowledge_part(s, assets, kb, raw, topic_id="crowd_track_record", now=NOW)
        again = store_knowledge(s, assets, kb, raw, NOW)
    by = {(p.metric, p.subject): p for p in part.points}
    assert format_point(by[("knowledge.crowd_funded_total", None)], "money") == "۵۵ میلیارد تومان"
    assert format_point(by[("knowledge.crowd_completion_rate", None)], "pct") == "۱۰۰٪"
    assert format_point(by[("knowledge.crowd_project_amount", "پویان‌چوب")], "name") == "پویان‌چوب"
    assert ("knowledge.crowd_path_steps", None) not in by  # only this topic's facts
    assert any("کامل تامین مالی" in f for f in part.facts)
    with sessions() as s:
        snaps = s.query(DataSnapshot).all()
        assert len(snaps) == 1 and snaps[0].id == again and assets.get(snaps[0].raw_sha256) == raw
        assert s.get(DataPointRow, by[("knowledge.crowd_funded_total", None)].point_id).snapshot_id == again
    with pytest.raises(KnowledgeError):
        with sessions.begin() as s:
            knowledge_part(s, assets, kb, raw, topic_id="no_such_topic", now=NOW)


def test_a_topic_post_is_checked_like_any_post(sessions, assets):
    kb, raw = load_knowledge("pouyan")
    brand = load_brand("pouyan")
    with sessions.begin() as s:
        part = knowledge_part(s, assets, kb, raw, topic_id="crowd_track_record", now=NOW)
    rep = compose("imports", [part])
    keys = {p.metric: p.key for p in rep.points if p.subject is None}
    total, rate = keys["knowledge.crowd_funded_total"], keys["knowledge.crowd_completion_rate"]
    post = {"hashtag": "#تامین_مالی_جمعی", "headline": "کارنامهٔ پویان کراد",
            "paragraphs": [f"تاکنون {{{{{total}|money}}}} برای طرح‌های پویان کراد تامین شده است.",
                           f"نرخ تکمیل تامین مالی طرح‌ها {{{{{rate}|pct}}}} بوده است."], "cta": "ثبت درخواست تامین مالی"}
    ok = verify_multichannel(MultiChannelDraft(post=post), rep, brand, ("telegram",))
    assert not ok.blocked and "۵۵ میلیارد تومان" in ok.post.paragraphs[0]
    typed = verify_multichannel(MultiChannelDraft(post={**post, "headline": "۵۵ میلیارد تومان تامین شد"}), rep, brand, ("telegram",))
    assert typed.blocked and any(f.rule_id == "V-UNBOUND" for f in typed.findings)


def test_registry_plans_a_topic_post_and_the_manager_sees_the_topics():
    reg = Registry.load(DEFAULT_ROOT)
    plan = reg.instantiate("topic_post", {"topic": "crowd_intro"}, today=date(2026, 11, 25), policy=POLICY)
    assert [s.agent for s in plan.steps][:2] == ["knowledge_curator", "writer"] and plan.has("design")
    with pytest.raises(RegistryError):
        reg.instantiate("topic_post", {}, today=date(2026, 11, 25), policy=POLICY)
    prompt = system_prompt(reg, date(2026, 11, 25), POLICY, [("crowd_intro", "معرفی پویان کراد")])
    assert "crowd_intro: معرفی پویان کراد" in prompt and "topic_post" in prompt
