"""Harbor-format tasks: SWE-bench Pro V2 (and its HARD-51 subset), Terminal-Bench style suites.

A Harbor task is a directory:

    task.toml               image, timeouts, resources
    instruction.md          what the agent sees
    environment/Dockerfile  FROM <image> (only prebuilt images are supported here)
    tests/                  the verifier: test.sh plus its data; injected only at verification
    solution/solve.sh       reference solution, used by `--validate`

ProofForge maps it onto its own contract: the repository ships inside the image, the
verifier is a hidden holdout gate the agent never sees, and the verifier's own reset of
test files plus ProofForge's frozen gates keep the grade honest.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from proofforge.engine.task import FixTask
from proofforge.gates.base import GateSpec
from proofforge.playbooks.pipeline_doctor import CaseValidation, _gate_run
from proofforge.sandbox.base import Sandbox

MAX_GATE_TIMEOUT_S = 3600
DEFAULT_WORKDIR = "/app"

# Runs the task's verifier and turns Harbor's reward file into a pass/fail exit code. The
# output ends with a short diagnostic (pass counts and the test runner's own tail) so
# receipts explain a failure; agents never see holdout output, only how many checks failed.
VERIFY = (
    "mkdir -p /logs/verifier; bash /tests/test.sh > /tmp/pf_verifier.log 2>&1; code=$?; "
    "echo '--- verifier summary'; "
    "grep -E '^(Required tests|Passed tests|RESULT)' /tmp/pf_verifier.log || "
    "tail -c 1500 /tmp/pf_verifier.log; "
    "echo '--- test runner output (tail)'; "
    "tail -c 2000 /logs/verifier/run-script-stdout.txt 2>/dev/null; "
    "tail -c 600 /logs/verifier/run-script-stderr.txt 2>/dev/null; "
    "r=$(cat /logs/verifier/reward.txt 2>/dev/null || true); "
    'echo "harbor reward: ${r:-none}"; '
    'if [ -n "$r" ]; then awk -v r="$r" \'BEGIN { exit !(r + 0 >= 1) }\'; '
    "else exit $code; fi"
)


class HarborError(ValueError):
    pass


def _texts(root: Path, mount: str) -> dict[str, str]:
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            # Byte-exact: test patches can carry CRLF fixtures (e.g. HTTP multipart bodies),
            # which read_text() would silently rewrite.
            text = p.read_bytes().decode("utf-8")
            if p.suffix == ".sh":
                text = text.replace("\r\n", "\n")  # tolerate Windows checkouts
            out[f"{mount}/{p.relative_to(root).as_posix()}"] = text
    return out


def _image(task_dir: Path, meta: dict[str, object]) -> str:
    env = meta.get("environment")
    image = env.get("docker_image") if isinstance(env, dict) else None
    dockerfile = task_dir / "environment" / "Dockerfile"
    if dockerfile.is_file():
        lines = [
            ln.strip()
            for ln in dockerfile.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if len(lines) > 1:
            raise HarborError(
                f"{task_dir.name}: environment needs a Docker build ({len(lines)} instructions); "
                "only prebuilt images are supported"
            )
        if not image and lines and lines[0].upper().startswith("FROM "):
            image = lines[0].split(None, 1)[1].strip()
    if not isinstance(image, str) or not image:
        raise HarborError(f"{task_dir.name}: no docker image in task.toml or Dockerfile")
    return image


def _workdir(instruction: str) -> str:
    found = re.search(r"available in the `(/[\w./-]+)` directory", instruction)
    return found.group(1).rstrip("/") if found else DEFAULT_WORKDIR


def load_task(task_dir: Path, *, use_solution: bool = False) -> FixTask:
    meta = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    verifier = meta.get("verifier", {})
    timeout = int(float(verifier.get("timeout_sec", 1800))) if isinstance(verifier, dict) else 1800
    solution = _texts(task_dir / "solution", "/solution") if use_solution else {}
    return FixTask(
        title=f"[Harbor] {task_dir.name}",
        description=instruction,
        image=_image(task_dir, meta),
        workdir=_workdir(instruction),
        repo_in_image=True,
        setup_command="bash /solution/solve.sh" if solution else "true",
        context=solution,
        holdout=_texts(task_dir / "tests", "/tests"),
        gates=[
            GateSpec(
                name="harbor-verifier",
                command=VERIFY,
                kind="holdout",
                timeout_s=max(60, min(timeout, MAX_GATE_TIMEOUT_S)),
            )
        ],
    )


def discover_tasks(root: Path, ids: list[str] | None = None) -> list[Path]:
    tasks = sorted(p.parent for p in root.glob("*/task.toml"))
    if ids is not None:
        wanted = set(ids)
        tasks = [t for t in tasks if t.name in wanted]
    return tasks


async def validate_task(sandbox: Sandbox, task_dir: Path) -> CaseValidation:
    """Free: the verifier must fail as shipped and pass with the reference solution."""
    broken = await _gate_run(sandbox, load_task(task_dir))
    solved = await _gate_run(sandbox, load_task(task_dir, use_solution=True))
    return CaseValidation(
        case=task_dir.name,
        broken_fails_visible=not all(g.passed for g in broken),
        solution_passes_all=all(g.passed for g in solved),
        solution_gates=solved,
    )
