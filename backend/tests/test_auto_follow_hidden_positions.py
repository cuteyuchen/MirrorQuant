"""Hidden holdings can follow verified order history without authorizing orphan cleanup."""
import json
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDelivery, CopyBinding, ExecutionRecord, LeadSource, OrderIntent,
    PositionAllocation, ProxyNode, TradingAccount,
)
from app.services.adapters.base import (
    BalanceInfo, ExchangePosition, InstrumentSpec, OrderBook, OrderResult,
)
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import (
    BinanceBapiCollector, is_authoritative_snapshot, is_routable_snapshot,
)
from app.services.proxy.manager import ProxyManager


NOW_MS = 1_800_000_000_000


def hidden_snapshot():
    return {
        "meta": {"generatedAt": NOW_MS, "detailOk": True},
        "profile": {"positionShow": False, "marginBalance": 1000},
        "openPositions": [{
            "symbol": "BTCUSDT", "positionSide": "LONG", "qty": 1, "qtyExact": "1",
        }],
        "openPositionsMeta": {
            "positionShow": False, "source": "derived-from-order-history",
            "derived": True, "reconstructed": True, "asOf": NOW_MS,
            "authoritative": False, "complete": False, "partial": False,
        },
    }


def opening_order():
    return {
        "orderId": "hidden-open", "orderTime": NOW_MS - 1000,
        "orderUpdateTime": NOW_MS - 1000, "symbol": "BTCUSDT",
        "side": "BUY", "positionSide": "LONG", "type": "MARKET",
        "executedQty": "1", "avgPrice": "100",
    }


def offline_collector(collector):
    collector.fetch_detail = AsyncMock(return_value={"positionShow": False, "marginBalance": 1000})
    collector.fetch_order_history = AsyncMock(return_value=[opening_order()])
    collector.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    collector.fetch_holdings = AsyncMock(side_effect=AssertionError("Hidden holdings must not be fetched"))
    collector.fetch_performance = AsyncMock(return_value={})
    collector.fetch_roi_chart = AsyncMock(return_value=[])
    collector.fetch_allocation = AsyncMock(return_value={})
    collector.fetch_marks = AsyncMock(return_value={})
    collector.fetch_brackets = AsyncMock(return_value={})
    return collector


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.time.time", lambda: NOW_MS / 1000)


def test_hidden_snapshot_is_routable_but_never_authoritative(fixed_clock):
    snapshot = hidden_snapshot()
    assert not is_authoritative_snapshot(snapshot)
    assert is_routable_snapshot(snapshot)


@pytest.mark.parametrize("field,value", [
    ("partial", True), ("error", "holdings unavailable"), ("asOf", NOW_MS - 30_001),
    ("asOf", NOW_MS + 1),
])
def test_degraded_stale_or_future_hidden_snapshot_is_not_routable(fixed_clock, field, value):
    snapshot = hidden_snapshot()
    snapshot["openPositionsMeta"][field] = value
    assert not is_routable_snapshot(snapshot)


def test_detail_failure_blocks_hidden_snapshot(fixed_clock):
    snapshot = hidden_snapshot()
    snapshot["meta"]["detailOk"] = False
    assert not is_routable_snapshot(snapshot)


@pytest.mark.parametrize("marker", [
    {"derived": True}, {"reconstructed": True}, {"source": "derived-from-order-history"},
    {"source": "derived"}, {"positionShow": False},
])
def test_each_reconstruction_marker_allows_routing(fixed_clock, marker):
    snapshot = hidden_snapshot()
    snapshot["openPositionsMeta"] = {"asOf": NOW_MS, **marker}
    assert is_routable_snapshot(snapshot)
    assert not is_authoritative_snapshot(snapshot)


@pytest.mark.parametrize("positions", [None, {}, "unknown"])
def test_hidden_snapshot_requires_a_position_list(fixed_clock, positions):
    snapshot = hidden_snapshot()
    snapshot["openPositions"] = positions
    assert not is_routable_snapshot(snapshot)


