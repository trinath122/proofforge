# jobs

Background job execution for the billing service.

```python
from jobs import WorkerPool

pool = WorkerPool(workers=8, max_attempts=3)
pool.submit(f"charge:{invoice.id}", lambda: gateway.charge(invoice))  # idempotent by id
...
pool.shutdown()  # waits for every job, including retries
pool.results()  # {job_id: value} for successes
pool.failures()  # {job_id: "last error"} for jobs that used every attempt
```

Guarantees: a job id runs at most once to success, never concurrently with itself, and
`shutdown(wait=True)` always returns once all accepted work is finished.
