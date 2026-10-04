# payments

Charges customer cards through the payment gateway. Mobile clients retry on
timeouts, so every request carries an idempotency key.

```python
from payments import PaymentService

svc = PaymentService(gateway)
receipt = svc.charge("order-8812-attempt", account="acct_42", amount_cents=1999, currency="USD")
```

| Member | Contract |
| --- | --- |
| `PaymentService(gateway, clock=None, key_ttl=86_400.0)` | `gateway.charge(account, amount_cents, currency)` returns a receipt dict or raises |
| `charge(idempotency_key, account, amount_cents, currency)` | At most one gateway charge per key and account; retries replay the original outcome |

Errors live in `payments.errors`: `GatewayError` (transient), `CardDeclined` (final),
`IdempotencyConflict` (key reused with different parameters).
