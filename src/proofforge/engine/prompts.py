"""Prompt construction and edit parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable

from proofforge.gates.base import GateResult
from proofforge.llm.base import Message

SYSTEM = """You are ProofForge's coding agent. Your change is accepted only if executable \
gates pass, including hidden checks you cannot see.

Rules:
- Edit ONLY the files listed under "Editable files". Edits to any other file are rejected.
- Never change, delete or work around tests. Tests are frozen and re-checked.
- Fix the behavior in general. Do not special-case test inputs or hard-code expected values.
- Return every file you change in full, in exactly this format and nothing else:

### FILE: relative/path.py
```python
<entire new file content>
```"""

# Reasoning models may wrap their thinking in <think> tags; an unclosed tag means the
# reply was cut off while thinking, so everything after it is discarded.
_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)
# Tolerates common variants: "### FILE: x", "**FILE: x**", "File: `x`".
_EDIT = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*)?FILE[ \t]*:?[ \t]*`?(?P<path>[\w./-]+)`?[ \t]*\**[ \t]*\n+"
    r"[ \t]*```[^\n]*\n(?P<body>.*?)\n[ \t]*```",
    re.M | re.S | re.I,
)
_FENCE = re.compile(r"```[^\n]*\n(?P<body>.*?)\n[ \t]*```", re.S)


def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text)


def parse_edits(text: str, editable: Iterable[str] = ()) -> dict[str, str]:
    """Extract whole-file edits. Leniency is safe: nothing is accepted until gates pass.

    If no labelled file block is found and exactly one file is editable, the last
    fenced code block is taken as that file's new content.
    """
    text = strip_reasoning(text)
    edits = {m["path"].removeprefix("./"): m["body"] + "\n" for m in _EDIT.finditer(text)}
    if edits:
        return edits
    targets = list(editable)
    blocks = _FENCE.findall(text)
    if len(targets) == 1 and blocks:
        return {targets[0]: blocks[-1] + "\n"}
    return {}


def _files_block(title: str, files: dict[str, str]) -> str:
    if not files:
        return ""
    parts = [f"## {title}"]
    for path, content in sorted(files.items()):
        parts.append(f"### {path}\n```\n{content.rstrip()}\n```")
    return "\n\n".join(parts)


def _gate_block(results: list[GateResult]) -> str:
    visible = [g for g in results if g.kind == "visible"]
    hidden_failed = sum(1 for g in results if g.kind == "holdout" and not g.passed)
    parts = ["## Current gate results"]
    for g in visible:
        status = "PASS" if g.passed else f"FAIL (exit {g.exit_code})"
        parts.append(f"### {g.name}: {status}")
        if not g.passed:
            output = (g.stdout_tail + "\n" + g.stderr_tail).strip()[-2500:]
            parts.append(f"```\n{output}\n```")
    if hidden_failed:
        parts.append(
            f"{hidden_failed} hidden check(s) also failed. Make the fix general; "
            "their details are intentionally not shown."
        )
    return "\n\n".join(parts)


def build_messages(
    description: str,
    *,
    editable: dict[str, str],
    context: dict[str, str],
    protected: dict[str, str],
    results: list[GateResult],
    rejected: list[str],
    notes: Iterable[str] = (),
) -> list[Message]:
    sections = [
        f"# Task\n{description}",
        _files_block("Editable files", editable),
        _files_block("Read-only context", context),
        _files_block("Frozen tests (read-only)", protected),
        _gate_block(results),
    ]
    if rejected:
        sections.append(
            "Your previous edits to these files were rejected because they are not editable: "
            + ", ".join(sorted(rejected))
        )
    sections.extend(f"## Note on your previous reply\n{n}" for n in notes)
    return [
        Message(role="system", content=SYSTEM),
        Message(role="user", content="\n\n".join(s for s in sections if s)),
    ]


AGENT_SYSTEM = """You are ProofForge, a senior software engineer working inside an isolated \
sandbox. You have tools to list, read and search files, run commands, and edit files.

How you work:
1. Explore first: find the relevant code and reproduce the problem by running the checks.
2. Make focused changes. Re-run the checks after each change.
3. When the checks pass and you are confident the fix is general, call `submit`.

