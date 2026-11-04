"""Owner-scoped orders and atomic, idempotent membership fulfillment."""
from __future__ import annotations

import calendar
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg.errors import UndefinedTable

from app.auth.context import service_context
from app.postgres_database import postgres_connection
from .development import (SIMULATION_APP_ID, SIMULATION_MERCHANT_ID, is_simulated_order,
                          simulation_token_matches, simulation_token_order_id)


class BillingError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def next_month(at: datetime) -> datetime:
    """One calendar month, clamping the day at the end of a shorter month."""
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo("Asia/Shanghai"))
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return local.replace(year=year, month=month,
                         day=min(local.day, calendar.monthrange(year, month)[1])).astimezone(timezone.utc)


def public_order(row):
    checkout = row.get("checkout_data") or {}
    token = checkout.get("simulation_checkout_token")
    return {**{key: row.get(key) for key in (
        "id", "provider", "amount_fen", "currency", "status", "expires_at",
        "created_at", "paid_at", "access_ends_at")},
        "simulation": is_simulated_order(row),
        "qr_code": checkout.get("qr_code") if row["status"] == "pending" else None,
        "simulation_checkout_token": token if (row["status"] == "pending"
            and row["expires_at"] > datetime.now(timezone.utc) and simulation_token_matches(row, token)) else None}


def simulation_checkout_view(row):
    """The bearer may see purchase facts, never account or full order identity."""
    return {"simulation": True, "period": "month", "order_suffix": str(row["id"])[-6:],
            "server_time": datetime.now(timezone.utc),
            **{key: row.get(key) for key in (
                "provider", "amount_fen", "currency", "status", "expires_at", "access_ends_at")}}


