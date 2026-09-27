"""A tiny built-in task used by `proofforge smoke` to prove the whole loop end to end."""

from __future__ import annotations

from proofforge.engine.task import FixTask
from proofforge.gates.base import GateSpec

BUGGY_STATS = '''\
def median(values):
    """Return the median of a non-empty list of numbers."""
    n = len(values)
    return values[n // 2]
'''

VISIBLE_TEST = """\
from stats import median

assert median([3, 1, 2]) == 2, median([3, 1, 2])
assert median([1, 2, 3, 4]) == 2.5, median([1, 2, 3, 4])
print("visible tests passed")
"""

HOLDOUT_TEST = """\
from stats import median

assert median([5]) == 5
assert median([9, -2, 7, 0]) == 3.5
assert median([10, 3, 8, 1, 4]) == 4
data = [4, 1, 3]
median(data)
assert data == [4, 1, 3], "median must not mutate its input"
print("holdout tests passed")
"""


def median_task() -> FixTask:
    return FixTask(
        title="Fix median() for unsorted and even-length input",
        description=(
            "Issue #1: `median()` returns wrong answers. It should return the middle value of "
            "the sorted input, and the mean of the two middle values when the length is even."
        ),
        editable={"stats.py": BUGGY_STATS},
        protected={"test_stats.py": VISIBLE_TEST},
        holdout={"test_holdout.py": HOLDOUT_TEST},
        gates=[
            GateSpec(name="visible-tests", command="python test_stats.py"),
            GateSpec(name="holdout-tests", command="python test_holdout.py", kind="holdout"),
        ],
    )
