import pytest
from app.core.security import (
    CredentialVault,
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    verify_password,
)


def test_password_hashing():
    pwd = "SuperSecretPassword123!"
    hashed = hash_password(pwd)
    assert hashed.startswith("$argon2id$")
    assert verify_password(pwd, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False


def test_aes_gcm_encryption_roundtrip():
    key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    plaintext = "super_sensitive_api_secret_key_okx_binance"
    encrypted = encrypt_secret(plaintext, key)

    assert encrypted != plaintext
    assert ":" in encrypted  # nonce:tag:ciphertext

    decrypted = decrypt_secret(encrypted, key)
    assert decrypted == plaintext


def test_credential_vault():
    vault = CredentialVault()
    creds = {
        "api_key": "my-api-key-123456",
        "secret_key": "my-secret-key-abcdef",
        "passphrase": "my-passphrase",
    }
    enc = vault.encrypt_dict(creds)
    dec = vault.decrypt_dict(enc)
    assert dec == creds

    masked = vault.mask_secret(creds["api_key"])
    assert masked.startswith("my-a")
    assert masked.endswith("3456")
    assert "***" in masked


def test_jwt_tokens():
    token = create_access_token(user_id=1, username="admin", role="admin")
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "1"
    assert payload["username"] == "admin"
    assert payload["role"] == "admin"

    # Tampered token
    tampered = token[:-4] + "fake"
    assert decode_access_token(tampered) is None
