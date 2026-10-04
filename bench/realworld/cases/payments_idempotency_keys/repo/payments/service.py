"""Charges cards through the payment gateway, safely under client retries."""

import time


class PaymentService:
    def __init__(self, gateway, clock=None, key_ttl=86_400.0):
        self.gateway = gateway
        self.clock = clock or time.monotonic
        self.key_ttl = key_ttl
        self._seen = {}

    def charge(self, idempotency_key, account, amount_cents, currency):
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]
        result = self.gateway.charge(account, amount_cents, currency)
        self._seen[idempotency_key] = result
        return result
