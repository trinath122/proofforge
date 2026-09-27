from __future__ import annotations

import shlex
import sys
from collections.abc import Callable

import pytest

from proofforge.budget import BudgetGuard
from proofforge.demo import median_task
from proofforge.engine.task import FixTask
from proofforge.gates.base import GateSpec
from proofforge.llm.base import Completion, Message
from proofforge.models.registry import MODELS, Role
from proofforge.sandbox.local import LocalSandbox

PY = shlex.quote(sys.executable)

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


class ScriptedLLM:
    """Returns canned responses in order and charges the budget like the real client."""

    def __init__(
        self,
        responses: list[str] | Callable[[Role, list[Message], float], str],
        budget: BudgetGuard,
        tokens: tuple[int, int] = (1000, 200),
    ) -> None:
        self._responses = responses
        self._budget = budget
        self._tokens = tokens
        self.calls: list[tuple[Role, list[Message], float]] = []

    async def complete(
        self, role: Role, messages: list[Message], *, temperature: float = 0.2
    ) -> Completion:
        self._budget.ensure_headroom()
        self.calls.append((role, messages, temperature))
        if callable(self._responses):
            text = self._responses(role, messages, temperature)
        else:
            text = self._responses[len(self.calls) - 1]
        spec = MODELS["lightning"]
        cost = self._budget.charge(spec, *self._tokens)
        return Completion(
            text=text,
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
