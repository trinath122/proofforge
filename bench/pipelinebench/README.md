# PipelineBench

Broken data pipelines with executable, data-quality-based verification. Each case is a real class of production bug: join fan-out, upstream schema drift, timestamp and timezone handling.

Every case ships with:

| File | Purpose |
| --- | --- |
| `case.json` | Title, issue text given to the agent, tags |
| `pipeline.sql` | The broken pipeline (SQLite). The only file an agent may edit |
| `solution.sql` | Reference fix. Never shown to the agent; proves the case is solvable |
| `data/*.csv` + `expectations.json` | Visible source data and checks |
| `holdout/data/*.csv` + `holdout/expectations.json` | Hidden data with different edge cases, checked only at verification |

Checks are run by `dq_check.py`: exact columns, not-null, uniqueness, accepted values, golden rows, and reconciliation against the raw source (for example, total revenue must equal total billed).

## Validity guarantees

`proofforge bench --validate` proves, with no model calls, that every case:

1. **fails as shipped** on the visible checks, and
2. **passes every check**, visible and hidden, with its reference solution.

The test suite also keeps a *shallow fix* for each case (`tests/fixtures/shallow/`): a plausible patch that passes the visible checks. Every one of them is caught by the hidden holdout. This is how the benchmark measures real repair rather than fitting to the sample.

## Cases

| Case | Bug class | What a shallow fix gets wrong |
| --- | --- | --- |
| `daily_revenue_join_fanout` | Join fan-out on history table, dropped unmatched rows | Picks the last row in the file instead of the latest by `updated_at` |
| `payments_schema_drift` | Renamed column, cents vs dollars, integer division, case drift | Hard-codes the currency spellings seen in the sample |
| `latest_status_mixed_timestamps` | Lexicographic comparison of mixed ISO-8601 timestamps | Normalizes the `T` separator but ignores `Z` and UTC offsets |

## Adding a case

Create a directory with the files above, then run `uv run proofforge bench --validate --case <name>`. A case is accepted only when it is valid and a shallow fix exists that the holdout catches.
