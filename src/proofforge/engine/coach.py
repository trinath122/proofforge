"""Lightweight steering for long agent episodes.

Observed on SWE-bench Pro: agents on large repositories either explore until the step
limit without editing anything, or repeat the same reads and searches. The coach adds
three short, deterministic notes: steps remaining near the limit, a nudge after a long
stretch without edits, and a marker on calls that exactly repeat an earlier one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STEP_WARNING = 8
EXPLORE_NUDGE_STEPS = 25
REPEAT_CHECKED = frozenset({"read_file", "search", "list_files"})


def steps_left_note(remaining: int) -> str:
    return (
        f"You have {remaining} steps left. Stop exploring: finish the change, run the most "
        "relevant check once, then call `submit`. Unsubmitted work is still verified, but a "
        "focused finish is better than running out mid-edit."
    )


def explore_nudge(steps: int) -> str:
    return (
        f"You have used {steps} steps without changing any file. Stop exploring: write your "
        "best change now based on what you have learned, then run a check and refine it."
    )


REREAD_NOTE = (
    "NOTE: you already read exactly this, and the file has not changed since. Its content is "
    "above in this conversation. Use it: make your edit now (edit_file or replace_lines). "
    "Need other lines? Read a different range."
)
REPEAT_NOTE = (
    "NOTE: you already made this exact call and nothing has changed since; the result is "
    "the same. Act on what you know.\n"
)


@dataclass
class Coach:
    max_steps: int
    _seen: dict[tuple[str, str], int] = field(default_factory=dict)
    _last_edit_step: int = -1
    _nudged: bool = False

    def before_step(self, step: int) -> str | None:
        remaining = self.max_steps - step
        if remaining == STEP_WARNING and self.max_steps > 2 * STEP_WARNING:
            return steps_left_note(remaining)
        return None

    def observe(
        self, step: int, call: tuple[str, str], *, writes_before: int, writes_after: int, out: str
    ) -> str:
        name = call[0]
        if writes_after != writes_before:
            self._last_edit_step = step
        key = call
        repeated = name in REPEAT_CHECKED and self._seen.get(key) == writes_after
        self._seen[key] = writes_after
        if not repeated:
            return out
        if name == "read_file":
            # The earlier read is still in context (see engine/context.py); resending it
            # only feeds the loop.
            return REREAD_NOTE
        return REPEAT_NOTE + out

    def after_step(self, step: int, *, submitted: bool) -> str | None:
        if self._nudged or submitted or step - self._last_edit_step < EXPLORE_NUDGE_STEPS:
            return None
        self._nudged = True
        return explore_nudge(step + 1)
