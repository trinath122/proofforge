from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from proofforge.budget import BudgetExceededError, BudgetGuard
from proofforge.config import Settings, key_from_contree_profile
from proofforge.engine.prompts import build_messages, parse_edits
from proofforge.engine.task import FixTask
from proofforge.gates.base import GateResult, GateSpec
from proofforge.models.registry import (
    GLOBAL_BASE_URL,
    MODELS,
    US_CENTRAL1_BASE_URL,
    Mode,
    Role,
    model_for,
)
from proofforge.receipts import Attempt, Receipt, render_markdown, write_receipt
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import GOOD_FIX, py


def test_models_match_console_values() -> None:
    assert MODELS["ultra"].base_url == US_CENTRAL1_BASE_URL
    assert MODELS["super"].base_url == US_CENTRAL1_BASE_URL
    assert MODELS["lightning"].base_url == GLOBAL_BASE_URL
    assert MODELS["lightning"].model_id == "nvidia/Nemotron-3_5-Lightning"
    assert MODELS["ultra"].cost_usd(1_000_000, 1_000_000) == pytest.approx(4.0)


@pytest.mark.parametrize("mode", list(Mode))
def test_every_role_is_routed(mode: Mode) -> None:
    for role in Role:
        assert model_for(role, mode).key in MODELS


def test_dev_mode_is_lightning_only() -> None:
    assert {model_for(r, Mode.DEV).key for r in Role} == {"lightning"}
    assert model_for(Role.PLANNER, Mode.MAX).key == "ultra"


def test_budget_caps() -> None:
    guard = BudgetGuard(task_cap_usd=0.001, session_cap_usd=0.01)
    guard.ensure_headroom()
    guard.charge(MODELS["ultra"], 1000, 0)
    with pytest.raises(BudgetExceededError, match="task cap"):
        guard.ensure_headroom()
    guard.start_task()
    guard.ensure_headroom()
    for _ in range(10):
        guard.charge(MODELS["ultra"], 1000, 0)
    guard.start_task()
    with pytest.raises(BudgetExceededError, match="session cap"):
        guard.ensure_headroom()


def test_parse_edits() -> None:
    text = GOOD_FIX + "\n\n### FILE: ./pkg/util.py\n```\nX = 1\n```\n"
    edits = parse_edits(text)
    assert set(edits) == {"stats.py", "pkg/util.py"}
    assert edits["pkg/util.py"] == "X = 1\n"
    assert parse_edits("no code here") == {}


def test_prompt_hides_holdout_details() -> None:
    results = [
        GateResult(
            name="v",
            kind="visible",
            passed=False,
            exit_code=1,
            stdout_tail="",
            stderr_tail="AssertionError: 3",
            elapsed_s=0.1,
        ),
        GateResult(
            name="h",
            kind="holdout",
            passed=False,
            exit_code=1,
            stdout_tail="SECRET",
            stderr_tail="SECRET",
            elapsed_s=0.1,
        ),
    ]
    msgs = build_messages(
        "fix it",
        editable={"a.py": "x"},
        context={},
        protected={"t.py": "assert x"},
        results=results,
        rejected=[],
    )
    body = msgs[-1].content
    assert "AssertionError: 3" in body
    assert "SECRET" not in body
    assert "1 hidden check(s)" in body


def test_task_validation() -> None:
    gate = GateSpec(name="t", command="true")
    with pytest.raises(ValidationError, match="cannot also be oracle"):
        FixTask(
            title="x", description="x", editable={"t.py": ""}, protected={"t.py": ""}, gates=[gate]
        )
    with pytest.raises(ValidationError, match="visible gate"):
        FixTask(
            title="x",
            description="x",
            editable={"a.py": ""},
            gates=[GateSpec(name="h", command="true", kind="holdout")],
        )
    with pytest.raises(ValidationError, match="no holdout gate"):
        FixTask(
            title="x", description="x", editable={"a.py": ""}, holdout={"h.py": ""}, gates=[gate]
        )


def test_receipt_roundtrip(tmp_path: Path) -> None:
    gate = GateResult(
        name="v",
        kind="visible",
        passed=True,
        exit_code=0,
        stdout_tail="",
        stderr_tail="",
        elapsed_s=0.1,
    )
    receipt = Receipt(
        run_id="r1",
        task_title="Fix",
        mode="dev",
        status="verified",
        image="img",
        base_checkpoint="cp0",
        final_checkpoint="cp1",
        oracle_digest="d" * 64,
        protected_hashes={},
        reproduction=[gate.model_copy(update={"passed": False})],
        attempts=[
            Attempt(
                round=1,
                branch=0,
                model_key="lightning",
                checkpoint="cp1",
                edited_files=["a.py"],
                gates=[gate],
            )
        ],
        final_gates=[gate],
        diff="--- a/a.py\n+++ b/a.py\n",
        note="ok",
    )
    path = write_receipt(receipt, tmp_path)
    assert Receipt.model_validate_json(path.read_text()) == receipt
    md = render_markdown(receipt)
    assert "PASS `v` [visible]" in md
    assert "```diff" in md
    assert (tmp_path / "r1.md").exists()


def test_key_fallback_from_contree_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ini = tmp_path / "auth.ini"
    ini.write_text("[DEFAULT]\nprofile = default\n\n[profile:default]\ntoken = abc\n")
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert key_from_contree_profile() == "abc"
    settings = Settings()
    assert settings.nebius_api_key is not None
    assert settings.nebius_api_key.get_secret_value() == "abc"

    monkeypatch.setenv("NEBIUS_API_KEY", "from-env")
    assert Settings().nebius_api_key.get_secret_value() == "from-env"  # type: ignore[union-attr]
    assert key_from_contree_profile(tmp_path / "missing.ini") is None


async def test_local_sandbox_branches_are_independent(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    base = await sb.base("any")
    v1 = await sb.run(base, "echo v1 > state.txt")
    a = await sb.run(v1.checkpoint, "echo A > state.txt")
    b = await sb.run(v1.checkpoint, "echo B > state.txt")
    assert (await sb.read(a.checkpoint, "/workspace/state.txt")).strip() == b"A"
    assert (await sb.read(b.checkpoint, "/workspace/state.txt")).strip() == b"B"
    assert (await sb.read(v1.checkpoint, "state.txt")).strip() == b"v1"
    timeout = await sb.run(v1.checkpoint, py("import time; time.sleep(3)"), timeout_s=1, keep=False)
    assert timeout.exit_code != 0


@pytest.mark.parametrize(
    "reply",
    [
        "**FILE: stats.py**\n```python\nX = 1\n```",
        "File: `stats.py`\n\n```\nX = 1\n```",
        "## file: ./stats.py\n```py\nX = 1\n```",
        "Here is the fix:\n```python\nX = 0\n```\nFinal version:\n```python\nX = 1\n```",
        "<think>maybe ```python\nX = 9\n```</think>\n```python\nX = 1\n```",
    ],
)
def test_parse_edits_tolerates_formats(reply: str) -> None:
    assert parse_edits(reply, ["stats.py"]) == {"stats.py": "X = 1\n"}


def test_parse_edits_refuses_ambiguous_or_truncated_replies() -> None:
    plain = "```python\nX = 1\n```"
    assert parse_edits(plain, ["a.py", "b.py"]) == {}  # which file? refuse to guess
    assert parse_edits("<think>long reasoning ```x\n1\n```", ["a.py"]) == {}
    assert parse_edits("no code at all", ["a.py"]) == {}
