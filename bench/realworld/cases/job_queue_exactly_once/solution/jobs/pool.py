"""Thread pool that runs billing jobs with retries, exactly once per job id."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

_STOP = object()


class WorkerPool:
    def __init__(
        self,
        workers: int = 4,
        max_attempts: int = 3,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._max_attempts = max_attempts
        self._backoff = backoff or (lambda n: 0.05 * 2 ** (n - 1))
        self._lock = threading.Lock()
        self._q: queue.Queue[Any] = queue.Queue()
        self._seen: set[Hashable] = set()
        self._attempts: dict[Hashable, int] = {}
        self._results: dict[Hashable, Any] = {}
        self._failures: dict[Hashable, str] = {}
        self._closed = False
        self._threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]
        for t in self._threads:
            t.start()

    def submit(self, job_id: Hashable, fn: Callable[[], Any]) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("pool is shut down")
            if job_id in self._seen:
                return False
            self._seen.add(job_id)
            # Enqueue under the lock so no job can land behind the stop markers.
            self._q.put((job_id, fn))
            return True

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            job_id, fn = item
            # Retries stay on this worker: the job never overlaps itself and is never lost.
            while True:
                with self._lock:
                    attempt = self._attempts.get(job_id, 0) + 1
                    self._attempts[job_id] = attempt
                try:
                    value = fn()
                except Exception as exc:
                    if attempt < self._max_attempts:
                        time.sleep(max(0.0, float(self._backoff(attempt) or 0)))
                        continue
                    with self._lock:
                        self._failures[job_id] = f"{type(exc).__name__}: {exc}"
                    break
                with self._lock:
                    self._results[job_id] = value
                break

    def shutdown(self, wait: bool = True) -> bool:
        with self._lock:
            first = not self._closed
            self._closed = True
            if first:
                for _ in self._threads:
                    self._q.put(_STOP)
        if wait:
            for t in self._threads:
                t.join()
        return True

    def results(self) -> dict[Hashable, Any]:
        with self._lock:
            return dict(self._results)

    def failures(self) -> dict[Hashable, str]:
        with self._lock:
            return dict(self._failures)

    def attempts(self, job_id: Hashable) -> int:
        with self._lock:
            return self._attempts.get(job_id, 0)
