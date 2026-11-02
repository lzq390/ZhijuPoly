"""Explicit development payment simulation. No network or accepted callbacks."""
import hmac
import re
import secrets
from uuid import UUID

from .providers import PaymentError
from .settings import BillingConfigurationError

SIMULATION_MERCHANT_ID = "nexpoly-development-simulation"
SIMULATION_APP_ID = "nexpoly-development-simulation"
SIMULATION_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9_-]{43}")


def is_simulated_order(order):
    return (order.get("merchant_id") == SIMULATION_MERCHANT_ID
            and order.get("app_id") == SIMULATION_APP_ID)


def simulation_token_order_id(token):
    if not isinstance(token, str) or not SIMULATION_TOKEN_PATTERN.fullmatch(token):
        return None
    return UUID(token[:32])


def simulation_token_matches(order, token):
    checkout = order.get("checkout_data") or {}
    saved = checkout.get("simulation_checkout_token")
    return (is_simulated_order(order) and checkout.get("simulation") is True
            and simulation_token_order_id(token) == order["id"]
            and simulation_token_order_id(saved) == order["id"]
            and hmac.compare_digest(saved, token))


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
        # A bearer capability for this order only. The owner builds a local page
        # URL with the token in its fragment; no real payment URI is generated.
        return {"simulation": True,
                "simulation_checkout_token": UUID(str(order_id)).hex + "." + secrets.token_urlsafe(32)}

    def query_order(self, provider, order_id, *, expected_amount_fen):
        self._validate(provider, order_id)
        return {"status": "pending"}

    def close_order(self, provider, order_id):
        self._validate(provider, order_id)
        return {"status": "closed"}

    def verify_notification(self, provider, headers, body):
        raise PaymentError("development_callbacks_disabled")
