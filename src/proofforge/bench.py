"""Benchmark suites.

- pipeline:  PipelineBench, broken data pipelines (bundled)
- realworld: hard system-design repositories (bundled)
- harbor:    external Harbor-format tasks such as SWE-bench Pro V2 / HARD-51 (point --root at them)
- impossible: RealWorld cases whose visible tests contradict the spec; measures cheating
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from proofforge.engine.loop import Strategy
from proofforge.engine.task import FixTask
from proofforge.playbooks import harbor, impossible, pipeline_doctor, realworld
from proofforge.playbooks.pipeline_doctor import CaseValidation
from proofforge.sandbox.base import Sandbox

SuiteName = Literal["pipeline", "realworld", "harbor", "impossible"]
SUITES: tuple[SuiteName, ...] = ("pipeline", "realworld")  # bundled; harbor is opt-in
ROOTS: dict[SuiteName, Path] = {
    "pipeline": Path("bench/pipelinebench/cases"),
    "realworld": Path("bench/realworld/cases"),
    "harbor": Path("bench/harbor/tasks"),
    "impossible": Path("bench/impossible/cases"),
}


@dataclass(frozen=True)
class Case:
    suite: SuiteName
    path: Path
    reward_target: float | None = None  # dense-reward Harbor tasks: score that counts as solved
    solution_setup: str | None = None  # Harbor: workaround to install the reference solution

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
            return harbor.load_task(
                self.path,
                use_solution=use_solution,
                reward_target=self.reward_target,
                solution_setup=self.solution_setup,
            )
        if self.suite == "impossible":
            return impossible.load_case(self.path, python=python, use_solution=use_solution)
        return realworld.load_case(self.path, python=python, use_solution=use_solution)

    async def validate(self, sandbox: Sandbox, *, python: str = "python") -> CaseValidation:
        if self.suite == "pipeline":
            return await pipeline_doctor.validate_case(sandbox, self.path, python=python)
        if self.suite == "harbor":
            return await harbor.validate_task(
                sandbox,
                self.path,
                reward_target=self.reward_target,
                solution_setup=self.solution_setup,
            )
        if self.suite == "impossible":
            return await impossible.validate_case(sandbox, self.path, python=python)
        return await realworld.validate_case(sandbox, self.path, python=python)


def with_targets(cases: list[Case], targets_file: Path) -> list[Case]:
    """Apply per-task reward targets ({"task": 0.9}) from a JSON file next to an ids file."""
    targets = json.loads(targets_file.read_text(encoding="utf-8"))
    return [
        replace(c, reward_target=float(targets[c.name])) if c.name in targets else c for c in cases
    ]


def with_task_settings(cases: list[Case], folder: Path) -> list[Case]:
    """Per-task settings kept next to an ids file: targets.json and solution_setup.json."""
    if (folder / "targets.json").is_file():
        cases = with_targets(cases, folder / "targets.json")
    setups_file = folder / "solution_setup.json"
    if setups_file.is_file():
        setups = json.loads(setups_file.read_text(encoding="utf-8"))
        cases = [
            replace(c, solution_setup=str(setups[c.name])) if c.name in setups else c for c in cases
        ]
    return cases


def detect(path: Path) -> Case:
    if path.parent.parent.name == "impossible" and (path / "case.json").is_file():
        return Case("impossible", path)
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
        elif suite == "impossible":
            paths = impossible.discover_cases(roots[suite])
        else:
            paths = harbor.discover_tasks(roots[suite])
        found += [Case(suite, p) for p in paths]
    if only:
        found = [c for c in found if c.name in set(only)]
    return found
