import threading
import time
import unittest

from cachelib import TTLCache


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def run_threads(targets, timeout=5):
    threads = [threading.Thread(target=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    return [t for t in threads if t.is_alive()]


class TestValidation(unittest.TestCase):
    def test_rejects_bad_arguments(self):
        for kwargs in ({"ttl": 0}, {"ttl": -1}, {"ttl": 5, "max_entries": 0}):
            with self.assertRaises(ValueError):
                TTLCache(lambda k: k, **kwargs)

    def test_none_is_cached(self):
        calls = []
        cache = TTLCache(lambda k: calls.append(k), ttl=10, clock=FakeClock())
        self.assertIsNone(cache.get("a"))
        self.assertIsNone(cache.get("a"))
        self.assertEqual(calls, ["a"])

    def test_freshness_counts_from_load_completion(self):
        clock = FakeClock()

        def slow(key):
            clock.t += 5  # the load itself takes 5 seconds
            return key

        cache = TTLCache(slow, ttl=10, clock=clock)
        cache.get("a")  # finished at t=105
        clock.t = 114.9
        calls_before = clock.t
        self.assertEqual(cache.get("a"), "a")
        self.assertEqual(clock.t, calls_before, "entry is still fresh; must not reload")


class TestSingleFlight(unittest.TestCase):
    def test_different_keys_load_in_parallel(self):
        def slow(key):
            time.sleep(0.4)
            return key

        cache = TTLCache(slow, ttl=10)
        started = time.monotonic()
        stuck = run_threads([lambda k=k: cache.get(k) for k in "abcd"])
        elapsed = time.monotonic() - started
        self.assertFalse(stuck)
        self.assertLess(elapsed, 1.2, f"4 loads of 0.4s took {elapsed:.2f}s: loads are serialized")

    def test_loader_may_call_get_for_other_keys(self):
        cache = None

        def loader(key):
            if key == "bundle":
                return cache.get("part-1") + cache.get("part-2")
            return 1

        cache = TTLCache(loader, ttl=10)
        out = []
        stuck = run_threads([lambda: out.append(cache.get("bundle"))], timeout=3)
        self.assertFalse(stuck, "nested get() deadlocked")
        self.assertEqual(out, [2])

    def test_waiters_share_the_loader_error(self):
        release = threading.Event()
        calls = []

        def failing(key):
            calls.append(key)
            release.wait(5)
            raise ConnectionError("db down")

        cache = TTLCache(failing, ttl=10)
        errors = []

        def caller():
            try:
                cache.get("k")
            except ConnectionError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=caller, daemon=True) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(errors), 6)
        self.assertEqual(len(cache), 0)

    def test_stampede_on_many_hot_keys(self):
        calls = []
        lock = threading.Lock()

        def slow(key):
            with lock:
                calls.append(key)
            time.sleep(0.2)
            return key.upper()

        cache = TTLCache(slow, ttl=60)
        results = []
        keys = [f"sku-{i % 4}" for i in range(40)]
        stuck = run_threads([lambda k=k: results.append((k, cache.get(k))) for k in keys])
        self.assertFalse(stuck)
        self.assertEqual(sorted(calls), [f"sku-{i}" for i in range(4)])
        self.assertTrue(all(v == k.upper() for k, v in results))


class TestInvalidation(unittest.TestCase):
    def test_invalidate_during_load_never_caches_old_value(self):
        gates = {1: threading.Event(), 2: threading.Event()}
        version = {"n": 0}
        lock = threading.Lock()

        def loader(key):
            with lock:
                version["n"] += 1
                n = version["n"]
            gates[n].wait(5)
            return f"price-v{n}"

        cache = TTLCache(loader, ttl=60)
        old = []
        t1 = threading.Thread(target=lambda: old.append(cache.get("sku")), daemon=True)
        t1.start()
        time.sleep(0.2)  # load 1 is in flight with the old price
        cache.invalidate("sku")  # the price changed in the database
        new = []
        t2 = threading.Thread(target=lambda: new.append(cache.get("sku")), daemon=True)
        t2.start()
        time.sleep(0.2)
        gates[2].set()  # the new load finishes first
        t2.join(5)
        gates[1].set()  # then the old one
        t1.join(5)
        self.assertEqual(new, ["price-v2"], "a get after invalidate must not join the old load")
        self.assertEqual(old, ["price-v1"])
        self.assertEqual(cache.get("sku"), "price-v2", "the old load overwrote the new price")
        self.assertEqual(version["n"], 2)


class TestBounds(unittest.TestCase):
    def test_size_bound_holds_under_concurrency(self):
        cache = TTLCache(lambda k: k, ttl=60, max_entries=16)

        def worker(base):
            for i in range(300):
                cache.get((base * 7 + i) % 97)

        stuck = run_threads([lambda b=b: worker(b) for b in range(8)])
        self.assertFalse(stuck)
        self.assertLessEqual(len(cache), 16)


if __name__ == "__main__":
    unittest.main()
