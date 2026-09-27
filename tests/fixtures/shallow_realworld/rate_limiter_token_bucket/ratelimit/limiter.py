"""Shallow fix: a textbook token bucket. Correct maths, but no lock, no memory bound,
and it rewinds its refill timestamp when the clock jumps backwards."""

import time


class RateLimiter:
    def __init__(self, rate, capacity, clock=None, max_keys=10_000, idle_ttl=300.0):
        if rate <= 0 or capacity < 1:
            raise ValueError("bad limiter config")
        self.rate, self.capacity = float(rate), capacity
        self.clock = clock or time.monotonic
        self._buckets = {}

    def _refill(self, key):
        now = self.clock()
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + max(0.0, now - last) * self.rate)
        return tokens, now

    def allow(self, key, cost=1):
        if not 1 <= cost <= self.capacity:
            raise ValueError("bad cost")
        tokens, now = self._refill(key)
        if tokens >= cost:
            self._buckets[key] = (tokens - cost, now)
            return True
        self._buckets[key] = (tokens, now)
        return False

    def retry_after(self, key, cost=1):
        tokens, _ = self._refill(key)
        return max(0.0, (cost - tokens) / self.rate)

    def __len__(self):
        return len(self._buckets)
