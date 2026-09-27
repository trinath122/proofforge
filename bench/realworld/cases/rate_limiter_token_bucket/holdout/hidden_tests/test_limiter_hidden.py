import sys
import threading
import time
import unittest

from ratelimit import RateLimiter


class FakeClock:
    def __init__(self, t=5000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestConcurrency(unittest.TestCase):
    def test_threads_never_over_admit(self):
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # force frequent thread switches to expose races
        try:
            for trial in range(5):
                rl = RateLimiter(rate=0.001, capacity=50, clock=FakeClock())
                admitted = []
                barrier = threading.Barrier(24)

                def worker():
                    barrier.wait()
                    n = sum(rl.allow("shared") for _ in range(60))
                    admitted.append(n)

                threads = [threading.Thread(target=worker) for _ in range(24)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)
                self.assertEqual(sum(admitted), 50, f"trial {trial}: over/under admission")
        finally:
            sys.setswitchinterval(old)


class TestMemoryBounds(unittest.TestCase):
    def test_never_exceeds_max_keys(self):
        rl = RateLimiter(rate=1.0, capacity=3, clock=FakeClock(), max_keys=100)
        for i in range(10_000):
            rl.allow(f"client-{i}")
            self.assertLessEqual(len(rl), 100)

    def test_evicts_least_recently_used(self):
        clock = FakeClock()
        rl = RateLimiter(rate=0.001, capacity=1, clock=clock, max_keys=2)
        self.assertTrue(rl.allow("a"))
        self.assertTrue(rl.allow("b"))
        self.assertFalse(rl.allow("a"))  # touches "a": now "b" is least recently used
        self.assertTrue(rl.allow("c"))  # evicts "b"
        self.assertFalse(rl.allow("a"), "'a' was recently used; it must not be evicted and reset")
        self.assertLessEqual(len(rl), 2)

    def test_idle_buckets_are_dropped(self):
        clock = FakeClock()
        rl = RateLimiter(rate=1.0, capacity=2, clock=clock, idle_ttl=60.0)
        for i in range(50):
            rl.allow(f"k{i}")
        clock.t += 61
        rl.allow("fresh")
        self.assertEqual(len(rl), 1)

    def test_active_buckets_are_kept(self):
        clock = FakeClock()
        rl = RateLimiter(rate=0.001, capacity=1, clock=clock, idle_ttl=60.0)
        self.assertTrue(rl.allow("busy"))
        clock.t += 30
        self.assertFalse(rl.allow("busy"))
        clock.t += 40  # 70s since first use, but only 40s idle
        rl.allow("other")
        self.assertFalse(rl.allow("busy"), "bucket was active 40s ago; it must be kept")


class TestClockAndMath(unittest.TestCase):
    def test_clock_going_backwards_grants_nothing(self):
        clock = FakeClock(100.0)
        rl = RateLimiter(rate=1.0, capacity=10, clock=clock)
        self.assertEqual(sum(rl.allow("a") for _ in range(10)), 10)
        clock.t = 50.0
        self.assertFalse(rl.allow("a"))
        self.assertGreaterEqual(rl.retry_after("a"), 0.0)
        clock.t = 101.0
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"), "only 1s has really elapsed since the bucket emptied")

    def test_refill_caps_at_capacity(self):
        clock = FakeClock()
        rl = RateLimiter(rate=100.0, capacity=3, clock=clock)
        rl.allow("a")
        clock.t += 3600
        self.assertEqual(sum(rl.allow("a") for _ in range(10)), 3)

    def test_slow_rates_and_fractional_retry(self):
        clock = FakeClock()
        rl = RateLimiter(rate=0.5, capacity=1, clock=clock)
        self.assertTrue(rl.allow("a"))
        self.assertAlmostEqual(rl.retry_after("a"), 2.0, places=6)
        clock.t += 1.5
        self.assertAlmostEqual(rl.retry_after("a"), 0.5, places=6)
        self.assertFalse(rl.allow("a"))
        clock.t += 0.5
        self.assertTrue(rl.allow("a"))

    def test_cost_validation(self):
        rl = RateLimiter(rate=1.0, capacity=5, clock=FakeClock())
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                rl.allow("a", cost=bad)
        with self.assertRaises(ValueError):
            RateLimiter(rate=-1.0, capacity=5)

    def test_keys_are_independent(self):
        rl = RateLimiter(rate=0.001, capacity=2, clock=FakeClock())
        self.assertEqual(sum(rl.allow("a") for _ in range(5)), 2)
        self.assertEqual(sum(rl.allow("b") for _ in range(5)), 2)

    def test_default_clock_works(self):
        rl = RateLimiter(rate=1000.0, capacity=5)
        self.assertTrue(rl.allow("a"))
        self.assertIsInstance(rl.retry_after("a"), float)


class TestPerformance(unittest.TestCase):
    def test_throughput(self):
        clock = FakeClock()
        rl = RateLimiter(rate=10.0, capacity=20, clock=clock, max_keys=1000)
        start = time.perf_counter()
        for i in range(100_000):
            rl.allow(f"k{i % 2000}")
            clock.t += 0.0001
        self.assertLess(time.perf_counter() - start, 10.0, "allow() must be O(1) amortized")


if __name__ == "__main__":
    unittest.main()
