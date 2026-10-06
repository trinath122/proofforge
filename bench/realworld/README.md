# RealWorld

Hard, multi-file engineering problems from real incident classes. Each is verified the
same way production code should be: by executable checks the agent cannot see or edit.

| Case | Incident | What the hidden checks prove |
| --- | --- | --- |
| `rate_limiter_token_bucket` | API gateway admits bursts and leaks memory | No over-admission under 24 contending threads, LRU eviction, bounded memory, idle expiry, no tokens granted when the clock jumps backwards, O(1) throughput |
| `job_queue_exactly_once` | Billing pool double-charges, hides failures, hangs on shutdown | Exactly-once under concurrent duplicate submits, no self-overlap, retries never lost at shutdown, failures reported with the last error, parallel execution |
| `cache_stampede_stale_reads` | Pricing cache stampedes the database and serves stale prices | One load per key under 40 concurrent misses, loads for different keys in parallel, no deadlock when a loader reads other keys, errors shared with waiters and never cached, no stale write-back after invalidation |
| `payments_idempotency_keys` | Payment retries double-charge and leak receipts across accounts | Keys scoped per account, declines replayed without re-charging, transient errors free the key, conflicts caught while the first request is in flight, exact TTL expiry, unrelated keys never serialized |
| `retry_storm_circuit_breaker` | Retry storm takes down a recovering service | Server `retry_after` honoured, nothing sleeps past the deadline, backoff capped for 1,500 attempts without overflow, a failed half-open trial re-opens with a fresh timeout, exactly one trial call under 12 concurrent callers |
| `drone_flight_controller` | Inspection drone overshoots, flips under full throttle and skips waypoints | Simulated flights of a planar quadrotor: no derivative kick on target changes, no integrator windup on a 10 m climb, tilt and speed limits held on a 15 m dash, attitude kept when weak motors saturate, wind rejected, waypoints reached only when close and slow, missions abort on timeout |

A textbook solution is not enough. `tests/fixtures/shallow_realworld/` holds a plausible fix
for each case that passes every visible test; the hidden checks catch all of them (for
example, the textbook token bucket admits 55 requests against a limit of 50 under load, and the
"one big lock" cache fix deadlocks as soon as a loader reads another key).

## Layout

```
case.json    title, issue, difficulty, protected globs, gates ({python} = interpreter)
repo/        starting repository: source + visible tests (tests are frozen)
holdout/     hidden tests, injected only at verification
solution/    reference overlay proving the case is solvable; never shown to the agent
```

Validate without any model calls: `uv run proofforge bench --suite realworld --validate`.
