import unittest

from ratelimit import RateLimiter


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestTokenBucket(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_bucket_starts_full_then_denies(self):
        rl = RateLimiter(rate=1.0, capacity=5, clock=self.clock)
        self.assertEqual([rl.allow("a") for _ in range(6)], [True] * 5 + [False])

    def test_no_burst_across_window_boundary(self):
        clock = FakeClock(1000.9)
        rl = RateLimiter(rate=1.0, capacity=5, clock=clock)
        self.assertEqual(sum(rl.allow("a") for _ in range(5)), 5)
        clock.advance(0.2)  # crosses a whole-second boundary
        self.assertFalse(rl.allow("a"), "only 0.2 tokens refilled; must deny")

    def test_continuous_fractional_refill(self):
        rl = RateLimiter(rate=2.0, capacity=4, clock=self.clock)
        for _ in range(4):
            rl.allow("a")
        self.clock.advance(0.25)
        self.assertFalse(rl.allow("a"))
        self.clock.advance(0.25)
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"))

    def test_cost_is_atomic(self):
        rl = RateLimiter(rate=1.0, capacity=5, clock=self.clock)
        self.assertTrue(rl.allow("a", cost=3))
        self.assertFalse(rl.allow("a", cost=3), "only 2 tokens left")
        self.assertTrue(rl.allow("a", cost=2), "the failed call must not consume anything")

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            RateLimiter(rate=0, capacity=5)
        with self.assertRaises(ValueError):
            RateLimiter(rate=1.0, capacity=0)
        rl = RateLimiter(rate=1.0, capacity=5, clock=self.clock)
        with self.assertRaises(ValueError):
            rl.allow("a", cost=6)

    def test_retry_after(self):
        rl = RateLimiter(rate=2.0, capacity=2, clock=self.clock)
        self.assertEqual(rl.retry_after("a"), 0.0)
        rl.allow("a")
        rl.allow("a")
        self.assertAlmostEqual(rl.retry_after("a"), 0.5, places=6)
        self.assertAlmostEqual(rl.retry_after("a", cost=2), 1.0, places=6)
        self.assertFalse(rl.allow("a"), "retry_after must not consume")


if __name__ == "__main__":
    unittest.main()