class BillingRepository:
    def __init__(self, auth_settings):
        self.settings = auth_settings

    @contextmanager
    def connection(self, *, service=False):
        try:
            if service:
                with service_context(self.settings), postgres_connection(self.settings.application_dsn) as connection:
                    yield connection
            else:
                with postgres_connection(self.settings.application_dsn) as connection:
                    yield connection
        except UndefinedTable:
            # Disabled billing may coexist with an older, supported database.
            # Fail closed with a useful response while migration is pending.
            raise BillingError('billing_not_ready', '会员订阅服务尚未开放。', 503) from None

    def assert_ready(self):
        from app.auth.isolation_ledger import validate_application_migration_ledger
        from app.services.billing_schema import validate_billing_schema
        with self.connection(service=True) as connection:
            validate_application_migration_ledger(connection)
            validate_billing_schema(connection)

    def subscription(self, owner):
        with self.connection() as connection:
            row = connection.execute("""SELECT a.legacy_member,a.override_tier,s.paid_through,
                clock_timestamp() AS server_time FROM billing.accounts a
                LEFT JOIN billing.subscriptions s USING(owner_user_id) WHERE a.owner_user_id=%s""",
                (owner,)).fetchone()
        if row is None:
            raise BillingError("billing_account_unavailable", "会员账户尚未准备好。", 503)
        if row["override_tier"] is not None:
            tier, source, expiry = row["override_tier"], "admin", None
        elif row["legacy_member"]:
            tier, source, expiry = "member", "legacy", None
        else:
            expiry = row["paid_through"]
            tier = "member" if expiry is not None and expiry > row["server_time"] else "free"
            source = "subscription" if expiry is not None else "none"
        return {"account_type": tier, "source": source, "expires_at": expiry,
                "server_time": row["server_time"], "purchase_blocked": row["override_tier"] is not None or row["legacy_member"]}

    @staticmethod
    def _lock_account(connection, owner):
        # Keep the existing account-first lock order used by task start and downgrade.
        user = connection.execute("SELECT user_id,status,is_system FROM auth.users WHERE user_id=%s FOR UPDATE", (owner,)).fetchone()
        if user is None or user["is_system"] or user["status"] != "active":
            raise BillingError("account_unavailable", "当前账号不能购买会员。", 403)
        account = connection.execute("SELECT * FROM billing.accounts WHERE owner_user_id=%s FOR UPDATE", (owner,)).fetchone()
        if account is None:
            raise BillingError("billing_account_unavailable", "会员账户尚未准备好。", 503)
        return account

    def create_order(self, owner, provider, amount_fen, request_key, *, merchant_id, app_id):
        with self.connection(service=True) as connection:
            account = self._lock_account(connection, owner)
            previous = connection.execute("SELECT * FROM billing.orders WHERE owner_user_id=%s AND request_key=%s", (owner, request_key)).fetchone()
            if previous is not None:
                if previous["provider"] != provider:
                    raise BillingError("order_identity_conflict", "该购买请求已用于另一种支付方式。")
                return previous
            if account["legacy_member"] or account["override_tier"] is not None:
                raise BillingError("purchase_not_required", "当前账号的权限由既有授权管理，无需购买。", 403)
            count = connection.execute("""SELECT count(*) AS count FROM billing.orders
                WHERE owner_user_id=%s AND created_at>clock_timestamp()-interval '1 day'""", (owner,)).fetchone()["count"]
            if count >= 30:
                raise BillingError("order_rate_limited", "创建订单过于频繁，请稍后再试。", 429)
            return connection.execute("""INSERT INTO billing.orders
                (id,owner_user_id,provider,amount_fen,currency,months,status,request_key,merchant_id,app_id,created_at,expires_at,checkout_data)
                VALUES(%s,%s,%s,%s,'CNY',1,'pending',%s,%s,%s,clock_timestamp(),clock_timestamp()+interval '15 minutes','{}'::jsonb)
                RETURNING *""", (uuid4(), owner, provider, amount_fen, request_key, merchant_id, app_id)).fetchone()

    def save_checkout(self, order_id, checkout):
        # Persist only the presentation payload, never raw provider responses.
        if checkout.get("simulation") is True:
            token = checkout.get("simulation_checkout_token")
            if simulation_token_order_id(token) != UUID(str(order_id)):
                raise BillingError("invalid_simulation_checkout", "模拟支付链接无效。", 400)
            payload = {"simulation": True, "simulation_checkout_token": token}
            with self.connection(service=True) as connection:
                # Compare-and-set lets old pending simulation orders gain a QR,
                # while concurrent refreshes cannot rotate a published token.
                connection.execute("""UPDATE billing.orders SET checkout_data=%s
                    WHERE id=%s AND status='pending' AND expires_at>clock_timestamp()
                    AND merchant_id=%s AND app_id=%s
                    AND checkout_data IN ('{}'::jsonb,'{"simulation":true}'::jsonb)""",
                    (Jsonb(payload), order_id, SIMULATION_MERCHANT_ID, SIMULATION_APP_ID))
            return
        payload = {"qr_code": checkout["qr_code"]}
        with self.connection(service=True) as connection:
            connection.execute("""UPDATE billing.orders SET checkout_data=%s
                WHERE id=%s AND status='pending' AND checkout_data='{}'::jsonb""",
                (Jsonb(payload), order_id))

    @staticmethod
    def _validate_simulation_token(order, token, now):
        if order is None or not simulation_token_matches(order, token):
            raise BillingError("simulation_checkout_unavailable", "模拟支付链接无效或不可用。", 404)
        if order["expires_at"] <= now:
            raise BillingError("simulation_checkout_expired", "模拟支付链接已过期，请在电脑上重新下单。", 410)

    def simulation_checkout(self, token):
        """Read only: inspection never reconciles, rotates a token or pays."""
        order_id = simulation_token_order_id(token)
        if order_id is None:
            raise BillingError("simulation_checkout_unavailable", "模拟支付链接无效或不可用。", 404)
        with self.connection(service=True) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            order = connection.execute("""SELECT o.*,clock_timestamp() AS server_time
                FROM billing.orders o JOIN auth.users u ON u.user_id=o.owner_user_id
                WHERE o.id=%s AND u.status='active' AND NOT u.is_system""", (order_id,)).fetchone()
            now = order["server_time"] if order is not None else datetime.now(timezone.utc)
            self._validate_simulation_token(order, token, now)
            return order

    def get_order(self, owner, order_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM billing.orders WHERE id=%s AND owner_user_id=%s", (order_id, owner)).fetchone()
        if row is None:
            raise BillingError("order_not_found", "订单不存在。", 404)
        return row

    def list_orders(self, owner, limit=25):
        with self.connection() as connection:
            return connection.execute("SELECT * FROM billing.orders WHERE owner_user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s", (owner, limit)).fetchall()

    def claim_check(self, order_id):
        """Database throttle also covers simultaneous browser tabs and API instances."""
        with self.connection(service=True) as connection:
            return connection.execute("""UPDATE billing.orders SET last_checked_at=clock_timestamp()
                WHERE id=%s AND status='pending' AND
                (last_checked_at IS NULL OR last_checked_at<clock_timestamp()-interval '10 seconds') RETURNING *""", (order_id,)).fetchone()

    def pending_orders(self, limit=25):
        with self.connection(service=True) as connection:
            return connection.execute("""SELECT * FROM billing.orders WHERE status='pending' AND
                (last_checked_at IS NULL OR last_checked_at<clock_timestamp()-interval '10 seconds')
                ORDER BY last_checked_at NULLS FIRST,created_at LIMIT %s""", (limit,)).fetchall()

    def close_confirmed(self, order_id):
        with self.connection(service=True) as connection:
            connection.execute("UPDATE billing.orders SET status='closed' WHERE id=%s AND status='pending'", (order_id,))

    def fulfill(self, payment, *, simulation_owner=None, simulation_token=None):
        """Verified payment, or an explicitly owner-authorized development action."""
        if simulation_token is not None and simulation_owner is None:
            raise BillingError("simulation_checkout_unavailable", "模拟支付链接无效或不可用。", 404)
        try:
            order_id = str(UUID(str(payment["order_id"])))
            provider, payment_id = payment["provider"], payment["transaction_id"]
            amount = payment["amount_fen"]
            if (payment["status"] != "paid" or payment["currency"] != "CNY"
                    or type(amount) is not int or amount <= 0
                    or not isinstance(payment_id, str) or not 1 <= len(payment_id) <= 128):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BillingError("invalid_payment", "支付结果校验失败。", 400) from None
        event_key = "paid:" + payment_id
        facts = {"order_id": order_id, "provider": provider, "transaction_id": payment_id,
                 "amount_fen": amount, "currency": "CNY"}
        digest = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.connection(service=True) as connection:
            original = connection.execute("SELECT owner_user_id FROM billing.orders WHERE id=%s", (order_id,)).fetchone()
            if original is None:
                raise BillingError("order_not_found", "订单不存在。", 404)
            owner = original["owner_user_id"]
            if simulation_owner is not None and str(owner) != str(simulation_owner):
                raise BillingError("order_not_found", "订单不存在。", 404)
            # Payment is recorded even if an administrator disabled the account after checkout.
            if simulation_owner is not None:
                self._lock_account(connection, owner)
            else:
                connection.execute("SELECT user_id FROM auth.users WHERE user_id=%s FOR UPDATE", (owner,)).fetchone()
                connection.execute("SELECT owner_user_id FROM billing.accounts WHERE owner_user_id=%s FOR UPDATE", (owner,)).fetchone()
            order = connection.execute("SELECT * FROM billing.orders WHERE id=%s FOR UPDATE", (order_id,)).fetchone()
            now = connection.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
            if simulation_token is not None:
                # Recheck after account + order locks, including paid replays.
                # A slow confirmation cannot outlive its bearer capability.
                self._validate_simulation_token(order, simulation_token, now)
            if is_simulated_order(order) != (simulation_owner is not None):
                raise BillingError("payment_mode_mismatch", "该订单不支持此支付方式。", 400)
            if (order["provider"] != provider or order["amount_fen"] != amount or order["currency"] != "CNY"
                    or payment.get("merchant_id") != order["merchant_id"] or payment.get("app_id") != order["app_id"]):
                raise BillingError("payment_mismatch", "支付结果与订单不一致。", 400)
            event = connection.execute("SELECT order_id,payload_digest FROM billing.payment_events WHERE provider=%s AND event_key=%s", (provider, event_key)).fetchone()
            if event is not None and (str(event["order_id"]) != order_id or event["payload_digest"] != digest):
                raise BillingError("payment_conflict", "支付流水已关联其他订单。")
            if order["status"] == "paid":
                if order["payment_id"] != payment_id:
                    raise BillingError("payment_conflict", "订单支付流水不一致。")
                return order
            # A late, genuine successful payment still fulfills a locally closed order.
            if simulation_owner is not None and (order["status"] != "pending" or order["expires_at"] <= now):
                raise BillingError("simulation_order_expired", "模拟订单已失效，请重新下单。")
            prior = connection.execute("SELECT paid_through FROM billing.subscriptions WHERE owner_user_id=%s FOR UPDATE", (owner,)).fetchone()
            start = max(now, prior["paid_through"]) if prior else now
            end = next_month(start)
            connection.execute("""INSERT INTO billing.payment_events(provider,event_key,order_id,payload_digest)
                VALUES(%s,%s,%s,%s)""", (provider, event_key, order_id, digest))
            connection.execute("""INSERT INTO billing.subscriptions(owner_user_id,paid_through,updated_at)
                VALUES(%s,%s,%s) ON CONFLICT(owner_user_id) DO UPDATE
                SET paid_through=excluded.paid_through,updated_at=excluded.updated_at""", (owner, end, now))
            return connection.execute("""UPDATE billing.orders SET status='paid',payment_id=%s,paid_at=%s,
                access_starts_at=%s,access_ends_at=%s WHERE id=%s RETURNING *""", (payment_id, now, start, end, order_id)).fetchone()
