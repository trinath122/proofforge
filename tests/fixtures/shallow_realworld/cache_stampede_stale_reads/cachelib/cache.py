"""Plausible fix: one global lock around everything. Passes the visible tests."""

import threading
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, loader, ttl, max_entries=1024, clock=None):
        if not ttl > 0 or max_entries < 1:
            raise ValueError("bad arguments")
        self.loader = loader
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock or time.monotonic
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and self.clock() < hit[1]:
                self._data.move_to_end(key)
                return hit[0]
            value = self.loader(key)
            self._data[key] = (value, self.clock() + self.ttl)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
            return value

    def invalidate(self, key):
        with self._lock:
            self._data.pop(key, None)

    def __len__(self):
        return len(self._data)
