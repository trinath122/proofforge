"""Token Factory Sandboxes (ConTree) implementation.

Credentials resolve the same way as the contree CLI: NEBIUS_API_KEY and
NEBIUS_PROJECT_ID environment variables, or the profile saved by `contree auth`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from contree_sdk import Contree
from contree_sdk.sdk.exceptions.api import (
    ApiStatusCodeError,
    ApiTimeoutError,
    ContreeTransportError,
    TooManyRequestsError,
)

from proofforge.sandbox.base import WORKDIR, Checkpoint, ExecResult

# Sandbox commands start without a login environment. Test runners such as ansible-test
# refuse to run without HOME, so give every command the usual defaults.
ENV_PREAMBLE = (
    'export HOME="${HOME:-/root}" USER="${USER:-root}" LANG="${LANG:-C.UTF-8}" '
    'PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"; '
)


RETRY_DELAYS_S = (2.0, 8.0, 30.0)


def _transient(exc: Exception) -> bool:
    if isinstance(exc, ApiTimeoutError | ContreeTransportError | TooManyRequestsError):
        return True
    status = getattr(exc, "status", None)
    return isinstance(exc, ApiStatusCodeError) and isinstance(status, int) and status >= 500


async def _retrying[T](call: Callable[[], Awaitable[T]]) -> T:
    """Retry network blips. Safe because every run starts from an immutable checkpoint:
    repeating one creates another child image and never alters the parent."""
    for delay in RETRY_DELAYS_S:
        try:
            return await call()
        except Exception as exc:
            if not _transient(exc):
                raise
            await asyncio.sleep(delay)
    return await call()


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
        image = await _retrying(lambda: self._client.images.oci(image_ref, timeout=1800))
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
        child = await _retrying(
            lambda: image.run(  # noqa: S604 - executes inside an isolated sandbox VM
                shell=f"{ENV_PREAMBLE}mkdir -p {cwd} && cd {cwd} && {command}",
                files=dict(files or {}),
                disposable=not keep,
                timeout=timeout_s,
            )
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
        data: bytes = await _retrying(lambda: self._images[at.id].read(path))
        return data
