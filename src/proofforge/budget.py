"""Hard spend caps. Every model call is charged here before its result is used."""

from __future__ import annotations

from dataclasses import dataclass, field

from proofforge.models.registry import ModelSpec


class BudgetExceededError(RuntimeError):
    """Raised when a call would push spend past a cap."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


@dataclass
class BudgetGuard:
    """Tracks spend per task and per session and refuses to exceed either cap."""

    task_cap_usd: float
    session_cap_usd: float
    session: Usage = field(default_factory=Usage)
    task: Usage = field(default_factory=Usage)
    task_by_model: dict[str, Usage] = field(default_factory=dict)

    def start_task(self) -> None:
        self.task = Usage()
        self.task_by_model = {}

    def ensure_headroom(self) -> None:
        if self.task.cost_usd >= self.task_cap_usd:
            raise BudgetExceededError(
                f"task cap ${self.task_cap_usd:.2f} reached (spent ${self.task.cost_usd:.4f})"
            )
        if self.session.cost_usd >= self.session_cap_usd:
            raise BudgetExceededError(
                f"session cap ${self.session_cap_usd:.2f} reached "
                f"(spent ${self.session.cost_usd:.4f})"
            )

    def charge(self, spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
        cost = spec.cost_usd(prompt_tokens, completion_tokens)
        for usage in (self.task, self.session, self.task_by_model.setdefault(spec.key, Usage())):
            usage.prompt_tokens += prompt_tokens
            usage.completion_tokens += completion_tokens
            usage.cost_usd += cost
            usage.calls += 1
        return cost
