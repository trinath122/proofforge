"""Prompt construction and edit parsing."""

from __future__ import annotations

import re

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

_EDIT = re.compile(r"^### FILE:\s*(?P<path>\S+)\s*\n```[^\n]*\n(?P<body>.*?)\n```", re.M | re.S)


def parse_edits(text: str) -> dict[str, str]:
    return {m["path"].removeprefix("./"): m["body"] + "\n" for m in _EDIT.finditer(text)}


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
    return [
        Message(role="system", content=SYSTEM),
        Message(role="user", content="\n\n".join(s for s in sections if s)),
    ]
