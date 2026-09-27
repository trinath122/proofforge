"""Thread pool that runs billing jobs with retries."""

import queue
import threading
import time


class WorkerPool:
    def __init__(self, workers=4, max_attempts=3, backoff=None):
        self._q = queue.Queue()
        self._max_attempts = max_attempts
        self._backoff = backoff or (lambda n: 0.05 * 2 ** (n - 1))
        self._seen = set()
        self._attempts = {}
        self._results = {}
        self._failures = {}
        self._closed = False
        self._threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]
        for t in self._threads:
            t.start()

    def submit(self, job_id, fn):
        if job_id in self._seen:
            return False
        self._seen.add(job_id)
        self._q.put((job_id, fn))
        return True

    def _work(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            job_id, fn = item
            n = self._attempts.get(job_id, 0) + 1
            self._attempts[job_id] = n
            try:
                self._results[job_id] = fn()
            except Exception:
                if n < self._max_attempts:
                    time.sleep(self._backoff(n))
                    self._q.put(item)
                else:
                    self._results[job_id] = None
            self._q.task_done()

    def shutdown(self, wait=True):
        self._closed = True
        for _ in self._threads:
            self._q.put(None)
        if wait:
            self._q.join()
            for t in self._threads:
                t.join()
        return True

    def results(self):
        return self._results

    def failures(self):
        return self._failures

    def attempts(self, job_id):
        return self._attempts.get(job_id, 0)
