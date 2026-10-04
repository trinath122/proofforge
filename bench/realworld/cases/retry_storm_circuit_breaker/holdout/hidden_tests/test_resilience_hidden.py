import threading
import time
import unittest

from resilience import CircuitBreaker, CircuitOpen, TransientError, retry


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def always(exc_factory, clock=None, cost=0.0):
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if clock is not None:
            clock.t += cost
        raise exc_factory()

    fn.state = state
    return fn


class TestRetryContract(unittest.TestCase):
    def test_attempts_must_be_positive(self):
        with self.assertRaises(ValueError):
            retry(lambda: "x", attempts=0)

    def test_honours_server_retry_after(self):
        sleeps = []
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise TransientError("429", retry_after=1.5)
            return "ok"

        self.assertEqual(retry(fn, sleep=sleeps.append, rng=lambda: 0.0), "ok")
        self.assertEqual(sleeps, [1.5])

    def test_retry_after_beyond_max_delay_gives_up(self):
        sleeps = []
        fn = always(lambda: TransientError("503", retry_after=120))
        with self.assertRaises(TransientError):
            retry(fn, max_delay=5.0, sleep=sleeps.append)
        self.assertEqual(fn.state["calls"], 1)
        self.assertEqual(sleeps, [])

    def test_never_sleeps_past_deadline(self):
        clock = FakeClock()
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock.t += s

        fn = always(lambda: TransientError("503"), clock=clock, cost=0.1)
        started = time.monotonic()
        with self.assertRaises(TransientError):
            retry(fn, attempts=10, base_delay=0.3, deadline=1.0, sleep=sleep, rng=lambda: 1.0, clock=clock)
        self.assertEqual(sleeps, [0.3], "slept although the next sleep would pass the deadline")
        self.assertEqual(fn.state["calls"], 2)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_custom_retry_on(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("reset")
            return "ok"

        self.assertEqual(retry(fn, retry_on=(ConnectionError,), sleep=lambda s: None), "ok")

    def test_backoff_stays_capped_for_many_attempts(self):
        sleeps = []
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 1500:
                raise TransientError("503")
            return "ok"

        self.assertEqual(retry(fn, attempts=1500, max_delay=2.0, sleep=sleeps.append, rng=lambda: 1.0), "ok")
        self.assertEqual(len(sleeps), 1499)
        self.assertEqual(max(sleeps), 2.0)


class TestBreakerContract(unittest.TestCase):
    def test_success_resets_consecutive_failures(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=10, clock=FakeClock())
        boom = always(lambda: TransientError("x"))
        for _ in range(4):
            for _ in range(2):
                with self.assertRaises(TransientError):
                    cb.call(boom)
            cb.call(lambda: "ok")
        self.assertEqual(cb.state, "closed")

    def test_failed_trial_reopens_with_fresh_timeout(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=10, clock=clock)
        boom = always(lambda: TransientError("x"))
        for _ in range(3):
            with self.assertRaises(TransientError):
                cb.call(boom)
        clock.t += 10
        with self.assertRaises(TransientError):
            cb.call(boom)  # the half-open trial fails
        self.assertEqual(cb.state, "open")
        clock.t += 9.9
        with self.assertRaises(CircuitOpen):
            cb.call(boom)
        self.assertEqual(boom.state["calls"], 4)
        clock.t += 0.1
        self.assertEqual(cb.call(lambda: "up"), "up")

    def test_half_open_admits_exactly_one_trial(self):
        clock = FakeClock()
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=5, clock=clock)
        with self.assertRaises(TransientError):
            cb.call(always(lambda: TransientError("x")))
        clock.t += 5

        release = threading.Event()
        trial_calls = []
        rejected = []
        lock = threading.Lock()

        def slow_ok():
            with lock:
                trial_calls.append(1)
            release.wait(5)
            return "ok"

        def caller():
            try:
                cb.call(slow_ok)
            except CircuitOpen:
                with lock:
                    rejected.append(1)

        threads = [threading.Thread(target=caller, daemon=True) for _ in range(12)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(trial_calls), 1, "the recovering service was flooded with trial calls")
        self.assertEqual(len(rejected), 11)
        self.assertEqual(cb.state, "closed")


if __name__ == "__main__":
    unittest.main()
