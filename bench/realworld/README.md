# RealWorld

Hard, multi-file engineering problems from real incident classes. Each is verified the
same way production code should be: by executable checks the agent cannot see or edit.

| Case | Incident | What the hidden checks prove |
| --- | --- | --- |
| `rate_limiter_token_bucket` | API gateway admits bursts and leaks memory | No over-admission under 24 contending threads, LRU eviction, bounded memory, idle expiry, no tokens granted when the clock jumps backwards, O(1) throughput |
| `job_queue_exactly_once` | Billing pool double-charges, hides failures, hangs on shutdown | Exactly-once under concurrent duplicate submits, no self-overlap, retries never lost at shutdown, failures reported with the last error, parallel execution |

A textbook solution is not enough. `tests/fixtures/shallow_realworld/` holds a plausible fix
for each case that passes every visible test; the hidden checks catch all of them (for
example, the textbook token bucket admits 55 requests against a limit of 50 under load).

## Layout

```
case.json    title, issue, difficulty, protected globs, gates ({python} = interpreter)
repo/        starting repository: source + visible tests (tests are frozen)
holdout/     hidden tests, injected only at verification
solution/    reference overlay proving the case is solvable; never shown to the agent
```

Validate without any model calls: `uv run proofforge bench --suite realworld --validate`.
