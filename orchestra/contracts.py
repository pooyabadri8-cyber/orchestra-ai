"""Typed messages between the orchestrator and agents. Agents never talk to each other: every handoff is an artifact
referenced by id and hash, routed by the workflow."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1"


class ArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_s: float = 120
    max_llm_calls: int = 4
    max_cost_usd: float = 0.5
    max_tool_calls: int = 10


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str = CONTRACT_VERSION
    org_id: str
    run_id: str
    step_key: str
    attempt: int = Field(ge=1)
    idempotency_key: str
    agent_id: str
    agent_version: str
    task_type: str
    inputs: dict[str, ArtifactRef] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    limits: Limits = Limits()


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error_class: Literal["retryable", "terminal", "policy", "budget"]
    message: str


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_key: str
    attempt: int
    status: Literal["succeeded", "failed", "needs_input"]
    outputs: dict[str, ArtifactRef] = Field(default_factory=dict)
    error: ErrorInfo | None = None
    usage: Usage = Usage()
