"""Plausible fix: global lock, fingerprint check, validation. Passes the visible tests."""

import threading
import time

from payments.errors import IdempotencyConflict


class PaymentService:
    def __init__(self, gateway, clock=None, key_ttl=86_400.0):
        self.gateway = gateway
        self.clock = clock or time.monotonic
        self.key_ttl = key_ttl
        self._seen = {}
        self._lock = threading.Lock()

    def charge(self, idempotency_key, account, amount_cents, currency):
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValueError("amount_cents must be a positive int")
        with self._lock:
            hit = self._seen.get(idempotency_key)
            if hit is not None and self.clock() - hit[2] < self.key_ttl:
                if hit[0] != (amount_cents, currency):
                    raise IdempotencyConflict(idempotency_key)
                return hit[1]
            result = self.gateway.charge(account, amount_cents, currency)
            self._seen[idempotency_key] = ((amount_cents, currency), result, self.clock())
            return result
