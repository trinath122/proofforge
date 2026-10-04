"""The verification engine: Reproduce -> Change -> Verify with proof -> Repeat.

Each round forks `branch_width` candidates from the same sandbox checkpoint and
runs them in parallel. The first candidate that passes every gate, including the
hidden holdouts, with no integrity flags, wins. Otherwise the best candidate
becomes the next round's starting point and its visible failures become feedback.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from proofforge.budget import BudgetExceededError, BudgetGuard
from proofforge.engine.coach import Coach
from proofforge.engine.context import compact
from proofforge.engine.prompts import build_agent_messages, build_messages, parse_edits
from proofforge.engine.spec import parse_interfaces
from proofforge.engine.task import FixTask
from proofforge.engine.tools import TOOL_SPECS, Workspace
from proofforge.gates.base import (
    GateResult,
    Oracle,
    detect_tampering,
    run_gates,
    workspace_path,
)
from proofforge.llm.base import LLM, Message
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
    summary: str = ""


Strategy = Literal["rewrite", "agent"]


def _tamper_note(files: list[str]) -> str:
    return (
        "These frozen files were modified by your commands, which disqualifies the attempt: "
        f"{', '.join(files)}. Leave them untouched and fix the code under test."
    )


def _agent_note(attempts: list[Attempt]) -> str:
    return (
        "It made no file changes before stopping. Explore briefly, then edit files with "
        "write_file or edit_file, run the checks, and call submit."
    )


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
        strategy: Strategy = "rewrite",
        max_steps: int = 30,
        python: str = "python3",
    ) -> None:
        self.sandbox = sandbox
        self.llm = llm
        self.budget = budget
        self.mode = mode
        self.max_rounds = max_rounds
        self.branch_width = branch_width
        self.strategy = strategy
        self.max_steps = max_steps
        self.python = python  # interpreter inside the sandbox, used by the agent's tools

    async def fix(self, task: FixTask) -> Receipt:
        started = time.monotonic()
        self.budget.start_task()
        oracle = task.oracle()
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        base = await self.sandbox.base(task.image)
        seed = {
            workspace_path(p, task.workdir): c.encode() for p, c in task.workspace_seed().items()
        }
        setup = await self.sandbox.run(
            base, task.setup_command, files=seed, keep=True, cwd=task.workdir
        )

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

        # With no visible checks (SWE-bench style) the hidden ones prove the bug exists.
        # Their output is never shown to the agent, only how many failed.
        repro = await run_gates(
            self.sandbox, setup.checkpoint, oracle, include_holdout=task.hidden_only
        )
        if all(g.passed for g in repro):
            return receipt(
                "already_passing",
                reproduction=repro,
                attempts=[],
                note="Gates already pass before any change; nothing to prove.",
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
                            diff=await self._diff(task, final),
                        )
                usable = [(a, s) for a, s in outcomes if s is not None and not a.tampered_files]
                if usable:
                    best_attempt, best = max(usable, key=lambda x: x[0].visible_passed)
                    best.rejected = best_attempt.rejected_edits
                    state = best
                else:
                    state.rejected = sorted({p for a, _ in outcomes for p in a.rejected_edits})
                    state.notes = self._retry_notes([a for a, _ in outcomes])
        except BudgetExceededError as exc:
            return receipt(
                "budget_exceeded",
                reproduction=repro,
                attempts=attempts,
                diff=await self._diff(task, state),
                note=str(exc),
            )
        return receipt(
            "failed",
            reproduction=repro,
            attempts=attempts,
            diff=await self._diff(task, state),
            note=f"No candidate passed every gate in {self.max_rounds} round(s).",
        )

    async def _diff(self, task: FixTask, state: _State) -> str:
        """The change as a unified diff: from git for in-image repositories."""
        if not task.repo_in_image:
            return unified_diff(task.editable, state.files)
        res = await self.sandbox.run(
            state.checkpoint,
            SCRATCH_AWARE_DIFF,
            keep=False,
            timeout_s=120,
            cwd=task.workdir,
        )
        return res.stdout if res.ok else ""

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
        if self.strategy == "agent" or task.repo_in_image:
            return await self._agent_attempt(task, oracle, state, rnd=rnd, branch=branch, role=role)
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
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
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
            files={workspace_path(p, task.workdir): c.encode() for p, c in allowed.items()},
            keep=True,
        )
        attempt.checkpoint = applied.checkpoint.id
        attempt.tampered_files = await detect_tampering(self.sandbox, applied.checkpoint, oracle)
        attempt.gates = await run_gates(
            self.sandbox, applied.checkpoint, oracle, include_holdout=True
        )
        new_state = _State(applied.checkpoint, state.files | allowed, attempt.gates, rejected)
        return attempt, new_state

    def _retry_notes(self, attempts: list[Attempt]) -> list[str]:
        """Explain to the model why none of this round's attempts could be kept."""
        notes: list[str] = []
        tampered = sorted({f for a in attempts for f in a.tampered_files})
        if tampered:
            notes.append(_tamper_note(tampered))
        failed = [a for a in attempts if a.error and not a.gates]
        if failed:
            notes.append(_agent_note(failed) if self.strategy == "agent" else _format_note(failed))
        return notes

    async def _episode(
        self,
        task: FixTask,
        ws: Workspace,
        messages: list[Message],
        attempt: Attempt,
        *,
        role: Role,
        temperature: float,
    ) -> bool:
        """Run tool-calling steps until submit or the step limit. Returns whether submitted."""
        coach = Coach(self.max_steps)
        for step in range(self.max_steps):
            if note := coach.before_step(step):
                messages.append(Message(role="user", content=note))
            completion = await self.llm.complete(
                role, compact(messages), temperature=temperature, tools=TOOL_SPECS
            )
            attempt.model_key = completion.model_key
            attempt.cost_usd += completion.cost_usd
            attempt.prompt_tokens += completion.prompt_tokens
            attempt.completion_tokens += completion.completion_tokens
            attempt.finish_reason = completion.finish_reason
            attempt.steps += 1
            messages.append(
                Message(role="assistant", content=completion.text, tool_calls=completion.tool_calls)
            )
            if not completion.tool_calls:
                # No tool use: accept whole-file blocks in plain text, then stop.
                for path, content in parse_edits(completion.text, task.editable).items():
                    await ws.call("write_file", _json(path, content))
                attempt.response_excerpt = completion.text[-2000:]
                return False
            submitted = False
            for call in completion.tool_calls:
                before = ws.writes
                observation, done = await ws.call(call.name, call.arguments)
                observation = coach.observe(
                    step,
                    (call.name, call.arguments),
                    writes_before=before,
                    writes_after=ws.writes,
                    out=observation,
                )
                messages.append(Message(role="tool", tool_call_id=call.id, content=observation))
                submitted = submitted or done
            if submitted:
                return True
            if note := coach.after_step(step, submitted=False):
                messages.append(Message(role="user", content=note))
        return False

    async def _agent_attempt(
        self,
        task: FixTask,
        oracle: Oracle,
        state: _State,
        *,
        rnd: int,
        branch: int,
        role: Role,
    ) -> tuple[Attempt, _State | None]:
        """One agent episode: explore, run, edit and submit, all inside the sandbox."""
        ws = Workspace(
            self.sandbox,
            state.checkpoint,
            protected=set(task.protected),
            python=self.python,
            workdir=task.workdir,
            interfaces=parse_interfaces(task.description),
        )
        messages: list[Message] = build_agent_messages(
            task.description,
            files=[]
            if task.repo_in_image
            else sorted(set(task.editable) | set(task.context) | set(task.protected)),
            workdir=task.workdir if task.repo_in_image else None,
            protected=sorted(task.protected),
            check_commands=[g.command for g in oracle.visible_gates()],
            results=state.results,
            notes=state.notes,
            previous_summary=state.summary,
        )
        temperature = 0.2 if branch == 0 else min(0.2 + 0.25 * branch, 1.0)
        attempt = Attempt(round=rnd, branch=branch, model_key="", checkpoint=None, edited_files=[])
        submitted = await self._episode(
            task, ws, messages, attempt, role=role, temperature=temperature
        )

        attempt.transcript = [m.to_openai() for m in messages]
        attempt.summary = ws.summary
        attempt.spec_misses = ws.spec_misses
        attempt.rejected_edits = sorted(set(ws.refused))
        attempt.edited_files = sorted(ws.touched)
        if not ws.touched:
            attempt.error = "agent made no file changes"
            if attempt.steps >= self.max_steps and not submitted:
                attempt.error += f" (step limit {self.max_steps} reached)"
            return attempt, None
        if not submitted and attempt.steps >= self.max_steps:
            attempt.error = f"step limit {self.max_steps} reached; verifying current changes"

        attempt.checkpoint = ws.checkpoint.id
        attempt.tampered_files = await detect_tampering(self.sandbox, ws.checkpoint, oracle)
        attempt.gates = await run_gates(self.sandbox, ws.checkpoint, oracle, include_holdout=True)
        files = dict(state.files)
        for path in ws.touched:
            try:
                raw = await self.sandbox.read(ws.checkpoint, workspace_path(path, task.workdir))
                files[path] = raw.decode(errors="replace")
            except Exception:  # deleted by a later command
                files[path] = ""
        notes = [_tamper_note(attempt.tampered_files)] if attempt.tampered_files else []
        new_state = _State(
            ws.checkpoint, files, attempt.gates, attempt.rejected_edits, notes, ws.summary
        )
        return attempt, new_state


# The diff of an in-image repository: tracked changes plus new files, except throwaway
# scripts the agent left at the repository root (test_x.py, debug.py, check_*.py, ...).
SCRATCH_AWARE_DIFF = (
    "git ls-files --others --exclude-standard | while IFS= read -r f; do "
    'case "$f" in */*) ;; test_*|*_test.py|debug*|check_*|manual*|scratch*|tmp*|repro*|'
    'original_*|final_*|comprehensive_*) continue ;; esac; git add -N -- "$f"; done; '
    "git -c core.quotepath=off diff --no-color"
)


def _json(path: str, content: str) -> str:
    return json.dumps({"path": path, "content": content})
