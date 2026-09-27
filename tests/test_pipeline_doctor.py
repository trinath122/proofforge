from __future__ import annotations

from pathlib import Path

import pytest

from proofforge.budget import BudgetGuard
from proofforge.engine.loop import Engine
from proofforge.models.registry import Mode
from proofforge.playbooks import discover_cases, load_case, validate_case
from proofforge.playbooks.pipeline_doctor import _gate_run
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY, ScriptedLLM

ROOT = Path(__file__).resolve().parents[1]
CASES = discover_cases(ROOT / "bench" / "pipelinebench" / "cases")
SHALLOW = ROOT / "tests" / "fixtures" / "shallow"


def as_edit(sql: str) -> str:
    return f"### FILE: pipeline.sql\n```sql\n{sql.rstrip()}\n```"


def test_cases_are_discovered() -> None:
    assert len(CASES) >= 3
    assert {c.name for c in CASES} >= {p.stem for p in SHALLOW.glob("*.sql")}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_case_is_broken_and_solvable(case: Path, tmp_path: Path) -> None:
    result = await validate_case(LocalSandbox(tmp_path), case, python=PY)
    assert result.broken_fails_visible, "shipped pipeline must fail the visible checks"
    assert result.solution_passes_all, [g.stdout_tail for g in result.solution_gates]
    assert result.valid


@pytest.mark.parametrize("sql", sorted(SHALLOW.glob("*.sql")), ids=lambda p: p.stem)
async def test_holdout_catches_shallow_fix(sql: Path, tmp_path: Path) -> None:
    task = load_case(CASES[[c.name for c in CASES].index(sql.stem)], python=PY)
    task = task.model_copy(update={"editable": {"pipeline.sql": sql.read_text()}})
    results = {g.name: g.passed for g in await _gate_run(LocalSandbox(tmp_path), task)}
    assert results == {"pipeline+dq": True, "holdout-pipeline+dq": False}


def test_case_layout_keeps_holdout_hidden() -> None:
    task = load_case(CASES[0], python=PY)
    seed = task.workspace_seed()
    assert set(task.editable) == {"pipeline.sql"}
    assert "solution.sql" not in seed
    assert not any(p.startswith("holdout") for p in seed)
    assert {"run_pipeline.py", "dq_check.py", "expectations.json"} <= set(task.protected)
    assert any(p.startswith("data/") for p in task.protected)
    assert "holdout_expectations.json" in task.holdout


async def test_engine_repairs_pipeline_after_holdout_feedback(tmp_path: Path) -> None:
    case = next(c for c in CASES if c.name == "latest_status_mixed_timestamps")
    shallow = (SHALLOW / f"{case.name}.sql").read_text()
    solution = (case / "solution.sql").read_text()
    budget = BudgetGuard(task_cap_usd=1.0, session_cap_usd=1.0)
    llm = ScriptedLLM([as_edit(shallow), as_edit(solution)], budget)
    engine = Engine(LocalSandbox(tmp_path), llm, budget, mode=Mode.DEV, max_rounds=3)

    receipt = await engine.fix(load_case(case, python=PY))

    assert receipt.status == "verified"
    first, second = receipt.attempts
    assert [g.passed for g in first.gates] == [True, False]
    assert second.all_passed
    feedback = llm.calls[1][1][-1].content
    assert "1 hidden check(s) also failed" in feedback
    assert "holdout_data" not in feedback
    assert "datetime(event_ts)" in receipt.diff
