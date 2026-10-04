from __future__ import annotations

from proofforge.budget import BudgetGuard, ProviderBudgetError
from proofforge.demo import VISIBLE_TEST
from proofforge.engine.loop import Engine, unified_diff
from proofforge.engine.task import FixTask
from proofforge.llm.base import Message
from proofforge.models.registry import Mode, Role
from proofforge.receipts import render_markdown
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import CHEAT_FIX, EDIT_TEST_FILE, GOOD_FIX, ScriptedLLM


def make_engine(sandbox: LocalSandbox, llm: ScriptedLLM, budget: BudgetGuard, **kw: int) -> Engine:
    return Engine(sandbox, llm, budget, mode=Mode.DEV, **kw)


async def test_verified_first_round(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([GOOD_FIX], budget)
    receipt = await make_engine(sandbox, llm, budget).fix(task)

    assert receipt.status == "verified"
    assert [g.passed for g in receipt.reproduction] == [False]
    assert {g.kind for g in receipt.final_gates} == {"visible", "holdout"}
    assert all(g.passed for g in receipt.final_gates)
    assert "+    ordered = sorted(values)" in receipt.diff
    assert receipt.total_cost_usd > 0
    assert receipt.usage["lightning"].calls == 1
    assert llm.calls[0][0] is Role.CODER


async def test_holdout_catches_special_casing_then_recovers(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([CHEAT_FIX, GOOD_FIX], budget)
    receipt = await make_engine(sandbox, llm, budget).fix(task)

    assert receipt.status == "verified"
    first, second = receipt.attempts
    assert first.visible_passed == 1
    assert not first.all_passed
    assert any(g.kind == "holdout" and not g.passed for g in first.gates)
    feedback = llm.calls[1][1][-1].content
    assert "hidden check(s) also failed" in feedback
    assert "test_holdout" not in feedback  # hidden test content never leaks
    assert llm.calls[1][0] is Role.FIXER
    assert second.all_passed


async def test_edit_to_frozen_test_is_rejected(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([EDIT_TEST_FILE, GOOD_FIX], budget)
    receipt = await make_engine(sandbox, llm, budget).fix(task)

    assert receipt.status == "verified"
    assert receipt.attempts[0].rejected_edits == ["test_stats.py"]
    assert receipt.attempts[0].error == "no applicable file edit in reply"
    assert "rejected because they are not editable: test_stats.py" in llm.calls[1][1][-1].content


async def test_branch_search_picks_passing_branch(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    def respond(role: Role, messages: list[Message], temperature: float) -> str:
        return GOOD_FIX if temperature > 0.2 else CHEAT_FIX

    llm = ScriptedLLM(respond, budget)
    receipt = await make_engine(sandbox, llm, budget, branch_width=2).fix(task)

    assert receipt.status == "verified"
    assert len(receipt.attempts) == 2
    assert {a.round for a in receipt.attempts} == {1}
    assert [a.all_passed for a in receipt.attempts] == [False, True]


async def test_fails_after_max_rounds(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([CHEAT_FIX] * 2, budget)
    receipt = await make_engine(sandbox, llm, budget, max_rounds=2).fix(task)

    assert receipt.status == "failed"
    assert len(receipt.attempts) == 2
    assert "special" not in receipt.diff  # diff reflects the best candidate's code
    assert "[1, 2, 3, 4]" in receipt.diff


async def test_budget_cap_stops_the_run(sandbox: LocalSandbox, task: FixTask) -> None:
    tight = BudgetGuard(task_cap_usd=0.0001, session_cap_usd=1.0)
    llm = ScriptedLLM([CHEAT_FIX] * 5, tight, tokens=(10_000, 1_000))
    receipt = await make_engine(sandbox, llm, tight, max_rounds=5).fix(task)

    assert receipt.status == "budget_exceeded"
    assert len(receipt.attempts) == 1
    assert "task cap" in (receipt.note or "")


async def test_already_passing_needs_no_model(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    fixed = task.model_copy(
        update={
            "editable": {
                "stats.py": "def median(v):\n    s=sorted(v);n=len(s)\n"
                "    return s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2\n"
            }
        }
    )
    llm = ScriptedLLM([], budget)
    receipt = await make_engine(sandbox, llm, budget).fix(fixed)

    assert receipt.status == "already_passing"
    assert llm.calls == []


async def test_setup_failure_is_reported(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    broken = task.model_copy(update={"setup_command": "exit 3"})
    receipt = await make_engine(sandbox, ScriptedLLM([], budget), budget).fix(broken)
    assert receipt.status == "reproduction_failed"


def test_unified_diff_only_changed_files() -> None:
    diff = unified_diff({"a.py": "x = 1\n", "b.py": "y\n"}, {"a.py": "x = 2\n", "b.py": "y\n"})
    assert "a/a.py" in diff
    assert "b.py" not in diff


def test_visible_test_fixture_is_the_frozen_one(task: FixTask) -> None:
    assert task.protected["test_stats.py"] == VISIBLE_TEST


async def test_unusable_reply_is_explained_to_the_model(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM(
        ["<think>The median should sort the values and", GOOD_FIX],
        budget,
        finish_reasons=["length", "stop"],
    )
    receipt = await make_engine(sandbox, llm, budget).fix(task)

    assert receipt.status == "verified"
    first = receipt.attempts[0]
    assert first.error == "no applicable file edit in reply (cut off at output limit)"
    assert first.finish_reason == "length"
    assert "median should sort" in first.response_excerpt
    retry_prompt = llm.calls[1][1][-1].content
    assert "Note on your previous reply" in retry_prompt
    assert "cut off at the output limit" in retry_prompt
    assert "Unusable model replies" in render_markdown(receipt)


async def test_provider_budget_exhaustion_is_reported(sandbox: LocalSandbox, task: FixTask) -> None:
    class Broke:
        async def complete(self, *args: object, **kwargs: object) -> object:
            raise ProviderBudgetError("budget is exhausted (HTTP 402)")

    budget = BudgetGuard(task_cap_usd=1.0, session_cap_usd=1.0)
    engine = Engine(sandbox, Broke(), budget, mode=Mode.DEV)  # type: ignore[arg-type]
    receipt = await engine.fix(task)
    assert receipt.status == "budget_exceeded"
    assert "HTTP 402" in (receipt.note or "")
