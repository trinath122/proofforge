"""Pipeline Doctor: repair broken data pipelines and prove the repair with data-quality gates.

A PipelineBench case is a directory:

    case.json                 {"title": ..., "issue": ..., "tags": [...]}
    pipeline.sql              the broken pipeline (the only file the agent may edit)
    solution.sql              reference fix, never shown to the agent; proves the case is solvable
    data/*.csv                visible source data, loaded as raw_<name> tables
    expectations.json         visible data-quality expectations
    holdout/data/*.csv        hidden source data with different edge cases
    holdout/expectations.json hidden expectations, checked only at verification
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel

from proofforge.engine.task import FixTask
from proofforge.gates.base import GateResult, GateSpec, run_gates, workspace_path
from proofforge.sandbox.base import NOOP, Sandbox

ASSETS = files("proofforge.playbooks.assets")
RUNNER = "run_pipeline.py"
DQ = "dq_check.py"
IMAGE = "python:3.12-slim"


class CaseMeta(BaseModel):
    title: str
    issue: str
    tags: list[str] = []


def _read_dir(root: Path, prefix: str) -> dict[str, str]:
    return {f"{prefix}/{p.name}": p.read_text(encoding="utf-8") for p in sorted(root.glob("*.csv"))}


def load_case(case_dir: Path, *, python: str = "python", use_solution: bool = False) -> FixTask:
    meta = CaseMeta.model_validate_json((case_dir / "case.json").read_text(encoding="utf-8"))
    sql_file = "solution.sql" if use_solution else "pipeline.sql"
    protected = {
        RUNNER: ASSETS.joinpath(RUNNER).read_text(encoding="utf-8"),
        DQ: ASSETS.joinpath(DQ).read_text(encoding="utf-8"),
        "expectations.json": (case_dir / "expectations.json").read_text(encoding="utf-8"),
        **_read_dir(case_dir / "data", "data"),
    }
    holdout = {
        "holdout_expectations.json": (case_dir / "holdout" / "expectations.json").read_text(
            encoding="utf-8"
        ),
        **_read_dir(case_dir / "holdout" / "data", "holdout_data"),
    }
    description = (
        f"{meta.issue}\n\n"
        "The pipeline is `pipeline.sql` (SQLite dialect). `run_pipeline.py` loads every CSV in "
        "`data/` as a `raw_<file name>` table, then runs `pipeline.sql`. `dq_check.py` verifies "
        "the output tables against `expectations.json`. Repair `pipeline.sql` so it is correct "
        "for any valid input data, not only the sample shown."
    )
    return FixTask(
        title=f"[PipelineBench] {case_dir.name}: {meta.title}",
        description=description,
        image=IMAGE,
        editable={"pipeline.sql": (case_dir / sql_file).read_text(encoding="utf-8")},
        protected=protected,
        holdout=holdout,
        gates=[
            GateSpec(
                name="pipeline+dq",
                command=f"{python} {RUNNER} data && {python} {DQ} expectations.json",
            ),
            GateSpec(
                name="holdout-pipeline+dq",
                command=(
                    f"{python} {RUNNER} holdout_data && {python} {DQ} holdout_expectations.json"
                ),
                kind="holdout",
            ),
        ],
    )


def discover_cases(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.glob("*/case.json"))


class CaseValidation(BaseModel):
    case: str
    broken_fails_visible: bool
    solution_passes_all: bool
    solution_gates: list[GateResult]

    @property
    def valid(self) -> bool:
        return self.broken_fails_visible and self.solution_passes_all


async def _gate_run(sandbox: Sandbox, task: FixTask) -> list[GateResult]:
    base = await sandbox.base(task.image)
    seed = {workspace_path(p): c.encode() for p, c in task.workspace_seed().items()}
    setup = await sandbox.run(base, NOOP, files=seed)
    return await run_gates(sandbox, setup.checkpoint, task.oracle(), include_holdout=True)


async def validate_case(
    sandbox: Sandbox, case_dir: Path, *, python: str = "python"
) -> CaseValidation:
    """A valid case is broken as shipped and fully solvable by its reference solution."""
    broken = await _gate_run(sandbox, load_case(case_dir, python=python))
    solved = await _gate_run(sandbox, load_case(case_dir, python=python, use_solution=True))
    return CaseValidation(
        case=case_dir.name,
        broken_fails_visible=not all(g.passed for g in broken if g.kind == "visible"),
        solution_passes_all=all(g.passed for g in solved),
        solution_gates=solved,
    )
