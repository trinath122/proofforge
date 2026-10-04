"""Retries for calls to the inventory service."""

import random
import time

_MAX_EXPONENT = 62  # beyond this the backoff is long capped; avoids float overflow


class TransientError(Exception):
    """A failure worth retrying. May carry `retry_after` (seconds) from the server."""

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
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    started = clock()
    for n in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if n == attempts:
                raise
            hint = getattr(exc, "retry_after", None)
            if hint is not None:
                if hint > max_delay:
                    raise
                delay = hint
            else:
                backoff = base_delay * 2 ** min(n - 1, _MAX_EXPONENT)
                delay = rng() * min(max_delay, backoff)
            if deadline is not None and (clock() - started) + delay > deadline:
                raise
            sleep(delay)
    raise AssertionError("unreachable")
