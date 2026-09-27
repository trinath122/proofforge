import sys
import threading
import time
import unittest

from jobs import WorkerPool


def shutdown_within(pool, seconds=6):
    done = threading.Event()
    threading.Thread(target=lambda: (pool.shutdown(), done.set()), daemon=True).start()
    return done.wait(seconds)


class TestExactlyOnce(unittest.TestCase):
    def test_concurrent_duplicate_submissions_run_once(self):
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            pool = WorkerPool(workers=4, backoff=lambda n: 0)
            runs = {}
            lock = threading.Lock()
            accepted = []
            barrier = threading.Barrier(12)

            def make(i):
                def fn():
                    with lock:
                        runs[i] = runs.get(i, 0) + 1

                return fn

            def submitter():
                barrier.wait()
                accepted.append(sum(pool.submit(f"inv-{i}", make(i)) for i in range(200)))

            threads = [threading.Thread(target=submitter) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertTrue(shutdown_within(pool), "shutdown() hung")
            self.assertEqual(sum(accepted), 200, "each id must be accepted exactly once")
            self.assertEqual(set(runs.values()), {1}, "some invoices were charged more than once")
            self.assertEqual(len(runs), 200)
        finally:
            sys.setswitchinterval(old)

    def test_never_retried_after_success_or_run_concurrently(self):
        pool = WorkerPool(workers=8, max_attempts=5, backoff=lambda n: 0.01)
        active, overlaps, lock = {}, [], threading.Lock()

        def make(i):
            state = {"calls": 0}

            def fn():
                with lock:
                    if active.get(i):
                        overlaps.append(i)
                    active[i] = True
                try:
                    time.sleep(0.002)
                    state["calls"] += 1
                    if state["calls"] == 1:
                        raise RuntimeError("first attempt fails")
                    return i
                finally:
                    with lock:
                        active[i] = False

            return fn

        for i in range(40):
            pool.submit(f"j{i}", make(i))
        self.assertTrue(shutdown_within(pool), "shutdown() hung")
        self.assertEqual(overlaps, [], "a job ran concurrently with itself")
        self.assertEqual({pool.attempts(f"j{i}") for i in range(40)}, {2})
        self.assertEqual(len(pool.results()), 40)


class YieldingKey:
    """A job id whose hashing yields the thread, widening any check-then-act race."""

    def __init__(self, value):
        self.value = value

    def __hash__(self):
        time.sleep(0)
        return hash(self.value)

    def __eq__(self, other):
        time.sleep(0)
        return isinstance(other, YieldingKey) and other.value == self.value


class TestExactlyOnceUnderContention(unittest.TestCase):
    def test_duplicate_race_with_yielding_keys(self):
        pool = WorkerPool(workers=4, backoff=lambda n: 0)
        runs, lock, accepted = {}, threading.Lock(), []
        barrier = threading.Barrier(8)

        def make(i):
            def fn():
                with lock:
                    runs[i] = runs.get(i, 0) + 1

            return fn

        def submitter():
            barrier.wait()
            accepted.append(sum(pool.submit(YieldingKey(i), make(i)) for i in range(100)))

        threads = [threading.Thread(target=submitter) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertTrue(shutdown_within(pool), "shutdown() hung")
        self.assertEqual(sum(accepted), 100, "duplicate submissions were accepted")
        self.assertEqual(set(runs.values()), {1}, "some invoices were charged more than once")


class TestShutdown(unittest.TestCase):
    def test_shutdown_waits_for_pending_retries(self):
        pool = WorkerPool(workers=2, max_attempts=3, backoff=lambda n: 0.02)
        states = {}

        def make(i):
            states[i] = 0

            def fn():
                states[i] += 1
                if states[i] < 3:
                    raise RuntimeError("retry me")
                return i

            return fn

        for i in range(20):
            pool.submit(f"r{i}", make(i))
        self.assertTrue(shutdown_within(pool), "shutdown() hung")
        self.assertEqual(pool.results(), {f"r{i}": i for i in range(20)}, "retries were lost")

    def test_shutdown_is_idempotent_and_survives_failing_jobs(self):
        pool = WorkerPool(workers=3, max_attempts=2, backoff=lambda n: 0)
        for i in range(10):
            pool.submit(f"bad{i}", lambda: 1 / 0)
        self.assertTrue(shutdown_within(pool), "shutdown() hung")
        self.assertTrue(shutdown_within(pool), "second shutdown() hung")
        self.assertEqual(len(pool.failures()), 10)
        self.assertTrue(all("division" in v for v in pool.failures().values()))

    def test_empty_pool_shuts_down(self):
        self.assertTrue(shutdown_within(WorkerPool(workers=4)))


class TestSemantics(unittest.TestCase):
    def test_none_return_value_is_a_success(self):
        pool = WorkerPool(workers=2, backoff=lambda n: 0)
        pool.submit("noop", lambda: None)
        self.assertTrue(shutdown_within(pool))
        self.assertEqual(pool.results(), {"noop": None})
        self.assertEqual(pool.failures(), {})

    def test_failure_keeps_last_error_and_backoff_sequence(self):
        seen = []
        pool = WorkerPool(workers=1, max_attempts=4, backoff=lambda n: seen.append(n) or 0)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ValueError(f"attempt {calls['n']} failed")

        pool.submit("x", fn)
        self.assertTrue(shutdown_within(pool))
        self.assertIn("attempt 4 failed", pool.failures()["x"])
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(pool.attempts("x"), 4)

    def test_results_are_copies(self):
        pool = WorkerPool(workers=1, backoff=lambda n: 0)
        pool.submit("a", lambda: 1)
        self.assertTrue(shutdown_within(pool))
        pool.results()["b"] = 2
        pool.failures()["c"] = "x"
        self.assertEqual(pool.results(), {"a": 1})
        self.assertEqual(pool.failures(), {})

    def test_workers_run_in_parallel(self):
        pool = WorkerPool(workers=4, backoff=lambda n: 0)
        start = time.perf_counter()
        for i in range(8):
            pool.submit(f"slow{i}", lambda: time.sleep(0.3))
        self.assertTrue(shutdown_within(pool))
        self.assertLess(time.perf_counter() - start, 1.8, "jobs did not run in parallel")

    def test_many_jobs(self):
        pool = WorkerPool(workers=6, backoff=lambda n: 0)
        for i in range(2000):
            pool.submit(i, lambda i=i: i)
        self.assertTrue(shutdown_within(pool, 20))
        self.assertEqual(len(pool.results()), 2000)


if __name__ == "__main__":
    unittest.main()
