from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from proofforge.bench import Case, detect, discover
from proofforge.budget import BudgetGuard
from proofforge.engine.loop import Engine
from proofforge.gates.base import GateSpec
from proofforge.models.registry import Mode
from proofforge.playbooks import harbor
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY, ScriptedLLM, calls

FIXTURES = Path(__file__).parent / "fixtures" / "harbor"
TASK = FIXTURES / "mini_add"
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
GIT_INIT = (
    "git init -q && git add -A && git -c user.name=pf -c user.email=pf@example.com commit -qm base"
)


def test_loader_maps_harbor_task() -> None:
    task = harbor.load_task(TASK)
    assert task.image == "ghcr.io/example/mini-add:latest"
    assert task.workdir == "/app"
    assert task.repo_in_image
    assert task.hidden_only
    assert set(task.holdout) == {"/tests/test.sh", "/tests/check.py"}
    assert task.editable == {}
    assert task.context == {}, "the reference solution never reaches the agent"
    (gate,) = task.gates
    assert gate.kind == "holdout"
    assert "bash /tests/test.sh" in gate.command
    assert gate.timeout_s == 120
    solved = harbor.load_task(TASK, use_solution=True)
    assert solved.setup_command == "bash /solution/solve.sh"
    assert set(solved.context) == {"/solution/solve.sh"}


