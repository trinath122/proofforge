"""Benchmark suites.

- pipeline:  PipelineBench, broken data pipelines (bundled)
- realworld: hard system-design repositories (bundled)
- harbor:    external Harbor-format tasks such as SWE-bench Pro V2 / HARD-51 (point --root at them)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from proofforge.engine.loop import Strategy
from proofforge.engine.task import FixTask
from proofforge.playbooks import harbor, pipeline_doctor, realworld
from proofforge.playbooks.pipeline_doctor import CaseValidation
from proofforge.sandbox.base import Sandbox

SuiteName = Literal["pipeline", "realworld", "harbor"]
SUITES: tuple[SuiteName, ...] = ("pipeline", "realworld")  # bundled; harbor is opt-in
ROOTS: dict[SuiteName, Path] = {
    "pipeline": Path("bench/pipelinebench/cases"),
    "realworld": Path("bench/realworld/cases"),
    "harbor": Path("bench/harbor/tasks"),
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
        if self.suite == "harbor":
            return harbor.load_task(self.path, use_solution=use_solution)
        return realworld.load_case(self.path, python=python, use_solution=use_solution)

    async def validate(self, sandbox: Sandbox, *, python: str = "python") -> CaseValidation:
        if self.suite == "pipeline":
            return await pipeline_doctor.validate_case(sandbox, self.path, python=python)
        if self.suite == "harbor":
            return await harbor.validate_task(sandbox, self.path)
        return await realworld.validate_case(sandbox, self.path, python=python)


def detect(path: Path) -> Case:
    if (path / "task.toml").is_file():
        return Case("harbor", path)
    if (path / "repo").is_dir():
        return Case("realworld", path)
    if (path / "pipeline.sql").is_file():
        return Case("pipeline", path)
    raise ValueError(f"{path} is not a PipelineBench, RealWorld or Harbor task directory")


def discover(
    suites: tuple[SuiteName, ...] = SUITES,
    *,
    roots: dict[SuiteName, Path] | None = None,
    only: list[str] | None = None,
) -> list[Case]:
    roots = ROOTS | (roots or {})
    found: list[Case] = []
    for suite in suites:
        if suite == "pipeline":
            paths = pipeline_doctor.discover_cases(roots[suite])
        elif suite == "realworld":
            paths = realworld.discover_cases(roots[suite])
        else:
            paths = harbor.discover_tasks(roots[suite])
        found += [Case(suite, p) for p in paths]
    if only:
        found = [c for c in found if c.name in set(only)]
    return found
