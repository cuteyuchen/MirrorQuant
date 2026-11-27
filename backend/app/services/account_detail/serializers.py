"""Serialization, pagination cursor, and redaction helpers for account detail."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.core.secret_mask import mask_sensitive_payload, redact_error_urls, redact_proxy_userinfo

SUPPORTED_RANGES: Dict[str, Optional[int]] = {
    "24h": 24 * 3_600_000,
    "1d": 24 * 3_600_000,
    "7d": 7 * 86_400_000,
    "30d": 30 * 86_400_000,
    "90d": 90 * 86_400_000,
    "1y": 365 * 86_400_000,
    "all": None,
}


def finite_float(value: Any, *, precision: Optional[int] = None) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        if isinstance(value, Decimal):
            if not value.is_finite():
                return None
            num = float(value)
        else:
            num = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(num):
        return None
    if precision is not None:
        num = round(num, precision)
    return 0.0 if num == 0 else num


def decimal_to_float(value: Optional[Decimal], *, precision: Optional[int] = None) -> Optional[float]:
    return finite_float(value, precision=precision)


def decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None or not value.is_finite():
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in ("", "-0") else rendered


def encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Invalid pagination cursor")
    return parsed


def sanitize_error_text(value: Optional[str], *, max_len: int = 400) -> Optional[str]:
    if not value:
        return None
    cleaned = redact_error_urls(redact_proxy_userinfo(str(value))).strip()
    return cleaned[:max_len] if cleaned else None


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "passphrase",
    "password",
    "token",
    "signature",
    "private_key",
    "authorization",
    "cookie",
)


def sanitize_payload(value: Any) -> Any:
    masked = mask_sensitive_payload(value)
    if isinstance(masked, dict):
        out: Dict[str, Any] = {}
        for k, v in masked.items():
            key_lower = str(k).lower()
            if any(part in key_lower for part in _SENSITIVE_KEY_PARTS):
                out[k] = "****" if v else v
            else:
                out[k] = sanitize_payload(v)
        return out
    if isinstance(masked, list):
        return [sanitize_payload(item) for item in masked]
    if isinstance(masked, str) and ("://" in masked or "@" in masked):
        return redact_error_urls(redact_proxy_userinfo(masked))
    return masked


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert ORM datetime (treating naive SQLite datetimes as UTC) to epoch ms."""
    if dt is None:
        return None
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return int(aware.timestamp() * 1000)


def parse_range_window(range_name: str, now_ms: int) -> tuple[Optional[int], int]:
    key = (range_name or "30d").strip().lower()
    if key not in SUPPORTED_RANGES:
        raise HTTPException(status_code=422, detail="Unsupported range parameter")
    span = SUPPORTED_RANGES[key]
    return (None if span is None else max(0, now_ms - span)), now_ms
