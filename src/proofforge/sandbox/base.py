"""Sandbox interface.

Every run produces an immutable checkpoint. Branching means running from the same
checkpoint more than once; rolling back means continuing from an older checkpoint.
This maps directly onto Token Factory Sandboxes, where each run yields a new image.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

WORKDIR = "/workspace"
NOOP = "true"  # a run that only writes files


class Checkpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    parent: str | None = None


class ExecResult(BaseModel):
    checkpoint: Checkpoint
    exit_code: int
    stdout: str
    stderr: str
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Sandbox(Protocol):
    async def base(self, image_ref: str) -> Checkpoint:
        """Resolve an image reference to a starting checkpoint."""
        ...

    async def run(
        self,
        at: Checkpoint,
        command: str,
        *,
        files: dict[str, bytes] | None = None,
        keep: bool = True,
        timeout_s: int = 300,
    ) -> ExecResult:
        """Run a shell command in WORKDIR at a checkpoint.

        `files` maps absolute paths to contents written before the command runs.
        With keep=True the resulting filesystem becomes a new checkpoint;
        with keep=False it is discarded and the returned checkpoint is `at`.
        """
        ...

    async def read(self, at: Checkpoint, path: str) -> bytes:
        """Read a file from a checkpoint without running anything."""
        ...