Rules:
- Frozen verification files (listed below) cannot be changed; attempts are refused and logged.
- Hidden checks you cannot see will also run. Fix the real behavior; never special-case \
inputs, hard-code expected outputs, or weaken error handling to make checks pass.
- Keep reasoning brief and act through tools. Command output is truncated; narrow it down.
- Commands time out after 60s. A timeout means the code hangs (deadlock, missed wakeup, \
a loop that never exits): read the code or run one narrower test, never re-run blindly.
- Put scratch and debug scripts under /tmp, never in the repository: everything you leave \
in the repository is part of your change.
- Before you submit, go through every requirement in the task one by one and confirm your \
change satisfies it, including exact error messages and edge cases.
- If the task lists new interfaces (paths, names, types), create each one exactly as listed: \
hidden tests import them by those names. Submissions are checked for them.
- If a frozen check contradicts the specification so that no correct implementation can \
pass it, never game it (special cases, test detection, editing tests): call \
report_impossible and name the contradiction. Use this only for a real contradiction."""


def build_agent_messages(
    description: str,
    *,
    files: list[str],
    protected: list[str],
    check_commands: list[str],
    results: list[GateResult],
    notes: Iterable[str] = (),
    previous_summary: str = "",
    workdir: str | None = None,
    approach: str = "",
) -> list[Message]:
    workspace = (
        f"## Repository\nThe repository is at `{workdir}`, your working directory. It is large: "
        "use `list_files` and `search` to find the relevant code before reading files."
        if workdir
        else "## Workspace files\n" + "\n".join(f"- {p}" for p in sorted(files))
    )
    sections = [
        f"# Task\n{description}",
        workspace,
        "## Frozen verification files (read-only)\n"
        + ("\n".join(f"- {p}" for p in sorted(protected)) or "- none"),
        "## Checks you can run yourself\n" + "\n".join(f"```\n{c}\n```" for c in check_commands)
        if check_commands
        else "## Checks\nNo checks are provided. Find or write a way to reproduce the problem "
        "with the project's own test tooling, then verify your change. Hidden tests decide.",
        _gate_block(results),
    ]
    if approach:
        sections.append(f"## Approach for this attempt\n{approach}")
    if previous_summary:
        sections.append(f"## Your previous attempt\n{previous_summary}")
    sections.extend(f"## Note on your previous attempt\n{n}" for n in notes)
    return [
        Message(role="system", content=AGENT_SYSTEM),
        Message(role="user", content="\n\n".join(s for s in sections if s)),
    ]


BREAKER_SYSTEM = """You are the Breaker on a ProofForge team. Other engineers are fixing \
the task below in parallel. Your job is to write tests that would expose a fix that only \
looks right: one that passes the provided checks but misses part of the specification.

How you work:
1. Read the task and the code under test. Do not fix anything.
2. Write pytest tests into the single file named below. Each test checks one concrete \
requirement stated in the task: edge cases, error behaviour, exact messages, boundary \
values, concurrency or ordering guarantees.
3. Run the file to make sure it imports and the tests are well-formed. Tests that fail on \
the current, unfixed code are expected and good.
4. Call `submit` with a one-line summary.

Rules:
- Test only behaviour the task states. Never invent requirements; a test that a correct fix \
fails is worse than no test.
- Use only the standard library and pytest. Keep each test fast (well under a second).
- Change no other file. Everything outside your test file is thrown away."""


def build_breaker_messages(
    description: str,
    *,
    test_path: str,
    files: list[str],
    workdir: str | None = None,
) -> list[Message]:
    workspace = (
        f"## Repository\nThe repository is at `{workdir}`, your working directory. "
        "Use `list_files` and `search` to find the code under test."
        if workdir
        else "## Workspace files\n" + "\n".join(f"- {p}" for p in sorted(files))
    )
    sections = [
        f"# Task the others are fixing\n{description}",
        workspace,
        f"## Your test file\nWrite your tests to `{test_path}` and run them with "
        f"`python -m pytest -q {test_path}`.",
    ]
    return [
        Message(role="system", content=BREAKER_SYSTEM),
        Message(role="user", content="\n\n".join(sections)),
    ]
