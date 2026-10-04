"""Token Factory client: one OpenAI-compatible client per base URL, budget-guarded."""

from __future__ import annotations

from typing import Any

from openai import APIStatusError, AsyncOpenAI

from proofforge.budget import BudgetGuard, ProviderBudgetError
from proofforge.llm.base import Completion, Message, ToolCall
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
        self,
        role: Role,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        spec = model_for(role, self._mode)
        self._budget.ensure_headroom()
        extra: dict[str, Any] = {"tools": tools, "tool_choice": "auto"} if tools else {}
        try:
            response = await self._client(spec.base_url).chat.completions.create(
                model=spec.model_id,
                messages=[m.to_openai() for m in messages],  # type: ignore[misc]
                temperature=temperature,
                max_tokens=MAX_OUTPUT_TOKENS,
                **extra,
            )
        except APIStatusError as exc:
            if exc.status_code == 402:
                raise ProviderBudgetError(
                    "Nebius Token Factory refused the call: the account's budget is exhausted "
                    "(HTTP 402). Add funds or raise the project's spending limit, then re-run."
                ) from exc
            raise
        usage = response.usage
        prompt = usage.prompt_tokens if usage else 0
        completion = usage.completion_tokens if usage else 0
        cost = self._budget.charge(spec, prompt, completion)
        choice = response.choices[0]
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in (choice.message.tool_calls or [])
            if c.type == "function"
        ]
        return Completion(
            text=choice.message.content or "",
            finish_reason=choice.finish_reason,
            tool_calls=calls,
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
