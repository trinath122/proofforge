"""The verification engine: Reproduce -> Change -> Verify with proof -> Repeat.

Each round forks `branch_width` candidates from the same sandbox checkpoint and
runs them in parallel. The first candidate that passes every gate, including the
hidden holdouts, with no integrity flags, wins. Otherwise the best candidate
becomes the next round's starting point and its visible failures become feedback.
"""

from __future__ import annotations

import asyncio
import difflib
import time
import uuid
from dataclasses import dataclass, field

from proofforge.budget import BudgetExceededError, BudgetGuard
from proofforge.engine.prompts import build_messages, parse_edits
from proofforge.engine.task import FixTask
from proofforge.gates.base import (
    GateResult,
    Oracle,
    detect_tampering,
    run_gates,
    workspace_path,
)
from proofforge.llm.base import LLM
from proofforge.models.registry import Mode, Role
from proofforge.receipts.schema import Attempt, ModelUsage, Receipt, Status
from proofforge.sandbox.base import NOOP, Checkpoint, Sandbox


@dataclass
class _State:
    checkpoint: Checkpoint
    files: dict[str, str]
    results: list[GateResult]
    rejected: list[str]
    notes: list[str] = field(default_factory=list)


def _format_note(attempts: list[Attempt]) -> str:
    truncated = any(a.finish_reason == "length" for a in attempts)
    cause = " It was cut off at the output limit." if truncated else ""
    return (
        "It contained no file edit that could be applied." + cause + " Keep reasoning brief and "
        "reply with the complete new file content in the required `### FILE:` format."
    )


def unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path, ""), after.get(path, "")
        if old != new:
            chunks.extend(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
    return "".join(chunks)


class Engine:
    def __init__(
        self,
        sandbox: Sandbox,
        llm: LLM,
        budget: BudgetGuard,
        *,
        mode: Mode,
        max_rounds: int = 3,
        branch_width: int = 1,
    ) -> None:
        self.sandbox = sandbox
        self.llm = llm
        self.budget = budget
        self.mode = mode
        self.max_rounds = max_rounds
        self.branch_width = branch_width

    async def fix(self, task: FixTask) -> Receipt:
        started = time.monotonic()
        self.budget.start_task()
        oracle = task.oracle()
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        base = await self.sandbox.base(task.image)
        seed = {workspace_path(p): c.encode() for p, c in task.workspace_seed().items()}
        setup = await self.sandbox.run(base, task.setup_command, files=seed, keep=True)

        def receipt(status: Status, **kw: object) -> Receipt:
            return Receipt(
                run_id=run_id,
                task_title=task.title,
                mode=self.mode.value,
                status=status,
                image=task.image,
                base_checkpoint=setup.checkpoint.id,
                oracle_digest=oracle.digest,
                protected_hashes=oracle.protected_hashes,
                usage={k: ModelUsage(**vars(u)) for k, u in self.budget.task_by_model.items()},
                total_cost_usd=self.budget.task.cost_usd,
                wall_time_s=time.monotonic() - started,
                **kw,  # type: ignore[arg-type]
            )

        if not setup.ok:
            return receipt(
                "reproduction_failed",
                reproduction=[],
                attempts=[],
                note=f"setup failed: {setup.stderr[-500:]}",
            )

        repro = await run_gates(self.sandbox, setup.checkpoint, oracle, include_holdout=False)
        if all(g.passed for g in repro):
            return receipt(
                "already_passing",
                reproduction=repro,
                attempts=[],
                note="Visible gates already pass before any change; nothing to prove.",
            )

        state = _State(setup.checkpoint, dict(task.editable), repro, [])
        attempts: list[Attempt] = []
        try:
            for rnd in range(1, self.max_rounds + 1):
                role = Role.CODER if rnd == 1 else Role.FIXER
                outcomes = await asyncio.gather(
                    *(
                        self._attempt(task, oracle, state, rnd=rnd, branch=b, role=role)
                        for b in range(self.branch_width)
                    )
                )
                attempts.extend(a for a, _ in outcomes)
                for attempt, final in outcomes:
                    if attempt.all_passed and final is not None:
                        return receipt(
                            "verified",
                            reproduction=repro,
                            attempts=attempts,
                            final_checkpoint=final.checkpoint.id,
                            final_gates=attempt.gates,
                            diff=unified_diff(task.editable, final.files),
                        )
                usable = [(a, s) for a, s in outcomes if s is not None and not a.tampered_files]
                if usable:
                    best_attempt, best = max(usable, key=lambda x: x[0].visible_passed)
                    best.rejected = best_attempt.rejected_edits
                    state = best
                else:
                    state.rejected = sorted({p for a, _ in outcomes for p in a.rejected_edits})
                    failed = [a for a, _ in outcomes if a.error]
                    state.notes = [_format_note(failed)] if failed else []
        except BudgetExceededError as exc:
            return receipt(
                "budget_exceeded",
                reproduction=repro,
                attempts=attempts,
                diff=unified_diff(task.editable, state.files),
                note=str(exc),
            )
        return receipt(
            "failed",
            reproduction=repro,
            attempts=attempts,
            diff=unified_diff(task.editable, state.files),
            note=f"No candidate passed every gate in {self.max_rounds} round(s).",
        )

    async def _attempt(
        self,
        task: FixTask,
        oracle: Oracle,
        state: _State,
        *,
        rnd: int,
        branch: int,
        role: Role,
    ) -> tuple[Attempt, _State | None]:
        messages = build_messages(
            task.description,
            editable=state.files,
            context=task.context,
            protected=task.protected,
            results=state.results,
            rejected=state.rejected,
            notes=state.notes,
        )
        temperature = 0.2 if branch == 0 else min(0.2 + 0.25 * branch, 1.0)
        completion = await self.llm.complete(role, messages, temperature=temperature)
        edits = parse_edits(completion.text, task.editable)
        allowed = {p: c for p, c in edits.items() if p in task.editable}
        rejected = sorted(p for p in edits if p not in task.editable)
        attempt = Attempt(
            round=rnd,
            branch=branch,
            model_key=completion.model_key,
            checkpoint=None,
            edited_files=sorted(allowed),
            rejected_edits=rejected,
            cost_usd=completion.cost_usd,
            finish_reason=completion.finish_reason,
        )
        if not allowed:
            attempt.error = "no applicable file edit in reply"
            if completion.finish_reason == "length":
                attempt.error += " (cut off at output limit)"
            attempt.response_excerpt = completion.text[-2000:]
            return attempt, None

        applied = await self.sandbox.run(
            state.checkpoint,
            NOOP,
            files={workspace_path(p): c.encode() for p, c in allowed.items()},
            keep=True,
        )
        attempt.checkpoint = applied.checkpoint.id
        attempt.tampered_files = await detect_tampering(self.sandbox, applied.checkpoint, oracle)
        attempt.gates = await run_gates(
            self.sandbox, applied.checkpoint, oracle, include_holdout=True
        )
        new_state = _State(applied.checkpoint, state.files | allowed, attempt.gates, rejected)
        return attempt, new_state
