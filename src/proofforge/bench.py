"""Benchmark suites: PipelineBench (data pipelines) and RealWorld (multi-file repositories)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from proofforge.engine.loop import Strategy
from proofforge.engine.task import FixTask
from proofforge.playbooks import pipeline_doctor, realworld
from proofforge.playbooks.pipeline_doctor import CaseValidation
from proofforge.sandbox.base import Sandbox

SuiteName = Literal["pipeline", "realworld"]
SUITES: tuple[SuiteName, ...] = ("pipeline", "realworld")
ROOTS: dict[SuiteName, Path] = {
    "pipeline": Path("bench/pipelinebench/cases"),
    "realworld": Path("bench/realworld/cases"),
}


@dataclass(frozen=True)
class Case:
    suite: SuiteName
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def default_strategy(self) -> Strategy:
        # Single-file pipelines work well as whole-file rewrites; repositories need tools.
        return "rewrite" if self.suite == "pipeline" else "agent"

    def load(self, *, python: str = "python", use_solution: bool = False) -> FixTask:
        if self.suite == "pipeline":
            return pipeline_doctor.load_case(self.path, python=python, use_solution=use_solution)
        return realworld.load_case(self.path, python=python, use_solution=use_solution)

    async def validate(self, sandbox: Sandbox, *, python: str = "python") -> CaseValidation:
        if self.suite == "pipeline":
            return await pipeline_doctor.validate_case(sandbox, self.path, python=python)
        return await realworld.validate_case(sandbox, self.path, python=python)


def detect(path: Path) -> Case:
    if (path / "repo").is_dir():
        return Case("realworld", path)
    if (path / "pipeline.sql").is_file():
        return Case("pipeline", path)
    raise ValueError(f"{path} is not a PipelineBench or RealWorld case directory")


def discover(
    suites: tuple[SuiteName, ...] = SUITES,
    *,
    roots: dict[SuiteName, Path] | None = None,
    only: list[str] | None = None,
) -> list[Case]:
    roots = roots or ROOTS
    found: list[Case] = []
    for suite in suites:
        module = pipeline_doctor if suite == "pipeline" else realworld
        found += [Case(suite, p) for p in module.discover_cases(roots[suite])]
    if only:
        found = [c for c in found if c.name in set(only)]
    return found
