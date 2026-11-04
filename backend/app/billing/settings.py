"""Explicit payment configuration. No commercial price or enabled payment default."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit


class BillingConfigurationError(ValueError):
    """Contains a variable name/code only, never a credential or file content."""


def _required(env: Mapping[str, str], name: str, pattern: str | None = None) -> str:
    value = env.get(name, "").strip()
    if not value or (pattern is not None and re.fullmatch(pattern, value) is None):
        raise BillingConfigurationError(f"Invalid or missing {name}")
    return value


def _file(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name)
    path = Path(value)
    try:
        valid = path.is_absolute() and path.is_file() and 0 < path.stat().st_size <= 65536
    except OSError:
        valid = False
    if not valid:
        raise BillingConfigurationError(f"Invalid payment file: {name}")
    return value


def _notify_url(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name)
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and bool(parts.hostname) and not parts.username
                 and not parts.password and not parts.fragment and not parts.query
                 and parts.port in {None, 443} and len(value) <= 255
                 and not any(character.isspace() for character in value))
    except ValueError:
        valid = False
    if not valid:
        raise BillingConfigurationError(f"A public HTTPS callback is required: {name}")
    return value


def _integer(env: Mapping[str, str], name: str, low: int, high: int, default=None):
    value = env.get(name, "").strip()
    if not value and default is not None:
        return default
    if not re.fullmatch(r"[0-9]+", value) or not low <= int(value) <= high:
        raise BillingConfigurationError(f"Invalid or missing {name}")
    return int(value)


@dataclass(frozen=True)
class WechatSettings:
    app_id: str
    merchant_id: str
    merchant_serial: str
    private_key_file: str = field(repr=False)
    api_v3_key_file: str = field(repr=False)
    platform_key_id: str
    platform_public_key_file: str = field(repr=False)
    notify_url: str


@dataclass(frozen=True)
class AlipaySettings:
    app_id: str
    seller_id: str
    private_key_file: str = field(repr=False)
    public_key_file: str = field(repr=False)
    notify_url: str


@dataclass(frozen=True)
class BillingSettings:
    enabled: bool = False
    simulation_enabled: bool = False
    registration_enabled: bool = False
    monthly_price_fen: int | None = None
    order_ttl_seconds: int = 900
    wechat: WechatSettings | None = None
    alipay: AlipaySettings | None = None

    @property
    def available_providers(self) -> tuple[str, ...]:
        """Configured channels, also available for reconciliation when sales are off."""
        if self.simulation_enabled:
            return ("wechat", "alipay")
        return tuple(name for name in ("wechat", "alipay") if getattr(self, name) is not None)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BillingSettings":
        env = os.environ if env is None else env
        raw_enabled = env.get("NEXPOLY_BILLING_ENABLED", "false").strip().lower()
        if raw_enabled not in {"true", "false", "1", "0"}:
            raise BillingConfigurationError("Invalid NEXPOLY_BILLING_ENABLED")
        enabled = raw_enabled in {"true", "1"}
        simulation = env.get("NEXPOLY_BILLING_SIMULATION_ENABLED", "false").strip().lower()
        if simulation not in {"true", "false", "1", "0"}:
            raise BillingConfigurationError("Invalid NEXPOLY_BILLING_SIMULATION_ENABLED")
        simulation_enabled = simulation in {"true", "1"}
        registration = env.get("NEXPOLY_BILLING_REGISTRATION_ENABLED", "false").strip().lower()
        if registration not in {"true", "false", "1", "0"}:
            raise BillingConfigurationError("Invalid NEXPOLY_BILLING_REGISTRATION_ENABLED")
        price = None
        if enabled or simulation_enabled or env.get("NEXPOLY_BILLING_MONTHLY_PRICE_FEN", "").strip():
            price = _integer(env, "NEXPOLY_BILLING_MONTHLY_PRICE_FEN", 1, 100_000_000)
        providers = [item.strip() for item in env.get("NEXPOLY_BILLING_PROVIDERS", "").split(",") if item.strip()]
        if len(set(providers)) != len(providers) or any(item not in {"wechat", "alipay"} for item in providers):
            raise BillingConfigurationError("Invalid NEXPOLY_BILLING_PROVIDERS")
        if simulation_enabled and (env.get("GPU_BROKER_ENVIRONMENT") != "dev"
                or not env.get("AUTH_COOKIE_NAME", "").startswith("nexpoly_dev_")
                or enabled or providers):
            raise BillingConfigurationError("Payment simulation requires an isolated development environment with real payments disabled")
        if enabled and not providers:
            raise BillingConfigurationError("NEXPOLY_BILLING_PROVIDERS is required when purchasing is enabled")
        values = {}
        if "wechat" in providers:
            values["wechat"] = WechatSettings(
                app_id=_required(env, "NEXPOLY_WECHAT_APP_ID", r"[A-Za-z0-9]{1,32}"),
                merchant_id=_required(env, "NEXPOLY_WECHAT_MERCHANT_ID", r"[0-9]{1,32}"),
                merchant_serial=_required(env, "NEXPOLY_WECHAT_MERCHANT_SERIAL", r"[A-Fa-f0-9]{1,64}").upper(),
                private_key_file=_file(env, "NEXPOLY_WECHAT_PRIVATE_KEY_FILE"),
                api_v3_key_file=_file(env, "NEXPOLY_WECHAT_API_V3_KEY_FILE"),
                platform_key_id=_required(env, "NEXPOLY_WECHAT_PLATFORM_KEY_ID", r"(?:PUB_KEY_ID_[0-9]+|[A-Fa-f0-9]{1,64})").upper(),
                platform_public_key_file=_file(env, "NEXPOLY_WECHAT_PLATFORM_PUBLIC_KEY_FILE"),
                notify_url=_notify_url(env, "NEXPOLY_WECHAT_NOTIFY_URL"),
            )
        if "alipay" in providers:
            values["alipay"] = AlipaySettings(
                app_id=_required(env, "NEXPOLY_ALIPAY_APP_ID", r"[0-9]{1,32}"),
                seller_id=_required(env, "NEXPOLY_ALIPAY_SELLER_ID", r"[0-9]{1,32}"),
                private_key_file=_file(env, "NEXPOLY_ALIPAY_PRIVATE_KEY_FILE"),
                public_key_file=_file(env, "NEXPOLY_ALIPAY_PUBLIC_KEY_FILE"),
                notify_url=_notify_url(env, "NEXPOLY_ALIPAY_NOTIFY_URL"),
            )
        return cls(enabled=enabled, simulation_enabled=simulation_enabled,
                   registration_enabled=registration in {"true", "1"}, monthly_price_fen=price, **values)