def test_unverified_public_snapshot_without_reconstruction_is_not_routable(fixed_clock):
    snapshot = hidden_snapshot()
    snapshot["openPositionsMeta"] = {"source": "api", "positionShow": True, "asOf": NOW_MS}
    assert not is_routable_snapshot(snapshot)
    snapshot["openPositionsMeta"].update(authoritative=True, complete=True)
    assert is_authoritative_snapshot(snapshot)
    assert is_routable_snapshot(snapshot)


@pytest.mark.parametrize("timestamp_field", ["generatedAt", "collectionStartedAtMs"])
def test_hidden_snapshot_fallback_timestamp_is_checked(fixed_clock, timestamp_field):
    snapshot = hidden_snapshot()
    snapshot["openPositionsMeta"]["asOf"] = None
    snapshot["meta"] = {timestamp_field: NOW_MS}
    assert is_routable_snapshot(snapshot)
    snapshot["meta"][timestamp_field] = NOW_MS - 30_001
    assert not is_routable_snapshot(snapshot)


def test_routing_age_limit_includes_boundary_and_accepts_explicit_clock():
    snapshot = hidden_snapshot()
    assert is_routable_snapshot(snapshot, now_ms=NOW_MS + 30_000)
    assert not is_routable_snapshot(snapshot, now_ms=NOW_MS + 30_001)
    assert not is_routable_snapshot(snapshot, now_ms=NOW_MS + 1001, max_age_ms=1000)


async def test_collector_marks_hidden_holdings_reconstructed_and_dates_observation(fixed_clock):
    collector = offline_collector(BinanceBapiCollector())
    store = {}
    snapshot = await collector.sync_snapshot("hidden-lead", store, [], with_analysis=False)
    assert len(store) == 1
    assert snapshot["openPositions"][0]["qty"] == 1
    meta = snapshot["openPositionsMeta"]
    assert meta["reconstructed"] is True and meta["derived"] is True
    assert meta["asOf"] == NOW_MS
    assert is_routable_snapshot(snapshot)
    assert not is_authoritative_snapshot(snapshot)
    collector.fetch_holdings.assert_not_awaited()


async def test_public_holdings_failure_retains_unknown_observation_time(fixed_clock):
    collector = offline_collector(BinanceBapiCollector())
    collector.fetch_detail.return_value["positionShow"] = True
    collector.fetch_holdings.side_effect = RuntimeError("holdings unavailable")
    snapshot = await collector.sync_snapshot("public-lead", {}, [], with_analysis=False)
    assert snapshot["openPositionsMeta"]["asOf"] is None
    assert snapshot["openPositionsMeta"]["reconstructed"] is True
    assert not is_routable_snapshot(snapshot)
    assert not is_authoritative_snapshot(snapshot)


async def test_orchestrator_routes_new_hidden_order_without_deferral(fixed_clock, monkeypatch, caplog):
    engine = EngineOrchestrator()
    offline_collector(engine.collector)
    engine.router.process_lead_events = AsyncMock()
    engine.ws_hub.broadcast = AsyncMock()
    monkeypatch.setattr(ProxyManager, "ordered_proxy_urls", lambda *args, **kwargs: [None])
    async with get_session_factory()() as db:
        lead = LeadSource(name="熬鹰", portfolio_id="hidden-lead", position_show=False)
        db.add(lead)
        await db.commit()
        with caplog.at_level(logging.DEBUG, logger="mirrorquant.orchestrator"):
            result = await engine.sync_lead(db, lead, proxies=[])
        assert result["ok"] is True
        engine.router.process_lead_events.assert_awaited_once()
        kwargs = engine.router.process_lead_events.await_args.kwargs
        assert kwargs["lead"] is lead
        assert len(kwargs["raw_orders"]) == 1
        assert kwargs["raw_orders"][0]["oid"] == "hidden-open"
        assert is_routable_snapshot(kwargs["snapshot"])
        assert not is_authoritative_snapshot(kwargs["snapshot"])
        assert "financial routing deferred" not in caplog.text
        await db.refresh(lead)
        assert lead.position_show is False and lead.status == "running"
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["asOf"] == NOW_MS


