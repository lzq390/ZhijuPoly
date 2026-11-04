"""Verified WeChat Native APIv3 and Alipay RSA2 direct-merchant payments.

This module never grants membership. The caller must atomically match the verified
provider, order, amount, currency and merchant to its immutable local order before
recording a payment. Only that transaction may acknowledge a notification.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qsl, quote, urlencode

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .settings import BillingConfigurationError, BillingSettings

_MAX_BODY = 1024 * 1024
_CHINA = timezone(timedelta(hours=8))
_WECHAT_URL = "https://api.mch.weixin.qq.com"
_ALIPAY_URL = "https://openapi.alipay.com/gateway.do"


class PaymentError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def _pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs_unique, parse_constant=_reject_constant)


def _json(raw: bytes):
    try:
        if len(raw) > _MAX_BODY:
            raise ValueError("oversized response")
        result = _DECODER.decode(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise PaymentError("payment_payload_invalid") from None


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _bounded_text(value, *, maximum=128):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise PaymentError("payment_payload_invalid")
    return value


def _order_id(value):
    # One identifier format fits both platforms; callers should use UUID.hex.
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{32}", value) is None:
        raise PaymentError("payment_order_id_invalid")
    return value


def _fen(value):
    if type(value) is not int or not 1 <= value <= 100_000_000:
        raise PaymentError("payment_amount_invalid")
    return value


def _yuan_to_fen(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value) is None:
        raise PaymentError("payment_amount_invalid")
    try:
        amount = Decimal(value) * 100
        return _fen(int(amount))
    except (ValueError, InvalidOperation, OverflowError):
        raise PaymentError("payment_amount_invalid") from None


def _b64(value):
    try:
        return base64.b64decode(_bounded_text(value, maximum=_MAX_BODY), validate=True)
    except (ValueError, binascii.Error):
        raise PaymentError("payment_signature_invalid") from None


def _sign(key, message: bytes):
    return base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")


def _verify(key, signature, message: bytes):
    try:
        key.verify(_b64(signature), message, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError):
        raise PaymentError("payment_signature_invalid") from None


def _key_file(path, *, private=False, serial=None):
    try:
        content = Path(path).read_bytes()
        if not 0 < len(content) <= 65536:
            raise ValueError("key size")
        if private:
            key = serialization.load_pem_private_key(content, password=None)
        elif b"-----BEGIN CERTIFICATE-----" in content:
            certificate = x509.load_pem_x509_certificate(content)
            if serial is None or format(certificate.serial_number, "X") != serial.upper():
                raise ValueError("certificate identity")
            now = datetime.now(timezone.utc)
            if not (certificate.not_valid_before.replace(tzinfo=timezone.utc) <= now
                    <= certificate.not_valid_after.replace(tzinfo=timezone.utc)):
                raise ValueError("certificate validity")
            key = certificate.public_key()
        else:
            key = serialization.load_pem_public_key(content)
        expected = rsa.RSAPrivateKey if private else rsa.RSAPublicKey
        if not isinstance(key, expected) or key.key_size < 2048:
            raise ValueError("RSA 2048 required")
        return key
    except (OSError, ValueError, TypeError):
        raise BillingConfigurationError("Invalid payment RSA key or certificate") from None


def _alipay_canonical(values, *, notification=False):
    excluded = {"sign", "sign_type"} if notification else {"sign"}
    return "&".join(f"{key}={values[key]}" for key in sorted(values)
                    if key not in excluded and values[key] not in {None, ""}).encode("utf-8")


def _alipay_signed_slice(raw: bytes, response_key: str):
    """Retain the exact signed JSON value; serializing a parsed dict changes bytes."""
    envelope = _json(raw)
    if response_key not in envelope or not isinstance(envelope[response_key], dict):
        raise PaymentError("payment_response_invalid")
    text = raw.decode("utf-8")
    offset = text.index("{") + 1
    try:
        while True:
            while text[offset].isspace():
                offset += 1
            key, offset = _DECODER.raw_decode(text, offset)
            while text[offset].isspace():
                offset += 1
            if text[offset] != ":":
                raise ValueError("colon required")
            offset += 1
            while text[offset].isspace():
                offset += 1
            start = offset
            _, offset = _DECODER.raw_decode(text, offset)
            if key == response_key:
                return envelope, text[start:offset].encode("utf-8")
            while text[offset].isspace():
                offset += 1
            if text[offset] != ",":
                raise ValueError("response absent")
            offset += 1
    except (ValueError, IndexError):
        raise PaymentError("payment_response_invalid") from None


class PaymentGateway:
    def __init__(self, settings: BillingSettings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self._private_keys, self._public_keys = {}, {}
        self._wechat_api_key = None
        if settings.wechat:
            config = settings.wechat
            self._private_keys["wechat"] = _key_file(config.private_key_file, private=True)
            self._public_keys["wechat"] = _key_file(config.platform_public_key_file, serial=config.platform_key_id)
            try:
                secret = Path(config.api_v3_key_file).read_bytes().rstrip(b"\r\n")
                if len(secret) != 32 or not secret.isascii():
                    raise ValueError("APIv3 key")
                self._wechat_api_key = secret
            except (OSError, ValueError):
                raise BillingConfigurationError("Invalid WeChat APIv3 key file") from None
        if settings.alipay:
            self._private_keys["alipay"] = _key_file(settings.alipay.private_key_file, private=True)
            self._public_keys["alipay"] = _key_file(settings.alipay.public_key_file)

    def _configured(self, provider):
        if provider not in self.settings.available_providers:
            raise PaymentError("payment_provider_unavailable")
        return getattr(self.settings, provider)

    def _http(self, method, url, **kwargs):
        try:
            # Fixed official hosts, TLS validation, no redirects or environment proxy.
            with httpx.Client(transport=self.transport, timeout=httpx.Timeout(10, connect=3),
                              follow_redirects=False, trust_env=False) as client:
                with client.stream(method, url, **kwargs) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_BODY:
                            raise PaymentError("payment_payload_invalid")
                    return response.status_code, dict(response.headers), bytes(body)
        except httpx.HTTPError:
            # The provider might have created/paid the order despite a timeout.
            # Reconcile the same local order instead of creating a new identity.
            raise PaymentError("payment_provider_unavailable", retryable=True) from None

    def _wechat_verify(self, headers, raw_body):
        config = self._configured("wechat")
        values = {str(key).lower(): value for key, value in headers.items()}
        if values.get("wechatpay-serial") != config.platform_key_id:
            raise PaymentError("payment_signing_key_unknown")
        stamp = values.get("wechatpay-timestamp", "")
        if not isinstance(stamp, str) or re.fullmatch(r"[0-9]{1,12}", stamp) is None:
            raise PaymentError("payment_signature_invalid")
        if abs(time.time() - int(stamp)) > 300:
            raise PaymentError("payment_signature_expired")
        nonce = _bounded_text(values.get("wechatpay-nonce"), maximum=128)
        message = stamp.encode() + b"\n" + nonce.encode() + b"\n" + raw_body + b"\n"
        _verify(self._public_keys["wechat"], values.get("wechatpay-signature"), message)

    def _wechat_request(self, method, path, payload=None):
        config = self._configured("wechat")
        body = _dump(payload) if payload is not None else b""
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        message = f"{method}\n{path}\n{stamp}\n{nonce}\n".encode() + body + b"\n"
        signature = _sign(self._private_keys["wechat"], message)
        authorization = (f'WECHATPAY2-SHA256-RSA2048 mchid="{config.merchant_id}",nonce_str="{nonce}",'
                         f'timestamp="{stamp}",serial_no="{config.merchant_serial}",signature="{signature}"')
        status, headers, raw = self._http(method, _WECHAT_URL + path, content=body,
            headers={"Authorization": authorization, "Accept": "application/json",
                     "Content-Type": "application/json", "Wechatpay-Serial": config.platform_key_id})
        self._wechat_verify(headers, raw)
        result = _json(raw) if raw else {}
        if not 200 <= status < 300:
            if status == 404 and result.get("code") == "ORDER_NOT_EXIST":
                raise PaymentError("payment_order_not_found")
            raise PaymentError("payment_provider_rejected", retryable=status >= 500)
        return result

    def _alipay_request(self, method, payload, *, notify=False):
        config = self._configured("alipay")
        params = {"app_id": config.app_id, "method": method, "format": "JSON", "charset": "utf-8",
                  "sign_type": "RSA2", "timestamp": datetime.now(_CHINA).strftime("%Y-%m-%d %H:%M:%S"),
                  "version": "1.0", "biz_content": _dump(payload).decode("utf-8")}
        if notify:
            params["notify_url"] = config.notify_url
        params["sign"] = _sign(self._private_keys["alipay"], _alipay_canonical(params))
        status, _, raw = self._http("POST", _ALIPAY_URL, data=params)
        if status != 200:
            raise PaymentError("payment_provider_unavailable", retryable=True)
        response_key = method.replace(".", "_") + "_response"
        envelope, signed = _alipay_signed_slice(raw, response_key)
        _verify(self._public_keys["alipay"], envelope.get("sign"), signed)
        result = envelope[response_key]
        if result.get("code") != "10000":
            if result.get("sub_code") == "ACQ.TRADE_NOT_EXIST":
                raise PaymentError("payment_order_not_found")
            raise PaymentError("payment_provider_rejected", retryable=result.get("code") == "20000")
        return result

    @staticmethod
    def _match(event, *, expected_order_id=None, expected_amount_fen=None):
        if expected_order_id is not None and event["order_id"] != expected_order_id:
            raise PaymentError("payment_order_mismatch")
        if expected_amount_fen is not None and event["amount_fen"] is not None:
            if event["amount_fen"] != _fen(expected_amount_fen):
                raise PaymentError("payment_amount_mismatch")
        return event

    def _base(self, provider, order_id, status, *, amount=None, transaction_id=None):
        config = self._configured(provider)
        return {"provider": provider, "order_id": _order_id(order_id), "status": status,
                "transaction_id": transaction_id, "amount_fen": amount, "currency": "CNY",
                "merchant_id": config.merchant_id if provider == "wechat" else config.seller_id,
                "app_id": config.app_id}

    def _wechat_event(self, data):
        config = self._configured("wechat")
        if data.get("mchid") != config.merchant_id or data.get("appid") != config.app_id:
            raise PaymentError("payment_merchant_mismatch")
        if data.get("trade_type") not in {None, "NATIVE"}:
            raise PaymentError("payment_trade_type_invalid")
        state = data.get("trade_state")
        mapping = {"SUCCESS": "paid", "NOTPAY": "pending", "USERPAYING": "pending", "CLOSED": "closed",
                   "REVOKED": "closed", "PAYERROR": "closed"}
        if state not in mapping:
            # REFUND needs a separate reversal workflow; it must never grant membership.
            raise PaymentError("payment_status_unsupported")
        amount = data.get("amount")
        total = None
        if amount is not None:
            if not isinstance(amount, dict) or amount.get("currency") != "CNY":
                raise PaymentError("payment_currency_mismatch")
            total = _fen(amount.get("total"))
        transaction = data.get("transaction_id")
        if mapping[state] == "paid":
            _fen(total)
            transaction = _bounded_text(transaction)
        event = self._base("wechat", data.get("out_trade_no"), mapping[state], amount=total, transaction_id=transaction)
        event["paid_at"] = data.get("success_time")
        return event

    def _alipay_event(self, data, *, notification=False):
        config = self._configured("alipay")
        if notification or "app_id" in data:
            if data.get("app_id") != config.app_id:
                raise PaymentError("payment_merchant_mismatch")
        if notification or "seller_id" in data:
            if data.get("seller_id") != config.seller_id:
                raise PaymentError("payment_merchant_mismatch")
        # Query responses do not always echo seller/app. The verified response is
        # bound to our authenticated HTTPS request; callbacks require both fields.
        if data.get("trans_currency", "CNY") != "CNY":
            raise PaymentError("payment_currency_mismatch")
        mapping = {"TRADE_SUCCESS": "paid", "TRADE_FINISHED": "paid", "WAIT_BUYER_PAY": "pending", "TRADE_CLOSED": "closed"}
        status = mapping.get(data.get("trade_status"))
        if status is None:
            raise PaymentError("payment_status_unsupported")
        amount = _yuan_to_fen(data["total_amount"]) if "total_amount" in data else None
        transaction = data.get("trade_no")
        if status == "paid":
            _fen(amount)
            transaction = _bounded_text(transaction)
        event = self._base("alipay", data.get("out_trade_no"), status, amount=amount, transaction_id=transaction)
        paid_at = data.get("gmt_payment") if notification else data.get("send_pay_date")
        if paid_at:
            try:
                paid_at = datetime.strptime(paid_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CHINA).isoformat()
            except (ValueError, TypeError):
                raise PaymentError("payment_payload_invalid") from None
        event["paid_at"] = paid_at
        return event

    def create_order(self, provider, order_id, amount_fen, description, expires_at):
        config = self._configured(provider)
        if not self.settings.enabled:
            raise PaymentError("payment_purchasing_disabled")
        _order_id(order_id)
        _fen(amount_fen)
        _bounded_text(description, maximum=127)
        if (not isinstance(expires_at, datetime) or expires_at.tzinfo is None
                or not 60 <= (expires_at - datetime.now(timezone.utc)).total_seconds() <= 7 * 86400):
            raise PaymentError("payment_expiry_invalid")
        if provider == "wechat":
            response = self._wechat_request("POST", "/v3/pay/transactions/native", {
                "appid": config.app_id, "mchid": config.merchant_id, "description": description,
                "out_trade_no": order_id, "notify_url": config.notify_url,
                "time_expire": expires_at.isoformat(timespec="seconds"),
                "amount": {"total": amount_fen, "currency": "CNY"}})
            qr = response.get("code_url")
        else:
            response = self._alipay_request("alipay.trade.precreate", {
                "out_trade_no": order_id, "total_amount": f"{amount_fen // 100}.{amount_fen % 100:02d}",
                "subject": description, "seller_id": config.seller_id,
                "time_expire": expires_at.astimezone(_CHINA).strftime("%Y-%m-%d %H:%M:%S")}, notify=True)
            if response.get("out_trade_no") != order_id:
                raise PaymentError("payment_order_mismatch")
            qr = response.get("qr_code")
        _bounded_text(qr, maximum=4096)
        return {**self._base(provider, order_id, "pending", amount=amount_fen), "qr_code": qr,
                "expires_at": expires_at.isoformat()}

    def query_order(self, provider, order_id, *, expected_amount_fen=None):
        config = self._configured(provider)
        _order_id(order_id)
        try:
            if provider == "wechat":
                path = f"/v3/pay/transactions/out-trade-no/{quote(order_id, safe='')}?{urlencode({'mchid': config.merchant_id})}"
                event = self._wechat_event(self._wechat_request("GET", path))
            else:
                event = self._alipay_event(self._alipay_request("alipay.trade.query", {"out_trade_no": order_id}))
        except PaymentError as exc:
            if exc.code != "payment_order_not_found":
                raise
            return self._base(provider, order_id, "pending")
        return self._match(event, expected_order_id=order_id, expected_amount_fen=expected_amount_fen)

    def close_order(self, provider, order_id):
        config = self._configured(provider)
        _order_id(order_id)
        try:
            if provider == "wechat":
                self._wechat_request("POST", f"/v3/pay/transactions/out-trade-no/{quote(order_id, safe='')}/close",
                                     {"mchid": config.merchant_id})
            else:
                response = self._alipay_request("alipay.trade.close", {"out_trade_no": order_id})
                if response.get("out_trade_no") != order_id:
                    raise PaymentError("payment_order_mismatch")
        except PaymentError as exc:
            # Only an authenticated, signed not-found response is conclusive.
            # The caller closes expired local orders and must never recreate them.
            if exc.code != "payment_order_not_found":
                raise
        return self._base(provider, order_id, "closed")

    def verify_notification(self, provider, headers, raw_body, *, expected_order_id=None, expected_amount_fen=None):
        self._configured(provider)
        if not isinstance(raw_body, bytes) or len(raw_body) > _MAX_BODY:
            raise PaymentError("payment_payload_invalid")
        if provider == "wechat":
            self._wechat_verify(headers, raw_body)
            notification = _json(raw_body)
            if notification.get("event_type") != "TRANSACTION.SUCCESS" or notification.get("resource_type") != "encrypt-resource":
                raise PaymentError("payment_notification_unsupported")
            resource = notification.get("resource")
            if (not isinstance(resource, dict) or resource.get("algorithm") != "AEAD_AES_256_GCM"
                    or resource.get("original_type") != "transaction"):
                raise PaymentError("payment_payload_invalid")
            try:
                nonce = _bounded_text(resource.get("nonce"), maximum=16).encode()
                associated = resource.get("associated_data", "")
                if not isinstance(associated, str) or len(associated) > 16:
                    raise ValueError("associated data")
                decrypted = AESGCM(self._wechat_api_key).decrypt(nonce, _b64(resource.get("ciphertext")), associated.encode())
            except (InvalidTag, ValueError, TypeError):
                raise PaymentError("payment_decryption_failed") from None
            event = self._wechat_event(_json(decrypted))
            if event["status"] != "paid":
                raise PaymentError("payment_notification_unsupported")
            event["event_id"] = _bounded_text(notification.get("id"))
        else:
            try:
                pairs = parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                                  encoding="utf-8", errors="strict", max_num_fields=128)
                data = _pairs_unique(pairs)
            except (ValueError, UnicodeError):
                raise PaymentError("payment_payload_invalid") from None
            if data.get("sign_type") != "RSA2" or data.get("charset", "utf-8").lower() != "utf-8":
                raise PaymentError("payment_signature_invalid")
            _verify(self._public_keys["alipay"], data.get("sign"), _alipay_canonical(data, notification=True))
            if data.get("notify_type") != "trade_status_sync":
                raise PaymentError("payment_notification_unsupported")
            event = self._alipay_event(data, notification=True)
            event["event_id"] = _bounded_text(data.get("notify_id"))
        return self._match(event, expected_order_id=expected_order_id, expected_amount_fen=expected_amount_fen)
