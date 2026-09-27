from __future__ import annotations

from pathlib import Path

import pytest

from proofforge.bench import Case, detect, discover
from proofforge.budget import BudgetGuard
from proofforge.engine.loop import Engine
from proofforge.gates.base import run_gates, workspace_path
from proofforge.models.registry import Mode
from proofforge.playbooks import realworld
from proofforge.playbooks.pipeline_doctor import _gate_run
from proofforge.sandbox.base import NOOP
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY, ScriptedLLM, calls

ROOT = Path(__file__).resolve().parents[1]
ROOTS = {
    "pipeline": ROOT / "bench/pipelinebench/cases",
    "realworld": ROOT / "bench/realworld/cases",
}
CASES = realworld.discover_cases(ROOTS["realworld"])
SHALLOW = ROOT / "tests" / "fixtures" / "shallow_realworld"


def by_name(name: str) -> Path:
    return next(c for c in CASES if c.name == name)


def test_suites_are_discovered() -> None:
    names = {c.name for c in CASES}
    assert {"rate_limiter_token_bucket", "job_queue_exactly_once"} <= names
    everything = discover(roots=ROOTS)  # type: ignore[arg-type]
    assert {c.suite for c in everything} == {"pipeline", "realworld"}
    assert [c.name for c in discover(roots=ROOTS, only=["payments_schema_drift"])] == [  # type: ignore[arg-type]
        "payments_schema_drift"
    ]


def test_detect_case_kind(tmp_path: Path) -> None:
    assert detect(by_name("rate_limiter_token_bucket")).suite == "realworld"
    assert detect(ROOTS["pipeline"] / "payments_schema_drift").suite == "pipeline"
    assert Case("realworld", tmp_path).default_strategy == "agent"
    assert Case("pipeline", tmp_path).default_strategy == "rewrite"
    with pytest.raises(ValueError, match="not a PipelineBench"):
        detect(tmp_path)


def test_loader_splits_editable_protected_and_hidden() -> None:
    task = realworld.load_case(by_name("rate_limiter_token_bucket"), python=PY)
    assert "ratelimit/limiter.py" in task.editable
    assert "tests/test_limiter.py" in task.protected
    assert not any(p.startswith("tests/") for p in task.editable)
    assert set(task.holdout) == {"hidden_tests/__init__.py", "hidden_tests/test_limiter_hidden.py"}
    assert not any("hidden" in p for p in task.workspace_seed())
    assert all("{python}" not in g.command for g in task.gates)
    solved = realworld.load_case(by_name("rate_limiter_token_bucket"), python=PY, use_solution=True)
    assert "threading.Lock" in solved.editable["ratelimit/limiter.py"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_reference_solution_passes_everything(case: Path, tmp_path: Path) -> None:
    task = realworld.load_case(case, python=PY, use_solution=True)
    results = await _gate_run(LocalSandbox(tmp_path), task)
    assert all(g.passed for g in results), [g.stdout_tail + g.stderr_tail for g in results]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_shipped_code_fails_visible_checks(case: Path, tmp_path: Path) -> None:
    task = realworld.load_case(case, python=PY)
    sandbox = LocalSandbox(tmp_path)
    base = await sandbox.base(task.image)
    seeded = await sandbox.run(
        base, NOOP, files={workspace_path(p): c.encode() for p, c in task.workspace_seed().items()}
    )
    results = await run_gates(sandbox, seeded.checkpoint, task.oracle(), include_holdout=False)
    assert not all(g.passed for g in results)


@pytest.mark.parametrize(
    "fixture", sorted(p for p in SHALLOW.iterdir() if p.is_dir()), ids=lambda p: p.name
)
async def test_hidden_checks_catch_shallow_fix(fixture: Path, tmp_path: Path) -> None:
    task = realworld.load_case(by_name(fixture.name), python=PY)
    overlay = {
        p.relative_to(fixture).as_posix(): p.read_text() for p in fixture.rglob("*") if p.is_file()
    }
    task = task.model_copy(update={"editable": task.editable | overlay})
    results = {g.name: g.passed for g in await _gate_run(LocalSandbox(tmp_path), task)}
    assert results == {"visible-tests": True, "hidden-tests": False}


async def test_agent_solves_real_world_case_end_to_end(tmp_path: Path) -> None:
    case = by_name("rate_limiter_token_bucket")
    solution = (case / "solution" / "ratelimit" / "limiter.py").read_text()
    test_cmd = f"{PY} -m unittest discover -s tests -t . -q"
    budget = BudgetGuard(task_cap_usd=1.0, session_cap_usd=1.0)
    llm = ScriptedLLM(
        [
            calls(("list_files", {"path": "."})),
            calls(
                ("read_file", {"path": "README.md"}),
                ("read_file", {"path": "ratelimit/limiter.py"}),
            ),
            calls(("run", {"command": test_cmd})),
            calls(("write_file", {"path": "ratelimit/limiter.py", "content": solution})),
            calls(("run", {"command": test_cmd})),
            calls(("submit", {"summary": "locked token buckets with LRU and idle eviction"})),
        ],
        budget,
    )
    engine = Engine(LocalSandbox(tmp_path), llm, budget, mode=Mode.DEV, strategy="agent")
    receipt = await engine.fix(realworld.load_case(case, python=PY))

    assert receipt.status == "verified", receipt.note
    attempt = receipt.attempts[0]
    assert {g.name for g in attempt.gates} == {"visible-tests", "hidden-tests"}
    assert attempt.edited_files == ["ratelimit/limiter.py"]
    assert "+import threading" in receipt.diff
