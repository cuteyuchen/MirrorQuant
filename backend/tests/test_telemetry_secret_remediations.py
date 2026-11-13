import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.database import get_session_factory
from app.core.secret_mask import (
    ERROR_FIELDS, MASK_SENTINEL, mask_proxy_url, mask_sensitive_payload,
    proxy_credentials_configured, redact_error_urls,
)
from app.core.security import CredentialVault, create_access_token
from app.models.domain import AuditLog, CopyBinding, ExecutionRecord, LeadSource, ProxyNode, TradingAccount, User
from app.services.engine.orchestrator import orchestrator
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.proxy.manager import ProxyManager


PROXY_URL = "http://synthetic-user:synthetic-pass-" + "x" * 190 + "@proxy.invalid:8080"
ERROR = "Synthetic connection failure: " + PROXY_URL


def assert_masked(value):
    encoded = json.dumps(value)
    assert "synthetic-user" not in encoded
    assert "synthetic-pass" not in encoded
    assert "***:***@proxy.invalid:8080" in encoded or "[redacted URL]" in encoded


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
    async with get_session_factory()() as db:
        db.add(ProxyNode(name="synthetic", url="http://proxy.invalid:8080", is_enabled=True, is_healthy=True))
        await db.commit()
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


@pytest.mark.parametrize("error", [
    "Failure http://synthetic-user:synthetic-pass",
    "Failure http://synthetic-user:synthetic-pass second-fragment",
    "Failure http://synthetic-user:synthetic-pass second-fragment@proxy.invalid:8080",
    "Failure socks5://synthetic-user:synthetic-pass\nsecond-fragment",
])
def test_error_redaction_handles_historical_truncation_and_whitespace(error):
    assert redact_error_urls(error) == "Failure [redacted URL]"


@pytest.mark.parametrize("field", sorted(ERROR_FIELDS))
def test_recursive_error_fields_hide_partial_credentials(field):
    error = "Failure http://synthetic-user:synthetic-pass second-fragment"
    result = mask_sensitive_payload({field: [{"message": error}]})
    assert result == {field: [{"message": "Failure [redacted URL]"}]}


def test_actual_proxy_urls_mask_literal_whitespace_userinfo_and_keep_host():
    url = "http://synthetic-user:synthetic-pass second-fragment@proxy.invalid:8080"
    expected = "http://***:***@proxy.invalid:8080"
    assert mask_proxy_url(url) == expected
    assert proxy_credentials_configured(url) is True
    result = mask_sensitive_payload({"url": url, "proxyUrl": url, "proxy_url": url,
                                     "avatarUrl": "https://images.invalid/avatar.png"})
    assert result == {"url": expected, "proxyUrl": expected, "proxy_url": expected,
                      "avatarUrl": "https://images.invalid/avatar.png"}


@pytest.mark.parametrize("url", ["http://synthetic-user:synthetic-pass", "http://[broken"])
def test_malformed_proxy_urls_fail_safe(url):
    assert mask_proxy_url(url) == MASK_SENTINEL


@pytest.mark.parametrize("endpoint", [
    "proxies", "proxy_detail", "snapshot", "public", "overview", "executions", "accounts", "leads", "bindings", "audit",
])
@pytest.mark.asyncio
async def test_historical_partial_credentials_hidden_on_all_diagnostic_reads(telemetry, endpoint):
    client, lead_id, account_id, snapshot = telemetry
    historical = "Failure http://synthetic-user:synthetic-pass second-fragment"
    snapshot["openPositionsMeta"]["error"] = historical
    snapshot["openPositions"][0]["analysis"]["reason"] = historical
    snapshot["profile"] = {"avatarUrl": "https://images.invalid/avatar.png"}
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, lead_id)
        lead.last_error = historical
        lead.snapshot_json = json.dumps(snapshot)
        (await db.get(TradingAccount, account_id)).last_error = historical
        proxy = ProxyNode(name="synthetic", url=PROXY_URL, last_error=historical)
        record = ExecutionRecord(lead_id=lead_id, account_id=account_id, lead_name="synthetic", account_name="synthetic",
                                 symbol="BTCUSDT", inst_id="BTCUSDT", side="BUY", position_side="LONG",
                                 action="OPEN", status="ERROR", details=historical)
        binding = CopyBinding(name="synthetic", lead_id=lead_id, account_id=account_id, circuit_breaker_reason=historical)
        audit = AuditLog(username="synthetic", action="TEST", resource_type="synthetic", details=historical)
        db.add_all([proxy, record, binding, audit])
        await db.commit()
        proxy_id = proxy.id
    path = {
        "proxies": "/api/proxies", "proxy_detail": f"/api/proxies/{proxy_id}",
        "snapshot": f"/api/leads/{lead_id}/snapshot", "public": "/api/dashboard/public",
        "overview": "/api/dashboard/overview", "executions": "/api/dashboard/executions",
        "accounts": "/api/accounts", "leads": "/api/leads", "bindings": "/api/bindings",
        "audit": "/api/settings/audit-logs",
    }[endpoint]
    if endpoint == "public":
        client.headers.pop("Authorization")
    response = await client.get(path)
    assert response.status_code == 200
    assert "synthetic-user" not in response.text and "synthetic-pass" not in response.text
    assert "second-fragment" not in response.text
    assert "[redacted URL]" in response.text
    if endpoint in ("proxies", "proxy_detail"):
        item = response.json()["items"][0] if endpoint == "proxies" else response.json()["item"]
        assert item["url"] == "http://***:***@proxy.invalid:8080"
    if endpoint in ("snapshot", "public"):
        assert "https://images.invalid/avatar.png" in response.text
