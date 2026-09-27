import threading
import unittest

from jobs import WorkerPool


def shutdown_within(pool, seconds=5):
    done = threading.Event()
    result = {}

    def run():
        result["value"] = pool.shutdown()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds), result.get("value")


def flaky(fail_times, value, message="transient"):
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise RuntimeError(f"{message} {state['calls']}")
        return value

    return fn


class TestWorkerPool(unittest.TestCase):
    def test_runs_jobs_and_shuts_down(self):
        pool = WorkerPool(workers=3, backoff=lambda n: 0)
        for i in range(10):
            self.assertTrue(pool.submit(f"job-{i}", lambda i=i: i * i))
        finished, returned = shutdown_within(pool)
        self.assertTrue(finished, "shutdown() hung")
        self.assertIs(returned, True)
        self.assertEqual(pool.results(), {f"job-{i}": i * i for i in range(10)})

    def test_exhausted_job_is_a_failure(self):
        pool = WorkerPool(workers=2, max_attempts=3, backoff=lambda n: 0)
        pool.submit("charge:1", flaky(99, None, "card declined"))
        self.assertTrue(shutdown_within(pool)[0], "shutdown() hung")
        self.assertNotIn("charge:1", pool.results())
        self.assertIn("card declined", pool.failures()["charge:1"])
        self.assertEqual(pool.attempts("charge:1"), 3)

    def test_retry_then_success(self):
        pool = WorkerPool(workers=2, max_attempts=3, backoff=lambda n: 0)
        pool.submit("charge:2", flaky(2, "ok"))
        self.assertTrue(shutdown_within(pool)[0], "shutdown() hung")
        self.assertEqual(pool.results(), {"charge:2": "ok"})
        self.assertEqual(pool.failures(), {})
        self.assertEqual(pool.attempts("charge:2"), 3)

    def test_duplicate_submission_is_ignored(self):
        pool = WorkerPool(workers=2, backoff=lambda n: 0)
        calls = []
        self.assertTrue(pool.submit("charge:3", lambda: calls.append(1)))
        self.assertFalse(pool.submit("charge:3", lambda: calls.append(2)))
        self.assertTrue(shutdown_within(pool)[0], "shutdown() hung")
        self.assertEqual(calls, [1])

    def test_submit_after_shutdown_raises(self):
        pool = WorkerPool(workers=1, backoff=lambda n: 0)
        self.assertTrue(shutdown_within(pool)[0], "shutdown() hung")
        with self.assertRaises(RuntimeError):
            pool.submit("late", lambda: 1)

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            WorkerPool(workers=0)
        with self.assertRaises(ValueError):
            WorkerPool(max_attempts=0)


if __name__ == "__main__":
    unittest.main()
