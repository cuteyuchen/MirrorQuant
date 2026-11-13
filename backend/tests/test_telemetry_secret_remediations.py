import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.database import get_session_factory
from app.core.secret_mask import mask_proxy_url
from app.core.security import CredentialVault, create_access_token
from app.models.domain import LeadSource, TradingAccount, User
from app.services.engine.orchestrator import orchestrator
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.proxy.manager import ProxyManager


PROXY_URL = "http://synthetic-user:synthetic-pass-" + "x" * 190 + "@proxy.invalid:8080"
ERROR = "Synthetic connection failure: " + PROXY_URL


def assert_masked(value):
    encoded = json.dumps(value)
    assert "synthetic-user" not in encoded
    assert "synthetic-pass" not in encoded
    assert "***:***@proxy.invalid:8080" in encoded


@pytest.fixture
async def telemetry(async_client):
    snapshot = {
        "openPositionsMeta": {"error": ERROR},
        "openPositions": [{"symbol": "BTCUSDT", "analysis": {"ok": False, "reason": ERROR}}],
    }
    async with get_session_factory()() as db:
        user = User(username="telemetry-admin", password_hash="unused", role="admin")
        lead = LeadSource(name="synthetic", portfolio_id="synthetic", is_public_dashboard=True,
                          snapshot_json=json.dumps(snapshot))
        account = TradingAccount(name="synthetic", exchange="binance", api_key_masked="synthetic",
                                 encrypted_credentials=CredentialVault().encrypt_dict({}),
                                 positions_json=json.dumps([{"error": ERROR}]))
        db.add_all([user, lead, account])
        await db.commit()
        token = create_access_token(user.id, user.username, user.role)
        lead_id, account_id = lead.id, account.id
    async_client.headers["Authorization"] = f"Bearer {token}"
    return async_client, lead_id, account_id, snapshot


@pytest.mark.parametrize("endpoint", ["snapshot", "public", "overview", "accounts"])
@pytest.mark.asyncio
async def test_nested_stored_telemetry_masks_proxy_credentials(telemetry, endpoint):
    client, lead_id, _, _ = telemetry
    path = {
        "snapshot": f"/api/leads/{lead_id}/snapshot",
        "public": "/api/dashboard/public",
        "overview": "/api/dashboard/overview",
        "accounts": "/api/accounts",
    }[endpoint]
    if endpoint == "public":
        client.headers.pop("Authorization")
    response = await client.get(path)
    assert response.status_code == 200
    assert_masked(response.json())


@pytest.mark.parametrize("kind", ["lead", "account"])
@pytest.mark.asyncio
async def test_raw_sync_results_are_masked_recursively(telemetry, monkeypatch, kind):
    client, lead_id, account_id, snapshot = telemetry
    result = {"ok": False, "error": ERROR, "snapshot": snapshot}
    monkeypatch.setattr(orchestrator, "sync_" + kind, AsyncMock(return_value=result))
    path = f"/api/leads/{lead_id}/sync" if kind == "lead" else f"/api/accounts/{account_id}/sync"
    response = await client.post(path)
    assert response.status_code == 200
    assert_masked(response.json())
    assert result["error"] == ERROR  # Serialization must not mutate stored data.


@pytest.mark.asyncio
async def test_account_test_redacts_credentials_before_error_truncation(telemetry, monkeypatch):
    client, _, account_id, _ = telemetry
    error = "x" * 300 + " " + ERROR
    assert "@" not in error[:480]
    adapter = Mock(test_connection=AsyncMock(side_effect=RuntimeError(error)))
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", lambda **_: adapter)
    response = await client.post(f"/api/accounts/{account_id}/test")
    assert response.status_code == 200
    assert_masked(response.json())
    async with get_session_factory()() as db:
        assert_masked((await db.get(TradingAccount, account_id)).last_error)


@pytest.mark.asyncio
async def test_proxy_test_redacts_credentials_before_error_truncation(monkeypatch):
    error = "x" * 200 + " " + ERROR
    assert "@" not in error[:400]
    monkeypatch.setattr(ProxyManager, "build_client", Mock(side_effect=RuntimeError(error)))
    result = await ProxyManager.test_proxy(PROXY_URL)
    assert result.healthy is False
    assert_masked(result.error)


@pytest.mark.asyncio
async def test_collector_masks_long_userinfo_before_storing_holdings_error(monkeypatch):
    assert len(PROXY_URL) <= 255
    assert "@" not in ERROR[:240]
    collector = BinanceBapiCollector()
    monkeypatch.setattr(collector, "fetch_detail", AsyncMock(return_value={"positionShow": True, "marginBalance": 1000}))
    monkeypatch.setattr(collector, "sync_orders_into_store", AsyncMock(return_value=0))
    monkeypatch.setattr(collector, "fetch_position_history", AsyncMock(return_value={"list": [], "total": 0}))
    monkeypatch.setattr(collector, "fetch_holdings", AsyncMock(side_effect=RuntimeError(ERROR)))
    result = await collector.sync_snapshot("synthetic", {}, [], proxy_url=PROXY_URL, with_analysis=False)
    assert result["openPositionsMeta"]["authoritative"] is False
    assert_masked(result["openPositionsMeta"]["error"])


@pytest.mark.asyncio
async def test_collector_masks_http_error_body_before_truncation(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = Mock(status_code=502, text=ERROR)
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: client)
    with pytest.raises(RuntimeError) as error:
        await BinanceBapiCollector()._http_json("https://example.invalid", retries=1, proxy_url=PROXY_URL)
    assert_masked(str(error.value))
    assert mask_proxy_url(PROXY_URL) in str(error.value)
