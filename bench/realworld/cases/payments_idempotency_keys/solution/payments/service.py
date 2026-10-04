"""Charges cards through the payment gateway, safely under client retries."""

import threading
import time

from payments.errors import CardDeclined, IdempotencyConflict


class _Request:
    """The single gateway call for one (account, idempotency key)."""

    __slots__ = ("fingerprint", "done", "receipt", "error", "recorded_at")

    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.done = threading.Event()
        self.receipt = None
        self.error = None
        self.recorded_at = None


class PaymentService:
    def __init__(self, gateway, clock=None, key_ttl=86_400.0):
        self.gateway = gateway
        self.clock = clock or time.monotonic
        self.key_ttl = key_ttl
        self._requests = {}  # (account, key) -> _Request
        self._lock = threading.Lock()

    @staticmethod
    def _validate(amount_cents, currency):
        if type(amount_cents) is not int or amount_cents <= 0:
            raise ValueError("amount_cents must be a positive int")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter code")
        return currency.upper()

    def charge(self, idempotency_key, account, amount_cents, currency):
        currency = self._validate(amount_cents, currency)
        scope = (account, idempotency_key)
        fingerprint = (amount_cents, currency)

        with self._lock:
            req = self._requests.get(scope)
            if req is not None and req.recorded_at is not None:
                if self.clock() - req.recorded_at >= self.key_ttl:
                    del self._requests[scope]
                    req = None
            if req is not None and req.fingerprint != fingerprint:
                raise IdempotencyConflict(f"key {idempotency_key!r} was used with other parameters")
            leader = req is None
            if leader:
                req = self._requests[scope] = _Request(fingerprint)

        if not leader:
            req.done.wait()
            if req.error is not None:
                raise req.error
            return req.receipt

        # The gateway call happens outside the lock so unrelated keys never wait.
        try:
            receipt = self.gateway.charge(account, amount_cents, currency)
        except CardDeclined as exc:
            req.error = exc
            with self._lock:
                req.recorded_at = self.clock()
            req.done.set()
            raise
        except BaseException as exc:
            # Transient: release the key so a retry reaches the gateway again.
            req.error = exc
            with self._lock:
                if self._requests.get(scope) is req:
                    del self._requests[scope]
            req.done.set()
            raise
        req.receipt = receipt
        with self._lock:
            req.recorded_at = self.clock()
        req.done.set()
        return receipt
