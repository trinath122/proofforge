"""Per-client request limiting for the API gateway."""

import time


class RateLimiter:
    """Allows up to `capacity` requests per one-second window for each key."""

    def __init__(self, rate, capacity, clock=None, max_keys=10_000, idle_ttl=300.0):
        self.rate = rate
        self.capacity = capacity
        self.clock = clock or time.monotonic
        self.max_keys = max_keys
        self.idle_ttl = idle_ttl
        self._windows = {}

    def allow(self, key, cost=1):
        window = int(self.clock())
        start, used = self._windows.get(key, (window, 0))
        if start != window:
            start, used = window, 0
        if used >= self.capacity:
            return False
        self._windows[key] = (start, used + cost)
        return True

    def retry_after(self, key, cost=1):
        now = self.clock()
        start, used = self._windows.get(key, (int(now), 0))
        if used + cost <= self.capacity:
            return 0.0
        return float(start + 1 - now)

    def __len__(self):
        return len(self._windows)
