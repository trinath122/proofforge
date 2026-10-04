from __future__ import annotations

import json

import pytest
from contree_sdk.sdk.exceptions.api import ApiTimeoutError, NotFoundError

from proofforge.engine.spec import Interface, parse_interfaces
from proofforge.engine.tools import Workspace
from proofforge.gates.base import workspace_path
from proofforge.sandbox import contree
from proofforge.sandbox.base import NOOP
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import PY

SPEC = """# Title
Fix it.

## Requirements
- Name: `not_an_interface` (outside the section, ignored)

## New Interfaces
- Path: `lib/pkg/mod.py`

- Name: `mod.py`

- Type: file

- Path: `lib/pkg/mod.py`
- Name: `Thing.do_work`
- Type: method

- Path: `src/user/picture.js`
- Name: `User.getLocalCoverPath`
- Type: method
"""


def test_parse_interfaces() -> None:
    found = parse_interfaces(SPEC)
    assert found == [
        Interface("lib/pkg/mod.py", "mod.py", "file"),
        Interface("lib/pkg/mod.py", "Thing.do_work", "method"),
        Interface("src/user/picture.js", "User.getLocalCoverPath", "method"),
    ]
    assert found[1].symbol == "do_work"
    assert found[2].symbol == "getLocalCoverPath"
    assert parse_interfaces("## Requirements\n- Path: `a.py`\n- Name: `x`\n") == []


async def test_submit_checks_interfaces_once(tmp_path: object) -> None:
    sandbox = LocalSandbox(tmp_path)  # type: ignore[arg-type]
    base = await sandbox.base("x")
    seeded = await sandbox.run(
        base, NOOP, files={workspace_path("lib/pkg/mod.py"): b"class Thing:\n    pass\n"}
    )
    ws = Workspace(
        sandbox, seeded.checkpoint, protected=set(), python=PY, interfaces=parse_interfaces(SPEC)
    )
    first, done = await ws.call("submit", json.dumps({"summary": "x"}))
    assert not done
    assert "NOT SUBMITTED" in first
    assert "`do_work` not found in lib/pkg/mod.py" in first
    assert "file src/user/picture.js does not exist" in first
    assert "mod.py: " not in first.split("missing:")[1].split("\n")[1], "existing file is fine"
    _, done = await ws.call("submit", json.dumps({"summary": "x"}))
    assert done, "the check runs once; the gates decide"
    assert ws.spec_misses


async def test_submit_passes_when_interfaces_exist(tmp_path: object) -> None:
    sandbox = LocalSandbox(tmp_path)  # type: ignore[arg-type]
    base = await sandbox.base("x")
    files = {
        "lib/pkg/mod.py": b"class Thing:\n    def do_work(self):\n        return 1\n",
        "src/user/picture.js": b"User.getLocalCoverPath = async function (uid) {};\n",
    }
    seeded = await sandbox.run(base, NOOP, files={workspace_path(k): v for k, v in files.items()})
    ws = Workspace(
        sandbox, seeded.checkpoint, protected=set(), python=PY, interfaces=parse_interfaces(SPEC)
    )
    out, done = await ws.call("submit", json.dumps({"summary": "x"}))
    assert done, out
    assert ws.spec_misses == []


async def test_sandbox_retries_only_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contree, "RETRY_DELAYS_S", (0.0, 0.0))
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ApiTimeoutError(timeout_type="connect", request=None, response=None)
        return "ok"

    assert await contree._retrying(flaky) == "ok"
    assert len(attempts) == 3

    async def missing() -> str:
        attempts.append(1)
        raise NotFoundError(request=None, response=None)

    attempts.clear()
    with pytest.raises(NotFoundError):
        await contree._retrying(missing)
    assert len(attempts) == 1
