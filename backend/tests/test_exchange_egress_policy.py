"""Offline regressions for exchange policy precedence and account routing."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from app.api.accounts import test_account_connectivity as account_connectivity
from app.api.exposure import require_account_flat
from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import CopyBinding, LeadSource, ProxyNode, TradingAccount
from app.services.adapters.base import BalanceInfo
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.okx import OKXAdapter
from app.services.analysis.pa_analyzer import PAAnalyzer
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.proxy.manager import ProxyEgressError, ProxyManager


@pytest.fixture(autouse=True)
def exchange_policy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allow_direct_egress", False)
    monkeypatch.setattr(settings, "proxy_required_exchanges", ["binance"])
    monkeypatch.setattr(settings, "direct_egress_exchanges", ["okx", "bybit", "bitget"])


def proxy(node_id=1, **kwargs):
    return ProxyNode(id=node_id, name="synthetic", url=f"http://127.0.0.1:{8000 + node_id}",
                     is_enabled=True, is_healthy=True, priority=10, **kwargs)


def test_default_exchange_lists():
    settings = Settings(_env_file=None)
    assert settings.proxy_required_exchanges == ["binance"]
    assert settings.direct_egress_exchanges == ["okx", "bybit", "bitget"]


@pytest.mark.parametrize("required, direct", [
    (" Binance , BYBIT, ", " OKX, Bitget, "),
    ('["Binance", "BYBIT"]', '["OKX", "Bitget"]'),
])
def test_exchange_lists_accept_environment_overrides(monkeypatch, required, direct):
    monkeypatch.setenv("MQ_PROXY_REQUIRED_EXCHANGES", required)
    monkeypatch.setenv("MQ_DIRECT_EGRESS_EXCHANGES", direct)
    settings = Settings(_env_file=None)
    assert settings.proxy_required_exchanges == ["binance", "bybit"]
    assert settings.direct_egress_exchanges == ["okx", "bitget"]


def test_empty_exchange_lists_accept_environment_overrides(monkeypatch):
    monkeypatch.setenv("MQ_PROXY_REQUIRED_EXCHANGES", "")
    monkeypatch.setenv("MQ_DIRECT_EGRESS_EXCHANGES", "[]")
    settings = Settings(_env_file=None)
    assert settings.proxy_required_exchanges == settings.direct_egress_exchanges == []


@pytest.mark.parametrize("exchange", ["okx", "bybit", "bitget", " OKX "])
async def test_allowlisted_exchange_builds_direct_client_despite_global_false(exchange, monkeypatch):
    # An ambient proxy must never silently reroute an explicitly direct client.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    async with ProxyManager.build_client(exchange=exchange) as client:
        assert isinstance(client, httpx.AsyncClient)
        assert client._trust_env is False
        assert not client._mounts


@pytest.mark.parametrize("direct_enabled", [False, True])
@pytest.mark.parametrize("allowlisted", [False, True])
def test_binance_direct_is_rejected_before_transport_creation(monkeypatch, direct_enabled, allowlisted):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct_enabled)
    if allowlisted:
        monkeypatch.setattr(get_settings(), "direct_egress_exchanges", ["binance", "okx"])
    constructor = Mock()
    monkeypatch.setattr("app.services.proxy.manager.httpx.AsyncClient", constructor)
    with pytest.raises(ProxyEgressError):
        ProxyManager.build_client(exchange="binance")
    constructor.assert_not_called()


@pytest.mark.parametrize("availability", ["missing", "disabled", "unhealthy"])
@pytest.mark.parametrize("direct_enabled", [False, True])
def test_binance_no_healthy_proxy_fails_closed(monkeypatch, availability, direct_enabled):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct_enabled)
    monkeypatch.setattr(get_settings(), "direct_egress_exchanges", ["binance", "okx"])
    node = proxy()
    node.is_enabled = availability != "disabled"
    node.is_healthy = availability != "unhealthy"
    nodes = [] if availability == "missing" else [node]
    for select in (ProxyManager.select_proxy_url, ProxyManager.ordered_proxy_urls):
        with pytest.raises(ProxyEgressError):
            select(nodes, preferred_proxy_id=node.id, exchange="binance")


@pytest.mark.parametrize("direct_enabled", [False, True])
def test_binance_ordered_routes_never_include_direct(monkeypatch, direct_enabled):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct_enabled)
    monkeypatch.setattr(get_settings(), "direct_egress_exchanges", ["binance"])
    nodes = [proxy(1), proxy(2)]
    assert ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id=2, exchange="binance") == [
        nodes[1].url, nodes[0].url,
    ]


def test_okx_prefers_configured_healthy_proxy_and_appends_direct(monkeypatch):
    nodes = [proxy(1), proxy(2)]
    nodes[0].priority = 1
    selected = ProxyManager.select_proxy_url(nodes, preferred_proxy_id=2, exchange="okx")
    assert selected == nodes[1].url
    assert ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id=2, exchange="okx") == [
        nodes[1].url, None,
    ]
    constructor = Mock()
    monkeypatch.setattr("app.services.proxy.manager.httpx.AsyncClient", constructor)
    ProxyManager.build_client(proxy_url=selected, exchange="okx")
    assert constructor.call_args.kwargs["proxy"] == selected
    assert constructor.call_args.kwargs["trust_env"] is False


@pytest.mark.parametrize("exchange", ["okx", "bybit", "bitget", " OKX "])
def test_direct_exchange_unassigned_proxy_orders_direct_only_with_healthy_pool(exchange):
    node = proxy()
    assert ProxyManager.ordered_proxy_urls([node], preferred_proxy_id=None, exchange=exchange) == [None]


def test_okx_unassigned_proxy_selects_direct_with_healthy_pool():
    node = proxy()
    assert ProxyManager.select_proxy_url([node], preferred_proxy_id=None, exchange="okx") is None


@pytest.mark.parametrize("preferred_proxy_id", [1, 2])
def test_okx_explicit_proxy_excludes_pool_favourite(preferred_proxy_id):
    favourite, assigned = proxy(3 - preferred_proxy_id), proxy(preferred_proxy_id)
    favourite.priority = 1
    assert ProxyManager.select_proxy_url([favourite, assigned], preferred_proxy_id=assigned.id, exchange="okx") == assigned.url
    assert ProxyManager.ordered_proxy_urls([favourite, assigned], preferred_proxy_id=assigned.id, exchange="okx") == [
        assigned.url, None,
    ]


def test_okx_unhealthy_assigned_proxy_does_not_borrow_healthy_pool():
    assigned, other = proxy(1), proxy(2)
    assigned.is_healthy = False
    assert ProxyManager.ordered_proxy_urls([assigned, other], preferred_proxy_id=assigned.id, exchange="okx") == [None]
    assert ProxyManager.select_proxy_url([assigned, other], preferred_proxy_id=assigned.id, exchange="okx") is None


def test_okx_missing_assigned_proxy_does_not_borrow_healthy_pool():
    nodes = [proxy(1), proxy(2)]
    assert ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id=99, exchange="okx") == [None]
    assert ProxyManager.select_proxy_url(nodes, preferred_proxy_id=99, exchange="okx") is None


def test_binance_unassigned_proxy_uses_healthy_pool_without_direct():
    node = proxy()
    assert ProxyManager.ordered_proxy_urls([node], preferred_proxy_id=None, exchange="binance") == [node.url]
    assert ProxyManager.select_proxy_url([node], preferred_proxy_id=None, exchange="binance") == node.url


def test_binance_unassigned_proxy_with_empty_pool_fails_closed():
    for select in (ProxyManager.ordered_proxy_urls, ProxyManager.select_proxy_url):
        with pytest.raises(ProxyEgressError):
            select([], preferred_proxy_id=None, exchange="binance")


async def test_okx_explicit_none_builds_direct_client_despite_global_false():
    assert get_settings().allow_direct_egress is False
    async with ProxyManager.build_client(proxy_url=None, exchange="okx") as client:
        assert isinstance(client, httpx.AsyncClient)
        assert client._trust_env is False
        assert not client._mounts


@pytest.mark.parametrize("availability", ["missing", "disabled", "unhealthy"])
async def test_okx_unavailable_configured_proxy_falls_back_to_direct(availability):
    node = proxy()
    node.is_enabled = availability != "disabled"
    node.is_healthy = availability != "unhealthy"
    nodes = [proxy(2)] if availability == "missing" else [node, proxy(2)]
    assert ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id=node.id, exchange="okx") == [None]
    selected = ProxyManager.select_proxy_url(nodes, preferred_proxy_id=node.id, exchange="okx")
    async with ProxyManager.build_client(proxy_url=selected, exchange="okx") as client:
        assert not client._mounts


@pytest.mark.parametrize("exchange", [None, "unknown"])
@pytest.mark.parametrize("direct_enabled", [False, True])
async def test_unknown_exchange_preserves_global_policy(monkeypatch, exchange, direct_enabled):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", direct_enabled)
    node = proxy()
    assert ProxyManager.ordered_proxy_urls([node], exchange=exchange) == (
        [node.url, None] if direct_enabled else [node.url]
    )
    if direct_enabled:
        assert ProxyManager.select_proxy_url([], exchange=exchange) is None
        async with ProxyManager.build_client(exchange=exchange):
            pass
    else:
        with pytest.raises(ProxyEgressError):
            ProxyManager.select_proxy_url([], exchange=exchange)
        with pytest.raises(ProxyEgressError):
            ProxyManager.build_client(exchange=exchange)


@pytest.mark.parametrize("kind", [OKXAdapter, BinanceAdapter, BybitAdapter, BitgetAdapter])
async def test_adapters_pass_inherited_exchange_identity(monkeypatch, kind):
    def respond(request):
        payload = {
            OKXAdapter: {"code": "0", "data": []}, BinanceAdapter: {},
            BybitAdapter: {"retCode": 0, "result": {}}, BitgetAdapter: {"code": "00000", "data": []},
        }[kind]
        return httpx.Response(200, json=payload)

    constructor = Mock(side_effect=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(ProxyManager, "build_client", constructor)

    class AccountAdapter(kind):
        pass

    adapter = AccountAdapter({})
    await adapter._request("GET", "/synthetic", auth=False)
    assert constructor.call_args.kwargs["exchange"] == adapter.exchange


@pytest.mark.parametrize("path", ["bapi", "fapi", "analysis"])
async def test_binance_public_clients_fail_closed_without_proxy(monkeypatch, path):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", True)
    with pytest.raises(ProxyEgressError):
        if path == "bapi":
            await BinanceBapiCollector()._http_json("https://www.binance.com/synthetic")
        elif path == "fapi":
            await BinanceBapiCollector()._fapi_get("/synthetic")
        else:
            await PAAnalyzer().fetch_klines("BTCUSDT")


@pytest.mark.parametrize("exchange, assigned_proxy", [("okx", True), ("binance", True), ("okx", False)])
@pytest.mark.parametrize("path", ["sync_account", "risk_tick", "router", "connectivity", "flat"])
async def test_account_proxy_applies_policy_at_every_entry_point(monkeypatch, exchange, assigned_proxy, path):
    monkeypatch.setattr(get_settings(), "allow_direct_egress", True)
    adapter = SimpleNamespace(
        sync_time=AsyncMock(return_value=0), get_instruments=AsyncMock(return_value={}),
        get_usdt_balance=AsyncMock(return_value=BalanceInfo(eq=1000, avail_eq=1000, cash_bal=1000, upl=0)),
        get_positions=AsyncMock(return_value=[]), get_open_orders=AsyncMock(return_value=[]),
        test_connection=AsyncMock(return_value={"ok": True}),
    )
    factory = Mock(return_value=adapter)
    module = {"connectivity": "app.api.accounts", "flat": "app.api.exposure", "router": "app.services.engine.router"}.get(
        path, "app.services.engine.orchestrator",
    )
    monkeypatch.setattr(f"{module}.create_exchange_adapter", factory)
    async with get_session_factory()() as db:
        node = proxy()
        node.is_healthy = False
        nodes = [node, proxy(2)] if exchange == "okx" else [node]
        lead = LeadSource(name="synthetic", portfolio_id="synthetic")
        account = TradingAccount(name="synthetic", exchange=exchange, proxy_id=node.id if assigned_proxy else None, api_key_masked="synthetic",
                                 encrypted_credentials=CredentialVault().encrypt_dict({}))
        db.add_all([*nodes, lead, account])
        await db.flush()
        db.add(CopyBinding(name="synthetic", lead_id=lead.id, account_id=account.id))
        await db.commit()
        engine = EngineOrchestrator()
        if path == "sync_account":
            result = await engine.sync_account(db, account)
            assert result["ok"] is (exchange == "okx")
        elif path == "risk_tick":
            guards = AsyncMock()
            monkeypatch.setattr(engine.router, "check_position_guards", guards)
            monkeypatch.setattr(engine, "_check_account_equity", AsyncMock())
            await engine.risk_tick()
            assert guards.await_count == (1 if exchange == "okx" else 0)
        elif path == "router":
            reconcile = AsyncMock(return_value=True)
            monkeypatch.setattr(engine.router, "reconcile_pending_orders", reconcile)
            snapshot = {"openPositions": [], "openPositionsMeta": {
                "source": "api", "complete": True, "authoritative": True, "positionShow": True,
                "asOf": int(time.time() * 1000),
            }}
            await engine.router.process_lead_events(db, lead, [], [], snapshot, nodes)
            assert reconcile.await_count == (1 if exchange == "okx" else 0)
        elif exchange == "binance":
            with pytest.raises(HTTPException):
                if path == "connectivity":
                    await account_connectivity(account.id, db=db, _user=None)
                else:
                    await require_account_flat(db, account)
        elif path == "connectivity":
            assert (await account_connectivity(account.id, db=db, _user=None))["ok"] is True
        else:
            await require_account_flat(db, account)
    if exchange == "binance":
        factory.assert_not_called()
    else:
        factory.assert_called_once()
        selected = factory.call_args.args[3] if path == "flat" else factory.call_args.kwargs["proxy_url"]
        assert selected is None
