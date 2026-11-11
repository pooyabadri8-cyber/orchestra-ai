"""Agent registry and workflow templates, loaded from YAML and validated before anything runs.

The Manager can only plan work that a template describes, and a template can only use agents the registry knows.
Load-time checks make a broken registry fail at startup, not halfway through a run:
- every step's agent exists, step ids are unique, dependencies exist and have no cycle;
- a template that publishes must pass the fact checker, the compliance officer and human approval first;
- every agent's entrypoint resolves to real code (optional, on by default).
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PLATFORMS = ("telegram", "bale", "instagram")
SECTIONS = ("technical", "fundamental")
MANDATORY_BEFORE_PUBLISH = ("fact_checker", "compliance_officer", "human_approval")
_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class RegistryError(Exception):
    pass


class AgentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    version: str
    name_fa: str
    role: str
    autonomy: Literal["L0", "L1", "L2", "human"]
    entrypoint: str
    model_tier: str | None = None
    tools: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"bad agent id {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not _VERSION.match(v):
            raise ValueError(f"bad version {v!r}")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _entry(cls, v: str) -> str:
        if not re.match(r"^[a-z_][\w.]*:[A-Za-z_]\w*$", v):
            raise ValueError(f"entrypoint must be module:attribute, got {v!r}")
        return v

    @model_validator(mode="after")
    def _tier(self):
        if self.autonomy == "L1" and not self.model_tier:
            raise ValueError(f"{self.id}: an L1 agent uses a model and must name its model_tier")
        if self.autonomy == "L0" and self.model_tier:
            raise ValueError(f"{self.id}: an L0 agent is deterministic and must not have a model_tier")
        return self

    def resolve(self):
        module, attr = self.entrypoint.split(":")
        try:
            return getattr(importlib.import_module(module), attr)
        except (ImportError, AttributeError) as e:
            raise RegistryError(f"agent {self.id}: entrypoint {self.entrypoint} not found ({e})") from e


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    type: Literal["date", "text", "platforms", "sections"]
    required: bool = False
    default: Any = None
    description: str = ""
    subject: Literal["stock", "index_or_stock"] | None = None  # a symbol parameter, checked against the org policy


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    agent: str
    depends_on: tuple[str, ...] = ()
    when: str | None = None  # a param name that must be set, "platform:<p>" or "section:<s>"


class TemplateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    version: str
    name_fa: str
    description: str
    examples: tuple[str, ...] = ()
    params: tuple[ParamSpec, ...] = ()
    single_stock: bool = False  # the whole template is about one stock
    steps: tuple[StepSpec, ...] = Field(min_length=1)

    def param(self, name: str) -> ParamSpec | None:
        return next((p for p in self.params if p.name == name), None)


@dataclass(frozen=True)
class OrgPolicy:
    """What an organisation allows the Manager to plan."""
    platforms: tuple[str, ...]
    single_stock_content: bool = False

    @classmethod
    def from_brand(cls, brand, platforms: tuple[str, ...]) -> "OrgPolicy":
        return cls(platforms=platforms, single_stock_content=bool(getattr(brand, "single_stock_content", False)))


def is_index(subject: str) -> bool:
    return "شاخص" in subject


class PlannedStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    agent: str
    depends_on: tuple[str, ...]


class ExecutionPlan(BaseModel):
    """What the Manager hands to the orchestrator: a validated template instance. Steps are in dependency order."""
    model_config = ConfigDict(frozen=True)
    template_id: str
    template_version: str
    params: dict[str, Any]
    steps: tuple[PlannedStep, ...]
    summary_fa: str = ""

    def has(self, step_id: str) -> bool:
        return any(s.id == step_id for s in self.steps)


def _toposort(steps: tuple[StepSpec, ...], where: str) -> list[StepSpec]:
    by_id = {s.id: s for s in steps}
    order, state = [], {}

    def visit(s: StepSpec, path: tuple[str, ...]):
        if state.get(s.id) == "done":
            return
        if state.get(s.id) == "open":
            raise RegistryError(f"{where}: dependency cycle {' → '.join(path + (s.id,))}")
        state[s.id] = "open"
        for d in s.depends_on:
            visit(by_id[d], path + (s.id,))
        state[s.id] = "done"
        order.append(s)

    for s in steps:
        visit(s, ())
    return order


def _ancestors(step_id: str, steps: dict[str, StepSpec]) -> set[str]:
    seen, stack = set(), list(steps[step_id].depends_on)
    while stack:
        x = stack.pop()
        if x not in seen:
            seen.add(x)
            stack += steps[x].depends_on
    return seen


class Registry:
    def __init__(self, agents: dict[str, AgentManifest], templates: dict[str, TemplateManifest]):
        self.agents = agents
        self.templates = templates

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, root: str | Path, *, check_entrypoints: bool = True) -> "Registry":
        root = Path(root)
        agents = {a.id: a for a in (cls._read(p, AgentManifest) for p in sorted((root / "agents").glob("*.yaml")))}
        templates = {t.id: t for t in (cls._read(p, TemplateManifest) for p in sorted((root / "templates").glob("*.yaml")))}
        reg = cls(agents, templates)
        for t in templates.values():
            reg._check_template(t)
        if check_entrypoints:
            for a in agents.values():
                a.resolve()
        return reg

    @staticmethod
    def _read(path: Path, model):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            obj = model.model_validate(data)
        except (yaml.YAMLError, ValidationError) as e:
            raise RegistryError(f"{path.name}: {e}") from e
        if obj.id != path.stem:
            raise RegistryError(f"{path.name}: id {obj.id!r} must match the file name")
        return obj

    def _check_template(self, t: TemplateManifest) -> None:
        where = f"template {t.id}"
        ids = [s.id for s in t.steps]
        if len(ids) != len(set(ids)):
            raise RegistryError(f"{where}: duplicate step ids")
        by_id = {s.id: s for s in t.steps}
        names = {p.name for p in t.params}
        for s in t.steps:
            if s.agent not in self.agents:
                raise RegistryError(f"{where}: step {s.id} uses unknown agent {s.agent}")
            for d in s.depends_on:
                if d not in by_id:
                    raise RegistryError(f"{where}: step {s.id} depends on unknown step {d}")
            if s.when:
                kind, _, val = s.when.partition(":")
                ok = (kind == "platform" and val in PLATFORMS) or (kind == "section" and val in SECTIONS) or (not val and kind in names)
                if not ok:
                    raise RegistryError(f"{where}: step {s.id} has an unknown condition {s.when!r}")
        _toposort(t.steps, where)
        for s in t.steps:
            if s.agent == "publisher":
                before = {by_id[a].agent for a in _ancestors(s.id, by_id)}
                missing = [m for m in MANDATORY_BEFORE_PUBLISH if m not in before]
                if missing:
                    raise RegistryError(f"{where}: publishing step {s.id} is not preceded by {', '.join(missing)}")
                for m in MANDATORY_BEFORE_PUBLISH:
                    if any(by_id[a].agent == m and by_id[a].when for a in _ancestors(s.id, by_id)):
                        raise RegistryError(f"{where}: the {m} step before publishing cannot be conditional")

    # ------------------------------------------------------------------ planning
    def instantiate(self, template_id: str, raw_params: dict[str, Any], *, today: date, policy: OrgPolicy,
                    summary_fa: str = "") -> ExecutionPlan:
        t = self.templates.get(template_id)
        if t is None:
            raise RegistryError(f"unknown template {template_id!r}")
        if t.single_stock and not policy.single_stock_content:
            raise RegistryError(STOCK_FORBIDDEN)
        unknown = set(raw_params) - {p.name for p in t.params}
        if unknown:
            raise RegistryError(f"{template_id}: unknown parameters {sorted(unknown)}")
        params: dict[str, Any] = {}
        for spec in t.params:
            value = raw_params.get(spec.name)
            if value in (None, "", []):
                value = spec.default
                if spec.type == "platforms" and value:  # the default means "every channel this org has"
                    value = [x for x in value if x in policy.platforms] or None
            if value in (None, "", []):
                if spec.required:
                    raise RegistryError(f"پارامتر «{spec.name}» لازم است: {spec.description}")
                if spec.type == "date":
                    value = today
                else:
                    continue
            params[spec.name] = _coerce(spec, value, policy.platforms)
            if spec.subject and not policy.single_stock_content:
                if spec.subject == "stock" or not is_index(params[spec.name]):
                    raise RegistryError(STOCK_FORBIDDEN + (" تحلیل تکنیکال شاخص‌ها مجاز است." if spec.subject == "index_or_stock" else ""))
        steps = [s for s in _toposort(t.steps, t.id) if _active(s, params)]
        active = {s.id for s in steps}
        planned = tuple(PlannedStep(id=s.id, agent=s.agent, depends_on=tuple(d for d in s.depends_on if d in active)) for s in steps)
        if not any(s.agent in ("market_analyst", "technical_analyst", "fundamental_analyst") for s in planned):
            raise RegistryError("این درخواست هیچ بخش تحلیلی ندارد")
        return ExecutionPlan(template_id=t.id, template_version=t.version, params=params, steps=planned, summary_fa=summary_fa)

    def catalog_fa(self) -> str:
        """The menu the Manager chooses from (templates and their parameters), in Persian."""
        lines = []
        for t in self.templates.values():
            lines.append(f"## {t.id} — {t.name_fa}\n{t.description}")
            for p in t.params:
                req = "الزامی" if p.required else "اختیاری"
                default = f"؛ پیش‌فرض {p.default}" if p.default is not None else ""
                lines.append(f"- {p.name} ({p.type}، {req}{default}): {p.description}")
            if t.examples:
                lines.append("نمونه درخواست‌ها: " + " | ".join(t.examples))
            lines.append("")
        return "\n".join(lines)


def _active(s: StepSpec, params: dict[str, Any]) -> bool:
    if not s.when:
        return True
    kind, _, val = s.when.partition(":")
    if kind == "platform":
        return val in params.get("platforms", ())
    if kind == "section":
        return val in params.get("sections", ())
    return bool(params.get(kind))


def _coerce(spec: ParamSpec, value: Any, allowed_platforms: tuple[str, ...]) -> Any:
    if spec.type == "date":
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as e:
            raise RegistryError(f"تاریخ «{value}» معتبر نیست (YYYY-MM-DD)") from e
    if spec.type == "text":
        text = " ".join(str(value).split())
        if len(text) > 200:
            raise RegistryError(f"پارامتر «{spec.name}» بیش از حد بلند است")
        return text
    items = [value] if isinstance(value, str) else list(value)
    items = [str(x).strip().lower() for x in items]
    allowed = allowed_platforms if spec.type == "platforms" else SECTIONS
    bad = [x for x in items if x not in allowed]
    if bad:
        raise RegistryError(f"«{', '.join(bad)}» برای «{spec.name}» مجاز نیست؛ مجازها: {', '.join(allowed)}")
    return tuple(dict.fromkeys(items))


STOCK_FORBIDDEN = "طبق قواعد این سازمان، محتوا دربارهٔ یک سهم یا نماد مشخص تولید نمی‌شود (single_stock_content در brand.yaml)."

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "registry"
