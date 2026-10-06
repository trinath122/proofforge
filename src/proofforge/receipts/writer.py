"""Persist receipts as JSON (machine) plus Markdown (human)."""

from __future__ import annotations

from pathlib import Path

from proofforge.receipts.schema import Receipt

_ICON = {True: "PASS", False: "FAIL"}


def render_markdown(r: Receipt) -> str:
    lines = [
        f"# Proof receipt: {r.task_title}",
        "",
        f"- **Status:** {r.status}",
        f"- **Run:** `{r.run_id}`  |  mode `{r.mode}`  |  image `{r.image}`",
        f"- **Oracle digest:** `{r.oracle_digest[:16]}...`",
        f"- **Cost:** ${r.total_cost_usd:.4f}  |  **Wall time:** {r.wall_time_s:.1f}s  |  "
        f"**Tokens:** {sum(a.prompt_tokens for a in r.attempts):,} in, "
        f"{sum(a.completion_tokens for a in r.attempts):,} out",
        f"- **Final checkpoint:** `{r.final_checkpoint or '-'}`",
        "",
        "## Reproduction (must fail before the change)",
        "",
    ]
    lines += [f"- {_ICON[g.passed]} `{g.name}` (exit {g.exit_code})" for g in r.reproduction]
    lines += [
        "",
        "## Attempts",
        "",
        "| Round | Branch | Entrant | Model | Steps | Visible | Breaker | Hidden holdout | Flags "
        "| Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in r.attempts:
        lines.append(
            f"| {a.round} | {a.branch} | {a.label or '-'} | {a.model_key} | {a.steps or '-'} "
            f"| {a.score('visible')} | {a.breaker_score or '-'} | {a.score('holdout')} "
            f"| {a.flags} | ${a.cost_usd:.4f} |"
        )
    failed_replies = [a for a in r.attempts if a.error and a.response_excerpt]
    if failed_replies:
        lines += ["", "## Unusable model replies (tail)", ""]
        for a in failed_replies:
            lines += [
                f"**Round {a.round}, branch {a.branch}** (finish: {a.finish_reason or '?'})",
                "",
                "```text",
                a.response_excerpt[-600:].rstrip(),
                "```",
                "",
            ]
    if r.final_gates:
        lines += ["", "## Final gates (visible + hidden holdout)", ""]
        lines += [f"- {_ICON[g.passed]} `{g.name}` [{g.kind}]" for g in r.final_gates]
    if r.diff:
        lines += ["", "## Diff", "", "```diff", r.diff.rstrip(), "```"]
    if r.note:
        lines += ["", f"> {r.note}"]
    return "\n".join(lines) + "\n"


def write_receipt(receipt: Receipt, directory: str | Path) -> Path:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{receipt.run_id}.json"
    json_path.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
    (out / f"{receipt.run_id}.md").write_text(render_markdown(receipt), encoding="utf-8")
    return json_path
