# cachelib

Read-through cache in front of the pricing database. Every product page calls
`prices.get(sku)`; a miss loads from the database.

```python
from cachelib import TTLCache

prices = TTLCache(loader=db.load_price, ttl=30.0, max_entries=50_000)
price = prices.get("sku-123")
prices.invalidate("sku-123")  # after a price change is written
```

| Member | Contract |
| --- | --- |
| `TTLCache(loader, ttl, max_entries=1024, clock=None)` | `ttl > 0`, `max_entries >= 1` or `ValueError`; `clock()` returns seconds |
| `get(key)` | Cached value if fresh, otherwise loads it. Concurrent misses for one key share one load |
| `invalidate(key)` | Drops the entry; loads already in flight must not re-cache the old value |
| `len(cache)` | Entries currently held; never more than `max_entries` (least recently used evicted) |
