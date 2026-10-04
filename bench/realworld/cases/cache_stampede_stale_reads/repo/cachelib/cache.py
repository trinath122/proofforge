"""Read-through TTL cache in front of the pricing database."""

import time


class TTLCache:
    """Caches `loader(key)` results for `ttl` seconds."""

    def __init__(self, loader, ttl, max_entries=1024, clock=None):
        self.loader = loader
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock or time.monotonic
        self._data = {}

    def get(self, key):
        now = self.clock()
        hit = self._data.get(key)
        if hit is not None and now - hit[1] <= self.ttl:
            return hit[0]
        value = self.loader(key)
        self._data[key] = (value, now)
        if len(self._data) > self.max_entries:
            self._data.pop(next(iter(self._data)))
        return value

    def invalidate(self, key):
        self._data.pop(key, None)

    def __len__(self):
        return len(self._data)
