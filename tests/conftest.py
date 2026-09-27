from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import pytest

from proofforge.budget import BudgetGuard
from proofforge.demo import median_task
from proofforge.engine.task import FixTask
from proofforge.gates.base import GateSpec
from proofforge.llm.base import Completion, Message, ToolCall
from proofforge.models.registry import MODELS, Role
from proofforge.sandbox.local import LocalSandbox

# Double quotes work in both POSIX sh and Windows cmd.
PY = f'"{sys.executable}"'


def py(code: str) -> str:
    """A portable shell command that runs a one-line Python snippet."""
    return f'{PY} -c "{code}"'


GOOD_FIX = '''### FILE: stats.py
```python
def median(values):
    """Return the median of a non-empty list of numbers."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
```'''

# Passes the visible test by special-casing its inputs; hidden holdouts catch it.
CHEAT_FIX = """### FILE: stats.py
```python
def median(values):
    if values == [1, 2, 3, 4]:
        return 2.5
    return sorted(values)[len(values) // 2]
```"""

EDIT_TEST_FILE = """### FILE: test_stats.py
```python
print("tests? what tests")
```"""


Step = str | list[tuple[str, dict[str, Any]]]


def calls(*items: tuple[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """A scripted agent step made of tool calls: calls(("read_file", {"path": "x"}))."""
    return list(items)


class ScriptedLLM:
    """Returns canned responses in order and charges the budget like the real client.

    A response is either text, or a list of (tool name, arguments) tool calls.
    """

    def __init__(
        self,
        responses: list[Step] | Callable[[Role, list[Message], float], Step],
        budget: BudgetGuard,
        tokens: tuple[int, int] = (1000, 200),
        finish_reasons: list[str] | None = None,
    ) -> None:
        self._responses = responses
        self._budget = budget
        self._tokens = tokens
        self._finish = finish_reasons or []
        self.calls: list[tuple[Role, list[Message], float]] = []

    async def complete(
        self,
        role: Role,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        self._budget.ensure_headroom()
        self.calls.append((role, list(messages), temperature))
        if callable(self._responses):
            step = self._responses(role, messages, temperature)
        else:
            step = self._responses[len(self.calls) - 1]
        text = step if isinstance(step, str) else ""
        tool_calls = (
            []
            if isinstance(step, str)
            else [
                ToolCall(id=f"call_{len(self.calls)}_{i}", name=n, arguments=json.dumps(a))
                for i, (n, a) in enumerate(step)
            ]
        )
        spec = MODELS["lightning"]
        cost = self._budget.charge(spec, *self._tokens)
        n = len(self.calls) - 1
        return Completion(
            text=text,
            finish_reason=self._finish[n] if n < len(self._finish) else "stop",
            tool_calls=tool_calls,
            model_key=spec.key,
            prompt_tokens=self._tokens[0],
            completion_tokens=self._tokens[1],
            cost_usd=cost,
        )


def local_median_task() -> FixTask:
    task = median_task()
    return task.model_copy(
        update={
            "gates": [
                GateSpec(name="visible-tests", command=f"{PY} test_stats.py"),
                GateSpec(name="holdout-tests", command=f"{PY} test_holdout.py", kind="holdout"),
            ]
        }
    )


@pytest.fixture
def task() -> FixTask:
    return local_median_task()


@pytest.fixture
def sandbox(tmp_path: pytest.TempPathFactory) -> LocalSandbox:
    return LocalSandbox(root=tmp_path)  # type: ignore[arg-type]


@pytest.fixture
def budget() -> BudgetGuard:
    return BudgetGuard(task_cap_usd=1.0, session_cap_usd=5.0)
