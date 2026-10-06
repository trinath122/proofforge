"""Tournament: several approaches race from one checkpoint, a Breaker tries to expose them.

1. Every entrant starts from the same sandbox checkpoint with a different approach.
2. In parallel, the Breaker reads the specification and writes extra tests that a fix
   which only looks right would fail. It never sees the hidden checks or the entrants.
3. Entrants that pass the visible checks without tampering are ranked by how many of the
   Breaker's tests they pass. Only tests at least one entrant passes count: a test every
   entrant fails cannot tell them apart and may simply be wrong.
4. Only the winner is graded by the hidden checks, and its result is the verdict.

Choosing the winner never looks at hidden results: picking the entrant that happens to pass
the hidden checks would be selecting on the test set. After the verdict the losers are
also graded, but only so the receipt can say whether the choice was right.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from proofforge.budget import BudgetExceededError
from proofforge.engine.loop import Engine, Prepared, _State
from proofforge.engine.prompts import build_breaker_messages
from proofforge.engine.task import FixTask
from proofforge.engine.tools import Workspace
from proofforge.gates.base import run_gates, workspace_path
from proofforge.models.registry import Role
from proofforge.receipts.schema import Attempt, Receipt
from proofforge.sandbox.offline import OfflineSandbox

APPROACHES: dict[str, str] = {
    "direct": "Find the root cause quickly and make the smallest change that fixes it.",
    "spec-first": (
        "Before editing anything, turn every requirement in the task into a numbered "
        "checklist in your reasoning. Implement against it and confirm each item before "
        "you submit."
    ),
    "test-first": (
        "Before changing any code, write a small script under /tmp that reproduces the "
        "problem and fails. Fix the code until it passes, then extend it with the edge "
        "cases the task mentions."
    ),
}
BREAKER_TEST = "pf_breaker_tests/test_breaker.py"
BREAKER_TIMEOUT_S = 180
_RESULT = re.compile(r"^(PASSED|FAILED|ERROR) [^\s:]*::(\S+)", re.MULTILINE)


@dataclass
class Entrant:
    label: str
    attempt: Attempt
    state: _State | None
    breaker: dict[str, bool] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        """Passed every visible check, changed something, and tampered with nothing."""
        a = self.attempt
        visible = [g for g in a.gates if g.kind == "visible"]
        return (
            self.state is not None
            and not a.tampered_files
            and not a.impossible_reason
            and all(g.passed for g in visible)
        )


def parse_pytest(output: str) -> dict[str, bool]:
    """Per-test outcome from `pytest -rA` output."""
    return {name: kind == "PASSED" for kind, name in _RESULT.findall(output)}


def discriminating(entrants: list[Entrant]) -> list[str]:
    """Breaker tests that at least one entrant passes."""
    names = {n for e in entrants for n in e.breaker}
    return sorted(n for n in names if any(e.breaker.get(n) for e in entrants))


def rank(entrants: list[Entrant]) -> list[Entrant]:
    """Best first: eligible, then breaker tests passed, then visible checks, then cheaper."""
    tests = discriminating(entrants)

    def key(e: Entrant) -> tuple[bool, int, int, float]:
        passed = sum(e.breaker.get(n, False) for n in tests)
        return (e.eligible, passed, e.attempt.visible_passed, -e.attempt.cost_usd)

    return sorted(entrants, key=key, reverse=True)


async def run_tournament(
    engine: Engine, task: FixTask, approaches: dict[str, str] | None = None
) -> Receipt:
    if task.offline and not isinstance(engine.sandbox, OfflineSandbox):
        engine = _sealed(engine)
    approaches = approaches or APPROACHES
    prep = await engine.prepare(task)
    if stop := engine.precheck(prep):
        return engine.receipt(prep, stop[0], attempts=[], note=stop[1])

    state = _State(prep.setup.checkpoint, dict(task.editable), prep.repro, [])
    races = [
        engine._agent_attempt(
            task,
            prep.oracle,
            state,
            rnd=1,
            branch=i,
            role=Role.CODER,
            approach=text,
            include_holdout=False,
        )
        for i, text in enumerate(approaches.values())
    ]
    breaker_job = asyncio.create_task(_breaker_safe(engine, prep))
    results = await asyncio.gather(*races, return_exceptions=True)
    breaker_attempt, test_code = await breaker_job

    entrants: list[Entrant] = []
    budget_hit = ""
    for label, result in zip(approaches, results, strict=True):
        if isinstance(result, BudgetExceededError):
            budget_hit = str(result)
            continue
        if isinstance(result, BaseException):
            raise result
        attempt, new_state = result
        attempt.label = label
        entrants.append(Entrant(label, attempt, new_state))
    attempts = [e.attempt for e in entrants] + [breaker_attempt]

    if test_code:
        await asyncio.gather(*(_score(engine, task, e, test_code) for e in entrants))
    tests = discriminating(entrants)
    for e in entrants:
        e.attempt.breaker_score = f"{sum(e.breaker.get(n, False) for n in tests)}/{len(tests)}"

    if not entrants:
        return engine.receipt(prep, "budget_exceeded", attempts=attempts, note=budget_hit)
    ordered = rank(entrants)
    winner = ordered[0]
    if not winner.eligible:
        reported = [e for e in entrants if e.attempt.impossible_reason]
        if reported and len(reported) == len(entrants):
            return engine.receipt(
                prep,
                "reported_impossible",
                attempts=attempts,
                note="Every entrant reported the task impossible: "
                + reported[0].attempt.impossible_reason,
            )

    # The verdict: hidden checks on the winner only.
    graded = await _grade(engine, prep, winner)
    verified = graded and winner.attempt.all_passed
    # Post-hoc audit of the losers, recorded but never used to choose.
    await asyncio.gather(*(_grade(engine, prep, e) for e in ordered[1:]))
    note = _note(winner, ordered, tests, test_code, verified=verified)
    final = winner.state
    return engine.receipt(
        prep,
        "verified" if verified else ("budget_exceeded" if budget_hit else "failed"),
        attempts=attempts,
        final_checkpoint=final.checkpoint.id if final else None,
        final_gates=winner.attempt.gates,
        diff=await engine._diff(task, final) if final else "",
        note=note,
    )


def _sealed(engine: Engine) -> Engine:
    import copy  # noqa: PLC0415 - only needed for offline tasks

    sealed = copy.copy(engine)
    sealed.sandbox = OfflineSandbox(engine.sandbox)
    return sealed


async def _breaker(engine: Engine, prep: Prepared) -> tuple[Attempt, str]:
    task = prep.task
    ws = Workspace(
        engine.sandbox,
        prep.setup.checkpoint,
        protected=set(task.protected),
        python=engine.python,
        workdir=task.workdir,
    )
    messages = build_breaker_messages(
        task.description,
        test_path=BREAKER_TEST,
        files=sorted(set(task.editable) | set(task.context) | set(task.protected)),
        workdir=task.workdir if task.repo_in_image else None,
    )
    attempt = Attempt(
        round=1, branch=-1, model_key="", checkpoint=None, edited_files=[], label="breaker"
    )
    await engine._episode(task, ws, messages, attempt, role=Role.BREAKER, temperature=0.3)
    attempt.transcript = [m.to_openai() for m in messages]
    attempt.summary = ws.summary
    attempt.edited_files = sorted(ws.touched)
    if BREAKER_TEST not in ws.touched:
        attempt.error = "breaker wrote no tests"
        return attempt, ""
    raw = await engine.sandbox.read(ws.checkpoint, workspace_path(BREAKER_TEST, task.workdir))
    return attempt, raw.decode(errors="replace")


async def _breaker_safe(engine: Engine, prep: Prepared) -> tuple[Attempt, str]:
    """The Breaker is optional: if it fails, the tournament ranks on visible checks."""
    try:
        return await _breaker(engine, prep)
    except Exception as exc:  # budget, sandbox or model failure
        return _failed_breaker(exc), ""


def _failed_breaker(exc: BaseException) -> Attempt:
    return Attempt(
        round=1,
        branch=-1,
        model_key="",
        checkpoint=None,
        edited_files=[],
        label="breaker",
        error=f"{type(exc).__name__}: {exc}"[:500],
    )


async def _score(engine: Engine, task: FixTask, entrant: Entrant, test_code: str) -> None:
    if entrant.state is None:
        return
    res = await engine.sandbox.run(
        entrant.state.checkpoint,
        f"{engine.python} -m pytest -q -rA -p no:cacheprovider {BREAKER_TEST}",
        files={workspace_path(BREAKER_TEST, task.workdir): test_code.encode()},
        keep=False,
        timeout_s=BREAKER_TIMEOUT_S,
        cwd=task.workdir,
    )
    entrant.breaker = parse_pytest(res.stdout + "\n" + res.stderr)


async def _grade(engine: Engine, prep: Prepared, entrant: Entrant) -> bool:
    """Run the full oracle, hidden checks included, on an entrant's change."""
    if entrant.state is None or entrant.attempt.tampered_files:
        return False
    entrant.attempt.gates = await run_gates(
        engine.sandbox, entrant.state.checkpoint, prep.oracle, include_holdout=True
    )
    return True


def _note(
    winner: Entrant,
    ordered: list[Entrant],
    tests: list[str],
    test_code: str,
    *,
    verified: bool,
) -> str:
    lines = [
        f"Tournament winner: {winner.label} "
        f"(breaker {winner.attempt.breaker_score}, chosen before any hidden check ran).",
    ]
    if not test_code:
        lines.append("The Breaker wrote no usable tests; ranking used visible checks only.")
    else:
        lines.append(f"The Breaker wrote tests; {len(tests)} told the entrants apart.")
    passed_hidden = [e.label for e in ordered if e.attempt.all_passed]
    if verified:
        lines.append("The winner passed every hidden check.")
    elif passed_hidden:
        lines.append(
            "Selection miss: the winner failed hidden checks that "
            + ", ".join(passed_hidden)
            + " passed (found in the post-hoc audit)."
        )
    else:
        lines.append("No entrant passed every hidden check.")
    return " ".join(lines)
