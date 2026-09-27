"""The model interface the engine depends on. Real and fake clients both implement it."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from proofforge.models.registry import Role


class Message(BaseModel):
    role: str
    content: str


class Completion(BaseModel):
    text: str
    model_key: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class LLM(Protocol):
    async def complete(
        self, role: Role, messages: list[Message], *, temperature: float = 0.2
    ) -> Completion: ...
