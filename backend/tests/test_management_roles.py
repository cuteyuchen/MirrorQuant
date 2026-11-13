from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import select, update

from app.api import accounts, exposure, leads, proxies, settings, ws
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.security import CredentialVault, create_access_token
from app.models.domain import (
    AuditLog, BindingDebt, CopyBinding, LeadSource, LoginAttemptGuard,
    ProxyNode, SystemConfig, TradingAccount, User,
)
from app.services.proxy.manager import ProxyTestResult


MUTATIONS = [
    ("PUT", "/api/settings", {"publicMonitoringEnabled": False}, 200),
    ("POST", "/api/settings/apply-defaults-to-bindings", None, 200),
    ("POST", "/api/settings/test-notify", None, 200),
    ("POST", "/api/settings/security-guards/{guard}/unlock", None, 200),
    ("POST", "/api/proxies", {"name": "new-proxy", "url": "http://proxy.invalid:8080", "isEnabled": False}, 201),
    ("PUT", "/api/proxies/{proxy}", {"name": "updated-proxy"}, 200),
    ("DELETE", "/api/proxies/{proxy}", None, 200),
    ("POST", "/api/proxies/{proxy}/test", None, 200),
    ("POST", "/api/accounts", {"name": "new-account", "exchange": "binance", "apiKey": "synthetic-key",
                               "secretKey": "synthetic-secret", "isEnabled": False}, 201),
    ("PUT", "/api/accounts/{account}", {"name": "updated-account"}, 200),
    ("DELETE", "/api/accounts/{account}", None, 200),
    ("POST", "/api/accounts/{account}/test", None, 200),
    ("POST", "/api/accounts/{account}/sync", None, 200),
    ("POST", "/api/leads", {"name": "new-lead", "portfolioId": "new-portfolio", "isEnabled": False}, 201),
    ("PUT", "/api/leads/{lead}", {"name": "updated-lead"}, 200),
    ("DELETE", "/api/leads/{lead}", None, 200),
    ("POST", "/api/leads/{lead}/sync", None, 200),
    ("POST", "/api/bindings", {"name": "new-binding", "leadId": "{free_lead}", "accountId": "{account}",
                               "isEnabled": False}, 201),
    ("PUT", "/api/bindings/{binding}", {"name": "updated-binding"}, 200),
    ("DELETE", "/api/bindings/{binding}", None, 200),
    ("POST", "/api/bindings/{binding}/toggle", None, 200),
    ("POST", "/api/bindings/{binding}/reset-breaker", None, 200),
    ("POST", "/api/bindings/{binding}/reset-debt", None, 200),
    ("POST", "/api/bindings/kill-switch", {"enabled": True}, 200),
]


