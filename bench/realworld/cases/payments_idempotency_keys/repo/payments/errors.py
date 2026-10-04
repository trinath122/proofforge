class PaymentError(Exception):
    """Base class for payment failures."""


class GatewayError(PaymentError):
    """Transient gateway failure (timeout, 5xx). Safe to retry."""


class CardDeclined(PaymentError):
    """The card was declined. Final: retrying the same request gives the same answer."""


class IdempotencyConflict(PaymentError):
    """An idempotency key was reused with different request parameters."""
