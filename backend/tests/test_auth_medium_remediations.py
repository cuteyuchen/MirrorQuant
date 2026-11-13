import re
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import jwt
import pytest
from starlette.requests import Request

from app.api import deps
from app.core import security
from app.core.config import Settings, get_settings


def request_from(peer, headers):
    return Request({
        "type": "http", "client": (peer, 1234),
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    })


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP"])
def test_untrusted_peer_cannot_spoof_client_ip(header):
    request = request_from("198.51.100.9", {header: "203.0.113.7"})
    assert deps.extract_client_ip(request) == "198.51.100.9"


def test_trusted_proxy_chain_stops_at_first_untrusted_hop(monkeypatch):
    settings = Settings(_env_file=None, trusted_proxy_cidrs=["127.0.0.0/8", "10.0.0.0/24"])
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = request_from("127.0.0.1", {
        "X-Forwarded-For": "203.0.113.7, 198.51.100.9, 10.0.0.5",
        "CF-Connecting-IP": "203.0.113.8",
    })
    assert deps.extract_client_ip(request) == "198.51.100.9"


@pytest.mark.parametrize("header", ["X-Real-IP", "CF-Connecting-IP"])
def test_trusted_proxy_accepts_validated_single_address(header):
    assert deps.extract_client_ip(request_from("::1", {header: "2001:db8::1"})) == "2001:db8::1"
    assert deps.extract_client_ip(request_from("::1", {header: "not-an-ip"})) == "::1"


def test_malformed_forwarded_chain_fails_closed():
    request = request_from("127.0.0.1", {"X-Forwarded-For": "spoofed, 198.51.100.9"})
    assert deps.extract_client_ip(request) == "127.0.0.1"
    assert deps.extract_client_ip(None) == "unknown"


def test_trusted_proxy_cidrs_parse_and_validate(monkeypatch):
    monkeypatch.setenv("MQ_TRUSTED_PROXY_CIDRS", "127.0.0.0/8, ::1/128, 172.30.0.3/32")
    assert Settings(_env_file=None).trusted_proxy_cidrs == ["127.0.0.0/8", "::1/128", "172.30.0.3/32"]
    monkeypatch.setenv("MQ_TRUSTED_PROXY_CIDRS", '["10.0.0.7/24"]')
    assert Settings(_env_file=None).trusted_proxy_cidrs == ["10.0.0.0/24"]
    with pytest.raises(ValueError, match="does not appear to be an IPv4 or IPv6 network"):
        Settings(_env_file=None, trusted_proxy_cidrs=["not-a-network"])


def challenge_answer(challenge):
    match = re.search(r">(\d+) ([+-]) (\d+) = \?</text>", challenge["svg"])
    first, operator, second = match.groups()
    return str(int(first) + int(second) if operator == "+" else int(first) - int(second))


def test_captcha_is_single_use_and_keeps_answer_digest_server_side(monkeypatch):
    monkeypatch.setattr(security, "_captcha_nonces", security._CaptchaNonceCache())
    challenge = security.create_captcha_challenge()
    token = challenge["captchaToken"]
    claims = jwt.decode(token, get_settings().resolved_jwt_secret(), algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 120
    assert "digest" not in claims
    assert security.verify_captcha_token(token, challenge_answer(challenge)) is True
    assert security.verify_captcha_token(token, challenge_answer(challenge)) is False


def test_incorrect_captcha_attempt_consumes_nonce(monkeypatch):
    monkeypatch.setattr(security, "_captcha_nonces", security._CaptchaNonceCache())
    challenge = security.create_captcha_challenge()
    assert security.verify_captcha_token(challenge["captchaToken"], "wrong") is False
    assert security.verify_captcha_token(challenge["captchaToken"], challenge_answer(challenge)) is False


def test_captcha_expires_on_monotonic_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(security, "_captcha_nonces", security._CaptchaNonceCache())
    challenge = security.create_captcha_challenge()
    now[0] += 120
    assert security.verify_captcha_token(challenge["captchaToken"], challenge_answer(challenge)) is False


def test_captcha_nonce_cache_is_bounded_and_consumption_is_atomic(monkeypatch):
    cache = security._CaptchaNonceCache(max_entries=2)
    cache.store("oldest", "digest", 120)
    cache.store("second", "digest", 120)
    cache.store("newest", "digest", 120)
    assert cache.consume("oldest") is None
    assert cache.consume("second") == "digest"
    monkeypatch.setattr(security, "_captcha_nonces", cache)
    challenge = security.create_captcha_challenge()
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(
            lambda _: security.verify_captcha_token(challenge["captchaToken"], challenge_answer(challenge)),
            range(8),
        ))
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_captcha_replay_returns_400_even_after_successful_login(async_client):
    password = "CaptchaRegressionPassword123!"
    created = await async_client.post("/api/auth/setup", json={"username": "captcha_admin", "password": password})
    assert created.status_code == 201
    challenge = (await async_client.get("/api/auth/captcha")).json()
    payload = {"username": "captcha_admin", "password": password,
               "captchaToken": challenge["captchaToken"], "captchaAnswer": challenge_answer(challenge)}
    assert (await async_client.post("/api/auth/login", json=payload)).status_code == 200
    replay = await async_client.post("/api/auth/login", json=payload)
    assert replay.status_code == 400
    assert replay.json()["code"] == "INVALID_CAPTCHA"


@pytest.mark.asyncio
async def test_password_helpers_await_to_thread(monkeypatch):
    off_thread = AsyncMock(side_effect=["synthetic-hash", True])
    monkeypatch.setattr(security.asyncio, "to_thread", off_thread)
    assert await security.hash_password_async("synthetic-password") == "synthetic-hash"
    assert await security.verify_password_async("synthetic-password", "synthetic-hash") is True
    assert off_thread.await_args_list[0].args == (security.hash_password, "synthetic-password")
    assert off_thread.await_args_list[1].args == (security.verify_password, "synthetic-password", "synthetic-hash")


@pytest.mark.asyncio
async def test_auth_password_operations_run_outside_event_loop(async_client, monkeypatch):
    loop_thread = threading.get_ident()
    calls = []

    def fake_hash(password):
        assert threading.get_ident() != loop_thread
        calls.append("hash")
        return "synthetic-password-hash"

    def fake_verify(password, password_hash):
        assert threading.get_ident() != loop_thread
        calls.append("verify")
        return True

    monkeypatch.setattr(security, "hash_password", fake_hash)
    monkeypatch.setattr(security, "verify_password", fake_verify)
    password = "ThreadRegressionPassword123!"
    assert (await async_client.post("/api/auth/setup", json={"username": "thread_admin", "password": password})).status_code == 201
    assert (await async_client.post("/api/auth/login", json={"username": "thread_admin", "password": password})).status_code == 200
    assert (await async_client.post("/api/auth/2fa/setup", json={"currentPassword": password})).status_code == 200
    assert (await async_client.post("/api/auth/2fa/disable", json={"currentPassword": password})).status_code == 200
    assert (await async_client.post("/api/auth/login", json={"username": "thread_admin", "password": password})).status_code == 200
    assert (await async_client.post("/api/auth/change-password", json={"currentPassword": password, "newPassword": password + "new"})).status_code == 200
    assert calls == ["hash", "verify", "verify", "verify", "verify", "verify", "hash"]
