"""Tools the agent uses to explore, run and change code inside the sandbox.

Every command runs in the task's sandbox, never on the host. Writes to frozen oracle
files are refused; shell commands that alter them anyway are caught later by tamper
detection, and gates always run the original copies.
"""

from __future__ import annotations

import json
import posixpath
import shlex
from typing import Any

from proofforge.gates.base import workspace_path
from proofforge.sandbox.base import NOOP, Checkpoint, Sandbox

OUTPUT_LIMIT = 6000
READ_LINE_LIMIT = 400


def _fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SPECS: list[dict[str, Any]] = [
    _fn(
        "list_files",
        "List files under a directory of the workspace (relative path).",
        {"path": _STR, "max_depth": _INT},
        [],
    ),
    _fn(
        "read_file",
        f"Read a file with line numbers. At most {READ_LINE_LIMIT} lines per call.",
        {"path": _STR, "start_line": _INT, "end_line": _INT},
        ["path"],
    ),
    _fn(
        "search",
        "Search file contents with an extended regular expression (grep -rnE).",
        {"pattern": _STR, "path": _STR},
        ["pattern"],
    ),
    _fn(
        "run",
        "Run a shell command in the workspace (e.g. tests, scripts, sqlite3 queries). "
        "Changes to the filesystem persist.",
        {"command": _STR, "timeout_s": _INT},
        ["command"],
    ),
    _fn(
        "write_file",
        "Create or overwrite a file with the full given content.",
        {"path": _STR, "content": _STR},
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Replace one exact, unique occurrence of `old` with `new` in a file.",
        {"path": _STR, "old": _STR, "new": _STR},
        ["path", "old", "new"],
    ),
    _fn(
        "submit",
        "Finish: your change is ready for verification by the gates, including hidden checks.",
        {"summary": _STR},
        ["summary"],
    ),
]


def _clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters omitted] ...\n{text[-half:]}"


class PathError(ValueError):
    pass


def normalize(path: str) -> str:
    """Workspace-relative POSIX path; refuses anything that escapes the workspace."""
    raw = path.strip().removeprefix("/workspace/").removeprefix("./")
    if raw.startswith("/"):
        raise PathError(f"absolute paths outside the workspace are not allowed: {path}")
    norm = posixpath.normpath(raw or ".")
    if norm == ".." or norm.startswith("../"):
        raise PathError(f"path escapes the workspace: {path}")
    return norm


class Workspace:
    """The agent's view of one sandbox branch. Tracks what it changed."""

    def __init__(self, sandbox: Sandbox, checkpoint: Checkpoint, protected: set[str]) -> None:
        self.sandbox = sandbox
        self.checkpoint = checkpoint
        self.protected = protected
        self.touched: set[str] = set()
        self.refused: list[str] = []
        self.summary = ""

    async def call(self, name: str, raw_args: str) -> tuple[str, bool]:
        """Execute one tool call. Returns (observation, submitted)."""
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise TypeError("arguments must be a JSON object")
        except (json.JSONDecodeError, TypeError) as exc:
            return f"ERROR: could not parse arguments: {exc}", False
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"ERROR: unknown tool '{name}'", False
        try:
            result: tuple[str, bool] = await handler(**args)
        except PathError as exc:
            return f"ERROR: {exc}", False
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}", False
        return result

    async def _exec(self, command: str, *, keep: bool, timeout_s: int = 120) -> tuple[int, str]:
        res = await self.sandbox.run(self.checkpoint, command, keep=keep, timeout_s=timeout_s)
        if keep:
            self.checkpoint = res.checkpoint
        out = res.stdout + (f"\n[stderr]\n{res.stderr}" if res.stderr.strip() else "")
        return res.exit_code, out

    async def _tool_list_files(self, path: str = ".", max_depth: int = 3) -> tuple[str, bool]:
        target = normalize(path)
        depth = max(1, min(int(max_depth), 6))
        cmd = (
            f"find {shlex.quote(target)} -maxdepth {depth} -type f "
            "-not -path '*/.git/*' -not -name '*.pyc' | sort | head -400"
        )
        _, out = await self._exec(cmd, keep=False)
        return _clip(out) or "(no files)", False

    async def _tool_read_file(
        self, path: str, start_line: int = 1, end_line: int = 0
    ) -> tuple[str, bool]:
        rel = normalize(path)
        try:
            text = (await self.sandbox.read(self.checkpoint, workspace_path(rel))).decode(
                errors="replace"
            )
        except Exception:
            return f"ERROR: cannot read {rel} (missing or not a file)", False
        lines = text.splitlines()
        start = max(1, int(start_line))
        stop = len(lines) if int(end_line) <= 0 else min(len(lines), int(end_line))
        stop = min(stop, start + READ_LINE_LIMIT - 1)
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, stop + 1))
        more = f"\n... ({len(lines) - stop} more lines)" if stop < len(lines) else ""
        return _clip(f"{rel} ({len(lines)} lines)\n{body}{more}"), False

    async def _tool_search(self, pattern: str, path: str = ".") -> tuple[str, bool]:
        target = normalize(path)
        cmd = (
            f"grep -rnE --exclude-dir=.git -- {shlex.quote(pattern)} {shlex.quote(target)} "
            "| head -200"
        )
        _, out = await self._exec(cmd, keep=False)
        return _clip(out) or "(no matches)", False

    async def _tool_run(self, command: str, timeout_s: int = 120) -> tuple[str, bool]:
        timeout = max(1, min(int(timeout_s), 600))
        code, out = await self._exec(command, keep=True, timeout_s=timeout)
        return _clip(f"exit code {code}\n{out}"), False

    async def _write(self, rel: str, content: str) -> tuple[str, bool]:
        if rel in self.protected:
            self.refused.append(rel)
            return (
                f"REFUSED: {rel} is a frozen verification file and cannot be changed. "
                "Fix the code under test instead.",
                False,
            )
        res = await self.sandbox.run(
            self.checkpoint, NOOP, files={workspace_path(rel): content.encode()}, keep=True
        )
        self.checkpoint = res.checkpoint
        self.touched.add(rel)
        return f"wrote {rel} ({len(content.splitlines())} lines)", False

    async def _tool_write_file(self, path: str, content: str) -> tuple[str, bool]:
        return await self._write(normalize(path), content)

    async def _tool_edit_file(self, path: str, old: str, new: str) -> tuple[str, bool]:
        rel = normalize(path)
        try:
            text = (await self.sandbox.read(self.checkpoint, workspace_path(rel))).decode()
        except Exception:
            return f"ERROR: cannot read {rel}", False
        count = text.count(old) if old else 0
        if count != 1:
            return (
                f"ERROR: `old` must match exactly once in {rel}, found {count}. "
                "Include more surrounding lines, or use write_file.",
                False,
            )
        return await self._write(rel, text.replace(old, new, 1))

    async def _tool_submit(self, summary: str = "") -> tuple[str, bool]:
        self.summary = summary
        return "submitted for verification", True
