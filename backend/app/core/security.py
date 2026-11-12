from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import jwt
import segno
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _derive_aes_key(master_key: str) -> bytes:
    """Derive a 32-byte AES-256 key from master_key string."""
    try:
        raw = base64.urlsafe_b64decode(master_key.encode("ascii"))
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    return hashlib.sha256(master_key.encode("utf-8")).digest()


class CredentialVault:
    """AES-256-GCM authenticated encryption vault for exchange API credentials."""

    PREFIX = "v1:"

    def __init__(self, master_key: Optional[str] = None):
        key_str = master_key or get_settings().resolved_master_key()
        self._key = _derive_aes_key(key_str)
        self._aesgcm = AESGCM(self._key)

    def encrypt_dict(self, payload: Dict[str, Any]) -> str:
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext, None)
        return (
            self.PREFIX
            + base64.urlsafe_b64encode(nonce).decode("ascii")
            + ":"
            + base64.urlsafe_b64encode(ct).decode("ascii")
        )

    def decrypt_dict(self, token: str) -> Dict[str, Any]:
        if not token or not token.startswith(self.PREFIX):
            raise ValueError("Invalid encrypted credential format")
        body = token[len(self.PREFIX) :]
        parts = body.split(":", 1)
        if len(parts) != 2:
            raise ValueError("Malformed encrypted credential payload")
        nonce = base64.urlsafe_b64decode(parts[0].encode("ascii"))
        ct = base64.urlsafe_b64decode(parts[1].encode("ascii"))
        plaintext = self._aesgcm.decrypt(nonce, ct, None)
        return json.loads(plaintext.decode("utf-8"))

    def encrypt_string(self, plaintext_str: str) -> str:
        plaintext = plaintext_str.encode("utf-8")
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext, None)
        return (
            self.PREFIX
            + base64.urlsafe_b64encode(nonce).decode("ascii")
            + ":"
            + base64.urlsafe_b64encode(ct).decode("ascii")
        )

    def decrypt_string(self, token: str) -> str:
        if not token or not token.startswith(self.PREFIX):
            raise ValueError("Invalid encrypted credential format")
        body = token[len(self.PREFIX) :]
        parts = body.split(":", 1)
        if len(parts) != 2:
            raise ValueError("Malformed encrypted credential payload")
        nonce = base64.urlsafe_b64decode(parts[0].encode("ascii"))
        ct = base64.urlsafe_b64decode(parts[1].encode("ascii"))
        plaintext = self._aesgcm.decrypt(nonce, ct, None)
        return plaintext.decode("utf-8")

    @staticmethod
    def mask_secret(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}****{value[-4:]}"


def create_access_token(
    user_id: int,
    username: str,
    role: str = "admin",
    expires_minutes: Optional[int] = None,
) -> str:
    settings = get_settings()
    exp_min = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=exp_min)).timestamp()),
    }
    return jwt.encode(payload, settings.resolved_jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.resolved_jwt_secret(),
            algorithms=["HS256"],
        )
    except jwt.PyJWTError:
        return None


def encrypt_secret(plaintext: str, master_key: Optional[str] = None) -> str:
    vault = CredentialVault(master_key=master_key)
    return vault.encrypt_string(plaintext)


def decrypt_secret(token: str, master_key: Optional[str] = None) -> str:
    vault = CredentialVault(master_key=master_key)
    return vault.decrypt_string(token)


# ---------------------------------------------------------------------------
# RFC 6238 TOTP (HMAC-SHA1, 6 digits, 30s period)
# ---------------------------------------------------------------------------

def generate_totp_secret(length_bytes: int = 20) -> str:
    raw = secrets.token_bytes(length_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def build_totp_uri(secret: str, username: str, issuer: str = "MirrorQuant") -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def totp_qr_svg_data_uri(otpauth_uri: str) -> str:
    qr = segno.make(otpauth_uri, error="M")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=4, border=2, dark="#0f172a", light="#ffffff")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _hotp(secret: str, counter: int, digits: int = 6) -> str:
    padded = secret.strip().upper()
    rem = len(padded) % 8
    if rem:
        padded += "=" * (8 - rem)
    key = base64.b32decode(padded, casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)


def verify_totp_code(secret: str, code: str, window: int = 1, now_ts: Optional[int] = None) -> bool:
    clean = (code or "").strip().replace(" ", "")
    if not clean.isdigit() or len(clean) != 6 or not secret:
        return False
    ts = int(now_ts if now_ts is not None else time.time())
    counter = ts // 30
    for delta in range(-window, window + 1):
        try:
            expected = _hotp(secret, counter + delta)
            if hmac.compare_digest(expected, clean):
                return True
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Local SVG Math/Alphanumeric Captcha (signed short-lived JWT)
# ---------------------------------------------------------------------------

def create_captcha_challenge(expires_minutes: int = 5) -> Dict[str, str]:
    """Generate an SVG math challenge + signed token (no external service needed)."""
    a = secrets.randbelow(18) + 2
    b = secrets.randbelow(9) + 1
    op = "+" if secrets.randbelow(2) == 0 else "-"
    if op == "-" and b > a:
        a, b = b, a
    answer = str(a + b if op == "+" else a - b)
    prompt = f"{a} {op} {b} = ?"

    nonce = secrets.token_hex(8)
    settings = get_settings()
    answer_digest = hashlib.sha256(f"{nonce}:{answer}".encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "typ": "mq_captcha",
            "nonce": nonce,
            "digest": answer_digest,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
        },
        settings.resolved_jwt_secret(),
        algorithm="HS256",
    )

    # Render clean SVG with subtle noise lines so bots cannot trivially parse
    x1, y1 = 10 + secrets.randbelow(20), 10 + secrets.randbelow(24)
    x2, y2 = 120 + secrets.randbelow(30), 10 + secrets.randbelow(24)
    rot = (secrets.randbelow(9) - 4)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="44" viewBox="0 0 160 44">'
        '<rect width="160" height="44" rx="8" fill="#0f172a" stroke="#334155" stroke-width="1"/>'
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.5"/>'
        f'<line x1="15" y1="36" x2="145" y2="8" stroke="#818cf8" stroke-opacity="0.25" stroke-width="1.2"/>'
        f'<circle cx="{30 + secrets.randbelow(100)}" cy="{12 + secrets.randbelow(20)}" r="1.5" fill="#94a3b8" opacity="0.5"/>'
        f'<text x="80" y="28" text-anchor="middle" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" '
        f'font-size="18" font-weight="700" fill="#e2e8f0" letter-spacing="2" '
        f'transform="rotate({rot} 80 22)">{prompt}</text>'
        "</svg>"
    )
    svg_b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "captchaToken": token,
        "svg": svg,
        "svgDataUri": f"data:image/svg+xml;base64,{svg_b64}",
    }


def verify_captcha_token(token: Optional[str], answer: Optional[str]) -> bool:
    if not token or answer is None:
        return False
    clean_answer = str(answer).strip()
    if not clean_answer:
        return False
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.resolved_jwt_secret(),
            algorithms=["HS256"],
        )
    except jwt.PyJWTError:
        return False
    if payload.get("typ") != "mq_captcha":
        return False
    nonce = payload.get("nonce") or ""
    expected_digest = payload.get("digest") or ""
    actual_digest = hashlib.sha256(f"{nonce}:{clean_answer}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected_digest, actual_digest)
