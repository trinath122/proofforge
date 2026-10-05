"""Gates are executable checks. A change is 'done' only when every gate passes.

Integrity rules enforced here:
- Frozen oracle: protected files (tests, fixtures) are hashed before any change.
  A candidate that alters them is flagged, and gates always run against the
  original copies, so editing a test can never make it pass.
- Hidden holdouts: holdout files never enter the agent's workspace. They are
  injected only for the verification run and the agent never sees their output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from proofforge.sandbox.base import WORKDIR, Checkpoint, Sandbox

TAIL_CHARS = 4000


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workspace_path(rel: str, workdir: str = WORKDIR) -> str:
    """Absolute sandbox path for a task file. Absolute paths (e.g. `/tests/...`) pass through."""
    if rel.startswith("/"):
        return rel
    return f"{workdir.rstrip('/')}/{rel}"


class GateSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    command: str
    kind: Literal["visible", "holdout"] = "visible"
    timeout_s: int = Field(default=300, ge=1, le=3600)


# Dense-reward verifiers (Harbor tasks such as Long-Horizon Terminal-Bench) print their
# score on this line; a higher score is progress even when the gate still fails.
REWARD_LINE = re.compile(r"harbor reward: ([0-9.eE+-]+)")


def gate_reward(output: str) -> float | None:
    """The last reward a verifier printed in `output`, if any."""
    found = REWARD_LINE.findall(output)
    try:
        return float(found[-1]) if found else None
    except ValueError:
        return None


class GateResult(BaseModel):
    name: str
    kind: Literal["visible", "holdout"]
    passed: bool
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    elapsed_s: float


class Oracle(BaseModel):
    """The frozen verification contract for one task."""

    model_config = ConfigDict(frozen=True)

    gates: tuple[GateSpec, ...]
    workdir: str = WORKDIR
    protected: dict[str, str] = Field(description="relative path -> original content")
    holdout: dict[str, str] = Field(default_factory=dict, description="relative path -> content")

    @property
    def protected_hashes(self) -> dict[str, str]:
        return {p: sha256(c.encode()) for p, c in sorted(self.protected.items())}

    @property
    def digest(self) -> str:
        payload = {
            "gates": [g.model_dump() for g in self.gates],
            "workdir": self.workdir,
            "protected": self.protected_hashes,
            "holdout": {p: sha256(c.encode()) for p, c in sorted(self.holdout.items())},
        }
        return sha256(json.dumps(payload, sort_keys=True).encode())

    def visible_gates(self) -> list[GateSpec]:
        return [g for g in self.gates if g.kind == "visible"]


async def detect_tampering(sandbox: Sandbox, at: Checkpoint, oracle: Oracle) -> list[str]:
    """Protected files whose content differs from the frozen original (or vanished)."""
    tampered: list[str] = []
    for rel, expected in oracle.protected_hashes.items():
        try:
            actual = sha256(await sandbox.read(at, workspace_path(rel, oracle.workdir)))
        except Exception:  # deleted or unreadable counts as tampered
            actual = "missing"
        if actual != expected:
            tampered.append(rel)
    return tampered


async def run_gates(
    sandbox: Sandbox,
    at: Checkpoint,
    oracle: Oracle,
    *,
    include_holdout: bool,
) -> list[GateResult]:
    """Run gates in parallel, each on a disposable copy with the original oracle files."""
    wd = oracle.workdir
    restore = {workspace_path(p, wd): c.encode() for p, c in oracle.protected.items()}
    if include_holdout:
        restore |= {workspace_path(p, wd): c.encode() for p, c in oracle.holdout.items()}
    selected = [g for g in oracle.gates if include_holdout or g.kind == "visible"]

    async def one(gate: GateSpec) -> GateResult:
        res = await sandbox.run(
            at, gate.command, files=restore, keep=False, timeout_s=gate.timeout_s, cwd=wd
        )
        return GateResult(
            name=gate.name,
            kind=gate.kind,
            passed=res.ok,
            exit_code=res.exit_code,
            stdout_tail=res.stdout[-TAIL_CHARS:],
            stderr_tail=res.stderr[-TAIL_CHARS:],
            elapsed_s=res.elapsed_s,
        )

    return list(await asyncio.gather(*(one(g) for g in selected)))
