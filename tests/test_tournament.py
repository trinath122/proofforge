from __future__ import annotations

from typing import Any

from proofforge.budget import BudgetGuard
from proofforge.engine.loop import Engine
from proofforge.engine.task import FixTask
from proofforge.engine.tournament import (
    APPROACHES,
    BREAKER_TEST,
    Entrant,
    discriminating,
    parse_unittest,
    rank,
    run_tournament,
)
from proofforge.llm.base import Message
from proofforge.models.registry import Mode, Role
from proofforge.receipts.schema import Attempt
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY, ScriptedLLM, Step, calls

GOOD = (
    "def median(values):\n"
    "    ordered = sorted(values)\n"
    "    n = len(ordered)\n"
    "    mid = n // 2\n"
    "    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2\n"
)
# Passes the visible test by special-casing its input; wrong for other even lists.
CHEAT = (
    "def median(values):\n"
    "    if values == [1, 2, 3, 4]:\n"
    "        return 2.5\n"
    "    return sorted(values)[len(values) // 2]\n"
)
SPEC_TESTS = (
    "import unittest\nfrom stats import median\n\n"
    "class TestMedian(unittest.TestCase):\n"
    "    def test_odd_unsorted(self):\n        self.assertEqual(median([3, 1, 2]), 2)\n\n"
    "    def test_even_other_values(self):\n        self.assertEqual(median([10, 0, 4, 6]), 5)\n\n"
    "    def test_wrong_and_failed_by_all(self):\n        self.assertEqual(median([1]), 99)\n"
)
# A Breaker can be wrong: this test rewards the cheat over the correct fix.
WRONG_TESTS = (
    "import unittest\nfrom stats import median\n\n"
    "class TestMedian(unittest.TestCase):\n"
    "    def test_wrong(self):\n        self.assertEqual(median([10, 0, 5, 7]), 7)\n"
)


def write(content: str) -> list[Step]:
    return [
        calls(("write_file", {"path": "stats.py", "content": content})),
        calls(("submit", {"summary": "fixed"})),
    ]


def scripted(
    breaker_tests: str,
    budget: BudgetGuard,
    *,
    spec_first: str = GOOD,
    repair: list[Step] | None = None,
) -> ScriptedLLM:
    plans: dict[str, list[Step]] = {
        "direct": write(CHEAT),
        "spec-first": write(spec_first),
        "repair": repair or ["Those failing tests look wrong to me."],
        "test-first": ["I could not find the problem."],
        "breaker": [
            calls(("write_file", {"path": BREAKER_TEST, "content": breaker_tests})),
            calls(("submit", {"summary": "spec tests"})),
        ],
    }

    def respond(role: Role, messages: list[Message], _t: float) -> Step:
        prompt = str(messages[1].content)
        if role == Role.BREAKER:
            who = "breaker"
        elif "independent reviewer" in prompt:
            who = "repair"
        else:
            who = next(k for k, text in APPROACHES.items() if text in prompt)
        step = sum(m.role == "assistant" for m in messages)
        return plans[who][step]

    return ScriptedLLM(respond, budget)


def engine(sandbox: LocalSandbox, llm: Any, budget: BudgetGuard) -> Engine:
    return Engine(sandbox, llm, budget, mode=Mode.DEV, strategy="agent", python=PY)


async def test_breaker_tests_pick_the_real_fix(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = scripted(SPEC_TESTS, budget)
    receipt = await run_tournament(engine(sandbox, llm, budget), task)

    assert receipt.status == "verified", receipt.note
    by_label = {a.label: a for a in receipt.attempts}
    assert set(by_label) == {"direct", "spec-first", "test-first", "breaker", "spec-first+repair"}
    # Both fixes pass the visible test; only the Breaker's tests tell them apart.
    assert by_label["spec-first"].breaker_score == "2/2"
    assert by_label["direct"].breaker_score == "1/2"
    # The winner was shown the one test every entrant failed; it declined to "fix" it.
    assert by_label["spec-first+repair"].error == "agent made no file changes"
    assert "winner: spec-first" in (receipt.note or "")
    assert "median" in receipt.diff
    # The post-hoc audit graded the loser too: it would have failed the hidden checks.
    assert not by_label["direct"].all_passed
    assert any(g.kind == "holdout" for g in by_label["direct"].gates)


async def test_winner_is_chosen_without_hidden_results(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = scripted(WRONG_TESTS, budget)
    receipt = await run_tournament(engine(sandbox, llm, budget), task)

    # A wrong Breaker test favoured the cheat. ProofForge does not overrule its choice with
    # hidden results; it reports the miss instead of quietly shipping the other entrant.
    assert receipt.status == "failed"
    assert "winner: direct" in (receipt.note or "")
    assert "Selection miss" in (receipt.note or "")
    assert "spec-first" in (receipt.note or "")


def test_parse_and_rank() -> None:
    out = (
        "test_a (pf_breaker_tests.test_breaker.TestA) ... ok\n"
        "test_b (pf_breaker_tests.test_breaker.TestA.test_b) ... FAIL\n"
        "test_c (pf_breaker_tests.test_breaker.TestA) ... ERROR\n"
        "test_d (pf_breaker_tests.test_breaker.TestB.test_d)\n"
        "Docstring line ... ok\n"
        "test_e (pf_breaker_tests.test_breaker.TestB) ... skipped 'later'\n"
        "FAIL: test_b (pf_breaker_tests.test_breaker.TestA.test_b)\n"
    )
    assert parse_unittest(out) == {
        "TestA.test_a": True,
        "TestA.test_b": False,
        "TestA.test_c": False,
        "TestB.test_d": True,
    }

    def entrant(label: str, breaker: dict[str, bool], cost: float = 0.0) -> Entrant:
        attempt = Attempt(
            round=1, branch=0, model_key="m", checkpoint="c", edited_files=["x"], cost_usd=cost
        )
        return Entrant(label, attempt, state=object(), breaker=breaker)  # type: ignore[arg-type]

    a = entrant("a", {"t1": True, "t2": False, "t3": False})
    b = entrant("b", {"t1": True, "t2": True, "t3": False}, cost=1.0)
    c = entrant("c", {"t1": True, "t2": True, "t3": False}, cost=0.5)
    assert discriminating([a, b, c]) == ["t1", "t2"], "t3 fails everywhere, so it cannot count"
    assert [e.label for e in rank([a, b, c])] == ["c", "b", "a"], "ties go to the cheaper one"


async def test_repair_from_breaker_feedback_is_kept_when_it_helps(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    # Both fixes are the cheat; the Breaker's tests expose it, and the repair pass fixes it.
    llm = scripted(SPEC_TESTS, budget, spec_first=CHEAT, repair=write(GOOD))
    receipt = await run_tournament(engine(sandbox, llm, budget), task)

    assert receipt.status == "verified", receipt.note
    repair = next(a for a in receipt.attempts if a.label.endswith("+repair"))
    assert repair.error is None
    assert repair.breaker_score == "2/3 (was 1/3)"
    assert "pf_breaker_tests" not in receipt.diff, "the reviewer's tests are not part of the fix"
