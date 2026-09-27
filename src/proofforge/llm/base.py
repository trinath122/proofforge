"""The model interface the engine depends on. Real and fake clients both implement it."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from proofforge.models.registry import Role


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str = Field(description="JSON-encoded arguments, exactly as the model sent them")


class Message(BaseModel):
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None

    def to_openai(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        return msg


class Completion(BaseModel):
    text: str
    model_key: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class LLM(Protocol):
    async def complete(
        self,
        role: Role,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion: ...
