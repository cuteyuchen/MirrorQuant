import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import CopyBinding, LeadSource, ProxyNode, TradingAccount
from app.services.adapters.base import BalanceInfo
from app.services.engine.orchestrator import EngineOrchestrator


@pytest.fixture
async def engine_data(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", False)
    async with get_session_factory()() as db:
        lead = LeadSource(name="synthetic", portfolio_id="synthetic")
        account = TradingAccount(name="synthetic", exchange="binance", api_key_masked="synthetic",
                                 encrypted_credentials=CredentialVault().encrypt_dict({}))
        proxy = ProxyNode(name="synthetic", url="http://127.0.0.1:8080")
        db.add_all([lead, account, proxy])
        await db.flush()
        db.add(CopyBinding(name="synthetic", lead_id=lead.id, account_id=account.id))
        await db.commit()
        yield SimpleNamespace(db=db, lead=lead, account=account, proxy=proxy, engine=EngineOrchestrator())


@pytest.mark.parametrize("direct_enabled", [False, True])
@pytest.mark.parametrize("path", ["sync_account", "risk_tick", "router"])
@pytest.mark.asyncio
async def test_unassigned_engine_account_uses_healthy_configured_proxy(engine_data, monkeypatch, path, direct_enabled):
    data = engine_data
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct_enabled)
    adapter = SimpleNamespace(
        sync_time=AsyncMock(return_value=0),
        get_usdt_balance=AsyncMock(return_value=BalanceInfo(eq=1000, avail_eq=1000, cash_bal=1000, upl=0)),
        get_positions=AsyncMock(return_value=[]),
        get_instruments=AsyncMock(return_value={}),
    )
    factory = Mock(return_value=adapter)
    module = "router" if path == "router" else "orchestrator"
    monkeypatch.setattr(f"app.services.engine.{module}.create_exchange_adapter", factory)
    if path == "sync_account":
        result = await data.engine.sync_account(data.db, data.account)
        assert result["ok"] is True
    elif path == "risk_tick":
        monkeypatch.setattr(data.engine, "_check_account_equity", AsyncMock())
        guards = AsyncMock()
        monkeypatch.setattr(data.engine.router, "check_position_guards", guards)
        await data.engine.risk_tick()
        guards.assert_awaited_once()
    else:
        reconcile = AsyncMock(return_value=True)
        monkeypatch.setattr(data.engine.router, "reconcile_pending_orders", reconcile)
        snapshot = {"openPositions": [], "openPositionsMeta": {
            "source": "api", "complete": True, "authoritative": True, "positionShow": True,
            "asOf": int(time.time() * 1000),
        }}
        await data.engine.router.process_lead_events(data.db, data.lead, [], [], snapshot, [data.proxy])
        reconcile.assert_awaited_once()
    factory.assert_called_once()
    assert factory.call_args.kwargs["proxy_url"] == data.proxy.url


@pytest.mark.asyncio
async def test_lead_sync_reports_unavailable_proxy_without_contacting_upstream(engine_data, monkeypatch):
    data = engine_data
    data.proxy.is_healthy = False
    await data.db.commit()
    collect = AsyncMock()
    monkeypatch.setattr(data.engine.collector, "sync_snapshot", collect)
    result = await data.engine.sync_lead(data.db, data.lead)
    assert result["ok"] is False
    assert "No healthy enabled proxy" in result["error"]
    assert data.lead.status == "error"
    collect.assert_not_awaited()


@pytest.mark.parametrize("direct_enabled", [False, True])
@pytest.mark.asyncio
async def test_binance_lead_failover_never_attempts_direct(engine_data, monkeypatch, direct_enabled):
    data = engine_data
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct_enabled)
    collect = AsyncMock(side_effect=RuntimeError("Synthetic upstream outage"))
    monkeypatch.setattr(data.engine.collector, "sync_snapshot", collect)
    result = await data.engine.sync_lead(data.db, data.lead)
    assert result["ok"] is False
    attempted = [call.kwargs["proxy_url"] for call in collect.await_args_list]
    assert attempted == [data.proxy.url]
