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

from proofforge.bench import SUITES, Case, SuiteName, detect, discover, with_targets
from proofforge.budget import BudgetGuard
from proofforge.config import Settings
from proofforge.demo import median_task
from proofforge.engine import Engine, FixTask
from proofforge.engine.loop import Strategy
from proofforge.llm.client import TokenFactoryLLM
from proofforge.models.registry import MODELS, Mode
from proofforge.playbooks import CaseValidation, harbor, impossible
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
        max_steps=settings.max_agent_steps,
    )
    return engine, budget


def _strategy_for(case: Case, settings: Settings) -> Strategy:
    return case.default_strategy if settings.strategy == "auto" else settings.strategy


def _report(receipt: Receipt, settings: Settings) -> None:
    path = write_receipt(receipt, settings.receipts_dir)
    color = "green" if receipt.status == "verified" else "yellow"
    console.print(f"\n[bold {color}]{receipt.status.upper()}[/bold {color}]  {receipt.task_title}")
    table = Table("Round", "Branch", "Model", "Steps", "Visible", "Holdout", "Flags", "Cost")
    for a in receipt.attempts:
        table.add_row(
            str(a.round),
            str(a.branch),
            a.model_key,
            str(a.steps or "-"),
            a.score("visible"),
            a.score("holdout"),
            a.flags,
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


StrategyOption = Annotated[
    str | None, typer.Option(help="rewrite | agent (default: auto per suite)")
]


def _apply_strategy(settings: Settings, strategy: str | None) -> None:
    if strategy is not None:
        if strategy not in ("rewrite", "agent", "auto"):
            console.print("[red]--strategy must be rewrite, agent or auto[/red]")
            raise typer.Exit(2)
        settings.strategy = strategy  # type: ignore[assignment]


@app.command()
def solve(
    case: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    mode: Annotated[Mode | None, typer.Option()] = None,
    strategy: StrategyOption = None,
) -> None:
    """Solve one PipelineBench, RealWorld or Harbor (SWE-bench Pro) task directory."""
    settings = _settings(mode)
    _apply_strategy(settings, strategy)
    found = detect(case)
    engine, _ = _engine(settings)
    engine.strategy = _strategy_for(found, settings)
    receipt = asyncio.run(engine.fix(found.load()))
    _report(receipt, settings)
    raise typer.Exit(0 if receipt.status == "verified" else 1)


@app.command()
def pipeline(
    case: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    mode: Annotated[Mode | None, typer.Option()] = None,
) -> None:
    """Pipeline Doctor: repair one PipelineBench case (alias of `solve`)."""
    solve(case, mode, None)


@app.command()
def bench(
    *,
    suite: Annotated[
        str, typer.Option(help="pipeline | realworld | all | harbor | impossible")
    ] = "all",
    only: Annotated[list[str] | None, typer.Option("--case", help="run only these")] = None,
    root: Annotated[
        Path | None,
        typer.Option(help="task directory for --suite harbor (e.g. SWE-bench Pro v2/tasks)"),
    ] = None,
    ids_file: Annotated[
        Path | None, typer.Option(help="file with one task id per line (e.g. v2/hard51_ids.txt)")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="run at most this many tasks")] = None,
    validate: Annotated[
        bool, typer.Option(help="no model calls: prove each case is broken and solvable")
    ] = False,
    mode: Annotated[Mode | None, typer.Option()] = None,
    strategy: StrategyOption = None,
) -> None:
    """Run the benchmark suites and report solve rate, cost and time."""
    load_dotenv()
    if suite not in ("pipeline", "realworld", "all", "harbor", "impossible"):
        console.print("[red]--suite must be pipeline, realworld, all, harbor or impossible[/red]")
        raise typer.Exit(2)
    suites: tuple[SuiteName, ...] = SUITES if suite == "all" else (suite,)  # type: ignore[assignment]
    roots: dict[SuiteName, Path] = {"harbor": root} if root else {}
    selected = discover(suites, roots=roots, only=only)
    if ids_file:
        wanted = [
            ln.strip() for ln in ids_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        order = {name: i for i, name in enumerate(wanted)}
        selected = sorted((c for c in selected if c.name in order), key=lambda c: order[c.name])
        targets = ids_file.parent / "targets.json"
        if targets.is_file():
            selected = with_targets(selected, targets)
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        console.print("[red]No cases found. Run from the repository root (or check --root).[/red]")
        raise typer.Exit(2)

    if validate:
        results = asyncio.run(_validate_all(selected))
        table = Table("Suite", "Case", "Shipped code fails", "Reference solution passes", "Valid")
        notes = any(v.note for v in results)
        if notes:
            table.add_column("Note")
        for c, v in zip(selected, results, strict=True):
            row = [
                c.suite,
                v.case,
                str(v.broken_fails_visible),
                str(v.solution_passes_all),
                "[green]yes[/green]" if v.valid else "[red]NO[/red]",
            ]
            table.add_row(*row, *([v.note] if notes else []))
        console.print(table)
        for v in results:
            if not v.valid:
                failing = [g for g in v.solution_gates if not g.passed]
                for g in failing:
                    tail = (g.stdout_tail + "\n" + g.stderr_tail).strip()[-1500:]
                    console.print(f"\n[bold]{v.case}[/bold]: {g.name} (exit {g.exit_code})")
                    console.print(tail, markup=False, highlight=False)
        report = Path("receipts") / f"validate-{time.strftime('%Y%m%d-%H%M%S')}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps([v.model_dump() for v in results], indent=2), encoding="utf-8")
        console.print(f"Full gate output: {report}")
        raise typer.Exit(0 if all(v.valid for v in results) else 1)

    settings = _settings(mode)
    _apply_strategy(settings, strategy)
    engine, budget = _engine(settings)
    receipts = asyncio.run(_run_all(engine, selected, settings))
    selected = selected[: len(receipts)]  # a provider outage can stop the run early
    solved = _report_bench(selected, receipts, settings, suites, budget)
    raise typer.Exit(0 if solved == len(receipts) else 1)


def _report_bench(
    selected: list[Case],
    receipts: list[Receipt],
    settings: Settings,
    suites: tuple[SuiteName, ...],
    budget: BudgetGuard,
) -> int:
    """Print the results table and write the run summary. Returns the number solved."""
    table = Table("Suite", "Case", "Strategy", "Status", "Attempts", "Steps", "Cost", "Time")
    for c, r in zip(selected, receipts, strict=True):
        status = impossible.integrity(r) if c.suite == "impossible" else r.status
        color = "green" if status in ("verified", "honest") else "yellow"
        if status == "cheated":
            color = "red"
        score = harbor.receipt_reward(r) if c.suite == "harbor" else None
        shown = f"{status} (reward {score:.2f})" if score is not None else status
        table.add_row(
            c.suite,
            c.name,
            _strategy_for(c, settings),
            f"[{color}]{shown}[/{color}]",
            str(len(r.attempts)),
            str(sum(a.steps for a in r.attempts)) or "-",
            f"${r.total_cost_usd:.4f}",
            f"{r.wall_time_s:.1f}s",
        )
    console.print(table)
    solved = sum(r.status == "verified" for r in receipts)
    per_suite = {
        s: {
            "solved": sum(
                r.status == "verified"
                for c, r in zip(selected, receipts, strict=True)
                if c.suite == s
            ),
            "total": sum(c.suite == s for c in selected),
        }
        for s in suites
    }
    honesty = {
        c.name: impossible.integrity(r)
        for c, r in zip(selected, receipts, strict=True)
        if c.suite == "impossible"
    }
    rewards = {
        c.name: harbor.receipt_reward(r)
        for c, r in zip(selected, receipts, strict=True)
        if c.suite == "harbor"
    }
    summary = {
        "mode": settings.mode.value,
        "rewards": rewards,
        "integrity": honesty,
        "strategy": settings.strategy,
        "solved": solved,
        "total": len(receipts),
        "solve_rate": solved / len(receipts),
        "suites": per_suite,
        "total_cost_usd": budget.session.cost_usd,
        "cases": {c.name: r.run_id for c, r in zip(selected, receipts, strict=True)},
    }
    out = Path(settings.receipts_dir) / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if honesty:
        kinds = ("honest", "gave_up", "cheated", "infra_error")
        counts = {k: sum(v == k for v in honesty.values()) for k in kinds}
        scored = len(honesty) - counts["infra_error"]
        rate = f"{counts['cheated'] / scored:.0%}" if scored else "n/a"
        skipped = (
            f", {counts['infra_error']} not run (sandbox error)" if counts["infra_error"] else ""
        )
        console.print(
            f"[bold]Impossible tasks:[/bold] {counts['honest']} honest, "
            f"{counts['gave_up']} gave up, [red]{counts['cheated']} cheated[/red]{skipped} "
            f"(cheat rate {rate} of {scored})"
        )
    console.print(
        f"[bold]Solved {solved}/{len(receipts)}[/bold] in mode {settings.mode.value} for "
        f"${budget.session.cost_usd:.4f}. Summary: {out}"
    )
    return solved


async def _validate_all(selected: list[Case]) -> list[CaseValidation]:
    sandbox = ContreeSandbox()
    return list(await asyncio.gather(*(c.validate(sandbox) for c in selected)))


async def _run_all(engine: Engine, selected: list[Case], settings: Settings) -> list[Receipt]:
    receipts: list[Receipt] = []
    for case in selected:
        engine.strategy = _strategy_for(case, settings)
        console.print(f"[dim]running {case.suite}/{case.name} ({engine.strategy})...[/dim]")
        try:
            receipt = await engine.fix(case.load())
        except Exception as exc:  # one broken task must never sink a whole run
            receipt = Receipt(
                run_id=f"{time.strftime('%Y%m%d-%H%M%S')}-error",
                task_title=case.name,
                mode=settings.mode.value,
                status="infra_error",
                image="",
                base_checkpoint="",
                oracle_digest="",
                protected_hashes={},
                reproduction=[],
                attempts=[],
                total_cost_usd=engine.budget.task.cost_usd,
                note=f"{type(exc).__name__}: {exc}"[:1000],
            )
            console.print(f"[red]{case.name}: {receipt.note}[/red] Continuing with the next task.")
        write_receipt(receipt, settings.receipts_dir)
        receipts.append(receipt)
        if receipt.status == "budget_exceeded" and "HTTP 402" in (receipt.note or ""):
            console.print(f"[red]{receipt.note}[/red] Stopping; results so far are kept.")
            break
    return receipts


if __name__ == "__main__":
    app()
