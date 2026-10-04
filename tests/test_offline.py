from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from proofforge.sandbox.local import LocalSandbox
from proofforge.sandbox.offline import UNSEALED_NOTE, OfflineSandbox, seal

needs_linux = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash"), reason="needs bash"
)


def _bash(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)


def _namespaces() -> bool:
    return bool(shutil.which("unshare")) and (
        _bash("unshare -n true").returncode == 0 or _bash("unshare -rn true").returncode == 0
    )


@needs_linux
def test_sealed_command_keeps_exit_code_and_quoting() -> None:
    result = _bash(seal('echo "it\'s $((2 + 3))"; exit 7'))
    assert result.returncode == 7
    assert result.stdout.strip() == "it's 5"


@needs_linux
@pytest.mark.skipif(not _namespaces(), reason="network namespaces unavailable here")
def test_sealed_command_has_loopback_but_no_egress() -> None:
    probe = (
        "import socket\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "socket.create_connection(s.getsockname()).close()\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('egress')\n"
        "except OSError:\n"
        "    print('sealed')\n"
    )
    result = _bash(seal(f'{sys.executable} -c "{probe}"'))
    assert result.stdout.strip() == "sealed", result.stderr
    assert UNSEALED_NOTE not in result.stderr


@needs_linux
async def test_offline_sandbox_wraps_commands() -> None:
    sandbox = OfflineSandbox(LocalSandbox())
    base = await sandbox.base("ignored")
    res = await sandbox.run(base, "echo ok > out.txt && cat out.txt", keep=True)
    assert res.exit_code == 0
    assert res.stdout.strip() == "ok"
    assert (await sandbox.read(res.checkpoint, "/workspace/out.txt")).strip() == b"ok"
