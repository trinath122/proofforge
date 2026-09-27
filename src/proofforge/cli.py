"""Command line: `proofforge doctor`, `proofforge smoke`, `proofforge fix TASK.json`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from proofforge.budget import BudgetGuard
from proofforge.config import Settings
from proofforge.demo import median_task
from proofforge.engine import Engine, FixTask
from proofforge.llm.client import TokenFactoryLLM
from proofforge.models.registry import MODELS, Mode
from proofforge.receipts import Receipt, write_receipt
from proofforge.sandbox.contree import ContreeSandbox

app = typer.Typer(add_completion=False, help="ProofForge: no 'done' without proof.")
console = Console()


def _settings(mode: Mode | None) -> Settings:
    load_dotenv()  # the sandbox SDK reads NEBIUS_API_KEY / NEBIUS_PROJECT_ID from the process env
    settings = Settings()
    if mode is not None:
        settings.mode = mode
    if settings.nebius_api_key is None:
        console.print(
            "[red]NEBIUS_API_KEY is not set.[/red] Put it in a .env file "
            "(see .env.example) or set it in your shell."
        )
        raise typer.Exit(2)
    return settings


def _engine(settings: Settings) -> tuple[Engine, BudgetGuard]:
    assert settings.nebius_api_key is not None  # noqa: S101 - checked in _settings
    budget = BudgetGuard(settings.task_budget_usd, settings.session_budget_usd)
    llm = TokenFactoryLLM(settings.nebius_api_key.get_secret_value(), settings.mode, budget)
    engine = Engine(
        ContreeSandbox(),
        llm,
        budget,
        mode=settings.mode,
        max_rounds=settings.max_fix_attempts,
        branch_width=settings.branch_width,
    )
    return engine, budget


def _report(receipt: Receipt, settings: Settings) -> None:
    path = write_receipt(receipt, settings.receipts_dir)
    color = "green" if receipt.status == "verified" else "yellow"
    console.print(f"\n[bold {color}]{receipt.status.upper()}[/bold {color}]  {receipt.task_title}")
    table = Table("Round", "Branch", "Model", "Visible", "Holdout", "Flags", "Cost")
    for a in receipt.attempts:
        vis = [g for g in a.gates if g.kind == "visible"]
        hold = [g for g in a.gates if g.kind == "holdout"]
        table.add_row(
            str(a.round),
            str(a.branch),
            a.model_key,
            f"{sum(g.passed for g in vis)}/{len(vis)}",
            f"{sum(g.passed for g in hold)}/{len(hold)}",
            ", ".join(a.tampered_files + a.rejected_edits) or a.error or "-",
            f"${a.cost_usd:.4f}",
        )
    console.print(table)
    if receipt.diff:
        console.print(receipt.diff, highlight=False, markup=False)
    console.print(
        f"Total ${receipt.total_cost_usd:.4f} in {receipt.wall_time_s:.1f}s. "
        f"Receipt: {path} (and .md)"
    )
    if receipt.note:
        console.print(f"[dim]{receipt.note}[/dim]")


@app.command()
def doctor() -> None:
    """Check credentials, model access and sandbox access. Costs nothing."""
    settings = _settings(None)
    assert settings.nebius_api_key is not None  # noqa: S101
    budget = BudgetGuard(0.01, 0.01)
    llm = TokenFactoryLLM(settings.nebius_api_key.get_secret_value(), Mode.DEV, budget)

    async def check() -> None:
        found = await llm.list_model_ids()
        available = {mid for ids in found.values() for mid in ids}
        table = Table("Model", "ID", "Endpoint", "Status")
        for spec in MODELS.values():
            ok = spec.model_id in available
            table.add_row(
                spec.key,
                spec.model_id,
                spec.base_url.split("//")[1].split("/")[0],
                "[green]available[/green]" if ok else "[red]not listed[/red]",
            )
        console.print(table)
        try:
            info = await ContreeSandbox().whoami()
            console.print(f"[green]Sandboxes: access OK[/green] {info}")
        except Exception as exc:
            console.print(f"[red]Sandboxes: {type(exc).__name__}: {exc}[/red]")

    asyncio.run(check())


@app.command()
def smoke(
    mode: Annotated[Mode, typer.Option(help="dev = Lightning only (cheapest)")] = Mode.DEV,
) -> None:
    """Run the built-in median() task end to end on real Nebius infrastructure (~$0.01)."""
    settings = _settings(mode)
    engine, _ = _engine(settings)
    receipt = asyncio.run(engine.fix(median_task()))
    _report(receipt, settings)
    raise typer.Exit(0 if receipt.status == "verified" else 1)


@app.command()
def fix(
    task_file: Annotated[Path, typer.Argument(exists=True, readable=True)],
    mode: Annotated[Mode | None, typer.Option()] = None,
) -> None:
    """Run a task described in a JSON file (see FixTask for the schema)."""
    settings = _settings(mode)
    task = FixTask.model_validate(json.loads(task_file.read_text(encoding="utf-8")))
    engine, _ = _engine(settings)
    receipt = asyncio.run(engine.fix(task))
    _report(receipt, settings)
    raise typer.Exit(0 if receipt.status == "verified" else 1)


if __name__ == "__main__":
    app()
