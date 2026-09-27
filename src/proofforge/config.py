"""Runtime settings, read from environment variables or a local .env file.

If NEBIUS_API_KEY is not set, the key saved by `contree auth` is reused, so a
machine that is already logged in to Sandboxes needs no extra setup.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from proofforge.models.registry import Mode


def contree_ini_path() -> Path:
    if home := os.environ.get("CONTREE_HOME"):
        return Path(home) / "auth.ini"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "contree" / "auth.ini"


def key_from_contree_profile(path: Path | None = None) -> str | None:
    path = path or contree_ini_path()
    if not path.is_file():
        return None
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    profile = os.environ.get("CONTREE_PROFILE") or parser.defaults().get("profile", "default")
    section = f"profile:{profile}"
    if not parser.has_section(section):
        return None
    return parser.get(section, "token", fallback=None) or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROOFFORGE_", env_file=".env", extra="ignore")

    nebius_api_key: SecretStr | None = Field(default=None, validation_alias="NEBIUS_API_KEY")
    mode: Mode = Mode.DEV
    task_budget_usd: float = Field(default=0.25, gt=0)
    session_budget_usd: float = Field(default=2.00, gt=0)
    max_fix_attempts: int = Field(default=3, ge=1, le=20)
    branch_width: int = Field(default=1, ge=1, le=8)
    strategy: Literal["auto", "rewrite", "agent"] = "auto"
    max_agent_steps: int = Field(default=30, ge=1, le=200)
    receipts_dir: str = "receipts"

    @model_validator(mode="after")
    def _fallback_key(self) -> Settings:
        if self.nebius_api_key is None and (key := key_from_contree_profile()):
            self.nebius_api_key = SecretStr(key)
        return self
