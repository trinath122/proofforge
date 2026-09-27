"""Live checks against real Nebius services. Run with: uv run pytest -m live --no-cov"""

from __future__ import annotations

import pytest

from proofforge.budget import BudgetGuard
from proofforge.config import Settings
from proofforge.demo import median_task
from proofforge.engine.loop import Engine
from proofforge.llm.client import TokenFactoryLLM
from proofforge.models.registry import Mode
from proofforge.sandbox.contree import ContreeSandbox

pytestmark = pytest.mark.live


async def test_sandbox_checkpoints_branch_independently() -> None:
    sb = ContreeSandbox()
    base = await sb.base("python:3.12-slim")
    v1 = await sb.run(base, "echo v1 > state.txt")
    a = await sb.run(v1.checkpoint, "echo A > state.txt")
    b = await sb.run(v1.checkpoint, "echo B > state.txt")
    assert (await sb.read(a.checkpoint, "/workspace/state.txt")).strip() == b"A"
    assert (await sb.read(b.checkpoint, "/workspace/state.txt")).strip() == b"B"
    assert (await sb.read(v1.checkpoint, "/workspace/state.txt")).strip() == b"v1"


async def test_smoke_task_verifies_on_lightning() -> None:
    settings = Settings()
    assert settings.nebius_api_key is not None
    budget = BudgetGuard(task_cap_usd=0.05, session_cap_usd=0.05)
    llm = TokenFactoryLLM(settings.nebius_api_key.get_secret_value(), Mode.DEV, budget)
    receipt = await Engine(ContreeSandbox(), llm, budget, mode=Mode.DEV).fix(median_task())
    assert receipt.status == "verified", receipt.note
