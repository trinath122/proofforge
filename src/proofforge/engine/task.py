"""Task definition for the Issue -> Verified Fix playbook."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from proofforge.gates.base import GateSpec, Oracle
from proofforge.sandbox.base import NOOP


class FixTask(BaseModel):
    title: str
    description: str
    image: str = "python:3.12-slim"
    setup_command: str = NOOP
    editable: dict[str, str] = Field(description="files the agent may change: path -> content")
    context: dict[str, str] = Field(
        default_factory=dict, description="read-only files shown to the agent"
    )
    protected: dict[str, str] = Field(
        default_factory=dict, description="frozen oracle files (visible tests, fixtures)"
    )
    holdout: dict[str, str] = Field(
        default_factory=dict, description="hidden tests, injected only at verification"
    )
    gates: list[GateSpec]

    @model_validator(mode="after")
    def _check(self) -> FixTask:
        overlap = set(self.editable) & (set(self.protected) | set(self.holdout))
        if overlap:
            raise ValueError(f"editable files cannot also be oracle files: {sorted(overlap)}")
        if not any(g.kind == "visible" for g in self.gates):
            raise ValueError("at least one visible gate is required")
        if self.holdout and not any(g.kind == "holdout" for g in self.gates):
            raise ValueError("holdout files were given but no holdout gate uses them")
        return self

    def oracle(self) -> Oracle:
        return Oracle(gates=tuple(self.gates), protected=self.protected, holdout=self.holdout)

    def workspace_seed(self) -> dict[str, str]:
        """Everything the agent's workspace starts with. Holdout files are excluded."""
        return self.editable | self.context | self.protected
