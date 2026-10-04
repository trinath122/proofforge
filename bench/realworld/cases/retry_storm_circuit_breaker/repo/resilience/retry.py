"""Retries for calls to the inventory service."""

import random
import time


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
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            sleep(base_delay * 2**i)
    raise RetryError("gave up") from last
