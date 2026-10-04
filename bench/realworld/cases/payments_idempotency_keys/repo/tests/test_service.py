import threading
import time
import unittest

from payments import IdempotencyConflict, PaymentService


class FakeGateway:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def charge(self, account, amount_cents, currency):
        with self.lock:
            self.calls.append((account, amount_cents, currency))
            n = len(self.calls)
        time.sleep(self.delay)
        return {"id": f"ch_{n}", "account": account, "amount": amount_cents, "currency": currency}


class TestIdempotency(unittest.TestCase):
    def test_retry_replays_receipt(self):
        gw = FakeGateway()
        svc = PaymentService(gw)
        first = svc.charge("k1", "acct_1", 1999, "USD")
        again = svc.charge("k1", "acct_1", 1999, "USD")
        self.assertEqual(first, again)
        self.assertEqual(len(gw.calls), 1)

    def test_key_reuse_with_different_amount_conflicts(self):
        gw = FakeGateway()
        svc = PaymentService(gw)
        svc.charge("k1", "acct_1", 1999, "USD")
        with self.assertRaises(IdempotencyConflict):
            svc.charge("k1", "acct_1", 2999, "USD")
        self.assertEqual(len(gw.calls), 1)

    def test_invalid_amount_never_reaches_gateway(self):
        gw = FakeGateway()
        svc = PaymentService(gw)
        for bad in (0, -5, 19.99):
            with self.assertRaises(ValueError):
                svc.charge(f"k-{bad}", "acct_1", bad, "USD")
        self.assertEqual(gw.calls, [])

    def test_concurrent_retries_charge_once(self):
        gw = FakeGateway(delay=0.3)
        svc = PaymentService(gw)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(svc.charge("k1", "acct_1", 500, "USD")))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(gw.calls), 1, "customer charged more than once")
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r == results[0] for r in results))


if __name__ == "__main__":
    unittest.main()
