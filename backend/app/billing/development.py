"""Explicit development payment simulation. No network or accepted callbacks."""
from uuid import UUID

from .providers import PaymentError
from .settings import BillingConfigurationError

SIMULATION_MERCHANT_ID = "nexpoly-development-simulation"
SIMULATION_APP_ID = "nexpoly-development-simulation"


def is_simulated_order(order):
    return (order.get("merchant_id") == SIMULATION_MERCHANT_ID
            and order.get("app_id") == SIMULATION_APP_ID)


class DevelopmentPaymentGateway:
    def __init__(self, settings):
        if (not settings.simulation_enabled or settings.enabled
                or settings.wechat is not None or settings.alipay is not None):
            raise BillingConfigurationError("Development simulation cannot use real payment channels")

    @staticmethod
    def _validate(provider, order_id):
        try:
            UUID(str(order_id))
        except (ValueError, TypeError, AttributeError):
            raise PaymentError("payment_order_id_invalid") from None
        if provider not in {"wechat", "alipay"}:
            raise PaymentError("payment_provider_unavailable")

    def create_order(self, provider, order_id, amount_fen, description, expires_at):
        self._validate(provider, order_id)
        # Deliberately no payment URI/QR that could be mistaken for a real charge.
        return {"simulation": True}

    def query_order(self, provider, order_id, *, expected_amount_fen):
        self._validate(provider, order_id)
        return {"status": "pending"}

    def close_order(self, provider, order_id):
        self._validate(provider, order_id)
        return {"status": "closed"}

    def verify_notification(self, provider, headers, body):
        raise PaymentError("development_callbacks_disabled")
