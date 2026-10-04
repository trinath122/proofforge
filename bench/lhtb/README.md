# Long-Horizon Terminal-Bench (LHTB) subset

[LHTB](https://github.com/zli12321/LHTB) (Apache-2.0) is a 46-task benchmark for agents
that must keep working for hundreds of steps. Each task ships a published Docker image and
a hidden verifier that writes a dense reward between 0 and 1.

ProofForge runs these through its Harbor adapter: the task image becomes the sandbox, the
verifier is a holdout gate the agent never sees, and on a retry the agent only learns that
the check failed (LHTB's own "binary rejection" rule). A task counts as verified only at
reward 1.0; the best reward reached is reported either way.

`ids.txt` lists the six tasks used here, chosen because they are software engineering
problems that run as ordinary commands (no GUI, no separate verifier image):

| Task | What the agent must do |
|---|---|
| great-expectations-audit | data-quality and reconciliation pipeline over four messy CSVs |
| spot-scheduler-traces | cost-minimizing spot/on-demand scheduling policy |
| grammar-fuzz-coverage-hunt | grammar-guided fuzzer that maximizes parser coverage |
| vector-db-iterative-build | approximate nearest-neighbor search service, recall and QPS |
| tabular-data-feature-covshift | sparse inverse model that survives covariate shift |
| commit0-multilib-tdd | reimplement tinydb, sortedcontainers and cachetools from docstrings (784 hidden tests) |

Task definitions are not vendored. Fetch them into `external/LHTB` (git-ignored):

```bash
git clone --depth 1 https://github.com/zli12321/LHTB external/LHTB
```

Then:

```bash
uv run proofforge bench --suite harbor --root external/LHTB/tasks --ids-file bench/lhtb/ids.txt --validate
uv run proofforge bench --suite harbor --root external/LHTB/tasks --ids-file bench/lhtb/ids.txt --case spot-scheduler-traces
```
