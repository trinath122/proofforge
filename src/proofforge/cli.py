"""Command line: `proofforge doctor`, `proofforge smoke`, `proofforge fix TASK.json`."""

from __future__ import annotations

import asyncio
import json
import time
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
from proofforge.playbooks import CaseValidation, discover_cases, load_case, validate_case
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


DEFAULT_CASES = Path("bench/pipelinebench/cases")


def _select(cases_dir: Path, only: list[str] | None) -> list[Path]:
    found = discover_cases(cases_dir)
    if only:
        found = [c for c in found if c.name in set(only)]
    if not found:
        console.print(f"[red]No PipelineBench cases found in {cases_dir}[/red]")
        raise typer.Exit(2)
    return found


@app.command()
def pipeline(
    case: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    mode: Annotated[Mode | None, typer.Option()] = None,
) -> None:
    """Pipeline Doctor: repair one PipelineBench case directory."""
    settings = _settings(mode)
    engine, _ = _engine(settings)
    receipt = asyncio.run(engine.fix(load_case(case)))
    _report(receipt, settings)
    raise typer.Exit(0 if receipt.status == "verified" else 1)


@app.command()
def bench(
    cases: Annotated[Path, typer.Option(help="PipelineBench cases directory")] = DEFAULT_CASES,
    only: Annotated[list[str] | None, typer.Option("--case", help="run only these")] = None,
    validate: Annotated[
        bool, typer.Option(help="no model calls: prove each case is broken and solvable")
    ] = False,
    mode: Annotated[Mode | None, typer.Option()] = None,
) -> None:
    """Run PipelineBench and report solve rate, cost and time."""
    load_dotenv()
    selected = _select(cases, only)

    if validate:
        results = asyncio.run(_validate_all(selected))
        table = Table("Case", "Shipped pipeline fails", "Reference solution passes", "Valid")
        for v in results:
            table.add_row(
                v.case,
                str(v.broken_fails_visible),
                str(v.solution_passes_all),
                "[green]yes[/green]" if v.valid else "[red]NO[/red]",
            )
        console.print(table)
        raise typer.Exit(0 if all(v.valid for v in results) else 1)

    settings = _settings(mode)
    engine, budget = _engine(settings)
    receipts = asyncio.run(_run_all(engine, selected, settings))
    table = Table("Case", "Status", "Attempts", "Cost", "Time")
    for r in receipts:
        color = "green" if r.status == "verified" else "yellow"
        table.add_row(
            r.task_title.split("] ")[1].split(":")[0],
            f"[{color}]{r.status}[/{color}]",
            str(len(r.attempts)),
            f"${r.total_cost_usd:.4f}",
            f"{r.wall_time_s:.1f}s",
        )
    console.print(table)
    solved = sum(r.status == "verified" for r in receipts)
    summary = {
        "benchmark": "PipelineBench",
        "mode": settings.mode.value,
        "solved": solved,
        "total": len(receipts),
        "solve_rate": solved / len(receipts),
        "total_cost_usd": budget.session.cost_usd,
        "receipts": [r.run_id for r in receipts],
    }
    out = Path(settings.receipts_dir) / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    console.print(
        f"[bold]Solved {solved}/{len(receipts)}[/bold] in mode {settings.mode.value} for "
        f"${budget.session.cost_usd:.4f}. Summary: {out}"
    )
    raise typer.Exit(0 if solved == len(receipts) else 1)


async def _validate_all(selected: list[Path]) -> list[CaseValidation]:
    sandbox = ContreeSandbox()
    return list(await asyncio.gather(*(validate_case(sandbox, c) for c in selected)))


async def _run_all(engine: Engine, selected: list[Path], settings: Settings) -> list[Receipt]:
    receipts: list[Receipt] = []
    for case in selected:
        console.print(f"[dim]running {case.name}...[/dim]")
        receipt = await engine.fix(load_case(case))
        write_receipt(receipt, settings.receipts_dir)
        receipts.append(receipt)
    return receipts


if __name__ == "__main__":
    app()
