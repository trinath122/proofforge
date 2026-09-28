"""Tools the agent uses to explore, run and change code inside the sandbox.

Every command runs in the task's sandbox, never on the host. Writes to frozen oracle
files are refused; shell commands that alter them anyway are caught later by tamper
detection, and gates always run the original copies.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any

from proofforge.gates.base import workspace_path
from proofforge.sandbox.base import NOOP, Checkpoint, Sandbox

OUTPUT_LIMIT = 6000
READ_LINE_LIMIT = 400
RUN_TIMEOUT_S = 60
RUN_TIMEOUT_MAX_S = 300

# list_files and search run this helper inside the sandbox instead of find/grep, so they
# behave the same on every image and on the local test sandbox (including Windows).
# Arguments travel in a JSON file, which avoids shell quoting differences entirely.
HELPER = ".pf_tool.py"
HELPER_ARGS = ".pf_tool_args.json"
_HELPER_SRC = r"""
import json, os, re, sys

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
SKIP = {".pf_tool.py", ".pf_tool_args.json"}
a = json.load(open(".pf_tool_args.json"))
root = a["path"]


def walk():
    if os.path.isfile(root):
        yield root
        return
    limit = a.get("max_depth") or 99
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS) if depth + 1 < limit else []
        for f in sorted(files):
            if f in SKIP or f.endswith(".pyc"):
                continue
            p = os.path.join(d, f).replace(os.sep, "/")
            yield p[2:] if p.startswith("./") else p


out = []
if a["op"] == "list":
    out = sorted(walk())[:400]
else:
    try:
        rx = re.compile(a["pattern"])
    except re.error as e:
        print("ERROR: invalid regular expression: %s" % e)
        sys.exit(0)
    for p in walk():
        try:
            with open(p, encoding="utf-8", errors="strict") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append("%s:%d:%s" % (p, i, line.rstrip("\n")[:300]))
        except (UnicodeDecodeError, OSError):
            continue
        if len(out) >= 200:
            break
sys.stdout.write("\n".join(out[:200 if a["op"] == "search" else 400]))
"""


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
        "Search file contents with a Python regular expression. Returns path:line:text.",
        {"pattern": _STR, "path": _STR},
        ["pattern"],
    ),
    _fn(
        "run",
        "Run a shell command in the workspace (e.g. tests, scripts, sqlite3 queries). "
        f"Changes to the filesystem persist. Times out after {RUN_TIMEOUT_S}s unless "
        f"timeout_s is given (max {RUN_TIMEOUT_MAX_S}).",
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

    def __init__(
        self,
        sandbox: Sandbox,
        checkpoint: Checkpoint,
        protected: set[str],
        *,
        python: str = "python3",
    ) -> None:
        self.sandbox = sandbox
        self.python = python
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

    async def _exec(
        self, command: str, *, keep: bool, timeout_s: int = RUN_TIMEOUT_S
    ) -> tuple[int, str, bool]:
        res = await self.sandbox.run(self.checkpoint, command, keep=keep, timeout_s=timeout_s)
        if keep:
            self.checkpoint = res.checkpoint
        out = res.stdout + (f"\n[stderr]\n{res.stderr}" if res.stderr.strip() else "")
        timed_out = res.exit_code == 124 or res.elapsed_s >= timeout_s - 0.5
        return res.exit_code, out, timed_out

    async def _helper(self, **args: object) -> str:
        res = await self.sandbox.run(
            self.checkpoint,
            f"{self.python} {HELPER}",
            files={
                workspace_path(HELPER): _HELPER_SRC.encode(),
                workspace_path(HELPER_ARGS): json.dumps(args).encode(),
            },
            keep=False,
            timeout_s=RUN_TIMEOUT_S,
        )
        if res.exit_code != 0:
            return f"ERROR: tool failed (exit {res.exit_code}): {res.stderr.strip()[-500:]}"
        return res.stdout.strip()

    async def _tool_list_files(self, path: str = ".", max_depth: int = 3) -> tuple[str, bool]:
        depth = max(1, min(int(max_depth), 6))
        out = await self._helper(op="list", path=normalize(path), max_depth=depth)
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
        out = await self._helper(op="search", pattern=pattern, path=normalize(path))
        return _clip(out) or "(no matches)", False

    async def _tool_run(self, command: str, timeout_s: int = RUN_TIMEOUT_S) -> tuple[str, bool]:
        timeout = max(1, min(int(timeout_s), RUN_TIMEOUT_MAX_S))
        code, out, timed_out = await self._exec(command, keep=True, timeout_s=timeout)
        note = (
            f"\nNOTE: the command hit the {timeout}s timeout. That usually means the code "
            "under test hangs (deadlock, missed wakeup, a loop that never exits). Read the "
            "code or run a narrower test instead of re-running the same command."
            if timed_out
            else ""
        )
        return _clip(f"exit code {code}\n{out}") + note, False

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
