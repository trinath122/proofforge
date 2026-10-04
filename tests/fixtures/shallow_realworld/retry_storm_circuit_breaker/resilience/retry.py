"""Plausible fix: correct basic backoff, but no retry_after, no deadline, no overflow guard."""

import random
import time


class TransientError(Exception):
    def __init__(self, message="", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class RetryError(Exception):
    pass


def retry(
    fn,
    *,
    attempts=5,
    base_delay=0.1,
    max_delay=5.0,
    retry_on=(TransientError,),
    deadline=None,
    sleep=time.sleep,
    rng=random.random,
    clock=time.monotonic,
):
    for n in range(1, attempts + 1):
        try:
            return fn()
        except retry_on:
            if n == attempts:
                raise
            sleep(rng() * min(max_delay, base_delay * 2 ** (n - 1)))
    raise ValueError("attempts must be >= 1")
