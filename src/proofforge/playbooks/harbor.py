"""Harbor-format tasks: SWE-bench Pro V2 (HARD-51), Long-Horizon Terminal-Bench, and the like.

A Harbor task is a directory:

    task.toml               image, timeouts, resources
    instruction.md          what the agent sees
    environment/Dockerfile  FROM <image>, or any Dockerfile when task.toml names a published image
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
from proofforge.gates.base import GateResult, GateSpec
from proofforge.playbooks.pipeline_doctor import CaseValidation, _gate_run
from proofforge.receipts import Receipt
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


def _qualified(image: str) -> str:
    """Docker Hub shorthand ("user/repo:tag") as a full registry reference."""
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}" if "/" in image else f"docker.io/library/{image}"


def _dockerfile(task_dir: Path) -> list[str]:
    path = task_dir / "environment" / "Dockerfile"
    if not path.is_file():
        return []
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _image(task_dir: Path, meta: dict[str, object]) -> str:
    env = meta.get("environment")
    image = env.get("docker_image") if isinstance(env, dict) else None
    verifier = meta.get("verifier")
    if isinstance(verifier, dict) and verifier.get("environment_mode") == "separate":
        raise HarborError(f"{task_dir.name}: verifier runs in its own image; not supported")
    if isinstance(image, str) and image:
        # A published image is the built environment (Long-Horizon Terminal-Bench ships
        # one per task), so the Dockerfile does not need to be rebuilt.
        return _qualified(image)
    lines = _dockerfile(task_dir)
    if len(lines) > 1:
        raise HarborError(
            f"{task_dir.name}: environment needs a Docker build ({len(lines)} instructions); "
            "only prebuilt images are supported"
        )
    if lines and lines[0].upper().startswith("FROM "):
        return lines[0].split(None, 1)[1].strip()
    raise HarborError(f"{task_dir.name}: no docker image in task.toml or Dockerfile")


def _workdir(task_dir: Path, instruction: str) -> str:
    found = re.search(r"available in the `(/[\w./-]+)` directory", instruction)
    if found:
        return found.group(1).rstrip("/")
    workdirs = [
        ln.split(None, 1)[1].strip()
        for ln in _dockerfile(task_dir)
        if ln.upper().startswith("WORKDIR ")
    ]
    if not workdirs:
        return DEFAULT_WORKDIR
    return workdirs[-1].rstrip("/") or "/"


REWARD_LINE = re.compile(r"harbor reward: ([0-9.eE+-]+)")


def reward(output: str) -> float | None:
    """The verifier's reward (0..1) from a harbor gate's output, if it wrote one."""
    found = REWARD_LINE.findall(output)
    try:
        return float(found[-1]) if found else None
    except ValueError:
        return None


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
        workdir=_workdir(task_dir, instruction),
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
        note=f"reward {_fmt(gates_reward(broken))} -> {_fmt(gates_reward(solved))}",
    )


def gates_reward(gates: list[GateResult]) -> float | None:
    found = [reward(g.stdout_tail) for g in gates]
    values = [r for r in found if r is not None]
    return max(values) if values else None


def receipt_reward(receipt: Receipt) -> float | None:
    """Best verifier reward the run reached (dense-reward suites such as LHTB)."""
    gates = list(receipt.final_gates)
    for attempt in receipt.attempts:
        gates.extend(attempt.gates)
    return gates_reward(gates)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"
