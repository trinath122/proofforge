"""Keep the agent's context small on long runs.

Every agent step resends the whole conversation, so without care the cost of a run grows
with the square of its step count. Observation masking keeps the most recent tool results
verbatim and shortens older ones, plus the bulky file bodies inside older write and edit
calls. The agent can always re-run a tool to see output again. The full, unmasked
conversation is still recorded in the receipt.
"""

from __future__ import annotations

import json

from proofforge.llm.base import Message, ToolCall

KEEP_RECENT = 10
READ_BUDGET_CHARS = 60_000
STUB_CHARS = 240
_BULKY_ARGS = ("content", "old", "new")


def _shrink_call(call: ToolCall) -> ToolCall:
    try:
        args = json.loads(call.arguments)
    except json.JSONDecodeError:
        return call
    if not isinstance(args, dict):
        return call
    changed = False
    for key in _BULKY_ARGS:
        value = args.get(key)
        if isinstance(value, str) and len(value) > STUB_CHARS:
            args[key] = f"<{len(value.splitlines())} lines, elided from older context>"
            changed = True
    return call.model_copy(update={"arguments": json.dumps(args)}) if changed else call


def _calls_by_id(messages: list[Message]) -> dict[str, tuple[str, dict[str, object]]]:
    calls: dict[str, tuple[str, dict[str, object]]] = {}
    for msg in messages:
        for call in msg.tool_calls:
            try:
                args = json.loads(call.arguments)
            except json.JSONDecodeError:
                args = {}
            calls[call.id] = (call.name, args if isinstance(args, dict) else {})
    return calls


def _current_reads(messages: list[Message], cutoff: int) -> set[int]:
    """Older read_file results worth keeping: the latest read of each file, unless the
    agent wrote that file afterwards, newest first until READ_BUDGET_CHARS is used.

    Masking every old read made agents on large repositories re-read the same files
    in a loop; keeping the current view of each file they read stops that."""
    calls = _calls_by_id(messages)
    latest: dict[str, int] = {}
    written_after: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.role != "tool" or msg.tool_call_id not in calls:
            continue
        name, args = calls[msg.tool_call_id]
        path = str(args.get("path", "")).removeprefix("/app/").removeprefix("./")
        if name == "read_file" and i < cutoff:
            latest[path] = i
        elif name in ("write_file", "edit_file"):
            written_after[path] = i
    keep: set[int] = set()
    budget = READ_BUDGET_CHARS
    for path, i in sorted(latest.items(), key=lambda kv: -kv[1]):
        if written_after.get(path, -1) > i:
            continue
        size = len(messages[i].content)
        if size <= budget:
            keep.add(i)
            budget -= size
    return keep


def compact(messages: list[Message], keep_recent: int = KEEP_RECENT) -> list[Message]:
    """Copy of `messages` with stale tool output older than the last `keep_recent` results
    masked. The latest read of each file stays visible (see _current_reads)."""
    tool_positions = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_positions) <= keep_recent:
        return list(messages)
    cutoff = tool_positions[-keep_recent]
    # Keep the call that produced the oldest kept result verbatim too.
    call_id = messages[cutoff].tool_call_id
    for i in range(cutoff - 1, -1, -1):
        if messages[i].role == "assistant" and any(c.id == call_id for c in messages[i].tool_calls):
            cutoff = i
            break
    current = _current_reads(messages, cutoff)
    out: list[Message] = []
    for i, msg in enumerate(messages):
        if i >= cutoff or i in current:
            out.append(msg)
        elif msg.role == "tool" and len(msg.content) > STUB_CHARS:
            hidden = len(msg.content) - STUB_CHARS
            out.append(
                msg.model_copy(
                    update={
                        "content": f"{msg.content[:STUB_CHARS]}\n[... {hidden} more characters "
                        "of older output elided; re-run the tool if you need them]"
                    }
                )
            )
        elif msg.role == "assistant" and msg.tool_calls:
            out.append(
                msg.model_copy(update={"tool_calls": [_shrink_call(c) for c in msg.tool_calls]})
            )
        else:
            out.append(msg)
    return out
