# Long-Horizon Terminal-Bench (LHTB) subset

[LHTB](https://github.com/zli12321/LHTB) (Apache-2.0) is a 46-task benchmark for agents
that must keep working for hundreds of steps. Each task ships a published Docker image and
a hidden verifier that writes a dense reward between 0 and 1.

ProofForge runs these through its Harbor adapter: the task image becomes the sandbox, the
verifier is a holdout gate the agent never sees, and on a retry the agent only learns that
the check failed (LHTB's own "binary rejection" rule). A task counts as verified only at
reward 1.0; the best reward reached is reported either way.

LHTB forbids internet access (a reimplement-the-library task is trivially "solved" by
downloading the library). Token Factory sandboxes are online, so for tasks with
`allow_internet = false` ProofForge runs every command, the agent's and the verifier's,
in a private network namespace with only loopback up. commit0's grader checks for egress
and zeroes the reward if it finds any.

Most LHTB rewards are dense and the reference solutions do not reach 1.0, so
`targets.json` sets the bar per task: the score the task's own reference solution reached
in our sandbox, rounded down a little for run-to-run noise. Reaching it counts as solved.

`ids.txt` lists the tasks used here, chosen because they run as ordinary commands (no GUI,
no separate verifier image): six software engineering tasks and, in `science_ids.txt`, four
robotics and scientific simulation tasks:

| Task | What the agent must do |
|---|---|
| great-expectations-audit | data-quality and reconciliation pipeline over four messy CSVs |
| spot-scheduler-traces | cost-minimizing spot/on-demand scheduling policy |
| grammar-fuzz-coverage-hunt | grammar-guided fuzzer that maximizes parser coverage |
| vector-db-iterative-build | approximate nearest-neighbor search service, recall and QPS |
| tabular-data-feature-covshift | sparse inverse model that survives covariate shift |
| commit0-multilib-tdd | reimplement tinydb, sortedcontainers and cachetools from docstrings (784 hidden tests) |
| robotics-slam-benchmark-repair | repair a robot SLAM audit: SE(2) pose composition, angle units, loop closures, robust weights |
| materials-phase-diagram-audit | repair an alloy phase-diagram pipeline: composition axes, convex hull per atom, ternary simplex |
| epidemic-inverse-control-audit | fit an age-structured SEIR-H-ICU model, forecast, and choose a budget-feasible intervention |
| matpower-opf-regression | run AC power flow, DC and AC optimal power flow on MATPOWER grids and audit the constraints |

Task definitions are not vendored. Fetch them into `external/LHTB` (git-ignored):

```bash
git clone --depth 1 https://github.com/zli12321/LHTB external/LHTB
```

Then:

```bash
uv run proofforge bench --suite harbor --root external/LHTB/tasks --ids-file bench/lhtb/ids.txt --validate
uv run proofforge bench --suite harbor --root external/LHTB/tasks --ids-file bench/lhtb/ids.txt --case spot-scheduler-traces
```

## Results (October 2026, Nemotron Super, `efficient` mode)

One run per task: at most 120 agent steps per round, 2 rounds, $1.50 per task. Reward is
the hidden verifier's score of the change the run ended with (not the best round).

| Task | Reward | Reference solution | Cost |
|---|---|---|---|
| spot-scheduler-traces | 0.79 | 0.91 | $0.79 |
| grammar-fuzz-coverage-hunt | 0.62 | 0.92 | $1.02 |
| tabular-data-feature-covshift | 0.33 | 0.40 | $1.46 |
| great-expectations-audit | 0.27 | 1.00 | $1.44 |
| vector-db-iterative-build | 0.00 | 0.83 | $1.50 (cap reached) |

None reached its target. Two observations matter more than the scores:

- **Self-reported progress is not progress.** On the fuzzing task the agent's final
  summary claimed "an aggregate score of 0.846" from the task's own scoring tool, whose
  coverage accumulates across runs. The hidden verifier, rerunning the fuzzer from
  scratch, measured 0.62. Its first round had actually scored higher (0.77). ProofForge
  reports the 0.62, because that is the change it ended with.
- **Honest when it can measure.** On the scheduler, whose scoring tool matches the
  verifier, the agent's summary said "~0.79" and the verifier said 0.79.
