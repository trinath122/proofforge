import threading
import unittest

from cachelib import TTLCache


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class CountingLoader:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, key):
        with self.lock:
            self.calls.append(key)
            return f"{key}-v{len(self.calls)}"


class TestTTLCache(unittest.TestCase):
    def test_hit_does_not_reload(self):
        loader = CountingLoader()
        cache = TTLCache(loader, ttl=10, clock=FakeClock())
        self.assertEqual(cache.get("a"), "a-v1")
        self.assertEqual(cache.get("a"), "a-v1")
        self.assertEqual(loader.calls, ["a"])

    def test_expires_at_exactly_ttl(self):
        clock, loader = FakeClock(), CountingLoader()
        cache = TTLCache(loader, ttl=10, clock=clock)
        cache.get("a")
        clock.t += 9.999
        self.assertEqual(cache.get("a"), "a-v1")
        clock.t += 0.001
        self.assertEqual(cache.get("a"), "a-v2", "an entry exactly ttl old is stale")

    def test_evicts_least_recently_used(self):
        loader = CountingLoader()
        cache = TTLCache(loader, ttl=60, max_entries=2, clock=FakeClock())
        cache.get("a")
        cache.get("b")
        cache.get("a")  # a is now more recent than b
        cache.get("c")
        self.assertEqual(len(cache), 2)
        cache.get("a")
        self.assertEqual(loader.calls, ["a", "b", "c"], "b should have been evicted, not a")

    def test_loader_error_is_not_cached(self):
        attempts = []

        def flaky(key):
            attempts.append(key)
            if len(attempts) == 1:
                raise ConnectionError("db timeout")
            return "ok"

        cache = TTLCache(flaky, ttl=10, clock=FakeClock())
        with self.assertRaises(ConnectionError):
            cache.get("a")
        self.assertEqual(cache.get("a"), "ok")

    def test_concurrent_misses_share_one_load(self):
        release = threading.Event()
        calls = []

        def slow(key):
            calls.append(key)
            release.wait(5)
            return "price"

        cache = TTLCache(slow, ttl=10)
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.get("hot"))) for _ in range(8)]
        for t in threads:
            t.start()
        threading.Event().wait(0.3)
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(results, ["price"] * 8)
        self.assertEqual(len(calls), 1, "a stampede: every caller hit the database")


if __name__ == "__main__":
    unittest.main()
