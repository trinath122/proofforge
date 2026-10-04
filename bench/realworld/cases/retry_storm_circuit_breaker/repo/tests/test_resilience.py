import unittest

from resilience import CircuitBreaker, CircuitOpen, TransientError, retry


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def failing(times, exc=None):
    """fn that raises `times` times, then returns 'ok'. Records its call count."""
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= times:
            raise exc or TransientError("503")
        return "ok"

    fn.state = state
    return fn


class TestRetry(unittest.TestCase):
    def test_succeeds_after_transient_failures(self):
        sleeps = []
        fn = failing(2)
        self.assertEqual(retry(fn, sleep=sleeps.append, rng=lambda: 1.0), "ok")
        self.assertEqual(fn.state["calls"], 3)
        self.assertEqual(sleeps, [0.1, 0.2])

    def test_non_retryable_error_propagates_immediately(self):
        sleeps = []
        fn = failing(5, ValueError("bad request"))
        with self.assertRaises(ValueError):
            retry(fn, sleep=sleeps.append)
        self.assertEqual(fn.state["calls"], 1)
        self.assertEqual(sleeps, [])

    def test_gives_up_with_the_real_error_and_no_final_sleep(self):
        sleeps = []
        fn = failing(10)
        with self.assertRaises(TransientError):
            retry(fn, attempts=3, sleep=sleeps.append, rng=lambda: 1.0)
        self.assertEqual(fn.state["calls"], 3)
        self.assertEqual(len(sleeps), 2, "slept after the last attempt")

    def test_full_jitter(self):
        sleeps = []
        with self.assertRaises(TransientError):
            retry(failing(10), attempts=4, sleep=sleeps.append, rng=lambda: 0.5)
        self.assertEqual(sleeps, [0.05, 0.1, 0.2])


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold_and_rejects_without_calling(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=10, clock=clock)
        fn = failing(100)
        for _ in range(3):
            with self.assertRaises(TransientError):
                cb.call(fn)
        self.assertEqual(cb.state, "open")
        with self.assertRaises(CircuitOpen):
            cb.call(fn)
        self.assertEqual(fn.state["calls"], 3)

    def test_trial_allowed_at_exactly_reset_timeout_and_success_closes(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=10, clock=clock)
        with self.assertRaises(TransientError):
            cb.call(failing(1))
        clock.t += 10
        self.assertEqual(cb.call(lambda: "up"), "up")
        self.assertEqual(cb.state, "closed")


if __name__ == "__main__":
    unittest.main()