def test_loader_rejects_docker_builds_and_finds_workdir(tmp_path: Path) -> None:
    task = tmp_path / "t"
    shutil.copytree(TASK, task)
    (task / "instruction.md").write_text(
        "A code repository is available in the `/testbed` directory."
    )
    assert harbor.load_task(task).workdir == "/testbed"
    (task / "task.toml").write_text("[verifier]\ntimeout_sec = 10\n")  # no published image
    (task / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\nRUN apt-get update\n")
    with pytest.raises(harbor.HarborError, match="Docker build"):
        harbor.load_task(task)
    (task / "environment" / "Dockerfile").unlink()
    with pytest.raises(harbor.HarborError, match="no docker image"):
        harbor.load_task(task)


def test_loader_uses_published_image_for_built_environments(tmp_path: Path) -> None:
    # Long-Horizon Terminal-Bench: a full Dockerfile, plus the image it was published as.
    task = tmp_path / "lhtb"
    shutil.copytree(TASK, task)
    (task / "instruction.md").write_text("Edit /work/app/policy.py.")
    (task / "environment" / "Dockerfile").write_text(
        "FROM python:3.11-slim\nRUN pip install numpy\nWORKDIR /tmp\nWORKDIR /work/app/\n"
    )
    toml = '[verifier]\ntimeout_sec = 300\n[environment]\ndocker_image = "{}"\n'
    (task / "task.toml").write_text(toml.format("someone/lhtb-x:20260615"))
    loaded = harbor.load_task(task)
    assert loaded.image == "docker.io/someone/lhtb-x:20260615"
    assert loaded.workdir == "/work/app"
    (task / "task.toml").write_text(toml.format("ghcr.io/org/img:1"))
    assert harbor.load_task(task).image == "ghcr.io/org/img:1"
    (task / "task.toml").write_text(toml.format("ubuntu:24.04"))
    assert harbor.load_task(task).image == "docker.io/library/ubuntu:24.04"
    separate = '[verifier]\nenvironment_mode = "separate"\n[environment]\ndocker_image = "a/b:1"\n'
    (task / "task.toml").write_text(separate)
    with pytest.raises(harbor.HarborError, match="own image"):
        harbor.load_task(task)


def test_reward_is_read_from_gate_output() -> None:
    assert harbor.reward("...\nharbor reward: 0.625\n") == 0.625
    assert harbor.reward("harbor reward: none") is None
    assert harbor.reward("no verifier ran") is None


def test_suite_discovery(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    shutil.copytree(TASK, root / "mini_add")
    assert detect(root / "mini_add") == Case("harbor", root / "mini_add")
    found = discover(("harbor",), roots={"harbor": root})
    assert [c.name for c in found] == ["mini_add"]
    assert found[0].default_strategy == "agent"
    assert found[0].load().repo_in_image
    assert harbor.discover_tasks(root, ids=["nope"]) == []


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize(
    ("script", "passes"),
    [
        ("echo 1 > LOGS/reward.txt; exit 1", True),
        ("echo 0.6 > LOGS/reward.txt; exit 0", False),
        ("exit 0", True),
        ("exit 3", False),
    ],
)
def test_verify_command_reads_reward(tmp_path: Path, script: str, passes: bool) -> None:
    tests, logs = tmp_path / "tests", tmp_path / "logs"
    tests.mkdir()
    (tests / "test.sh").write_text(script.replace("LOGS", str(logs / "verifier")))
    command = harbor.VERIFY.replace("/tests/", f"{tests}/").replace("/logs/", f"{logs}/")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    assert "harbor reward:" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="in-image repositories run on Linux images")
async def test_agent_fixes_in_image_repo_end_to_end(tmp_path: Path) -> None:
    # LocalSandbox stands in for the task image: the repo is seeded at /app and committed,
    # and a portable Python verifier replaces the bash one.
    task = harbor.load_task(TASK).model_copy(
        update={
            "context": {"calc.py": BUGGY},
            "setup_command": GIT_INIT,
            "gates": [
                GateSpec(name="harbor-verifier", command=f"{PY} ../tests/check.py", kind="holdout")
            ],
        }
    )
    budget = BudgetGuard(task_cap_usd=1.0, session_cap_usd=1.0)
    llm = ScriptedLLM(
        [
            calls(("list_files", {"path": "/app"})),
            calls(("search", {"pattern": "def add"})),
            calls(("write_file", {"path": "/app/calc.py", "content": FIXED})),
            calls(("write_file", {"path": "debug_calc.py", "content": "print(1)\n"})),
            calls(("write_file", {"path": "pkg/new_mod.py", "content": "X = 1\n"})),
            calls(("submit", {"summary": "add instead of subtract"})),
        ],
        budget,
    )
    engine = Engine(
        LocalSandbox(tmp_path), llm, budget, mode=Mode.DEV, strategy="rewrite", python=PY
    )
    receipt = await engine.fix(task)

    assert receipt.status == "verified", receipt.note
    assert [g.passed for g in receipt.reproduction] == [False], "hidden verifier proves the bug"
    first_prompt = llm.calls[0][1][1].content
    assert "repository is at `/app`" in first_prompt
    assert "check.py" not in first_prompt, "the hidden verifier never reaches the agent"
    assert "calc.py" in llm.calls[1][1][-1].content, "list_files works on the in-image repo"
    assert "calc.py:1:def add" in llm.calls[2][1][-1].content
    assert "+    return a + b" in receipt.diff
    assert "-    return a - b" in receipt.diff
    assert ".pf_tool" not in receipt.diff
    assert "debug_calc.py" not in receipt.diff, "root-level scratch scripts are left out"
    assert "pkg/new_mod.py" in receipt.diff, "real new files are part of the change"


def test_loader_keeps_crlf_in_test_data(tmp_path: Path) -> None:
    task = tmp_path / "t"
    shutil.copytree(TASK, task)
    (task / "tests" / "fixture.patch").write_bytes(b"+--boundary\r\n+Content-Type: text/plain\r\n")
    (task / "tests" / "test.sh").write_bytes(b"#!/bin/bash\r\necho ok\r\n")
    holdout = harbor.load_task(task).holdout
    assert holdout["/tests/fixture.patch"] == "+--boundary\r\n+Content-Type: text/plain\r\n"
    assert holdout["/tests/test.sh"] == "#!/bin/bash\necho ok\n", "shell scripts are normalized"
