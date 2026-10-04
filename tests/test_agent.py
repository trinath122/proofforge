from __future__ import annotations

import json

import pytest

from proofforge.budget import BudgetGuard
from proofforge.engine.coach import STEP_WARNING
from proofforge.engine.loop import Engine
from proofforge.engine.task import FixTask
from proofforge.engine.tools import TOOL_SPECS, PathError, Workspace, normalize
from proofforge.gates.base import workspace_path
from proofforge.models.registry import Mode
from proofforge.receipts import render_markdown
from proofforge.sandbox.base import NOOP
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY, ScriptedLLM, calls

GOOD = (
    "def median(values):\n"
    "    ordered = sorted(values)\n"
    "    n = len(ordered)\n"
    "    mid = n // 2\n"
    "    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2\n"
)
TEST_CMD = f"{PY} test_stats.py"


def agent(sandbox: LocalSandbox, llm: ScriptedLLM, budget: BudgetGuard, **kw: int) -> Engine:
    return Engine(sandbox, llm, budget, mode=Mode.DEV, strategy="agent", python=PY, **kw)


async def test_agent_explores_fixes_and_submits(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM(
        [
            calls(("list_files", {})),
            calls(("read_file", {"path": "stats.py"}), ("run", {"command": TEST_CMD})),
            calls(
                (
                    "edit_file",
                    {
                        "path": "stats.py",
                        "old": "    n = len(values)\n    return values[n // 2]\n",
                        "new": GOOD.split("\n", 1)[1],
                    },
                )
            ),
            calls(("run", {"command": TEST_CMD})),
            calls(("submit", {"summary": "sort first; average middle pair for even length"})),
        ],
        budget,
    )
    receipt = await agent(sandbox, llm, budget).fix(task)

    assert receipt.status == "verified", receipt.note
    attempt = receipt.attempts[0]
    assert attempt.steps == 5
    assert attempt.edited_files == ["stats.py"]
    assert attempt.summary.startswith("sort first")
    tool_outputs = [m["content"] for m in attempt.transcript if m["role"] == "tool"]
    assert any("test_stats.py" in o for o in tool_outputs)  # list_files saw the tests
    assert any("exit code 1" in o for o in tool_outputs)  # reproduced the failure
    assert any("exit code 0" in o for o in tool_outputs)  # confirmed the fix
    assert "test_holdout" not in json.dumps(attempt.transcript)  # hidden tests never visible
    assert llm.calls[0][0].value == "coder"


async def test_writes_to_frozen_tests_are_refused(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM(
        [
            calls(("write_file", {"path": "./test_stats.py", "content": "print('ok')\n"})),
            calls(("write_file", {"path": "stats.py", "content": GOOD})),
            calls(("submit", {"summary": "fixed"})),
        ],
        budget,
    )
    receipt = await agent(sandbox, llm, budget).fix(task)

    assert receipt.status == "verified"
    attempt = receipt.attempts[0]
    assert attempt.rejected_edits == ["test_stats.py"]
    assert any("REFUSED" in m["content"] for m in attempt.transcript if m["role"] == "tool")


async def test_tampering_through_shell_is_caught_and_explained(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    sneaky = f"{PY} -c \"open('test_stats.py', 'w').write('print(1)')\""
    llm = ScriptedLLM(
        [
            calls(
                ("write_file", {"path": "stats.py", "content": "def median(v):\n    return 0\n"})
            ),
            calls(("run", {"command": sneaky})),
            calls(("submit", {"summary": "done"})),
            calls(("write_file", {"path": "stats.py", "content": GOOD})),
            calls(("submit", {"summary": "real fix"})),
        ],
        budget,
    )
    receipt = await agent(sandbox, llm, budget).fix(task)

    first, second = receipt.attempts
    assert first.tampered_files == ["test_stats.py"]
    assert not first.all_passed
    assert "modified by your commands" in llm.calls[3][1][-1].content
    assert second.all_passed
    assert receipt.status == "verified"
    assert "tampered: test_stats.py" in render_markdown(receipt)


async def test_step_limit_without_changes(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([calls(("list_files", {}))] * 3, budget)
    receipt = await agent(sandbox, llm, budget, max_steps=3, max_rounds=1).fix(task)

    assert receipt.status == "failed"
    assert receipt.attempts[0].error == "agent made no file changes (step limit 3 reached)"


async def test_plain_text_reply_with_file_block_is_applied(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([f"### FILE: stats.py\n```python\n{GOOD}```"], budget)
    receipt = await agent(sandbox, llm, budget).fix(task)
    assert receipt.status == "verified"


def test_path_normalization() -> None:
    assert normalize("./a/b.py") == "a/b.py"
    assert normalize("/workspace/a.py") == "a.py"
    assert normalize("") == "."
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(PathError):
            normalize(bad)


def test_tool_specs_are_well_formed() -> None:
    names = {t["function"]["name"] for t in TOOL_SPECS}
    assert names == {
        "list_files",
        "read_file",
        "search",
        "run",
        "write_file",
        "edit_file",
        "replace_lines",
        "report_impossible",
        "submit",
    }
    for spec in TOOL_SPECS:
        params = spec["function"]["parameters"]
        assert set(params["required"]) <= set(params["properties"])


async def test_workspace_tool_errors(sandbox: LocalSandbox) -> None:
    base = await sandbox.base("x")
    seeded = await sandbox.run(base, NOOP, files={workspace_path("a.py"): b"x = 1\nx = 1\ny = 2\n"})
    ws = Workspace(sandbox, seeded.checkpoint, protected={"t.py"}, python=PY)

    assert (await ws.call("nope", "{}"))[0].startswith("ERROR: unknown tool")
    assert (await ws.call("read_file", "not json"))[0].startswith("ERROR: could not parse")
    assert (await ws.call("read_file", '{"path": "../x"}'))[0].startswith("ERROR: path escapes")
    assert (await ws.call("read_file", '{"path": "missing.py"}'))[0].startswith(
        "ERROR: cannot read"
    )
    assert (await ws.call("read_file", '{"wrong": 1}'))[0].startswith("ERROR: bad arguments")
    dup = await ws.call("edit_file", json.dumps({"path": "a.py", "old": "x = 1\n", "new": "z\n"}))
    assert "matches 2 places" in dup[0]
    assert "run" in (await ws.call("grep", "{}"))[0], "unknown tools point to run"
    ranged = await ws.call(
        "read_file", json.dumps({"path": "a.py", "start_line": 3, "end_line": 3})
    )
    assert "y = 2" in ranged[0]
    assert "x = 1" not in ranged[0]
    found = await ws.call("search", json.dumps({"pattern": "y ="}))
    assert "a.py:3" in found[0]
    assert (await ws.call("search", json.dumps({"pattern": "zzz"})))[0] == "(no matches)"
    assert ws.touched == set()


async def test_list_and_search_are_portable(sandbox: LocalSandbox) -> None:
    base = await sandbox.base("x")
    files = {
        "top.py": b"import os\n",
        "pkg/mod.py": b"def handler():\n    return 1\n",
        "pkg/deep/er/leaf.py": b"handler = None\n",
        "pkg/__pycache__/mod.cpython-312.pyc": b"\x00",
        "data.bin": b"\xff\xfe handler",
    }
    seeded = await sandbox.run(base, NOOP, files={workspace_path(k): v for k, v in files.items()})
    ws = Workspace(sandbox, seeded.checkpoint, protected=set(), python=PY)

    listed = (await ws.call("list_files", "{}"))[0].splitlines()
    assert listed == ["data.bin", "pkg/mod.py", "top.py"]  # depth 3 stops before leaf.py
    deep = (await ws.call("list_files", json.dumps({"path": "pkg", "max_depth": 6})))[0]
    assert deep.splitlines() == ["pkg/deep/er/leaf.py", "pkg/mod.py"]
    assert ".pf_tool" not in deep

    hits = (await ws.call("search", json.dumps({"pattern": r"^def \w+\("})))[0]
    assert hits == "pkg/mod.py:1:def handler():"
    scoped = (await ws.call("search", json.dumps({"pattern": "handler", "path": "pkg/deep"})))[0]
    assert scoped == "pkg/deep/er/leaf.py:1:handler = None"
    bad = (await ws.call("search", json.dumps({"pattern": "("})))[0]
    assert bad.startswith("ERROR: invalid regular expression")


async def test_run_timeout_explains_hang(sandbox: LocalSandbox) -> None:
    base = await sandbox.base("x")
    ws = Workspace(sandbox, base, protected=set(), python=PY)
    hang = f'{PY} -c "import time; time.sleep(5)"'
    out, _ = await ws.call("run", json.dumps({"command": hang, "timeout_s": 1}))
    assert "hit the 1s timeout" in out
    quick, _ = await ws.call("run", json.dumps({"command": f'{PY} -c "print(7)"'}))
    assert "timeout" not in quick


async def test_agent_is_warned_before_the_step_limit(
    sandbox: LocalSandbox, budget: BudgetGuard, task: FixTask
) -> None:
    llm = ScriptedLLM([calls(("list_files", {}))] * 20, budget)
    await agent(sandbox, llm, budget, max_steps=20, max_rounds=1).fix(task)
    warned = [i for i, (_, msgs, _) in enumerate(llm.calls) if "steps left" in msgs[-1].content]
    assert warned == [20 - STEP_WARNING]
