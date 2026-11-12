from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
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
