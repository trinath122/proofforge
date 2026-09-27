"""Per-client token-bucket rate limiting for the API gateway."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable


class RateLimiter:
    """Thread-safe token buckets with LRU and idle eviction."""

    def __init__(
        self,
        rate: float,
        capacity: int,
        clock: Callable[[], float] | None = None,
        max_keys: int = 10_000,
        idle_ttl: float = 300.0,
    ) -> None:
        if not rate > 0:
            raise ValueError("rate must be > 0")
        if int(capacity) != capacity or capacity < 1:
            raise ValueError("capacity must be an integer >= 1")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self.rate = float(rate)
        self.capacity = int(capacity)
        self.clock = clock or time.monotonic
        self.max_keys = max_keys
        self.idle_ttl = idle_ttl
        # key -> [tokens, last_refill, last_used]; ordered from least to most recently used
        self._buckets: OrderedDict[Hashable, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _check_cost(self, cost: int) -> None:
        if isinstance(cost, bool) or int(cost) != cost or not 1 <= cost <= self.capacity:
            raise ValueError(f"cost must be an integer in 1..{self.capacity}")

    def _bucket(self, key: Hashable, now: float) -> list[float]:
        bucket = self._buckets.get(key)
        if bucket is None:
            return [float(self.capacity), now, now]
        tokens, last, _ = bucket
        if now > last:  # a clock that moved backwards grants nothing
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            last = now
        return [tokens, last, bucket[2]]

    def _evict(self, now: float) -> None:
        while self._buckets:
            _, oldest = next(iter(self._buckets.items()))
            if now - oldest[2] >= self.idle_ttl:
                self._buckets.popitem(last=False)
            else:
                break

    def allow(self, key: Hashable, cost: int = 1) -> bool:
        self._check_cost(cost)
        with self._lock:
            now = self.clock()
            self._evict(now)
            bucket = self._bucket(key, now)
            bucket[2] = max(bucket[2], now)
            admitted = bucket[0] >= cost
            if admitted:
                bucket[0] -= cost
            self._buckets[key] = bucket
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return admitted

    def retry_after(self, key: Hashable, cost: int = 1) -> float:
        self._check_cost(cost)
        with self._lock:
            tokens = self._bucket(key, self.clock())[0]
            missing = cost - tokens
            return 0.0 if missing <= 0 else missing / self.rate

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
