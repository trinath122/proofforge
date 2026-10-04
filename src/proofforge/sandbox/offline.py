"""Run every command of a task with no network.

Token Factory sandboxes can reach the internet, which some benchmarks forbid: a task that
asks for a library to be reimplemented is trivially "solved" by downloading the original.
`OfflineSandbox` runs each command in a fresh network namespace (`unshare -n`) with only
the loopback interface up, so local servers still work but nothing leaves the sandbox.
If the image cannot create a namespace, the command still runs and says so on stderr;
benchmarks that check for egress then refuse to credit the run.
"""

from __future__ import annotations

import shlex

from proofforge.sandbox.base import WORKDIR, Checkpoint, ExecResult, Sandbox

UNSEALED_NOTE = "proofforge: could not isolate the network for this command"

# A new namespace starts with loopback down. Bring it up with ioctl (SIOCGIFFLAGS /
# SIOCSIFFLAGS) so it works in slim images without iproute2.
_LO_UP = (
    "import socket, fcntl, struct\n"
    "s = socket.socket()\n"
    "req = struct.pack('16sH22x', b'lo', 0)\n"
    "flags = struct.unpack('16sH22x', fcntl.ioctl(s, 0x8913, req))[1]\n"
    "fcntl.ioctl(s, 0x8914, struct.pack('16sH22x', b'lo', flags | 1))\n"
)
_INNER = (
    f"{{ python3 -c {shlex.quote(_LO_UP)} || python -c {shlex.quote(_LO_UP)} "
    "|| ip link set lo up; } >/dev/null 2>&1; "
    'eval "$PF_SEALED_CMD"'
)


def seal(command: str) -> str:
    """Wrap a shell command so it runs without network access."""
    return (
        f"PF_SEALED_CMD={shlex.quote(command)}; export PF_SEALED_CMD; "
        "if unshare -n true 2>/dev/null; then "
        f"unshare -n -- bash -c {shlex.quote(_INNER)}; "
        # Not root: a user namespace can still own a private network namespace.
        "elif unshare -rn true 2>/dev/null; then "
        f"unshare -rn -- bash -c {shlex.quote(_INNER)}; "
        f'else echo {shlex.quote(UNSEALED_NOTE)} >&2; eval "$PF_SEALED_CMD"; fi'
    )


class OfflineSandbox:
    """A Sandbox whose commands cannot reach the network."""

    def __init__(self, inner: Sandbox) -> None:
        self.inner = inner

    async def base(self, image_ref: str) -> Checkpoint:
        return await self.inner.base(image_ref)

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
        return await self.inner.run(
            at, seal(command), files=files, keep=keep, timeout_s=timeout_s, cwd=cwd
        )

    async def read(self, at: Checkpoint, path: str) -> bytes:
        return await self.inner.read(at, path)
