import threading
import time
import unittest

from payments import CardDeclined, GatewayError, IdempotencyConflict, PaymentService


class FakeClock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ScriptedGateway:
    """Returns receipts, or raises the scripted exceptions in order."""

    def __init__(self, script=(), delay=0.0):
        self.script = list(script)
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def charge(self, account, amount_cents, currency):
        with self.lock:
            self.calls.append((account, amount_cents, currency))
            n = len(self.calls)
            outcome = self.script.pop(0) if self.script else None
        time.sleep(self.delay)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"id": f"ch_{n}", "account": account, "amount": amount_cents, "currency": currency}


class TestScoping(unittest.TestCase):
    def test_keys_are_scoped_per_account(self):
        gw = ScriptedGateway()
        svc = PaymentService(gw)
        a = svc.charge("checkout-1", "acct_A", 1000, "USD")
        b = svc.charge("checkout-1", "acct_B", 1000, "USD")
        self.assertEqual(a["account"], "acct_A")
        self.assertEqual(b["account"], "acct_B", "acct_B received acct_A's receipt")
        self.assertEqual(len(gw.calls), 2)

    def test_currency_is_case_insensitive(self):
        gw = ScriptedGateway()
        svc = PaymentService(gw)
        first = svc.charge("k", "acct", 700, "usd")
        self.assertEqual(gw.calls[0][2], "USD")
        self.assertEqual(svc.charge("k", "acct", 700, "USD"), first)
        with self.assertRaises(IdempotencyConflict):
            svc.charge("k", "acct", 700, "EUR")
        self.assertEqual(len(gw.calls), 1)

    def test_bool_and_bad_currency_rejected(self):
        gw = ScriptedGateway()
        svc = PaymentService(gw)
        with self.assertRaises(ValueError):
            svc.charge("k", "acct", True, "USD")
        for cur in ("US", "USDT", "12$", None):
            with self.assertRaises(ValueError):
                svc.charge("k2", "acct", 100, cur)
        self.assertEqual(gw.calls, [])


class TestOutcomes(unittest.TestCase):
    def test_decline_is_replayed_not_retried(self):
        gw = ScriptedGateway([CardDeclined("insufficient funds")])
        svc = PaymentService(gw)
        for _ in range(3):
            with self.assertRaises(CardDeclined):
                svc.charge("k", "acct", 5000, "USD")
        self.assertEqual(len(gw.calls), 1, "a declined request was sent to the gateway again")

    def test_transient_error_frees_the_key(self):
        gw = ScriptedGateway([GatewayError("timeout")])
        svc = PaymentService(gw)
        with self.assertRaises(GatewayError):
            svc.charge("k", "acct", 5000, "USD")
        receipt = svc.charge("k", "acct", 5000, "USD")
        self.assertEqual(receipt["id"], "ch_2")
        self.assertEqual(svc.charge("k", "acct", 5000, "USD"), receipt)
        self.assertEqual(len(gw.calls), 2)

    def test_waiters_share_a_transient_error(self):
        gw = ScriptedGateway([GatewayError("timeout")], delay=0.3)
        svc = PaymentService(gw)
        errors = []

        def call():
            try:
                svc.charge("k", "acct", 100, "USD")
            except GatewayError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(gw.calls), 1)
        self.assertEqual(len(errors), 5)

    def test_conflict_detected_while_first_request_in_flight(self):
        gw = ScriptedGateway(delay=0.4)
        svc = PaymentService(gw)
        t = threading.Thread(target=lambda: svc.charge("k", "acct", 100, "USD"), daemon=True)
        t.start()
        time.sleep(0.1)
        with self.assertRaises(IdempotencyConflict):
            svc.charge("k", "acct", 999, "USD")
        t.join(5)
        self.assertEqual(len(gw.calls), 1)


class TestExpiry(unittest.TestCase):
    def test_key_expires_at_exactly_ttl(self):
        clock = FakeClock()
        gw = ScriptedGateway()
        svc = PaymentService(gw, clock=clock, key_ttl=60)
        first = svc.charge("k", "acct", 100, "USD")
        clock.t += 59.999
        self.assertEqual(svc.charge("k", "acct", 100, "USD"), first)
        clock.t += 0.001
        self.assertEqual(svc.charge("k", "acct", 200, "USD")["id"], "ch_2")
        self.assertEqual(len(gw.calls), 2)


class TestThroughput(unittest.TestCase):
    def test_different_keys_do_not_wait_for_each_other(self):
        gw = ScriptedGateway(delay=0.4)
        svc = PaymentService(gw)
        threads = [
            threading.Thread(target=lambda i=i: svc.charge(f"k{i}", "acct", 100, "USD"), daemon=True)
            for i in range(5)
        ]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        elapsed = time.monotonic() - started
        self.assertEqual(len(gw.calls), 5)
        self.assertLess(elapsed, 1.2, f"5 gateway calls of 0.4s took {elapsed:.2f}s: serialized")


if __name__ == "__main__":
    unittest.main()
