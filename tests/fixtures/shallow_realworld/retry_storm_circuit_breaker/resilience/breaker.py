"""Plausible fix: right boundary and counter reset, but no lock and no single trial."""

import time


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout=30.0, clock=None):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock or time.monotonic
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0

    def call(self, fn):
        if self.state == "open":
            if self.clock() - self.opened_at >= self.reset_timeout:
                self.state = "half_open"
            else:
                raise CircuitOpen("circuit open")
        try:
            result = fn()
        except Exception:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = self.clock()
                self.failures = 0
            raise
        self.state = "closed"
        self.failures = 0
        return result
