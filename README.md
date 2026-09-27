# ProofForge

**A development agent that never says "done" without proof.**

Coding agents routinely claim success they cannot back up, and they are known to game their own tests. ProofForge treats every change as unproven until executable gates pass inside an isolated [Nebius Token Factory Sandbox](https://docs.tokenfactory.nebius.com/sandboxes/overview), including hidden holdout checks the agent never sees. Every run ships a replayable proof receipt.

Built for the [Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) (Coding and Agentic Engineering track).

## How it works

```
Reproduce  ->  Change  ->  Verify with proof  ->  Repeat until every gate passes
```

1. **Reproduce.** The task's visible gates must fail before any change. If they already pass, there is nothing to prove.
2. **Change.** A Nemotron model proposes edits. Only files marked editable can change; edits to anything else are rejected and recorded.
3. **Verify.** Each candidate becomes a sandbox checkpoint. Gates run on disposable copies, always against the original frozen tests plus hidden holdout tests.
4. **Branch.** With `branch_width > 1`, several candidates are forked from the same checkpoint and verified in parallel. The first one that passes everything wins.

### Integrity by construction

| Control | What it prevents |
| --- | --- |
| Frozen oracle | Tests are hashed up front; gates always run the original copies, so editing a test can never make it pass |
| Hidden holdouts | Holdout tests never enter the agent's workspace; it only learns *how many* failed, never their content |
| Edit allowlist | Edits outside the declared files are rejected and logged in the receipt |
| Tamper detection | Any change to a protected file is flagged and disqualifies the candidate |
| Budget guard | Hard per-task and per-session spend caps on every model call |

## Models (NVIDIA Nemotron on Token Factory)

| Key | Model | Default role |
| --- | --- | --- |
| `ultra` | `nvidia/Nemotron-3-Ultra-550b-a55b` | Planner in `max` mode |
| `super` | `nvidia/nemotron-3-super-120b-a12b` | Coder in `efficient` and `max` modes |
| `lightning` | `nvidia/Nemotron-3_5-Lightning` | Everything in `dev` mode; fixer loops |
| `nano` | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Fallback |

Routing lives in [`src/proofforge/models/registry.py`](src/proofforge/models/registry.py). `dev` mode uses Lightning only, which keeps development runs at fractions of a cent.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd proofforge
uv sync
```

Credentials: if you have already run `contree auth`, ProofForge reuses that saved key and project. Otherwise copy `.env.example` to `.env` and fill it in. Never commit `.env`.

```bash
uv run proofforge doctor     # checks model and sandbox access; costs nothing
uv run proofforge smoke      # fixes a real bug end to end on Nebius (~$0.01)
```

`smoke` writes a proof receipt to `receipts/` as JSON and Markdown.

### Pipeline Doctor and PipelineBench

Pipeline Doctor repairs broken data pipelines and proves each repair with data-quality gates: schema, nulls, uniqueness, golden rows, and reconciliation against the raw source. [PipelineBench](bench/pipelinebench/README.md) is the open set of broken pipelines it is measured on.

```bash
uv run proofforge bench --validate          # prove every case is broken and solvable; free
uv run proofforge bench                     # run the agent on all cases; prints solve rate and cost
uv run proofforge pipeline bench/pipelinebench/cases/payments_schema_drift
```

Run your own task with `uv run proofforge fix task.json`, where `task.json` follows the `FixTask` schema in [`engine/task.py`](src/proofforge/engine/task.py).

## Development

```bash
uv run pytest                      # offline tests (local sandbox + scripted model)
uv run pytest -m live --no-cov     # live tests against Nebius (needs credentials)
uv run ruff check . && uv run ruff format --check .
uv run mypy                        # strict
```

## Project layout

```
src/proofforge/
  models/     Nemotron registry, endpoints, prices, routing by mode
  llm/        Token Factory client with budget enforcement
  sandbox/    Sandbox interface; ContreeSandbox (real) and LocalSandbox (tests only)
  gates/      Gate specs, frozen oracle, tamper detection, parallel gate runner
  engine/     Task schema, prompts, the verification loop with branch search
  playbooks/  Pipeline Doctor: case loader, validation, sandbox-side runner and DQ checker
  receipts/   Receipt schema and JSON/Markdown writer
  cli.py      doctor, smoke, fix, pipeline, bench
bench/pipelinebench/  Broken-pipeline cases with hidden holdout data
```

## Roadmap

More PipelineBench cases (dbt, PySpark), Build, Upgrade, Proof-Carrying Tests and Accelerate playbooks; Breaker agent; SWE-rebench, Spider 2.0-DBT and ImpossibleBench evaluation; fine-tuning Nemotron-3.5-Lightning on verified trajectories; web UI.

## License

Apache 2.0. See [LICENSE](LICENSE).
