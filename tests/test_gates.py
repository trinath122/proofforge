from __future__ import annotations

from proofforge.engine.task import FixTask
from proofforge.gates.base import detect_tampering, run_gates, workspace_path
from proofforge.sandbox.base import NOOP, Checkpoint
from proofforge.sandbox.local import LocalSandbox
from tests.conftest import py


async def seeded(sandbox: LocalSandbox, task: FixTask) -> Checkpoint:
    base = await sandbox.base(task.image)
    files = {workspace_path(p): c.encode() for p, c in task.workspace_seed().items()}
    res = await sandbox.run(base, NOOP, files=files)
    return res.checkpoint


async def test_holdout_never_enters_workspace(sandbox: LocalSandbox, task: FixTask) -> None:
    cp = await seeded(sandbox, task)
    listing = await sandbox.run(cp, py("import os; print(os.listdir())"), keep=False)
    assert "test_holdout.py" not in listing.stdout
    assert "test_stats.py" in listing.stdout


async def test_tampering_detected_and_gates_use_original_tests(
    sandbox: LocalSandbox, task: FixTask
) -> None:
    cp = await seeded(sandbox, task)
    hacked = await sandbox.run(cp, py("open('test_stats.py', 'w').write('print(1)')"))
    oracle = task.oracle()

    assert await detect_tampering(sandbox, hacked.checkpoint, oracle) == ["test_stats.py"]
    results = await run_gates(sandbox, hacked.checkpoint, oracle, include_holdout=False)
    assert [r.passed for r in results] == [False]  # the original test still runs and fails


async def test_deleted_protected_file_counts_as_tampering(
    sandbox: LocalSandbox, task: FixTask
) -> None:
    cp = await seeded(sandbox, task)
    gone = await sandbox.run(cp, py("import os; os.remove('test_stats.py')"))
    assert await detect_tampering(sandbox, gone.checkpoint, task.oracle()) == ["test_stats.py"]


async def test_gate_runs_do_not_create_checkpoints(sandbox: LocalSandbox, task: FixTask) -> None:
    cp = await seeded(sandbox, task)
    before = len(sandbox._dirs)
    await run_gates(sandbox, cp, task.oracle(), include_holdout=True)
    assert len(sandbox._dirs) == before


def test_oracle_digest_is_stable_and_sensitive(task: FixTask) -> None:
    a, b = task.oracle(), task.oracle()
    assert a.digest == b.digest
    changed = task.model_copy(update={"protected": {"test_stats.py": "assert True\n"}})
    assert changed.oracle().digest != a.digest
