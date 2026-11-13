import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api.accounts import test_account_connectivity as account_connectivity
from app.api.exposure import require_account_flat
from app.api.leads import analyze_lead_symbol
from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import LeadSource, ProxyNode, TradingAccount
from app.services.engine.orchestrator import orchestrator
from app.services.proxy.manager import ProxyEgressError, ProxyManager


def node(node_id=1, *, enabled=True, healthy=True, priority=10, latency=10, url=None):
    return ProxyNode(id=node_id, name=f"node-{node_id}", url=url or f"http://127.0.0.1:{8000 + node_id}",
                     is_enabled=enabled, is_healthy=healthy, priority=priority, latency_ms=latency)


@pytest.fixture(autouse=True)
def disallow_direct_egress(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", False)


def test_direct_egress_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("MQ_ALLOW_DIRECT_EGRESS")
    assert Settings(_env_file=None).allow_direct_egress is True


def test_healthy_preferred_proxy_is_selected_first():
    nodes = [node(1, priority=1), node(2, priority=100)]
    assert ProxyManager.select_proxy_url(nodes, preferred_proxy_id=2) == nodes[1].url
    assert ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id=2) == [nodes[1].url, nodes[0].url]


@pytest.mark.parametrize("enabled, healthy", [(False, True), (True, False)])
def test_unavailable_preferred_proxy_falls_through(enabled, healthy):
    nodes = [node(1, enabled=enabled, healthy=healthy), node(2)]
    assert ProxyManager.select_proxy_url(nodes, preferred_proxy_id=1) == nodes[1].url
    assert ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id=1) == [nodes[1].url]


def test_failover_orders_only_healthy_nodes_without_direct_egress():
    nodes = [node(1, priority=1, latency=None), node(2, priority=1, latency=5),
             node(3, priority=0, healthy=False), node(4, priority=2),
             node(5, priority=1, latency=6, url="http://127.0.0.1:8002")]
    assert ProxyManager.ordered_proxy_urls(nodes) == [nodes[1].url, nodes[0].url, nodes[3].url]


@pytest.mark.parametrize("nodes", [[], [node(enabled=False)], [node(healthy=False)]])
@pytest.mark.parametrize("select", [ProxyManager.select_proxy_url, ProxyManager.ordered_proxy_urls])
def test_unavailable_egress_fails_closed(nodes, select):
    with pytest.raises(ProxyEgressError, match="No healthy enabled proxy.*direct egress is disabled"):
        select(nodes)


@pytest.mark.parametrize("nodes", [[], [node(enabled=False)], [node(healthy=False)]])
def test_explicit_opt_in_permits_direct_when_no_healthy_proxy(monkeypatch, nodes):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", True)
    assert ProxyManager.select_proxy_url(nodes) is None
    assert ProxyManager.ordered_proxy_urls(nodes) == [None]


def test_direct_fallback_is_only_appended_after_explicit_opt_in(monkeypatch):
    nodes = [node()]
    monkeypatch.setattr(get_settings(), "allow_direct_egress", True)
    assert ProxyManager.ordered_proxy_urls(nodes) == [nodes[0].url, None]


def test_client_rejects_direct_bypass_before_creating_transport(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr("app.services.proxy.manager.httpx.AsyncClient", constructor)
    with pytest.raises(ProxyEgressError, match="Direct egress is disabled"):
        ProxyManager.build_client()
    constructor.assert_not_called()


@pytest.mark.parametrize("direct", [False, True])
def test_client_uses_explicit_proxy_or_authorized_direct(monkeypatch, direct):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct)
    constructor = Mock()
    monkeypatch.setattr("app.services.proxy.manager.httpx.AsyncClient", constructor)
    proxy_url = None if direct else node().url
    ProxyManager.build_client(proxy_url=proxy_url)
    assert constructor.call_args.kwargs.get("proxy") == proxy_url


async def seed_account(db):
    account = TradingAccount(name="synthetic", exchange="binance", api_key_masked="synthetic",
                             encrypted_credentials=CredentialVault().encrypt_dict({}))
    db.add(account)
    await db.commit()
    return account


@pytest.mark.asyncio
async def test_unassigned_account_uses_configured_proxy_for_connectivity(monkeypatch):
    adapter = SimpleNamespace(test_connection=AsyncMock(return_value={"ok": True}))
    factory = Mock(return_value=adapter)
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", factory)
    async with get_session_factory()() as db:
        proxy = node()
        db.add(proxy)
        account = await seed_account(db)
        result = await account_connectivity(account.id, db=db, _user=None)
        assert result["ok"] is True
        assert factory.call_args.kwargs["proxy_url"] == proxy.url


@pytest.mark.asyncio
async def test_account_connectivity_reports_denied_egress_without_adapter(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", factory)
    async with get_session_factory()() as db:
        account = await seed_account(db)
        with pytest.raises(HTTPException) as error:
            await account_connectivity(account.id, db=db, _user=None)
        assert error.value.status_code == 503
        assert "direct egress is disabled" in error.value.detail
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_flat_account_verification_uses_configured_proxy(monkeypatch):
    adapter = SimpleNamespace(get_positions=AsyncMock(return_value=[]), get_open_orders=AsyncMock(return_value=[]))
    factory = Mock(return_value=adapter)
    monkeypatch.setattr("app.api.exposure.create_exchange_adapter", factory)
    async with get_session_factory()() as db:
        proxy = node()
        db.add(proxy)
        account = await seed_account(db)
        await require_account_flat(db, account)
        assert factory.call_args.args[3] == proxy.url


@pytest.mark.parametrize("explicit_symbol", [False, True])
@pytest.mark.asyncio
async def test_lead_analysis_uses_proxy_and_refreshes_saved_analysis(monkeypatch, explicit_symbol):
    current_analysis = {"ok": True, "stale": True, "asOf": 123, "analyzedAt": 123}
    analysis = AsyncMock(return_value=current_analysis)
    monkeypatch.setattr(orchestrator.pa_analyzer, "analyze_position", analysis)
    async with get_session_factory()() as db:
        proxy = node()
        lead = LeadSource(name="synthetic", portfolio_id="synthetic", snapshot_json=json.dumps({
            "openPositions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "avgEntry": 100,
                               "analysis": {"ok": True, "stale": False, "analyzedAt": 1}}],
        }))
        db.add_all([proxy, lead])
        await db.commit()
        result = await analyze_lead_symbol(lead.id, symbol="BTCUSDT" if explicit_symbol else None,
                                           avg_entry=100 if explicit_symbol else None, db=db, _user=None)
        assert result == {"items": [current_analysis]}
        assert analysis.await_args.kwargs["proxy_url"] == proxy.url


@pytest.mark.asyncio
async def test_lead_analysis_reports_denied_egress_without_network(monkeypatch):
    analysis = AsyncMock()
    monkeypatch.setattr(orchestrator.pa_analyzer, "analyze_position", analysis)
    async with get_session_factory()() as db:
        lead = LeadSource(name="synthetic", portfolio_id="synthetic")
        db.add(lead)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await analyze_lead_symbol(lead.id, symbol="BTCUSDT", avg_entry=100, db=db, _user=None)
        assert error.value.status_code == 503
    analysis.assert_not_awaited()
