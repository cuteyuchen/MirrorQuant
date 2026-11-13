import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.secret_mask import (
    MASK_SENTINEL,
    NOTIFICATION_SECRET_FIELDS,
    is_mask_sentinel,
    mask_proxy_url,
    mask_secret,
    mask_sensitive_payload,
    merge_proxy_url,
    merge_secrets,
)
from app.core.security import create_access_token
from app.models.domain import ProxyNode, SystemConfig, User
from app.services.engine.orchestrator import WebSocketHub
from app.services.proxy.manager import ProxyTestResult


@pytest.fixture
async def secret_client(async_client):
    async with get_session_factory()() as db:
        user = User(username="mask-admin", password_hash="unused", role="admin")
        db.add(user)
        await db.commit()
        token = create_access_token(user.id, user.username, user.role)
    async_client.headers["Authorization"] = f"Bearer {token}"
    return async_client


@pytest.mark.parametrize("value", [None, "", "a", "1234", "synthetic-long-secret"])
def test_mask_secret_never_discloses_even_short_values(value):
    assert mask_secret(value) == (MASK_SENTINEL if value else "")


def test_mask_sentinel_is_distinct_from_new_values():
    assert is_mask_sentinel(MASK_SENTINEL)
    for value in (None, "", "replacement", "••••replacement"):
        assert not is_mask_sentinel(value)


def test_merge_secrets_preserves_omitted_empty_and_masked_values():
    stored = {"secret": "original", "other": "before"}
    for incoming in ({}, {"secret": ""}, {"secret": MASK_SENTINEL}):
        assert merge_secrets(stored, incoming, {"secret"}) == stored
    assert merge_secrets(stored, {"secret": "replacement", "other": ""}, {"secret"}) == {
        "secret": "replacement", "other": "",
    }
    assert stored == {"secret": "original", "other": "before"}


@pytest.mark.parametrize("url,masked", [
    ("http://synthetic-user:synthetic-pass@proxy.invalid:8080", "http://***:***@proxy.invalid:8080"),
    ("socks5://user%40name:pass%3Aword@[::1]:1080", "socks5://***:***@[::1]:1080"),
    ("https://token@proxy.invalid", "https://***:***@proxy.invalid"),
    ("http://proxy.invalid:8080", "http://proxy.invalid:8080"),
])
def test_proxy_masks_and_updates_round_trip(url, masked):
    assert mask_proxy_url(url) == masked
    assert merge_proxy_url(url, masked) == url
    assert merge_proxy_url(url, "") == url
    assert merge_proxy_url(url, MASK_SENTINEL) == url


def test_proxy_mask_cannot_forward_stored_credentials_to_another_host():
    with pytest.raises(ValueError, match="endpoint requires new credentials"):
        merge_proxy_url("http://user:password@proxy.invalid", "http://***:***@another.invalid")
    with pytest.raises(ValueError, match="existing credential"):
        merge_proxy_url("http://proxy.invalid", "http://***:***@proxy.invalid")


@pytest.mark.asyncio
async def test_notification_secrets_masked_on_get_and_put_and_round_trip(secret_client):
    aliases = NOTIFICATION_SECRET_FIELDS
    values = {key: f"synthetic-{key}-secret" for key in aliases}
    response = await secret_client.put("/api/settings", json={"notifications": {
        aliases[key]: value for key, value in values.items()
    }})
    assert response.status_code == 200
    for key, value in values.items():
        assert value not in response.text
        assert response.json()["notifications"][key] == MASK_SENTINEL
        assert response.json()["notifications"][key + "_configured"] is True
    response = await secret_client.get("/api/settings")
    returned = response.json()["notifications"]
    assert (await secret_client.put("/api/settings", json={"notifications": returned})).status_code == 200
    for replacement in ("", MASK_SENTINEL):
        assert (await secret_client.put("/api/settings", json={"notifications": {
            key: replacement for key in aliases
        }})).status_code == 200
    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "notifications"))
        stored = json.loads(row.value_json)
        assert {key: stored[key] for key in aliases} == values
        assert not any(key.endswith("_configured") for key in stored)
    response = await secret_client.put("/api/settings", json={"notifications": {"telegramBotToken": "replacement"}})
    assert "replacement" not in response.text
    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "notifications"))
        assert json.loads(row.value_json)["telegram_bot_token"] == "replacement"


@pytest.mark.asyncio
async def test_unconfigured_notifications_have_false_flags(secret_client):
    result = (await secret_client.get("/api/settings")).json()["notifications"]
    for key in NOTIFICATION_SECRET_FIELDS:
        assert result[key] == ""
        assert result[key + "_configured"] is False


@pytest.mark.asyncio
async def test_proxy_secrets_hidden_on_all_api_reads_and_preserved_on_update(secret_client, monkeypatch):
    url = "http://synthetic-user:synthetic-password@proxy.invalid:8080"
    masked = mask_proxy_url(url)
    response = await secret_client.post("/api/proxies", json={"name": "Synthetic proxy", "url": url})
    assert response.status_code == 201
    proxy_id = response.json()["item"]["id"]
    assert response.json()["item"]["url"] == masked
    for path in ("/api/proxies", f"/api/proxies/{proxy_id}"):
        response = await secret_client.get(path)
        assert response.status_code == 200 and "synthetic-password" not in response.text
    for replacement in (masked, "", MASK_SENTINEL):
        response = await secret_client.put(f"/api/proxies/{proxy_id}", json={"url": replacement, "priority": 20})
        assert response.status_code == 200
        assert response.json()["item"]["url"] == masked
        assert response.json()["item"]["url_configured"] is True
        async with get_session_factory()() as db:
            assert (await db.get(ProxyNode, proxy_id)).url == url
    monkeypatch.setattr("app.api.proxies.ProxyManager.test_proxy", AsyncMock(return_value=ProxyTestResult(
        healthy=False, latency_ms=None, error=f"Cannot connect to {url}",
    )))
    response = await secret_client.post(f"/api/proxies/{proxy_id}/test")
    assert response.status_code == 200 and "synthetic-password" not in response.text
    assert "synthetic-password" not in (await secret_client.get(f"/api/proxies/{proxy_id}")).text
    replacement = "socks5://new-user:new-password@proxy.invalid:1080"
    assert (await secret_client.put(f"/api/proxies/{proxy_id}", json={"url": replacement})).status_code == 200
    async with get_session_factory()() as db:
        assert (await db.get(ProxyNode, proxy_id)).url == replacement


def test_nested_telemetry_masks_explicit_secret_keys_and_proxy_errors():
    original = {"notifications": {"telegramBotToken": "synthetic-token"}, "errors": [
        "Cannot connect http://user:password@proxy.invalid:8080",
    ]}
    result = mask_sensitive_payload(original)
    assert result["notifications"]["telegramBotToken"] == MASK_SENTINEL
    assert result["errors"] == ["Cannot connect http://***:***@proxy.invalid:8080"]
    assert original["notifications"]["telegramBotToken"] == "synthetic-token"


@pytest.mark.asyncio
async def test_websocket_broadcast_masks_proxy_credentials():
    hub = WebSocketHub()
    socket = AsyncMock()
    await hub.connect(socket, validator=AsyncMock(return_value=True))
    await hub.broadcast({"url": "http://user:password@proxy.invalid:8080"})
    socket.send_json.assert_awaited_once_with({"url": "http://***:***@proxy.invalid:8080"})
