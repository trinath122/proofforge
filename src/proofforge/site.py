"""Build the static receipts site (docs/) from saved receipts.

The site replays real runs: what the agent did step by step, which checks ran, what the
hidden checks said and the final verdict. It makes no model calls, so it can be hosted
for free (GitHub Pages serves docs/) and stays up as long as the repository does.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from proofforge.gates.base import GateResult
from proofforge.playbooks import harbor, impossible
from proofforge.receipts.schema import Attempt, Receipt

TAIL = 1500
DIFF_LIMIT = 30_000
ARG_LIMIT = 160
OUT_LIMIT = 280


def suite_of(receipt: Receipt) -> str:
    title = receipt.task_title
    for prefix, suite in (
        ("[Impossible]", "impossible"),
        ("[Harbor]", "harbor"),
        ("[RealWorld", "realworld"),
        ("[PipelineBench]", "pipeline"),
    ):
        if title.startswith(prefix):
            return suite
    return "other"


def verdict(receipt: Receipt) -> str:
    """The one word stamped on the receipt."""
    if suite_of(receipt) == "impossible":
        return impossible.integrity(receipt)
    return receipt.status


def _gate(g: GateResult) -> dict[str, Any]:
    return {
        "name": g.name,
        "kind": g.kind,
        "passed": g.passed,
        "exit": g.exit_code,
        "seconds": round(g.elapsed_s, 1),
        "output": (g.stdout_tail + ("\n" + g.stderr_tail if g.stderr_tail.strip() else ""))[-TAIL:],
    }


def _arg(name: str, raw: str) -> str:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw[:ARG_LIMIT]
    if not isinstance(args, dict):
        return str(args)[:ARG_LIMIT]
    if name in {"write_file"}:
        lines = len(str(args.get("content", "")).splitlines())
        return f"{args.get('path', '')} ({lines} lines)"
    if name in {"edit_file"}:
        return str(args.get("path", ""))
    for key in ("command", "pattern", "path", "summary", "reason"):
        if key in args:
            return str(args[key])[:ARG_LIMIT]
    return json.dumps(args)[:ARG_LIMIT]


def trace(attempt: Attempt) -> list[dict[str, str]]:
    """The agent's tool calls in order, each with a short argument and the start of its output."""
    outputs = {
        str(m.get("tool_call_id")): str(m.get("content") or "")
        for m in attempt.transcript
        if m.get("role") == "tool"
    }
    steps: list[dict[str, str]] = []
    for m in attempt.transcript:
        if m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls") or []
        if isinstance(calls, str):  # older receipts stored the list as its repr
            continue
        for call in calls:
            fn = call.get("function", call)
            name = str(fn.get("name", ""))
            steps.append(
                {
                    "tool": name,
                    "arg": _arg(name, str(fn.get("arguments", ""))),
                    "out": outputs.get(str(call.get("id")), "")[:OUT_LIMIT],
                }
            )
    return steps


def _attempt(a: Attempt) -> dict[str, Any]:
    return {
        "label": a.label or f"round {a.round}" + (f", branch {a.branch}" if a.branch else ""),
        "round": a.round,
        "model": a.model_key,
        "steps": a.steps,
        "cost": round(a.cost_usd, 4),
        "visible": a.score("visible"),
        "hidden": a.score("holdout"),
        "breaker": a.breaker_score,
        "flags": "" if a.flags == "-" else a.flags,
        "summary": a.summary,
        "impossible": a.impossible_reason,
        "gates": [_gate(g) for g in a.gates],
        "trace": trace(a),
    }


def run_record(receipt: Receipt, story: dict[str, str]) -> dict[str, Any]:
    suite = suite_of(receipt)
    reward = harbor.receipt_reward(receipt) if suite == "harbor" else None
    return {
        "id": receipt.run_id,
        "title": receipt.task_title,
        "suite": suite,
        "verdict": verdict(receipt),
        "status": receipt.status,
        "reward": reward,
        "headline": story.get("headline", ""),
        "story": story.get("story", ""),
        "date": receipt.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        "mode": receipt.mode,
        "cost": round(receipt.total_cost_usd, 4),
        "seconds": round(receipt.wall_time_s),
        "note": receipt.note or "",
        "oracle": receipt.oracle_digest,
        "protected": len(receipt.protected_hashes),
        "reproduction": [_gate(g) for g in receipt.reproduction],
        "attempts": [_attempt(a) for a in receipt.attempts],
        "final": [_gate(g) for g in receipt.final_gates],
        "diff": receipt.diff[:DIFF_LIMIT],
        "diff_truncated": len(receipt.diff) > DIFF_LIMIT,
    }


def build_site(receipts_dir: Path, showcase: Path, out: Path) -> Path:
    spec = json.loads(showcase.read_text(encoding="utf-8"))
    runs = []
    for item in spec["runs"]:
        path = receipts_dir / f"{item['id']}.json"
        receipt = Receipt.model_validate_json(path.read_text(encoding="utf-8"))
        runs.append(run_record(receipt, item))
    data = {"intro": spec.get("intro", ""), "findings": spec.get("findings", []), "runs": runs}
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    # A script rather than JSON so the page also works when opened straight from disk.
    (out / "runs.js").write_text(f"window.PROOFFORGE = {payload};\n", encoding="utf-8")
    page = files("proofforge.site_assets").joinpath("index.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return out / "index.html"
