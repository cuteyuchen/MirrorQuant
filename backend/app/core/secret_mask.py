from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MASK_SENTINEL = "••••"
PROXY_USERINFO_MASK = "***:***"
# Explicit public aliases and their stored configuration keys.
NOTIFICATION_SECRET_FIELDS = {
    "telegram_bot_token": "telegramBotToken",
    "webhook_url": "webhookUrl",
    "wecom_webhook_url": "wecomWebhookUrl",
    "dingtalk_webhook_url": "dingtalkWebhookUrl",
}
SECRET_KEYS = frozenset(NOTIFICATION_SECRET_FIELDS) | frozenset(NOTIFICATION_SECRET_FIELDS.values())
_PROXY_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/?#]*@")


def mask_secret(value: str | None) -> str:
    """Use a fixed mask so even short secrets disclose no characters."""
    return MASK_SENTINEL if value else ""


def is_mask_sentinel(value: Any) -> bool:
    return value == MASK_SENTINEL


def merge_secrets(stored: Mapping, incoming: Mapping, secret_keys: Iterable[str]) -> dict:
    """Merge partial updates without replacing secrets with UI placeholders."""
    secrets = set(secret_keys)
    merged = dict(stored)
    for key, value in incoming.items():
        if key in secrets and (value is None or value == "" or is_mask_sentinel(value)):
            continue
        merged[key] = value
    return merged


def redact_proxy_userinfo(value: str) -> str:
    """Also scrub URLs embedded in connection errors and telemetry strings."""
    return _PROXY_USERINFO.sub(r"\1" + PROXY_USERINFO_MASK + "@", value)


def mask_proxy_url(value: str) -> str:
    return redact_proxy_userinfo(value)


def proxy_credentials_configured(value: str) -> bool:
    return bool(_PROXY_USERINFO.search(value))


def merge_proxy_url(stored: str, incoming: str | None) -> str:
    if incoming is None or incoming == "" or is_mask_sentinel(incoming):
        return stored
    parsed = urlsplit(incoming)
    userinfo, separator, host = parsed.netloc.rpartition("@")
    if separator and userinfo == PROXY_USERINFO_MASK:
        previous = urlsplit(stored)
        credentials, configured, _ = previous.netloc.rpartition("@")
        if not configured:
            raise ValueError("Masked proxy credentials require an existing credential")
        # Do not forward an existing credential to a different endpoint via a mask.
        if (parsed.scheme, host) != (previous.scheme, previous.netloc.rpartition("@")[2]):
            raise ValueError("Changing the proxy endpoint requires new credentials")
        return urlunsplit(parsed._replace(netloc=credentials + "@" + host))
    return incoming


def mask_sensitive_payload(value: Any) -> Any:
    """Sanitize outward-facing telemetry, including nested proxy error strings."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in SECRET_KEYS:
                result[key] = mask_secret(item)
                result[key + "_configured"] = bool(item)
            else:
                result[key] = mask_sensitive_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        return [mask_sensitive_payload(item) for item in value]
    if isinstance(value, str):
        return redact_proxy_userinfo(value)
    return value
