"""Token Factory Sandboxes (ConTree) implementation.

Credentials resolve the same way as the contree CLI: NEBIUS_API_KEY and
NEBIUS_PROJECT_ID environment variables, or the profile saved by `contree auth`.
"""

from __future__ import annotations

import time
from typing import Any

from contree_sdk import Contree

from proofforge.sandbox.base import WORKDIR, Checkpoint, ExecResult


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


class ContreeSandbox:
    def __init__(self, client: Contree | None = None) -> None:
        self._client = client or Contree()
        self._images: dict[str, Any] = {}

    async def whoami(self) -> Any:
        return await self._client.get_token_info()

    async def base(self, image_ref: str) -> Checkpoint:
        # Reuses an image already in the project, or imports it from its registry
        # (Docker Hub, GHCR, ...) on first use.
        image = await self._client.images.oci(image_ref, timeout=1800)
        key = str(image.uuid or f"tag:{image.tag}")
        self._images[key] = image
        return Checkpoint(id=key)

    async def run(
        self,
        at: Checkpoint,
        command: str,
        *,
        files: dict[str, bytes] | None = None,
        keep: bool = True,
        timeout_s: int = 300,
        cwd: str = WORKDIR,
    ) -> ExecResult:
        image = self._images[at.id]
        started = time.monotonic()
        child = await image.run(  # noqa: S604 - executes inside an isolated sandbox VM
            shell=f"mkdir -p {cwd} && cd {cwd} && {command}",
            files=dict(files or {}),
            disposable=not keep,
            timeout=timeout_s,
        )
        checkpoint = at
        if keep and child.uuid is not None:
            checkpoint = Checkpoint(id=str(child.uuid), parent=at.id)
            self._images[checkpoint.id] = child
        return ExecResult(
            checkpoint=checkpoint,
            exit_code=child.exit_code,
            stdout=_text(child.stdout),
            stderr=_text(child.stderr),
            elapsed_s=time.monotonic() - started,
        )

    async def read(self, at: Checkpoint, path: str) -> bytes:
        data: bytes = await self._images[at.id].read(path)
        return data
