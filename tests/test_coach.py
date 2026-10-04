from __future__ import annotations

from proofforge.engine.coach import (
    EXPLORE_NUDGE_STEPS,
    REPEAT_NOTE,
    REREAD_NOTE,
    STEP_WARNING,
    Coach,
)


def test_steps_left_warning_once_near_the_limit() -> None:
    coach = Coach(max_steps=40)
    notes = [s for s in range(40) if coach.before_step(s)]
    assert notes == [40 - STEP_WARNING]
    assert not any(Coach(max_steps=10).before_step(s) for s in range(10))


def test_repeated_calls_are_marked_until_something_changes() -> None:
    coach = Coach(max_steps=80)
    call = ("read_file", '{"path": "a.py"}')
    assert coach.observe(0, call, writes_before=0, writes_after=0, out="x") == "x"
    assert coach.observe(1, call, writes_before=0, writes_after=0, out="x") == REREAD_NOTE
    hunt = ("search", '{"pattern": "a"}')
    coach.observe(1, hunt, writes_before=0, writes_after=0, out="hit")
    assert coach.observe(1, hunt, writes_before=0, writes_after=0, out="hit") == REPEAT_NOTE + "hit"
    coach.observe(2, ("write_file", "{}"), writes_before=0, writes_after=1, out="wrote")
    assert coach.observe(3, call, writes_before=1, writes_after=1, out="x") == "x"
    run = ("run", '{"command": "pytest"}')
    coach.observe(4, run, writes_before=1, writes_after=1, out="ok")
    assert coach.observe(5, run, writes_before=1, writes_after=1, out="ok") == "ok"


def test_nudge_after_long_exploration_only_once() -> None:
    coach = Coach(max_steps=200)
    nudges = [s for s in range(100) if coach.after_step(s, submitted=False)]
    assert nudges == [EXPLORE_NUDGE_STEPS - 1]
    fresh = Coach(max_steps=200)
    fresh.observe(10, ("write_file", "{}"), writes_before=0, writes_after=1, out="")
    assert [s for s in range(60) if fresh.after_step(s, submitted=False)] == [
        10 + EXPLORE_NUDGE_STEPS
    ]
