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
    msgs = _conversation(20)
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
