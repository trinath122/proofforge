# ratelimit

Per-client rate limiting for the API gateway.

```python
from ratelimit import RateLimiter

limiter = RateLimiter(rate=5.0, capacity=10)  # 5 req/s sustained, bursts of 10
if limiter.allow(client_id):
    handle(request)
else:
    reject(retry_after=limiter.retry_after(client_id))
```

| Member | Contract |
| --- | --- |
| `RateLimiter(rate, capacity, clock=None, max_keys=10_000, idle_ttl=300.0)` | `rate > 0`, `capacity >= 1` or `ValueError`; `clock()` returns seconds |
| `allow(key, cost=1) -> bool` | Atomic; consumes `cost` tokens or nothing; `1 <= cost <= capacity` or `ValueError` |
| `retry_after(key, cost=1) -> float` | Seconds until `cost` tokens are available; never consumes |
| `len(limiter)` | Buckets currently held; never more than `max_keys` |
