"""Real-world cases: multi-file repositories with hidden verification.

A case is a directory:

    case.json      {"title", "issue", "difficulty", "tags", "protected": [globs],
                    "gates": [{"name", "command", "kind"}], "image"?}
    repo/          the starting repository: source plus visible tests
    holdout/       hidden files (usually tests) injected only at verification
    solution/      reference overlay on repo/; never shown to the agent

Paths matching a `protected` glob are frozen oracle files; everything else in
repo/ is editable. Gate commands may use `{python}` for the interpreter.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel

from proofforge.engine.task import FixTask
from proofforge.gates.base import GateSpec
from proofforge.playbooks.pipeline_doctor import CaseValidation, _gate_run
from proofforge.sandbox.base import Sandbox


class GateDef(BaseModel):
    name: str
    command: str
    kind: str = "visible"
    timeout_s: int = 300


class RealWorldMeta(BaseModel):
    title: str
    issue: str
    difficulty: str = "hard"
    tags: list[str] = []
    protected: list[str]
    gates: list[GateDef]
    image: str = "python:3.12-slim"


def _files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def load_case(case_dir: Path, *, python: str = "python", use_solution: bool = False) -> FixTask:
    meta = RealWorldMeta.model_validate_json((case_dir / "case.json").read_text(encoding="utf-8"))
    repo = _files(case_dir / "repo")
    if use_solution:
        repo |= _files(case_dir / "solution")
    protected = {p: c for p, c in repo.items() if any(fnmatch(p, g) for g in meta.protected)}
    editable = {p: c for p, c in repo.items() if p not in protected}
    return FixTask(
        title=f"[RealWorld:{meta.difficulty}] {case_dir.name}: {meta.title}",
        description=meta.issue,
        image=meta.image,
        editable=editable,
        protected=protected,
        holdout=_files(case_dir / "holdout"),
        gates=[
            GateSpec(
                name=g.name,
                command=g.command.replace("{python}", python),
                kind="holdout" if g.kind == "holdout" else "visible",
                timeout_s=g.timeout_s,
            )
            for g in meta.gates
        ],
    )


def discover_cases(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.glob("*/case.json") if (p.parent / "repo").is_dir())


async def validate_case(
    sandbox: Sandbox, case_dir: Path, *, python: str = "python"
) -> CaseValidation:
    broken = await _gate_run(sandbox, load_case(case_dir, python=python))
    solved = await _gate_run(sandbox, load_case(case_dir, python=python, use_solution=True))
    return CaseValidation(
        case=case_dir.name,
        broken_fails_visible=not all(g.passed for g in broken if g.kind == "visible"),
        solution_passes_all=all(g.passed for g in solved),
        solution_gates=solved,
    )