@pytest.fixture
async def workflow(fixed_clock, monkeypatch):
    vault = CredentialVault()
    async with get_session_factory()() as db:
        proxy = ProxyNode(name="offline proxy", url="http://proxy.invalid:8080", is_enabled=True, is_healthy=True)
        lead = LeadSource(name="hidden lead", portfolio_id="hidden-router", position_show=False,
                          margin_balance=1000, status="running")
        account = TradingAccount(name="follower", exchange="binance", api_key_masked="***",
                                 encrypted_credentials=vault.encrypt_dict({"api_key": "offline", "secret_key": "dummy"}),
                                 usdt_equity=1000, avail_equity=1000, hwm_equity=1000, day_start_equity=1000)
        db.add_all([lead, account, proxy])
        await db.flush()
        binding = CopyBinding(name="hidden follow", lead_id=lead.id, account_id=account.id, baseline_seeded=True)
        db.add(binding)
        await db.commit()
        spec = InstrumentSpec("BTCUSDT", Decimal("1"), Decimal("0.1"), Decimal("0.1"), Decimal("0.1"),
                              min_notional=Decimal("5"), max_lvr=100)
        adapter = SimpleNamespace(
            sync_time=AsyncMock(return_value=0),
            get_instruments=AsyncMock(return_value={"BTCUSDT": spec}),
            get_usdt_balance=AsyncMock(return_value=BalanceInfo(1000, 1000, 1000, 0)),
            get_positions=AsyncMock(return_value=[]),
            get_orderbook=AsyncMock(return_value=OrderBook([[99.9, 100]], [[100, 100]])),
            get_max_leverage=AsyncMock(return_value=100),
            ensure_leverage=AsyncMock(return_value=True),
            place_order=AsyncMock(),
            get_order=AsyncMock(),
            resolve_symbol=lambda symbol, instruments, symbol_map: (symbol, symbol in instruments),
        )

        async def place_order(**order):
            return OrderResult("hidden-exchange-order", order["cl_ord_id"])

        async def get_order(inst_id, cl_ord_id):
            order = adapter.place_order.call_args.kwargs
            return {"ordId": "hidden-exchange-order", "accFillSz": order["sz"], "avgPx": "100", "state": "filled"}

        adapter.place_order.side_effect = place_order
        adapter.get_order.side_effect = get_order
        monkeypatch.setattr("app.services.engine.router.create_exchange_adapter", lambda **kwargs: adapter)
        yield SimpleNamespace(db=db, lead=lead, binding=binding, account=account, proxy=proxy,
                              adapter=adapter, spec=spec, router=CopyRouter(vault))


@pytest.mark.parametrize("timestamp_field", ["asOf", "generatedAt", "collectionStartedAtMs", None])
async def test_hidden_open_persists_delivery_and_filled_intent(workflow, timestamp_field):
    t = workflow
    snapshot = hidden_snapshot()
    snapshot["openPositionsMeta"]["asOf"] = None
    snapshot["meta"].pop("generatedAt")
    if timestamp_field == "asOf":
        snapshot["openPositionsMeta"]["asOf"] = NOW_MS
    elif timestamp_field:
        snapshot["meta"][timestamp_field] = NOW_MS
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    submitted_states = []

    async def submit(**order):
        async with get_session_factory()() as persisted:
            intent = await persisted.get(OrderIntent, order["cl_ord_id"])
            delivery = await persisted.get(BindingDelivery, (t.binding.id, intent.source_event_id))
            submitted_states.append((intent.state, delivery.state))
        return OrderResult("hidden-exchange-order", order["cl_ord_id"])

    t.adapter.place_order.side_effect = submit
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], snapshot, [t.proxy])
    t.adapter.place_order.assert_awaited_once()
    order = t.adapter.place_order.await_args.kwargs
    assert order["side"] == "buy" and order["pos_side"] == "long"
    assert order["reduce_only"] is False and Decimal(order["sz"]) == 1
    assert submitted_states == [("SUBMITTING", "PENDING")]
    async with get_session_factory()() as persisted:
        deliveries = (await persisted.scalars(select(BindingDelivery))).all()
        intents = (await persisted.scalars(select(OrderIntent))).all()
        assert len(deliveries) == 1 and deliveries[0].state == "APPLIED"
        assert len(intents) == 1 and intents[0].state == "FILLED"
        assert intents[0].source_event_id == deliveries[0].source_event_id
        assert intents[0].binding_id == t.binding.id and intents[0].account_id == t.account.id
        assert intents[0].source_time_ms == opening_order()["orderTime"]
        assert Decimal(intents[0].filled_qty) == 1 and intents[0].exchange_order_id == "hidden-exchange-order"
        assert (await persisted.scalar(select(ExecutionRecord))).status == "EXECUTED"
        assert (await persisted.scalar(select(PositionAllocation))).owned_qty == 1
    # Repeated polling must not submit the same hidden-position event twice.
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], snapshot, [t.proxy])
    t.adapter.place_order.assert_awaited_once()