@pytest.fixture
async def management_state(monkeypatch):
    factory = get_session_factory()
    async with factory() as db:
        admin = User(username="role_admin", password_hash="unused", role="admin")
        viewer = User(username="role_viewer", password_hash="unused", role="viewer")
        # Offline Binance adapters still require an eligible egress route.
        proxy = ProxyNode(name="proxy", url="http://proxy.invalid:8080", is_enabled=True, is_healthy=True)
        lead = LeadSource(name="lead", portfolio_id="portfolio", is_enabled=False)
        free_lead = LeadSource(name="free-lead", portfolio_id="free-portfolio", is_enabled=False)
        account = TradingAccount(
            name="account", exchange="binance", environment="demo", is_enabled=False,
            api_key_masked="****", encrypted_credentials=CredentialVault().encrypt_dict({
                "api_key": "synthetic-key", "secret_key": "synthetic-secret",
            }),
        )
        guard = LoginAttemptGuard(
            guard_key="ip:192.0.2.1", key_type="ip", key_value="192.0.2.1", fail_count=7,
            locked_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db.add_all([admin, viewer, proxy, lead, free_lead, account, guard])
        await db.flush()
        binding = CopyBinding(name="binding", lead_id=lead.id, account_id=account.id, is_enabled=False,
                              circuit_breaker_tripped=True, circuit_breaker_reason="synthetic", consecutive_failures=3)
        db.add(binding)
        await db.flush()
        db.add(BindingDebt(binding_id=binding.id, symbol="BTCUSDT", position_side="LONG", pending_usd=25))
        await db.commit()
        ids = {"proxy": proxy.id, "lead": lead.id, "free_lead": free_lead.id,
               "account": account.id, "binding": binding.id, "guard": guard.id}
        tokens = {user.role: create_access_token(user.id, user.username, user.role) for user in (admin, viewer)}
        viewer_id = viewer.id

    adapter = SimpleNamespace(
        test_connection=AsyncMock(return_value={"ok": True, "usdtEquity": 100, "availEquity": 90}),
        get_positions=AsyncMock(return_value=[]), get_open_orders=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(accounts, "create_exchange_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(exposure, "create_exchange_adapter", lambda *args, **kwargs: adapter)
    sync_account = AsyncMock(return_value={"ok": True})
    sync_lead = AsyncMock(return_value={"ok": True})
    notification = AsyncMock(return_value={"webhook": {"ok": True}})
    test_proxy = AsyncMock(return_value=ProxyTestResult(healthy=True, latency_ms=1.5))
    monkeypatch.setattr(accounts.orchestrator, "sync_account", sync_account)
    monkeypatch.setattr(leads.orchestrator, "sync_lead", sync_lead)
    monkeypatch.setattr(settings.NotificationService, "dispatch", notification)
    monkeypatch.setattr(proxies.ProxyManager, "test_proxy", test_proxy)
    return SimpleNamespace(ids=ids, tokens=tokens, viewer_id=viewer_id, external_calls=[
        adapter.test_connection, adapter.get_positions, adapter.get_open_orders,
        sync_account, sync_lead, notification, test_proxy,
    ])


async def management_snapshot():
    async with get_session_factory()() as db:
        snapshot = {}
        for model in (AuditLog, BindingDebt, CopyBinding, LeadSource, LoginAttemptGuard,
                      ProxyNode, SystemConfig, TradingAccount):
            table = model.__table__
            snapshot[table.name] = list((await db.execute(select(table).order_by(*table.primary_key))).all())
        return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "anonymous", "admin"])
@pytest.mark.parametrize("method,path,payload,success_status", MUTATIONS,
                         ids=[f"{method} {path}" for method, path, _, _ in MUTATIONS])
async def test_management_mutations_require_current_admin(
    async_client, management_state, role, method, path, payload, success_status,
):
    state = management_state
    headers = {"Authorization": f"Bearer {state.tokens[role]}"} if role != "anonymous" else {}
    body = {
        key: int(value.format(**state.ids)) if isinstance(value, str) and value.startswith("{") else value
        for key, value in payload.items()
    } if payload is not None else None
    before = await management_snapshot() if role != "admin" else None
    response = await async_client.request(method, path.format(**state.ids), json=body, headers=headers)
    expected = success_status if role == "admin" else 403 if role == "viewer" else 401
    assert response.status_code == expected
    if role != "admin":
        assert await management_snapshot() == before
        for external_call in state.external_calls:
            external_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/settings", "/api/settings/security-guards", "/api/settings/audit-logs",
    "/api/proxies", "/api/proxies/{proxy}", "/api/accounts", "/api/leads",
    "/api/leads/{lead}/snapshot", "/api/leads/{lead}/analysis", "/api/leads/{lead}/export-csv",
    "/api/bindings", "/api/dashboard/overview",
])
async def test_viewers_retain_read_only_management_access(async_client, management_state, path):
    state = management_state
    response = await async_client.get(path.format(**state.ids), headers={"Authorization": f"Bearer {state.tokens['viewer']}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_gate_uses_current_database_role_over_token_claim(async_client, management_state):
    state = management_state
    claimed_admin = create_access_token(state.viewer_id, "role_viewer", "admin")
    headers = {"Authorization": f"Bearer {claimed_admin}"}
    assert (await async_client.put("/api/settings", json={}, headers=headers)).status_code == 403
    async with get_session_factory()() as db:
        await db.execute(update(User).where(User.id == state.viewer_id).values(role="operator"))
        await db.commit()
    assert (await async_client.put("/api/settings", json={}, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_viewer_can_receive_broadcasts_and_ping(management_state):
    # The socket exposes broadcasts and ping only; there are no admin channels.
    class ViewerSocket(SimpleNamespace):
        __hash__ = object.__hash__

    socket = ViewerSocket(
        headers={"origin": "http://localhost:8000"},
        cookies={get_settings().cookie_name: management_state.tokens["viewer"]},
        close=AsyncMock(), accept=AsyncMock(), send_json=AsyncMock(),
        receive_text=AsyncMock(side_effect=["ping", WebSocketDisconnect()]),
    )
    await ws.websocket_stream(socket)
    socket.accept.assert_awaited_once()
    socket.close.assert_not_awaited()
    assert [call.args[0]["type"] for call in socket.send_json.await_args_list] == ["connected", "pong"]
