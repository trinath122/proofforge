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
from dataclasses import dataclass, field, replace

from proofforge.budget import BudgetExceededError
from proofforge.engine.loop import Engine, Prepared, _State
from proofforge.engine.prompts import build_breaker_messages
from proofforge.engine.task import FixTask
from proofforge.engine.tools import Workspace
from proofforge.gates.base import run_gates, workspace_path
from proofforge.models.registry import Role
from proofforge.receipts.schema import Attempt, Receipt
from proofforge.sandbox.base import NOOP
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
BREAKER_DIR = "pf_breaker_tests"
BREAKER_TEST = f"{BREAKER_DIR}/test_breaker.py"
BREAKER_TIMEOUT_S = 180


def breaker_command(python: str) -> str:
    """Standard library only: pytest is not installed in every task image."""
    return f"{python} -m unittest -v {BREAKER_TEST}"


_HEAD = re.compile(r"^(test\w*) \(([\w.]+)\)")
_OUTCOME = re.compile(r" \.\.\. (ok|FAIL|ERROR|expected failure|unexpected success|skipped.*)$")


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


def parse_unittest(output: str) -> dict[str, bool]:
    """Per-test outcome (`Class.test_name` -> passed) from `python -m unittest -v` output.

    Handles both `test_x (pkg.mod.Class)` and the 3.11+ `test_x (pkg.mod.Class.test_x)`
    forms, and docstrings printed on the line before the result. Skipped tests are left out.
    """
    results: dict[str, bool] = {}
    pending: str | None = None
    for line in output.splitlines():
        head = _HEAD.match(line)
        if head:
            name, where = head.group(1), head.group(2).removesuffix("." + head.group(1))
            pending = f"{where.rsplit('.', 1)[-1]}.{name}"
        if pending and (outcome := _OUTCOME.search(line)):
            if not outcome.group(1).startswith("skipped"):
                results[pending] = outcome.group(1) in {"ok", "expected failure"}
            pending = None
    return results


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

    # One repair pass: the winner sees which Breaker tests its change fails. Kept only if it
    # stays eligible and passes more Breaker tests; hidden checks still play no part.
    repair_attempt, repaired = (
        await _repair(engine, prep, winner, entrants, test_code) if test_code else (None, None)
    )
    if repair_attempt is not None:
        attempts.append(repair_attempt)
    if repaired is not None:
        ordered = [repaired, *ordered]
        winner = repaired

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


def _passed(entrant: Entrant) -> int:
    return sum(entrant.breaker.values())


def _evidence(test: str, entrants: list[Entrant]) -> str:
    """How the other independent fixes fared on a test: the agent's only hint that it is wrong."""
    others = [e for e in entrants if e.breaker and e.state is not None]
    failed = sum(not e.breaker.get(test, False) for e in others)
    if failed == len(others) and len(others) > 1:
        return f"{test} (fails on all {failed} independent fixes; possibly a wrong test)"
    return f"{test} (fails on {failed} of {len(others)} fixes)"


async def _repair(
    engine: Engine,
    prep: Prepared,
    winner: Entrant,
    entrants: list[Entrant],
    test_code: str,
) -> tuple[Attempt | None, Entrant | None]:
    """Returns the repair attempt (for the receipt) and the repaired entrant if kept."""
    failing = sorted(n for n, ok in winner.breaker.items() if not ok)
    if not winner.eligible or winner.state is None or not failing:
        return None, None
    task = prep.task
    seeded = await engine.sandbox.run(
        winner.state.checkpoint,
        NOOP,
        files={workspace_path(BREAKER_TEST, task.workdir): test_code.encode()},
        keep=True,
        cwd=task.workdir,
    )
    note = (
        f"An independent reviewer wrote extra tests from the task specification in "
        f"`{BREAKER_TEST}`. {len(failing)} of them fail on your change:\n"
        + "\n".join(f"- {_evidence(n, entrants)}" for n in failing[:20])
        + f"\nRun `{breaker_command(engine.python)}` to see why. The reviewer is often wrong: "
        "for each failure, compare the test with the exact wording of the specification "
        "first. Change the code only where the test matches the specification, never change "
        "the test file, and if every failing test is wrong, submit without changes and say "
        "which tests are wrong and why."
    )
    state = replace(winner.state, checkpoint=seeded.checkpoint, notes=[note])
    try:
        attempt, new_state = await engine._agent_attempt(
            task,
            prep.oracle,
            state,
            rnd=2,
            branch=0,
            role=Role.FIXER,
            include_holdout=False,
        )
    except BudgetExceededError:
        return None, None
    attempt.label = f"{winner.label}+repair"
    if new_state is None:
        attempt.error = attempt.error or "repair made no change"
        return attempt, None
    # The reviewer's file is not part of the change.
    cleaned = await engine.sandbox.run(
        new_state.checkpoint, f"rm -rf {BREAKER_DIR}", keep=True, cwd=task.workdir
    )
    new_state.checkpoint = cleaned.checkpoint
    attempt.checkpoint = cleaned.checkpoint.id
    attempt.edited_files = [f for f in attempt.edited_files if not f.startswith(BREAKER_DIR)]
    candidate = Entrant(attempt.label, attempt, new_state)
    await _score(engine, task, candidate, test_code)
    total = len(winner.breaker)
    attempt.breaker_score = f"{_passed(candidate)}/{total} (was {_passed(winner)}/{total})"
    if candidate.eligible and _passed(candidate) > _passed(winner):
        return attempt, candidate
    attempt.error = "repair not kept: it did not pass more reviewer tests while staying eligible"
    return attempt, None


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
        run_command=breaker_command(engine.python),
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
        breaker_command(engine.python),
        files={workspace_path(BREAKER_TEST, task.workdir): test_code.encode()},
        keep=False,
        timeout_s=BREAKER_TIMEOUT_S,
        cwd=task.workdir,
    )
    entrant.breaker = parse_unittest(res.stderr + "\n" + res.stdout)


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
