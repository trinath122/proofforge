"""Nemotron model registry: IDs, endpoints, prices and roles.

Values were confirmed in the Nebius Token Factory console on 2026-09-26.
Prices are USD per 1M tokens and are approximate; re-check before large runs.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

GLOBAL_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
US_CENTRAL1_BASE_URL = "https://api.tokenfactory.us-central1.nebius.com/v1/"


class Role(StrEnum):
    """Jobs inside the agent loop. Each maps to one model."""

    PLANNER = "planner"
    ROUTER = "router"
    CODER = "coder"
    FIXER = "fixer"
    BREAKER = "breaker"
    MONITOR = "monitor"


class ModelSpec(BaseModel):
    """One Token Factory model endpoint."""

    model_config = ConfigDict(frozen=True)

    key: str
    model_id: str
    base_url: str
    price_in_per_m: float
    price_out_per_m: float
    context_tokens: int
    tool_calling: bool = True

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.price_in_per_m + completion_tokens * self.price_out_per_m
        ) / 1_000_000


ULTRA = ModelSpec(
    key="ultra",
    model_id="nvidia/Nemotron-3-Ultra-550b-a55b",
    base_url=US_CENTRAL1_BASE_URL,
    price_in_per_m=1.00,
    price_out_per_m=3.00,
    context_tokens=1_024_000,
)
SUPER = ModelSpec(
    key="super",
    model_id="nvidia/nemotron-3-super-120b-a12b",
    base_url=US_CENTRAL1_BASE_URL,
    price_in_per_m=0.30,
    price_out_per_m=0.90,
    context_tokens=256_000,
)
LIGHTNING = ModelSpec(
    key="lightning",
    model_id="nvidia/Nemotron-3_5-Lightning",
    base_url=GLOBAL_BASE_URL,
    price_in_per_m=0.06,
    price_out_per_m=0.24,
    context_tokens=1_024_000,
)
NANO = ModelSpec(
    key="nano",
    model_id="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    base_url=GLOBAL_BASE_URL,
    price_in_per_m=0.06,
    price_out_per_m=0.24,
    context_tokens=262_000,
)

MODELS: dict[str, ModelSpec] = {m.key: m for m in (ULTRA, SUPER, LIGHTNING, NANO)}


class Mode(StrEnum):
    """Quality vs cost presets. 'dev' keeps credit burn minimal while building."""

    DEV = "dev"
    EFFICIENT = "efficient"
    MAX = "max"


ROUTING: dict[Mode, dict[Role, str]] = {
    Mode.DEV: {role: "lightning" for role in Role},
    Mode.EFFICIENT: {
        Role.PLANNER: "super",
        Role.ROUTER: "lightning",
        Role.CODER: "super",
        Role.FIXER: "lightning",
        Role.BREAKER: "lightning",
        Role.MONITOR: "lightning",
    },
    Mode.MAX: {
        Role.PLANNER: "ultra",
        Role.ROUTER: "super",
        Role.CODER: "super",
        Role.FIXER: "super",
        Role.BREAKER: "super",
        Role.MONITOR: "super",
    },
}


def model_for(role: Role, mode: Mode) -> ModelSpec:
    return MODELS[ROUTING[mode][role]]
