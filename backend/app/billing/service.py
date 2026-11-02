"""Payment orchestration; browser navigation never grants membership."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

from .providers import PaymentError
from .repository import BillingError, public_order, simulation_checkout_view
from .development import (SIMULATION_MERCHANT_ID, SIMULATION_APP_ID, is_simulated_order,
                          simulation_token_matches)

logger = logging.getLogger("nexpoly.billing")


class BillingService:
    def __init__(self, settings, repository, gateway, *, application_settings=None):
        self.settings, self.repository, self.gateway = settings, repository, gateway
        self.application_settings, self.write_tracker = application_settings, None

    @contextmanager
    def _reconciliation_admission(self):
        if self.write_tracker is not None:
            self.write_tracker.enter()
        try:
            allowed = True
            if getattr(self.application_settings, 'deployment_drain_enabled', False):
                from app.services.deployment_control import get_drain_state
                with self.repository.connection(service=True) as connection:
                    allowed = not get_drain_state(connection).enabled
            yield allowed
        finally:
            if self.write_tracker is not None:
                self.write_tracker.exit()

    def plans(self):
        simulation = getattr(self.settings, "simulation_enabled", False)
        purchasing = self.settings.enabled or simulation
        return {"enabled": bool(purchasing and self.settings.available_providers), "simulation": simulation,
                "currency": "CNY", "period": "month", "monthly_price_fen": self.settings.monthly_price_fen,
                "providers": list(self.settings.available_providers) if purchasing else [],
                "registration_enabled": self.settings.registration_enabled}

    def create_order(self, owner, provider, key):
        simulation = getattr(self.settings, "simulation_enabled", False)
        if not (self.settings.enabled or simulation) or provider not in self.settings.available_providers:
            raise BillingError("purchase_unavailable", "此支付方式暂未开放。", 503)
        if simulation:
            merchant_id, app_id = SIMULATION_MERCHANT_ID, SIMULATION_APP_ID
        else:
            channel = getattr(self.settings, provider)
            merchant_id = channel.merchant_id if provider == 'wechat' else channel.seller_id
            app_id = channel.app_id
        order = self.repository.create_order(owner, provider, self.settings.monthly_price_fen, key,
                                             merchant_id=merchant_id, app_id=app_id)
        if not self._merchant_matches(order):
            raise BillingError('order_merchant_changed', '该订单的收款配置已变更，请联系管理员确认。')
        if order["status"] == "pending" and not self._checkout_ready(order):
            if order["expires_at"] <= datetime.now(timezone.utc):
                self.reconcile_order(order)
            else:
                try:
                    checkout = self.gateway.create_order(provider, UUID(str(order["id"])).hex,
                        order["amount_fen"], "NexPoly 科研会员（一个月）", order["expires_at"])
                    self.repository.save_checkout(order["id"], checkout)
                except PaymentError:
                    # The provider may have created/charged the order before a timeout.
                    # Keep its identity durable and let queries/callbacks reconcile it.
                    logger.warning("billing_checkout_pending", extra={"provider": provider})
        return public_order(self.repository.get_order(owner, order["id"]))

    def _merchant_matches(self, order):
        if getattr(self.settings, "simulation_enabled", False):
            return is_simulated_order(order)
        channel = getattr(self.settings, order['provider'], None)
        if channel is None:
            return False
        merchant_id = channel.merchant_id if order['provider'] == 'wechat' else channel.seller_id
        return merchant_id == order['merchant_id'] and channel.app_id == order['app_id']

    @staticmethod
    def _checkout_ready(order):
        return bool(order["checkout_data"].get("qr_code") or
                    simulation_token_matches(order, order["checkout_data"].get("simulation_checkout_token")))

    def _require_simulation(self):
        if not getattr(self.settings, "simulation_enabled", False):
            raise BillingError("simulation_unavailable", "开发模拟支付未开放。", 404)

    @staticmethod
    def _simulation_payment(order):
        return {"order_id": str(order["id"]), "provider": order["provider"],
                "transaction_id": "dev-simulation-" + str(order["id"]),
                "status": "paid", "currency": "CNY", "amount_fen": order["amount_fen"],
                "merchant_id": SIMULATION_MERCHANT_ID, "app_id": SIMULATION_APP_ID}

    def simulation_checkout(self, token):
        self._require_simulation()
        return simulation_checkout_view(self.repository.simulation_checkout(token))

    def confirm_simulation_checkout(self, token):
        self._require_simulation()
        order = self.repository.simulation_checkout(token)
        return simulation_checkout_view(self.repository.fulfill(self._simulation_payment(order),
            simulation_owner=order["owner_user_id"], simulation_token=token))

    def simulate_payment(self, owner, order_id):
        self._require_simulation()
        order = self.repository.get_order(owner, order_id)
        if not is_simulated_order(order):
            raise BillingError("payment_mode_mismatch", "该订单不支持开发模拟支付。", 400)
        return public_order(self.repository.fulfill(self._simulation_payment(order), simulation_owner=owner))

    def get_order(self, owner, order_id):
        order = self.repository.get_order(owner, order_id)
        self.reconcile_order(order)
        return public_order(self.repository.get_order(owner, order_id))

    def reconcile_order(self, order):
        if order["status"] != "pending":
            return
        with self._reconciliation_admission() as allowed:
            if allowed:
                self._reconcile_order(order)

    def _reconcile_order(self, order):
        if (getattr(self.settings, "simulation_enabled", False) and is_simulated_order(order)
                and not self._checkout_ready(order) and order["expires_at"] > datetime.now(timezone.utc)):
            # Owner refresh upgrades old pending simulation orders even when a
            # background check recently claimed them. Admission covers this write.
            checkout = self.gateway.create_order(order["provider"], UUID(str(order["id"])).hex,
                order["amount_fen"], "NexPoly 开发模拟会员（一个月）", order["expires_at"])
            self.repository.save_checkout(order["id"], checkout)
        claimed = self.repository.claim_check(order["id"])
        if claimed is None:
            return
        if not self._merchant_matches(claimed):
            logger.warning('billing_order_merchant_changed')
            return
        provider, external_id = claimed["provider"], UUID(str(claimed["id"])).hex
        try:
            payment = self.gateway.query_order(provider, external_id,
                                               expected_amount_fen=claimed["amount_fen"])
            if payment["status"] == "paid":
                self.repository.fulfill(payment)
            elif payment["status"] == "closed":
                self.repository.close_confirmed(claimed["id"])
            elif claimed["expires_at"] <= datetime.now(timezone.utc):
                confirmed = self.gateway.close_order(provider, external_id)
                # Only a verified successful close is authoritative. If payment won
                # the race, the provider rejects close and the next query recovers it.
                if confirmed["status"] == "closed":
                    self.repository.close_confirmed(claimed["id"])
            elif not self._checkout_ready(claimed) and self.settings.enabled:
                # A lost create response is retried with the same merchant order
                # identity, so it cannot create a second bill or grant access.
                checkout = self.gateway.create_order(provider, external_id, claimed['amount_fen'],
                    'NexPoly 科研会员（一个月）', claimed['expires_at'])
                self.repository.save_checkout(claimed['id'], checkout)
        except (PaymentError, BillingError):
            logger.warning("billing_reconciliation_pending", extra={"provider": provider})

    def reconcile_pending(self, stopping=lambda: False):
        for order in self.repository.pending_orders():
            if stopping():
                break
            self.reconcile_order(order)

    def notification(self, provider, headers, body):
        payment = self.gateway.verify_notification(provider, headers, body)
        if payment["status"] == "paid":
            self.repository.fulfill(payment)

