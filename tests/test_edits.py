from __future__ import annotations

import json

from proofforge.engine.edits import apply_edit, replace_lines
from proofforge.engine.tools import Workspace
from proofforge.gates.base import workspace_path
from proofforge.sandbox.base import NOOP
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY

JS = "Groups.removeCover = async function (data) {\n\tawait db.del(data);\n};\n\nexport {};\n"


def test_exact_and_ambiguous() -> None:
    assert apply_edit("a = 1\n", "a = 1", "a = 2").text == "a = 2\n"
    dup = apply_edit("x\nx\n", "x", "y")
    assert dup.text is None
    assert "matches 2 places" in dup.message
    assert apply_edit("a", "", "b").text is None


def test_tabs_vs_spaces_keep_the_files_indentation() -> None:
    old = "Groups.removeCover = async function (data) {\n    await db.del(data);\n};"
    new = (
        "Groups.removeCover = async function (data) {\n"
        "    await db.del(data);\n    await files.rm(data);\n};"
    )
    result = apply_edit(JS, old, new)
    assert result.text is not None, result.message
    assert "\n\tawait files.rm(data);\n" in result.text, "tabs carried over"
    assert result.text.endswith("\nexport {};\n")
    assert "ignoring whitespace" in result.message


def test_line_number_prefixes_are_ignored() -> None:
    numbered = "    2  \tawait db.del(data);"
    result = apply_edit(JS, numbered, "    2  \tawait db.remove(data);")
    assert result.text is not None, result.message
    assert "\tawait db.remove(data);" in result.text


def test_not_found_points_to_the_closest_lines() -> None:
    result = apply_edit(JS, "Groups.removeCovers = function () {", "x")
    assert result.text is None
    assert "near line 1" in result.message
    assert "replace_lines" in result.message
    far = apply_edit(JS, "completely unrelated text here", "x")
    assert "does not appear" in far.message


def test_replace_lines() -> None:
    assert replace_lines("a\nb\nc\n", 2, 2, "B").text == "a\nB\nc\n"
    assert replace_lines("a\nb\n", 2, 5, "x").text is None


async def test_workspace_replace_lines(sandbox: LocalSandbox) -> None:
    base = await sandbox.base("x")
    seeded = await sandbox.run(base, NOOP, files={workspace_path("g.js"): JS.encode()})
    ws = Workspace(sandbox, seeded.checkpoint, protected=set(), python=PY)
    out, _ = await ws.call(
        "replace_lines",
        json.dumps(
            {"path": "g.js", "start_line": 2, "end_line": 2, "content": "\tawait db.rm(data);"}
        ),
    )
    assert out.startswith("wrote g.js")
    assert ws.writes == 1
    text = (await sandbox.read(ws.checkpoint, workspace_path("g.js"))).decode()
    assert "\tawait db.rm(data);\n};" in text
