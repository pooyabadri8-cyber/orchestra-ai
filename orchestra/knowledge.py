"""Per-organization knowledge base: the only facts a topic post (or a campaign) may state.

orgs/<org>/knowledge.yaml holds topics (what a post can be about, with plain-language notes) and facts (every figure,
each with its unit). Notes and titles may not contain digits: a figure lives only in `facts`, becomes a stored data
point, and reaches a post through a token, exactly like a market figure. So the rule "the model never types a number"
holds for marketing content too, and every figure in a post traces back to the exact file it came from.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PARSER = "knowledge_yaml@1"
_DIGIT = re.compile(r"[0-9۰-۹٠-٩]")
_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
UNITS = {"IRT": ("money",), "percent": ("pct", "abs_pct"), "count": ("int",)}
SCALES = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3, "": 1.0}


class KnowledgeError(Exception):
    pass


def _no_digits(v: str, what: str) -> str:
    if _DIGIT.search(v):
        raise ValueError(f"{what} «{v[:40]}» عدد دارد؛ عددها فقط در facts می‌آیند")
    return v


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    title: str
    brief: str
    notes: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"شناسهٔ موضوع نامعتبر: {v!r}")
        return v

    @model_validator(mode="after")
    def _plain(self) -> "Topic":
        _no_digits(self.title, "عنوان")
        _no_digits(self.brief, "شرح")
        for n in self.notes:
            _no_digits(n, "یادداشت")
        return self


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    label: str
    value: float
    unit: str
    scale: str = ""  # for money: the value is in this scale (B = billion toman), shown the same way
    subject: str | None = None  # e.g. a project's symbol
    topics: tuple[str, ...] = ()  # empty = every topic

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        if v not in UNITS:
            raise ValueError(f"واحد «{v}» پشتیبانی نمی‌شود؛ مجاز: {', '.join(UNITS)}")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Fact":
        if not _ID.match(self.id):
            raise ValueError(f"شناسهٔ واقعیت نامعتبر: {self.id!r}")
        if self.scale not in SCALES or (self.scale and self.unit != "IRT"):
            raise ValueError(f"{self.id}: scale فقط برای مبلغ (IRT) و یکی از T، B، M، K است")
        _no_digits(self.label, "برچسب")
        if self.subject:
            _no_digits(self.subject, "نام")
        return self

    @property
    def metric(self) -> str:
        return f"knowledge.{self.id}"

    @property
    def base_value(self) -> float:
        return self.value * SCALES[self.scale]


class Knowledge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str
    topics: tuple[Topic, ...] = Field(min_length=1)
    facts: tuple[Fact, ...] = ()

    @model_validator(mode="after")
    def _refs(self) -> "Knowledge":
        ids = [t.id for t in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError("شناسهٔ موضوع تکراری")
        keys = [(f.id, f.subject) for f in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("واقعیت تکراری (id و subject یکسان)")
        for f in self.facts:
            unknown = set(f.topics) - set(ids)
            if unknown:
                raise ValueError(f"{f.id}: موضوع ناشناخته {sorted(unknown)}")
        return self

    def topic(self, topic_id: str) -> Topic:
        for t in self.topics:
            if t.id == topic_id:
                return t
        raise KnowledgeError(f"موضوع «{topic_id}» در پایگاه دانش نیست؛ موضوع‌ها: {', '.join(t.id for t in self.topics)}")

    def facts_for(self, topic_id: str) -> list[Fact]:
        return [f for f in self.facts if not f.topics or topic_id in f.topics]


def knowledge_path(org_id: str, root: str | Path = "orgs") -> Path:
    return Path(root) / org_id / "knowledge.yaml"


def load_knowledge_bytes(raw: bytes, org_id: str) -> Knowledge:
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        kb = Knowledge.model_validate(data)
    except (yaml.YAMLError, ValueError) as e:
        raise KnowledgeError(f"پایگاه دانش {org_id} خراب است: {str(e)[:400]}") from e
    if kb.org_id != org_id:
        raise KnowledgeError(f"پایگاه دانش برای {kb.org_id} است، نه {org_id}")
    return kb


def load_knowledge(org_id: str, root: str | Path = "orgs") -> tuple[Knowledge, bytes] | None:
    """(knowledge, raw bytes), or None when the organization has no knowledge base."""
    path = knowledge_path(org_id, root)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    return load_knowledge_bytes(raw, org_id), raw
