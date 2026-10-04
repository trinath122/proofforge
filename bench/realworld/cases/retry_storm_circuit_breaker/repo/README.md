# resilience

Retry and circuit-breaker helpers used by every service that calls the inventory API.

```python
from resilience import CircuitBreaker, retry

breaker = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)
stock = retry(lambda: breaker.call(lambda: inventory.get(sku)), attempts=4, deadline=2.0)
```

| Member | Contract |
| --- | --- |
| `retry(fn, *, attempts, base_delay, max_delay, retry_on, deadline, sleep, rng, clock)` | Capped exponential backoff with full jitter; honours server `retry_after`; never sleeps past `deadline` |
| `CircuitBreaker(failure_threshold, reset_timeout, clock)` | closed -> open after consecutive failures -> half-open single trial -> closed or open |
| `TransientError(message, retry_after=None)` | Retryable failure |
| `CircuitOpen` | Raised without calling the service while open |
