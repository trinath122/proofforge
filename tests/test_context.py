from __future__ import annotations

import json

from proofforge.engine.context import KEEP_RECENT, STUB_CHARS, compact
from proofforge.llm.base import Message, ToolCall


def _conversation(steps: int) -> list[Message]:
    msgs = [Message(role="system", content="S" * 5000), Message(role="user", content="U" * 5000)]
    for i in range(steps):
        body = json.dumps({"path": f"f{i}.py", "content": "x\n" * 400})
        msgs.append(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id=f"c{i}", name="write_file", arguments=body)],
            )
        )
        msgs.append(Message(role="tool", tool_call_id=f"c{i}", content=f"out{i} " + "o" * 3000))
    return msgs


def test_short_conversations_are_untouched() -> None:
    msgs = _conversation(KEEP_RECENT)
    assert compact(msgs) == msgs


def test_older_output_and_file_bodies_are_masked() -> None:
    msgs = _conversation(40)
    out = compact(msgs)
    assert len(out) == len(msgs)
    assert out[:2] == msgs[:2], "system prompt and task are never masked"
    assert out[-2 * KEEP_RECENT :] == msgs[-2 * KEEP_RECENT :], "recent steps stay verbatim"
    old_tool, old_call = out[3], out[2]
    assert old_tool.content.startswith("out0 ")
    assert "elided" in old_tool.content
    assert len(old_tool.content) < STUB_CHARS + 120
    assert old_tool.tool_call_id == "c0"
    args = json.loads(old_call.tool_calls[0].arguments)
    assert args["path"] == "f0.py"
    assert "elided" in args["content"]
    assert old_call.tool_calls[0].id == "c0"
    before = sum(len(m.content) + sum(len(c.arguments) for c in m.tool_calls) for m in msgs)
    after = sum(len(m.content) + sum(len(c.arguments) for c in m.tool_calls) for m in out)
    assert after < before / 2
    assert msgs[3].content.endswith("o" * 100), "the recorded conversation is not modified"


def test_malformed_arguments_are_left_alone() -> None:
    msgs = _conversation(KEEP_RECENT + 2)
    msgs[2] = Message(
        role="assistant", tool_calls=[ToolCall(id="c0", name="run", arguments="{not json")]
    )
    assert compact(msgs)[2] == msgs[2]
    msgs[2] = Message(
        role="assistant", tool_calls=[ToolCall(id="c0", name="run", arguments="[1, 2]")]
    )
    assert compact(msgs)[2] == msgs[2]


def _read(i: int, path: str, body: str) -> list[Message]:
    args = json.dumps({"path": path})
    return [
        Message(
            role="assistant", tool_calls=[ToolCall(id=f"r{i}", name="read_file", arguments=args)]
        ),
        Message(role="tool", tool_call_id=f"r{i}", content=body),
    ]


def test_latest_read_of_each_file_stays_visible() -> None:
    msgs = [Message(role="system", content="S"), Message(role="user", content="U")]
    msgs += _read(0, "/app/a.py", "A-old " + "a" * 2000)
    msgs += _read(1, "a.py", "A-new " + "a" * 2000)
    msgs += _read(2, "b.py", "B " + "b" * 2000)
    msgs += _read(3, "c.py", "C " + "c" * 2000)
    edit = json.dumps({"path": "c.py", "content": "x"})
    msgs.append(
        Message(role="assistant", tool_calls=[ToolCall(id="w", name="write_file", arguments=edit)])
    )
    msgs.append(Message(role="tool", tool_call_id="w", content="wrote c.py"))
    for i in range(KEEP_RECENT):
        msgs.append(
            Message(
                role="assistant", tool_calls=[ToolCall(id=f"s{i}", name="search", arguments="{}")]
            )
        )
        msgs.append(Message(role="tool", tool_call_id=f"s{i}", content="hit " + "s" * 2000))
    contents = [m.content for m in compact(msgs) if m.role == "tool"]
    assert any(c.startswith("A-new") and "elided" not in c for c in contents)
    assert any(c.startswith("A-old") and "elided" in c for c in contents), "stale read masked"
    assert any(c.startswith("B ") and "elided" not in c for c in contents)
    assert any(c.startswith("C ") and "elided" in c for c in contents), "edited since: masked"


def test_reread_note_does_not_displace_the_real_read() -> None:
    msgs = [Message(role="system", content="S"), Message(role="user", content="U")]
    msgs += _read(0, "a.py", "A-real " + "a" * 2000)
    msgs += _read(1, "a.py", "NOTE: you already read exactly this")
    for i in range(KEEP_RECENT):
        call = ToolCall(id=f"s{i}", name="search", arguments="{}")
        msgs.append(Message(role="assistant", tool_calls=[call]))
        msgs.append(Message(role="tool", tool_call_id=f"s{i}", content="hit " + "s" * 2000))
    contents = [m.content for m in compact(msgs) if m.role == "tool"]
    assert any(c.startswith("A-real") and "elided" not in c for c in contents)
