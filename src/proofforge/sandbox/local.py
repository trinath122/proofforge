"""Local directory-snapshot sandbox for tests and offline development.

NOT isolated: never point it at model-generated code you do not trust.
Real runs always use ContreeSandbox.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from proofforge.sandbox.base import NOOP, WORKDIR, Checkpoint, ExecResult


class LocalSandbox:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path(tempfile.mkdtemp(prefix="proofforge-local-"))
        self._dirs: dict[str, Path] = {}

    def _host_path(self, at: Checkpoint, path: str) -> Path:
        rel = path.removeprefix(WORKDIR) if path.startswith(WORKDIR) else path
        rel = rel.lstrip("/")
        return self._dirs[at.id] / rel

    async def base(self, image_ref: str) -> Checkpoint:
        cp = Checkpoint(id=f"base-{uuid.uuid4().hex[:8]}")
        self._dirs[cp.id] = self._root / cp.id
        self._dirs[cp.id].mkdir(parents=True)
        return cp

    async def run(
        self,
        at: Checkpoint,
        command: str,
        *,
        files: dict[str, bytes] | None = None,
        keep: bool = True,
        timeout_s: int = 300,
    ) -> ExecResult:
        new = Checkpoint(id=f"cp-{uuid.uuid4().hex[:8]}", parent=at.id)
        workdir = self._root / new.id
        shutil.copytree(self._dirs[at.id], workdir)
        self._dirs[new.id] = workdir
        for path, content in (files or {}).items():
            target = self._host_path(new, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        started = time.monotonic()
        code, out, err = (
            (0, b"", b"")
            if command.strip() == NOOP
            else (await self._exec(command, workdir, timeout_s))
        )
        result = ExecResult(
            checkpoint=new if keep else at,
            exit_code=code,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            elapsed_s=time.monotonic() - started,
        )
        if not keep:
            shutil.rmtree(workdir)
            del self._dirs[new.id]
        return result

    @staticmethod
    async def _exec(command: str, cwd: Path, timeout_s: int) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return 124, b"", b"timeout"
        return (proc.returncode if proc.returncode is not None else 1), out, err

    async def read(self, at: Checkpoint, path: str) -> bytes:
        return self._host_path(at, path).read_bytes()
