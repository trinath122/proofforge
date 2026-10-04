"""Forgiving but unambiguous text edits for the agent's edit_file tool.

On SWE-bench Pro about half of all edit_file calls failed with "found 0": models copy
code with different indentation (tabs vs spaces), trailing spaces, or the line-number
prefixes that read_file shows. Each failure costs a step and often starts a re-read
loop. An edit is still applied only when it matches exactly one place.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_LINE_NO = re.compile(r"^\s*\d+  ", re.MULTILINE)


@dataclass(frozen=True)
class EditResult:
    text: str | None
    message: str


def _norm(line: str) -> str:
    return " ".join(line.split())


def _strip_line_numbers(block: str) -> str:
    lines = block.splitlines(keepends=True)
    if lines and all(_LINE_NO.match(ln) or not ln.strip() for ln in lines):
        return "".join(_LINE_NO.sub("", ln, count=1) for ln in lines)
    return block


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(new: str, old_lines: list[str], block: list[str]) -> str:
    """Carry the file's indentation style (e.g. tabs) into `new` when `old` used another."""
    pair = next(
        ((_indent(o), _indent(f)) for o, f in zip(old_lines, block, strict=False) if _indent(o)),
        None,
    )
    if pair is None or pair[0] == pair[1] or not pair[1]:
        return new
    src, dst = pair
    out = []
    for line in new.splitlines(keepends=True):
        lead = _indent(line)
        depth, rest = divmod(len(lead), len(src))
        same_style = bool(lead) and not rest and lead == src * depth
        out.append(dst * depth + line[len(lead) :] if same_style else line)
    return "".join(out)


def _windows(lines: list[str], want: list[str]) -> list[int]:
    n = len(want)
    keys = [_norm(ln) for ln in lines]
    return [i for i in range(len(lines) - n + 1) if keys[i : i + n] == want]


def _hint(text: str, old: str) -> str:
    lines = text.splitlines()
    first = next((ln for ln in old.splitlines() if ln.strip()), "")
    if not first or not lines:
        return ""
    scores = [difflib.SequenceMatcher(None, _norm(first), _norm(ln)).ratio() for ln in lines]
    best = max(range(len(lines)), key=scores.__getitem__)
    if scores[best] < 0.6:
        return " The first line of `old` does not appear in the file."
    lo, hi = max(0, best - 2), min(len(lines), best + 6)
    snippet = "\n".join(f"{i + 1:>5}  {lines[i]}" for i in range(lo, hi))
    return (
        f" Closest match is near line {best + 1}:\n{snippet}\n"
        "Copy the exact text, or use replace_lines with these line numbers."
    )


def _loose(text: str, old: str, new: str) -> EditResult:
    """Match `old` line by line ignoring whitespace; apply only if exactly one place fits."""
    old_lines = old.strip("\n").splitlines()
    want = [_norm(ln) for ln in old_lines]
    if not any(want):
        return EditResult(None, "`old` has no content.")
    lines = text.splitlines(keepends=True)
    hits = _windows(lines, want)
    if len(hits) > 1:
        return EditResult(
            None, f"`old` matches {len(hits)} places; include more surrounding lines."
        )
    if not hits:
        return EditResult(None, "`old` was not found." + _hint(text, old))
    i, n = hits[0], len(want)
    block = lines[i : i + n]
    replacement = _reindent(new, old_lines, block)
    if block[-1].endswith("\n") and not replacement.endswith("\n"):
        replacement += "\n"
    result = "".join(lines[:i]) + replacement + "".join(lines[i + n :])
    return EditResult(result, f"matched lines {i + 1}-{i + n} ignoring whitespace")


def apply_edit(text: str, old: str, new: str) -> EditResult:
    if not old:
        return EditResult(None, "`old` is empty.")
    count = text.count(old)
    if count == 1:
        return EditResult(text.replace(old, new, 1), "exact match")
    if count > 1:
        return EditResult(None, f"`old` matches {count} places; include more surrounding lines.")
    unnumbered, new_unnumbered = _strip_line_numbers(old), _strip_line_numbers(new)
    if unnumbered != old and text.count(unnumbered) == 1:
        return EditResult(
            text.replace(unnumbered, new_unnumbered, 1), "matched without line numbers"
        )
    return _loose(text, unnumbered, new_unnumbered)


def replace_lines(text: str, start: int, end: int, content: str) -> EditResult:
    lines = text.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines):
        return EditResult(None, f"line range {start}-{end} is outside 1-{len(lines)}.")
    if content and not content.endswith("\n"):
        content += "\n"
    return EditResult(
        "".join(lines[: start - 1]) + content + "".join(lines[end:]),
        f"replaced lines {start}-{end}",
    )
