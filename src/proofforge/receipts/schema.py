"""Proof receipts: the full, replayable record of what the agent did and what proved it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from proofforge.gates.base import GateResult

Status = Literal[
    "verified",
    "failed",
    "budget_exceeded",
    "already_passing",
    "reproduction_failed",
]


class Attempt(BaseModel):
    round: int
    branch: int
    model_key: str
    checkpoint: str | None
    edited_files: list[str]
    rejected_edits: list[str] = Field(
        default_factory=list, description="edits outside the allowed files"
    )
    tampered_files: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None
    finish_reason: str | None = None
    response_excerpt: str = Field(default="", description="tail of the raw model reply")
    steps: int = 0
    summary: str = ""
    transcript: list[dict[str, Any]] = Field(
        default_factory=list, description="full agent conversation; training data"
    )

    @property
    def visible_passed(self) -> int:
        return sum(g.passed for g in self.gates if g.kind == "visible")

    @property
    def all_passed(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates) and not self.tampered_files


class ModelUsage(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class Receipt(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    task_title: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mode: str
    status: Status
    image: str
    base_checkpoint: str
    final_checkpoint: str | None = None
    oracle_digest: str
    protected_hashes: dict[str, str]
    reproduction: list[GateResult]
    attempts: list[Attempt]
    final_gates: list[GateResult] = Field(default_factory=list)
    diff: str = ""
    usage: dict[str, ModelUsage] = Field(default_factory=dict)
    total_cost_usd: float = 0.0
    wall_time_s: float = 0.0
    note: str | None = None
