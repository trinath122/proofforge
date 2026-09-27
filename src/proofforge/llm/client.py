"""Token Factory client: one OpenAI-compatible client per base URL, budget-guarded."""

from __future__ import annotations

from openai import AsyncOpenAI

from proofforge.budget import BudgetGuard
from proofforge.llm.base import Completion, Message
from proofforge.models.registry import (
    GLOBAL_BASE_URL,
    US_CENTRAL1_BASE_URL,
    Mode,
    Role,
    model_for,
)

# Room for reasoning plus a full file; reasoning models truncate silently without it.
MAX_OUTPUT_TOKENS = 16_384


class TokenFactoryLLM:
    def __init__(self, api_key: str, mode: Mode, budget: BudgetGuard) -> None:
        self._api_key = api_key
        self._mode = mode
        self._budget = budget
        self._clients: dict[str, AsyncOpenAI] = {}

    def _client(self, base_url: str) -> AsyncOpenAI:
        if base_url not in self._clients:
            self._clients[base_url] = AsyncOpenAI(
                base_url=base_url, api_key=self._api_key, max_retries=3, timeout=180
            )
        return self._clients[base_url]

    async def complete(
        self, role: Role, messages: list[Message], *, temperature: float = 0.2
    ) -> Completion:
        spec = model_for(role, self._mode)
        self._budget.ensure_headroom()
        response = await self._client(spec.base_url).chat.completions.create(
            model=spec.model_id,
            messages=[{"role": m.role, "content": m.content} for m in messages],  # type: ignore[misc]
            temperature=temperature,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        usage = response.usage
        prompt = usage.prompt_tokens if usage else 0
        completion = usage.completion_tokens if usage else 0
        cost = self._budget.charge(spec, prompt, completion)
        return Completion(
            text=response.choices[0].message.content or "",
            finish_reason=response.choices[0].finish_reason,
            model_key=spec.key,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=cost,
        )

    async def list_model_ids(self) -> dict[str, list[str]]:
        """Model IDs visible at each endpoint; used by `proofforge doctor`."""
        found: dict[str, list[str]] = {}
        for base_url in (GLOBAL_BASE_URL, US_CENTRAL1_BASE_URL):
            page = await self._client(base_url).models.list()
            found[base_url] = sorted(m.id for m in page.data)
        return found
