import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_api_full_workflow(async_client: AsyncClient):
    # 1. Health check
    res = await async_client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # 2. Check initial auth status (uninitialized)
    res = await async_client.get("/api/auth/status")
    assert res.status_code == 200
    assert res.json()["initialized"] is False

    # 3. Setup admin
    res = await async_client.post(
        "/api/auth/setup",
        json={"username": "superadmin", "password": "SecurePassword2026!"},
    )
    assert res.status_code == 201
    token = res.json()["token"]
    assert token

    headers = {"Authorization": f"Bearer {token}"}

    # 4. Check me
    res = await async_client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200
    assert res.json()["user"]["username"] == "superadmin"

    # 5. Create Proxy
    res = await async_client.post(
        "/api/proxies",
        headers=headers,
        json={"name": "Tokyo-Node-1", "url": "http://127.0.0.1:7890", "region": "JP", "priority": 10},
    )
    assert res.status_code == 201
    proxy_id = res.json()["item"]["id"]

    # 6. Create Lead
    res = await async_client.post(
        "/api/leads",
        headers=headers,
        json={
            "name": "Alpha-Lead-1",
            "platform": "binance_bapi",
            "portfolioId": "3829104719284719283",
            "pollIntervalSec": 15,
            "proxyId": proxy_id,
            "isPublicDashboard": True,
        },
    )
    assert res.status_code == 201
    lead_id = res.json()["item"]["id"]

    # 7. Create OKX Demo Account
    res = await async_client.post(
        "/api/accounts",
        headers=headers,
        json={
            "name": "My-OKX-Demo",
            "exchange": "okx",
            "environment": "demo",
            "api_key": "test_okx_key_12345",
            "secret_key": "test_okx_secret_67890",
            "passphrase": "test_okx_passphrase",
            "proxyId": proxy_id,
        },
    )
    assert res.status_code == 201
    account_id = res.json()["item"]["id"]

    # 8. Create Copy Binding
    res = await async_client.post(
        "/api/bindings",
        headers=headers,
        json={
            "name": "Alpha -> OKX Demo",
            "lead_id": lead_id,
            "account_id": account_id,
            "risk_scale": 1.5,
            "max_drawdown_pct": 25.0,
            "max_slippage_pct": 0.8,
        },
    )
    assert res.status_code == 201
    binding_id = res.json()["item"]["id"]

    # 9. Test Kill Switch Toggle
    res = await async_client.post("/api/bindings/kill-switch", headers=headers, json={"enabled": True})
    assert res.status_code == 200
    assert res.json()["globalKillSwitch"] is True

    # 10. Dashboard Overview
    res = await async_client.get("/api/dashboard/overview", headers=headers)
    assert res.status_code == 200
    kpis = res.json()["kpis"]
    assert kpis["totalLeads"] == 1
    assert kpis["totalAccounts"] == 1
    assert kpis["activeBindings"] == 1
    assert kpis["globalKillSwitch"] is True

    # 11. Public Feed
    res = await async_client.get("/api/dashboard/public")
    assert res.status_code == 200
    assert res.json()["enabled"] is True
    assert len(res.json()["leads"]) == 1
    assert res.json()["leads"][0]["name"] == "Alpha-Lead-1"

    # 12. Settings View & Update
    res = await async_client.get("/api/settings", headers=headers)
    assert res.status_code == 200
    assert res.json()["security"]["encryptionAlgorithm"] == "AES-256-GCM"

    res = await async_client.put(
        "/api/settings",
        headers=headers,
        json={"notifications": {"webhook_enabled": True, "webhook_url": "https://example.com/webhook"}},
    )
    assert res.status_code == 200
    assert res.json()["notifications"]["webhook_enabled"] is True

    # 13. Audit logs
    res = await async_client.get("/api/settings/audit-logs", headers=headers)
    assert res.status_code == 200
    assert len(res.json()["items"]) > 0

    # 14. Lead CSV export
    res = await async_client.get(f"/api/leads/{lead_id}/export-csv?export_type=orders", headers=headers)
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]

    res = await async_client.get(f"/api/leads/{lead_id}/export-csv?export_type=cycles", headers=headers)
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]

    # 15. Proxy test endpoint (mocked)
    from unittest.mock import AsyncMock, patch
    from app.services.proxy.manager import ProxyTestResult

    with patch(
        "app.api.proxies.ProxyManager.test_proxy",
        new=AsyncMock(return_value=ProxyTestResult(healthy=True, latency_ms=42.5, error=None)),
    ):
        res = await async_client.post(f"/api/proxies/{proxy_id}/test", headers=headers)
        assert res.status_code == 200
        assert res.json()["test"]["healthy"] is True
        assert res.json()["test"]["latencyMs"] == 42.5

    # 16. Login + Cookie auth test
    res = await async_client.post(
        "/api/auth/login",
        json={"username": "superadmin", "password": "SecurePassword2026!"},
    )
    assert res.status_code == 200
    assert "mq_access_token" in res.cookies

    # 17. WebSocket connection test (sync TestClient for WS handshake)
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/stream?token={token}") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "connected"
            assert msg["user"] == "superadmin"
            ws.send_text("ping")
            pong = ws.receive_json()
            assert pong["type"] == "pong"
