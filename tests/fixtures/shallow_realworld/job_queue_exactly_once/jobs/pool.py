"""Shallow fix: correct retries and shutdown, but the duplicate check is an unlocked
check-then-act (concurrent submits double-charge) and results leak internal state."""

import queue
import threading
import time

_STOP = object()


class WorkerPool:
    def __init__(self, workers=4, max_attempts=3, backoff=None):
        if workers < 1 or max_attempts < 1:
            raise ValueError("bad config")
        self._max_attempts = max_attempts
        self._backoff = backoff or (lambda n: 0.05 * 2 ** (n - 1))
        self._q = queue.Queue()
        self._seen = set()
        self._attempts = {}
        self._results = {}
        self._failures = {}
        self._closed = False
        self._threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]
        for t in self._threads:
            t.start()

    def submit(self, job_id, fn):
        if self._closed:
            raise RuntimeError("closed")
        if job_id in self._seen:
            return False
        self._seen.add(job_id)
        self._q.put((job_id, fn))
        return True

    def _work(self):
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            job_id, fn = item
            while True:
                n = self._attempts[job_id] = self._attempts.get(job_id, 0) + 1
                try:
                    self._results[job_id] = fn()
                    break
                except Exception as exc:
                    if n >= self._max_attempts:
                        self._failures[job_id] = str(exc)
                        break
                    time.sleep(self._backoff(n))

    def shutdown(self, wait=True):
        if not self._closed:
            self._closed = True
            for _ in self._threads:
                self._q.put(_STOP)
        if wait:
            for t in self._threads:
                t.join()
        return True

    def results(self):
        return self._results

    def failures(self):
        return self._failures

    def attempts(self, job_id):
        return self._attempts.get(job_id, 0)
