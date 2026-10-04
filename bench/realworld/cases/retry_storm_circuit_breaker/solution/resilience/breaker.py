"""Circuit breaker in front of the inventory service."""

import threading
import time


class CircuitOpen(Exception):
    """Raised instead of calling the service while the circuit is open."""


class CircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout=30.0, clock=None):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    @property
    def state(self):
        with self._lock:
            return self._state

    def _open(self):
        self._state = "open"
        self._opened_at = self.clock()
        self._failures = 0

    def call(self, fn):
        with self._lock:
            if self._state == "open":
                if self.clock() - self._opened_at < self.reset_timeout:
                    raise CircuitOpen("circuit open")
                self._state = "half_open"
            trial = self._state == "half_open"
            if trial:
                if self._trial_running:
                    raise CircuitOpen("half-open trial in progress")
                self._trial_running = True

        try:
            result = fn()
        except Exception:
            with self._lock:
                if trial:
                    self._trial_running = False
                    self._open()
                elif self._state == "closed":
                    self._failures += 1
                    if self._failures >= self.failure_threshold:
                        self._open()
            raise

        with self._lock:
            if trial:
                self._trial_running = False
                self._state = "closed"
                self._failures = 0
            elif self._state == "closed":
                self._failures = 0
        return result
