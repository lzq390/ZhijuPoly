"""Payment protocol tests use fresh synthetic keys and an in-process transport only."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.billing.providers import PaymentError, PaymentGateway
from app.billing.settings import BillingConfigurationError, BillingSettings

ORDER = "22cb40bbaefb423b98bf211946fa1ca5"


def sign(key, data):
    return base64.b64encode(key.sign(data, padding.PKCS1v15(), hashes.SHA256())).decode()


def verify(key, signature, data):
    key.public_key().verify(base64.b64decode(signature), data, padding.PKCS1v15(), hashes.SHA256())


def canonical(params, *, notification=False):
    ignored = {"sign", "sign_type"} if notification else {"sign"}
    return "&".join(f"{key}={params[key]}" for key in sorted(params)
                    if key not in ignored and params[key] != "").encode()


@pytest.fixture(scope="module")
def keys():
    return {name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
            for name in ("merchant", "wechat", "alipay")}


@pytest.fixture
def configuration(tmp_path, keys):
    private_path = tmp_path / "merchant.pem"
    private_path.write_bytes(keys["merchant"].private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for provider in ("wechat", "alipay"):
        (tmp_path / f"{provider}.pem").write_bytes(keys[provider].public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    (tmp_path / "api-key").write_bytes(b"0123456789abcdef0123456789abcdef\n")
    env = {
        "NEXPOLY_BILLING_ENABLED": "true",
        "NEXPOLY_BILLING_MONTHLY_PRICE_FEN": "12345",
        "NEXPOLY_BILLING_PROVIDERS": "wechat,alipay",
        "NEXPOLY_WECHAT_APP_ID": "wxsynthetic123",
        "NEXPOLY_WECHAT_MERCHANT_ID": "1900000001",
        "NEXPOLY_WECHAT_MERCHANT_SERIAL": "ABCDEF123",
        "NEXPOLY_WECHAT_PRIVATE_KEY_FILE": str(private_path),
        "NEXPOLY_WECHAT_API_V3_KEY_FILE": str(tmp_path / "api-key"),
        "NEXPOLY_WECHAT_PLATFORM_KEY_ID": "PUB_KEY_ID_123456",
        "NEXPOLY_WECHAT_PLATFORM_PUBLIC_KEY_FILE": str(tmp_path / "wechat.pem"),
        "NEXPOLY_WECHAT_NOTIFY_URL": "https://nexpoly.example/api/v1/billing/notify/wechat",
        "NEXPOLY_ALIPAY_APP_ID": "2026000000000001",
        "NEXPOLY_ALIPAY_SELLER_ID": "2088000000000001",
        "NEXPOLY_ALIPAY_PRIVATE_KEY_FILE": str(private_path),
        "NEXPOLY_ALIPAY_PUBLIC_KEY_FILE": str(tmp_path / "alipay.pem"),
        "NEXPOLY_ALIPAY_NOTIFY_URL": "https://nexpoly.example/api/v1/billing/notify/alipay",
    }
    return BillingSettings.from_env(env), env


def wechat_headers(keys, raw, *, timestamp=None):
    stamp, nonce = str(int(time.time()) if timestamp is None else timestamp), "synthetic-nonce"
    return {"Wechatpay-Serial": "PUB_KEY_ID_123456", "Wechatpay-Timestamp": stamp,
            "Wechatpay-Nonce": nonce,
            "Wechatpay-Signature": sign(keys["wechat"], f"{stamp}\n{nonce}\n".encode() + raw + b"\n")}


def wechat_response(keys, value=None, status=200):
    raw = json.dumps(value, ensure_ascii=False).encode() if value is not None else b""
    return httpx.Response(status, headers=wechat_headers(keys, raw), content=raw)


def wechat_transaction(**updates):
    return {"appid": "wxsynthetic123", "mchid": "1900000001", "out_trade_no": ORDER,
            "transaction_id": "wxtransaction123", "trade_type": "NATIVE", "trade_state": "SUCCESS",
            "amount": {"total": 12345, "payer_total": 12000, "currency": "CNY"},
            "success_time": "2026-11-02T12:34:56+08:00", **updates}


def wechat_notification(keys, transaction=None, *, ciphertext_mutation=False):
    transaction = wechat_transaction() if transaction is None else transaction
    nonce, aad = b"123456789012", b"transaction"
    ciphertext = AESGCM(b"0123456789abcdef0123456789abcdef").encrypt(
        nonce, json.dumps(transaction).encode(), aad)
    if ciphertext_mutation:
        ciphertext = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
    raw = json.dumps({"id": "wx-event-123", "event_type": "TRANSACTION.SUCCESS", "resource_type": "encrypt-resource",
        "resource": {"algorithm": "AEAD_AES_256_GCM", "original_type": "transaction", "nonce": nonce.decode(),
                     "associated_data": aad.decode(), "ciphertext": base64.b64encode(ciphertext).decode()}}).encode()
    return wechat_headers(keys, raw), raw


def alipay_response(keys, method, value, *, tamper=False):
    # Deliberate whitespace and escaped Unicode: re-serialization must fail verification.
    inner = json.dumps(value, ensure_ascii=True, indent=2)
    signature = sign(keys["alipay"], inner.encode())
    if tamper:
        inner = inner.replace('"123.45"', '"123.46"')
    raw = ('{ "sign" : ' + json.dumps(signature) + ', "' + method.replace(".", "_")
           + '_response" : ' + inner + ' }').encode()
    return httpx.Response(200, content=raw)


def alipay_transaction(**updates):
    return {"out_trade_no": ORDER, "trade_no": "alitransaction123", "trade_status": "TRADE_SUCCESS",
            "total_amount": "123.45", "app_id": "2026000000000001", "seller_id": "2088000000000001", **updates}


def alipay_notification(keys, **updates):
    values = {**alipay_transaction(), "notify_id": "ali-event-123", "notify_type": "trade_status_sync",
              "charset": "utf-8", "sign_type": "RSA2", "gmt_payment": "2026-11-02 12:34:56", **updates}
    values["sign"] = sign(keys["alipay"], canonical(values, notification=True))
    return urlencode(values).encode()


def test_configuration_disabled_has_no_invented_price_or_provider():
    settings = BillingSettings.from_env({})
    assert not settings.enabled and not settings.registration_enabled
    assert settings.monthly_price_fen is None
    assert settings.available_providers == ()
    assert PaymentGateway(settings).settings == settings


def test_registration_is_independent_of_purchasing():
    settings = BillingSettings.from_env({"NEXPOLY_BILLING_REGISTRATION_ENABLED": "true"})
    assert settings.registration_enabled and not settings.enabled


@pytest.mark.parametrize("price", ["", "0", "-1", "1.00", "100000001", "NaN"])
def test_configuration_requires_positive_integer_price(configuration, price):
    _, env = configuration
    env["NEXPOLY_BILLING_MONTHLY_PRICE_FEN"] = price
    with pytest.raises(BillingConfigurationError, match="MONTHLY_PRICE_FEN"):
        BillingSettings.from_env(env)


@pytest.mark.parametrize("url", ["http://example.com/notify", "https://user:secret@example.com/notify",
                                "https://example.com/notify#fragment", "https://example.com/notify?token=secret"])
def test_configuration_rejects_unsafe_callbacks(configuration, url):
    _, env = configuration
    env["NEXPOLY_WECHAT_NOTIFY_URL"] = url
    with pytest.raises(BillingConfigurationError, match="HTTPS") as failure:
        BillingSettings.from_env(env)
    assert url not in str(failure.value)


def test_configuration_requires_complete_named_channels(configuration):
    _, env = configuration
    del env["NEXPOLY_WECHAT_MERCHANT_ID"]
    with pytest.raises(BillingConfigurationError, match="MERCHANT_ID"):
        BillingSettings.from_env(env)


def test_wechat_create_query_close_verify_requests_and_responses(configuration, keys):
    settings, _ = configuration
    calls = []

    def transport(request):
        fields = dict(re.findall(r'(\w+)="([^"]+)"', request.headers["Authorization"]))
        signed = (request.method + "\n" + request.url.raw_path.decode() + "\n" + fields["timestamp"]
                  + "\n" + fields["nonce_str"] + "\n").encode() + request.content + b"\n"
        verify(keys["merchant"], fields["signature"], signed)
        assert fields["mchid"] == settings.wechat.merchant_id
        assert request.url.host == "api.mch.weixin.qq.com"
        calls.append(request)
        if request.url.path.endswith("/native"):
            payload = json.loads(request.content)
            assert payload["amount"] == {"total": 12345, "currency": "CNY"}
            assert payload["out_trade_no"] == ORDER
            assert payload["notify_url"] == settings.wechat.notify_url
            return wechat_response(keys, {"code_url": "weixin://wxpay/bizpayurl?pr=synthetic"})
        if request.method == "GET":
            assert request.url.params["mchid"] == settings.wechat.merchant_id
            return wechat_response(keys, wechat_transaction())
        assert request.url.path.endswith("/close")
        return wechat_response(keys, status=204)

    gateway = PaymentGateway(settings, transport=httpx.MockTransport(transport))
    created = gateway.create_order("wechat", ORDER, 12345, "NexPoly 月费会员", datetime.now(timezone.utc) + timedelta(minutes=15))
    assert created["status"] == "pending" and created["qr_code"].startswith("weixin://")
    paid = gateway.query_order("wechat", ORDER, expected_amount_fen=12345)
    assert paid["status"] == "paid" and paid["amount_fen"] == 12345
    assert gateway.close_order("wechat", ORDER)["status"] == "closed"
    assert len(calls) == 3


def test_wechat_real_signature_and_aes_notification(configuration, keys):
    settings, _ = configuration
    headers, body = wechat_notification(keys)
    event = PaymentGateway(settings).verify_notification("wechat", headers, body,
        expected_order_id=ORDER, expected_amount_fen=12345)
    assert event["event_id"] == "wx-event-123"
    assert event["transaction_id"] == "wxtransaction123" and event["status"] == "paid"
    assert event["amount_fen"] == 12345  # Order total, not discounted payer_total.


@pytest.mark.parametrize("mutation,code", [
    ({"mchid": "1900000002"}, "payment_merchant_mismatch"),
    ({"appid": "other-app"}, "payment_merchant_mismatch"),
    ({"amount": {"total": 12345, "currency": "USD"}}, "payment_currency_mismatch"),
    ({"amount": {"total": True, "currency": "CNY"}}, "payment_amount_invalid"),
    ({"amount": {"total": 12346, "currency": "CNY"}}, "payment_amount_mismatch"),
    ({"out_trade_no": "f" * 32}, "payment_order_mismatch"),
    ({"trade_state": "REFUND"}, "payment_status_unsupported"),
    ({"trade_type": "JSAPI"}, "payment_trade_type_invalid"),
])
def test_wechat_signed_but_wrong_business_facts_rejected(configuration, keys, mutation, code):
    headers, body = wechat_notification(keys, wechat_transaction(**mutation))
    with pytest.raises(PaymentError, match=code):
        PaymentGateway(configuration[0]).verify_notification("wechat", headers, body,
            expected_order_id=ORDER, expected_amount_fen=12345)


def test_wechat_tampering_unknown_key_stale_signature_and_invalid_gcm(configuration, keys):
    gateway = PaymentGateway(configuration[0])
    headers, body = wechat_notification(keys)
    with pytest.raises(PaymentError, match="signature_invalid"):
        gateway.verify_notification("wechat", headers, body + b" ")
    with pytest.raises(PaymentError, match="signing_key_unknown"):
        gateway.verify_notification("wechat", {**headers, "Wechatpay-Serial": "PUB_KEY_ID_999"}, body)
    with pytest.raises(PaymentError, match="signature_expired"):
        gateway.verify_notification("wechat", wechat_headers(keys, body, timestamp=int(time.time()) - 301), body)
    with pytest.raises(PaymentError, match="signature_invalid"):
        gateway.verify_notification("wechat", {**headers, "Wechatpay-Signature": "WECHATPAY/SIGNTEST/probe"}, body)
    headers, body = wechat_notification(keys, ciphertext_mutation=True)
    with pytest.raises(PaymentError, match="decryption_failed"):
        gateway.verify_notification("wechat", headers, body)


def test_wechat_unsigned_query_never_confirms_payment(configuration):
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=wechat_transaction())))
    with pytest.raises(PaymentError, match="signing_key_unknown"):
        gateway.query_order("wechat", ORDER)


def test_alipay_create_query_close_real_rsa2_and_original_response_bytes(configuration, keys):
    settings, _ = configuration
    methods = []

    def transport(request):
        params = dict(parse_qsl(request.content.decode()))
        verify(keys["merchant"], params["sign"], canonical(params))
        assert params["sign_type"] == "RSA2" and params["charset"] == "utf-8"
        payload = json.loads(params["biz_content"])
        assert payload["out_trade_no"] == ORDER
        method = params["method"]
        methods.append(method)
        if method == "alipay.trade.precreate":
            assert payload["total_amount"] == "123.45"
            assert payload["subject"] == "NexPoly 月费 & 计算"
            assert payload["seller_id"] == settings.alipay.seller_id
            assert params["notify_url"] == settings.alipay.notify_url
            return alipay_response(keys, method, {"code": "10000", "msg": "成功 } nested text",
                "out_trade_no": ORDER, "qr_code": "https://qr.alipay.com/synthetic"})
        if method == "alipay.trade.query":
            return alipay_response(keys, method, {"code": "10000", **alipay_transaction(),
                "send_pay_date": "2026-11-02 12:34:56"})
        return alipay_response(keys, method, {"code": "10000", "out_trade_no": ORDER})

    gateway = PaymentGateway(settings, transport=httpx.MockTransport(transport))
    result = gateway.create_order("alipay", ORDER, 12345, "NexPoly 月费 & 计算", datetime.now(timezone.utc) + timedelta(minutes=15))
    assert result["qr_code"] == "https://qr.alipay.com/synthetic"
    paid = gateway.query_order("alipay", ORDER, expected_amount_fen=12345)
    assert paid["status"] == "paid" and paid["paid_at"] == "2026-11-02T12:34:56+08:00"
    assert gateway.close_order("alipay", ORDER)["status"] == "closed"
    assert methods == ["alipay.trade.precreate", "alipay.trade.query", "alipay.trade.close"]


def test_alipay_response_tampering_rejected(configuration, keys):
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(lambda request:
        alipay_response(keys, "alipay.trade.query", {"code": "10000", **alipay_transaction()}, tamper=True)))
    with pytest.raises(PaymentError, match="signature_invalid"):
        gateway.query_order("alipay", ORDER)


def test_alipay_notification_normalization_and_disabled_sales_reconciliation(configuration, keys):
    settings = replace(configuration[0], enabled=False)
    gateway = PaymentGateway(settings)
    result = gateway.verify_notification("alipay", {}, alipay_notification(keys), expected_amount_fen=12345)
    assert result["event_id"] == "ali-event-123" and result["status"] == "paid"
    assert result["paid_at"] == "2026-11-02T12:34:56+08:00"
    with pytest.raises(PaymentError, match="purchasing_disabled"):
        gateway.create_order("alipay", ORDER, 12345, "NexPoly", datetime.now(timezone.utc) + timedelta(minutes=15))


@pytest.mark.parametrize("mutation,code", [
    ({"seller_id": "2088999999999999"}, "payment_merchant_mismatch"),
    ({"app_id": "other-app"}, "payment_merchant_mismatch"),
    ({"total_amount": "123.46"}, "payment_amount_mismatch"),
    ({"total_amount": "123.451"}, "payment_amount_invalid"),
    ({"total_amount": "1e2"}, "payment_amount_invalid"),
    ({"total_amount": "-123.45"}, "payment_amount_invalid"),
    ({"trans_currency": "USD"}, "payment_currency_mismatch"),
    ({"sign_type": "RSA"}, "payment_signature_invalid"),
    ({"out_trade_no": "f" * 32}, "payment_order_mismatch"),
])
def test_alipay_signed_but_wrong_business_facts_rejected(configuration, keys, mutation, code):
    with pytest.raises(PaymentError, match=code):
        PaymentGateway(configuration[0]).verify_notification("alipay", {}, alipay_notification(keys, **mutation),
            expected_order_id=ORDER, expected_amount_fen=12345)


def test_alipay_notification_tamper_and_duplicate_fields_rejected(configuration, keys):
    gateway = PaymentGateway(configuration[0])
    body = alipay_notification(keys)
    with pytest.raises(PaymentError, match="signature_invalid"):
        gateway.verify_notification("alipay", {}, body.replace(b"123.45", b"123.46"))
    with pytest.raises(PaymentError, match="payload_invalid"):
        gateway.verify_notification("alipay", {}, body + b"&total_amount=1.00")


def test_signed_not_found_is_pending_and_network_timeout_is_not_closed(configuration, keys):
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(lambda request:
        alipay_response(keys, "alipay.trade.query", {"code": "40004", "sub_code": "ACQ.TRADE_NOT_EXIST"})))
    event = gateway.query_order("alipay", ORDER)
    assert event["status"] == "pending" and event["amount_fen"] is None

    def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)

    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(timeout))
    with pytest.raises(PaymentError, match="provider_unavailable") as failure:
        gateway.query_order("wechat", ORDER)
    assert failure.value.retryable


@pytest.mark.parametrize("provider", ["wechat", "alipay"])
def test_signed_close_not_found_confirms_closed(configuration, keys, provider):
    def transport(request):
        if provider == "wechat":
            return wechat_response(keys, {"code": "ORDER_NOT_EXIST", "message": "synthetic"}, status=404)
        return alipay_response(keys, "alipay.trade.close", {"code": "40004", "sub_code": "ACQ.TRADE_NOT_EXIST"})
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(transport))
    assert gateway.close_order(provider, ORDER)["status"] == "closed"


def test_unsigned_close_not_found_cannot_confirm_closed(configuration):
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(lambda request:
        httpx.Response(404, json={"code": "ORDER_NOT_EXIST"})))
    with pytest.raises(PaymentError, match="signing_key_unknown"):
        gateway.close_order("wechat", ORDER)


def test_duplicate_json_response_and_wrong_order_rejected(configuration, keys):
    raw = b'{"appid":"one","appid":"two"}'
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(lambda request:
        httpx.Response(200, headers=wechat_headers(keys, raw), content=raw)))
    with pytest.raises(PaymentError, match="payload_invalid"):
        gateway.query_order("wechat", ORDER)
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(lambda request:
        wechat_response(keys, wechat_transaction(out_trade_no="f" * 32))))
    with pytest.raises(PaymentError, match="order_mismatch"):
        gateway.query_order("wechat", ORDER)


@pytest.mark.parametrize("order", ["x", "../" + "a" * 29, "a" * 36, "中" * 32])
def test_invalid_order_ids_are_never_transmitted(configuration, order):
    def no_network(request):
        pytest.fail("invalid input reached payment transport")
    gateway = PaymentGateway(configuration[0], transport=httpx.MockTransport(no_network))
    with pytest.raises(PaymentError, match="order_id_invalid"):
        gateway.query_order("wechat", order)
