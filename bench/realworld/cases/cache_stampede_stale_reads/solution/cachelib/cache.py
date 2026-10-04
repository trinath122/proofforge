"""Read-through TTL cache in front of the pricing database."""

import threading
import time
from collections import OrderedDict


class _Flight:
    """One in-progress load that concurrent callers wait on."""

    __slots__ = ("done", "value", "error", "invalidated")

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None
        self.invalidated = False


class TTLCache:
    """Caches `loader(key)` results for `ttl` seconds, with single-flight loads."""

    def __init__(self, loader, ttl, max_entries=1024, clock=None):
        if not ttl > 0:
            raise ValueError("ttl must be > 0")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.loader = loader
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock or time.monotonic
        self._data = OrderedDict()  # key -> (value, expires_at)
        self._flights = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                if self.clock() < hit[1]:
                    self._data.move_to_end(key)
                    return hit[0]
                del self._data[key]
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()

        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value

        # The lock is not held while loading: other keys load in parallel and the
        # loader may call get() for other keys.
        try:
            value = self.loader(key)
        except BaseException as exc:
            flight.error = exc
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.done.set()
            raise

        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
            if not flight.invalidated:
                self._data[key] = (value, self.clock() + self.ttl)
                self._data.move_to_end(key)
                while len(self._data) > self.max_entries:
                    self._data.popitem(last=False)
        flight.value = value
        flight.done.set()
        return value

    def invalidate(self, key):
        with self._lock:
            self._data.pop(key, None)
            flight = self._flights.pop(key, None)
            if flight is not None:
                flight.invalidated = True

    def __len__(self):
        with self._lock:
            return len(self._data)