@pytest.mark.parametrize("reason", ["newer_observation", "newer_failure"])
async def test_hidden_routing_timestamp_fallback_rejects_superseded_work(workflow, reason):
    t = workflow
    incoming = hidden_snapshot()
    incoming["openPositionsMeta"]["asOf"] = None
    latest = hidden_snapshot()
    latest["openPositionsMeta"]["asOf"] = None
    if reason == "newer_observation":
        latest["meta"]["generatedAt"] = NOW_MS + 1
    else:
        latest["openPositionsMeta"]["lastFailureStartedAtMs"] = NOW_MS
    t.lead.snapshot_json = json.dumps(latest)
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], incoming, [t.proxy])
    t.adapter.place_order.assert_not_awaited()
    t.adapter.get_instruments.assert_not_awaited()
    assert await t.db.scalar(select(BindingDelivery)) is None
    assert await t.db.scalar(select(OrderIntent)) is None


@pytest.mark.parametrize("explicit_snapshot", [False, True])
async def test_hidden_empty_snapshot_never_authorizes_orphan_close(workflow, explicit_snapshot):
    t = workflow
    snapshot = hidden_snapshot()
    snapshot["openPositions"] = []
    # Even misleading public-holdings flags and close evidence cannot override hidden visibility.
    snapshot["openPositionsMeta"].update(source="api", authoritative=True, complete=True, emptyVerified=True)
    snapshot["closed"] = [{"symbol": "BTCUSDT", "side": "LONG", "opened": NOW_MS - 2000, "closed": NOW_MS - 1000}]
    t.lead.snapshot_json = json.dumps(snapshot)
    allocation = PositionAllocation(binding_id=t.binding.id, account_id=t.account.id,
                                    inst_id="BTCUSDT", pos_side="long", owned_qty=1,
                                    entry_cost=100, opened_at_ms=NOW_MS - 2000)
    t.db.add(allocation)
    await t.db.commit()
    position = ExchangePosition("BTCUSDT", "BTCUSDT", "long", "cross", 1, 1, 100, 0, 0, 100, 1)
    t.adapter.get_positions.return_value = [position]
    assert is_routable_snapshot(snapshot)
    assert not is_authoritative_snapshot(snapshot)
    actions = await t.router.reconcile_orphan_positions(
        t.db, t.lead, t.account, t.binding, t.adapter, [], [position], {"BTCUSDT": t.spec},
        resilience_cfg={"auto_close_orphan_positions": "auto_close"},
        snapshot=snapshot if explicit_snapshot else None,
    )
    assert actions == []
    t.adapter.place_order.assert_not_awaited()
    t.adapter.get_positions.assert_not_awaited()
    await t.db.refresh(allocation)
    assert allocation.owned_qty == 1
    assert await t.db.scalar(select(OrderIntent)) is None
