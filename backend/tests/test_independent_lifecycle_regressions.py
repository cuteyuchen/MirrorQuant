"""OFFLINE SIMULATION: actual collector/router/Binance adapter; synthetic HTTP only.

This does not use a real exchange demo, testnet, portfolio, account or webhook.
"""
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.models.domain import (BindingDebt, BindingDelivery, BindingSourceProgress, BindingPositionProgress, ExecutionRecord, NotificationOutbox,
    OrderIntent, PositionAllocation, SystemConfig, LeadSource, TradingAccount)
from app.services.adapters.binance import BinanceAdapter
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.lead_collector.binance_bapi import order_dedup_key
from app.services.proxy.manager import ProxyManager
from test_router_remediations import trading, NOW_MS


class SimulatedBinanceHTTP:
    """Scripted external fills are distinct from POST acknowledgements."""
    def __init__(self):
        self.now = NOW_MS
        self.lead_qty = Decimal("0")
        self.physical = Decimal("5")  # 3 manual + 2 owned by another binding.
        self.history, self.orders, self.lookups = [], {}, []
        self.leverage = 10
        self.fill_fraction = Decimal("1")
        self.order_state = "FILLED"
        self.lookup_error = False
        self.lose_ack = False
        self.book_error = False
        self.holdings = "valid"

    def event(self, side, quantity="1"):
        self.now += 1000
        quantity = Decimal(quantity)
        self.lead_qty += quantity if side == "BUY" else -quantity
        self.history.append({"orderId": f"synthetic-lead-{len(self.history)}", "symbol": "BTCUSDT",
            "side": side, "positionSide": "LONG", "type": "MARKET",
            "orderTime": self.now - 10, "orderUpdateTime": self.now - 10,
            "executedQty": str(quantity), "avgPrice": "100"})

    def position(self):
        return {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": str(self.physical),
            "entryPrice": "100", "markPrice": "100", "unRealizedProfit": "0",
            "notional": str(self.physical * 100), "leverage": str(self.leverage),
            "marginType": "cross", "updateTime": self.now}

    def respond(self, request):
        assert request.url.host == "simulation.invalid"
        path, method = request.url.path, request.method
        if path.endswith("/detail"):
            data = {"positionShow": True, "marginBalance": 1000}
        elif path.endswith("/order-history"):
            data = {"list": list(reversed(self.history)), "total": len(self.history)}
        elif path.endswith("/position-history"):
            data = {"list": [], "total": 0}
        elif path.endswith("/positions"):
            if self.holdings == "error":
                raise httpx.ReadTimeout("synthetic upstream unavailable")
            data = {} if self.holdings == "malformed" else ([] if self.holdings == "empty" or self.lead_qty == 0 else [{
                "symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": str(self.lead_qty),
                "entryPrice": "100"}])
        elif path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": self.now})
        elif path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC",
                "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
                "filters": [{"filterType": "LOT_SIZE", "minQty": ".1", "maxQty": "1000", "stepSize": ".1"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": ".1", "maxQty": "1000", "stepSize": ".1"},
                    {"filterType": "PRICE_FILTER", "tickSize": ".1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]})
        elif path == "/fapi/v2/account":
            return httpx.Response(200, json={"multiAssetsMargin": False, "updateTime": self.now,
                "totalMarginBalance": "1000", "availableBalance": "1000",
                "totalWalletBalance": "1000", "totalUnrealizedProfit": "0", "assets": [{
                    "asset": "USDT", "marginBalance": "1000", "availableBalance": "1000",
                    "walletBalance": "1000", "unrealizedProfit": "0"}]})
        elif path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[self.position()])
        elif path == "/fapi/v1/depth":
            if self.book_error:
                raise httpx.ReadTimeout("synthetic book unavailable")
            return httpx.Response(200, json={"bids": [["99.9", "1000"]], "asks": [["100", "1000"]]})
        elif path == "/fapi/v1/leverageBracket":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "brackets": [{
                "notionalFloor": 0, "notionalCap": 100000, "initialLeverage": 100}]}])
        elif path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": True})
        elif path == "/fapi/v1/multiAssetsMargin":
            return httpx.Response(200, json={"multiAssetsMargin": False})
        elif path == "/fapi/v1/leverage":
            assert method == "POST"
            self.leverage = int(request.url.params["leverage"])
            return httpx.Response(200, json={"leverage": self.leverage})
        elif path == "/fapi/v1/order":
            params = request.url.params
            if method == "POST":
                cid = params["newClientOrderId"]
                assert cid not in self.orders, "simulation detected duplicate submission"
                filled = Decimal(params["quantity"]) * self.fill_fraction
                closing = params["side"] == "SELL"
                assert params["positionSide"] == "LONG"
                self.physical += -filled if closing else filled
                self.orders[cid] = {"orderId": f"synthetic-venue-{len(self.orders)}", "clientOrderId": cid,
                    "executedQty": str(filled), "avgPrice": "100" if filled else "0",
                    "status": self.order_state, "quantity": params["quantity"], "side": params["side"]}
                if self.lose_ack:
                    raise httpx.ReadTimeout("synthetic accepted ACK lost")
                return httpx.Response(200, json={"orderId": self.orders[cid]["orderId"],
                    "clientOrderId": cid, "executedQty": "0", "avgPrice": "0", "status": "NEW"})
            assert method == "GET"
            cid = params["origClientOrderId"]
            self.lookups.append(cid)
            if self.lookup_error:
                raise httpx.ReadTimeout("synthetic confirmation unavailable")
            return httpx.Response(200, json=self.orders[cid])
        else:
            raise AssertionError(f"Unexpected synthetic HTTP path: {path}")
        return httpx.Response(200, json={"code": "000000", "data": data})


async def prepare_simulation(t, monkeypatch):
    simulation = SimulatedBinanceHTTP()
    monkeypatch.setattr("time.time", lambda: simulation.now / 1000)
    monkeypatch.setattr(ProxyManager, "build_client", lambda **kwargs:
        httpx.AsyncClient(transport=httpx.MockTransport(simulation.respond)))
    monkeypatch.setattr(ProxyManager, "select_proxy_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProxyManager, "ordered_proxy_urls", lambda *args, **kwargs: [None])
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.FRIENDLY_COPY", "https://simulation.invalid")
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.PUBLIC_COPY", "https://simulation.invalid")
    def adapter(**kwargs):
        value = BinanceAdapter(credentials={"api_key": "SYNTHETIC", "secret_key": "SYNTHETIC"})
        value.base_url = "https://simulation.invalid"
        return value
    monkeypatch.setattr("app.services.engine.router.create_exchange_adapter", adapter)
    engine = EngineOrchestrator()
    monkeypatch.setattr(engine.router.vault, "decrypt_dict", lambda value: {})
    engine.ws_hub.broadcast = AsyncMock()
    # Ancillary performance analysis is outside the financial lifecycle under test.
    real_sync = engine.collector.sync_snapshot
    async def sync_without_analysis(**kwargs):
        kwargs["with_analysis"] = False
        return await real_sync(**kwargs)
    monkeypatch.setattr(engine.collector, "sync_snapshot", sync_without_analysis)
    t.binding.baseline_seeded = True
    other = PositionAllocation(binding_id=t.other.id, account_id=t.account.id, inst_id="BTCUSDT",
        pos_side="long", owned_qty=2, owned_qty_exact="2", entry_cost=200, entry_cost_exact="200")
    t.db.add(other)
    notifications = await t.db.get(SystemConfig, "notifications")
    notifications.value_json = json.dumps({"outbox_enabled": True, "notify_on_execution": True,
        "notify_on_risk_block": True})
    await t.db.commit()
    return simulation, engine, other


async def seed_legacy_standalone_reduction(workflow, simulation, engine):
    """Confirm a pre-delivery reduction and import its already-consumed source fill."""
    source = simulation.history[-1]
    source_key = order_dedup_key(source)
    adapter = BinanceAdapter(credentials={"api_key": "SYNTHETIC", "secret_key": "SYNTHETIC"})
    adapter.base_url = "https://simulation.invalid"
    positions = await adapter.get_positions()
    await engine.router._close_owned(workflow.db, workflow.binding, workflow.lead, workflow.account, adapter,
        positions[0], workflow.spec, Decimal("1"), source_key + ":1", "PARTIAL_REDUCE",
        {"outbox_enabled": True, "notify_on_execution": True},
        lead_qty=1, lead_price=100, lead_notional=100)
    workflow.db.add(BindingSourceProgress(binding_id=workflow.binding.id, source_key=source_key,
        cumulative_qty=source["executedQty"], cumulative_notional="100"))
    await workflow.db.commit()
    assert await workflow.db.get(BindingDelivery, (workflow.binding.id, source_key + ":1")) is None


@pytest.mark.parametrize("failure", ("risk", "book_api"))
async def test_mocked_http_risk_and_market_failure_have_no_orders(trading, monkeypatch, failure):
    t = trading
    sim, engine, other = await prepare_simulation(t, monkeypatch)
    if failure == "risk":
        t.binding.symbol_blacklist_json = json.dumps(["BTCUSDT"])
        await t.db.commit()
    else:
        sim.book_error = True
    sim.event("BUY")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    assert sim.orders == {} and sim.physical == 5 and other.owned_qty == 2
    records = (await t.db.scalars(select(ExecutionRecord))).all()
    assert len(records) == 1 and records[0].status != "EXECUTED" and records[0].my_qty == 0
    messages = (await t.db.scalars(select(NotificationOutbox))).all()
    assert len(messages) == 1 and "✅" not in json.loads(messages[0].payload)["message"]
    if failure == "risk":
        assert records[0].status == "BLOCKED_FILTER"
        assert (await t.db.scalar(select(BindingDelivery))).state == "APPLIED"
    else:
        assert (await t.db.scalar(select(BindingDelivery))).state == "PENDING"


async def test_mocked_partial_reduction_then_new_lead_close_completes_remaining_owned_quantity(trading, monkeypatch):
    t = trading
    sim, engine, other = await prepare_simulation(t, monkeypatch)
    for _ in range(2):
        sim.event("BUY")
        await engine.sync_lead(t.db, t.lead, proxies=[])
    sim.fill_fraction, sim.order_state = Decimal(".4"), "CANCELED"
    sim.event("SELL")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    intent = await t.db.scalar(select(OrderIntent).order_by(OrderIntent.created_at.desc(), OrderIntent.id.desc()).limit(1))
    assert own.owned_qty == 1.6 and sim.physical == Decimal("6.6")
    assert Decimal(intent.requested_qty) - Decimal(intent.filled_qty) == Decimal(".6")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    assert len(sim.orders) == 3  # Replay cannot retry a canceled order blindly.
    sim.fill_fraction, sim.order_state = Decimal("1"), "FILLED"
    sim.event("SELL")  # New independent lead closure authorizes the remaining owned exit.
    await engine.sync_lead(t.db, t.lead, proxies=[])
    await t.db.refresh(own)
    assert own.owned_qty == 0 and sim.physical == 5 and other.owned_qty == 2
    assert len(sim.orders) == 4
    assert Decimal(list(sim.orders.values())[-1]["quantity"]) == Decimal("1.6")


async def test_mocked_persisted_partial_reduction_retries_fixed_target_before_new_lead_close(trading, monkeypatch):
    workflow = trading
    simulation, engine, other = await prepare_simulation(workflow, monkeypatch)
    for opening in range(2):
        simulation.event("BUY")
        await engine.sync_lead(workflow.db, workflow.lead, proxies=[])
    simulation.fill_fraction, simulation.order_state = Decimal(".4"), "CANCELED"
    simulation.event("SELL")
    await engine.sync_lead(workflow.db, workflow.lead, proxies=[])
    own = await workflow.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == workflow.binding.id))
    delivery = await workflow.db.scalar(select(BindingDelivery).where(BindingDelivery.exit_target_qty.is_not(None)))
    assert Decimal(own.owned_qty_exact) == Decimal("1.6") and simulation.physical == Decimal("6.6")
    assert (Decimal(delivery.exit_target_qty), Decimal(delivery.exit_filled_qty),
        Decimal(delivery.exit_remaining_qty)) == (Decimal("1"), Decimal(".4"), Decimal(".6"))
    assert delivery.state == "PENDING" and delivery.exit_progress == "READY"
    simulation.fill_fraction, simulation.order_state = Decimal("1"), "FILLED"
    simulation.event("SELL")
    await engine.sync_lead(workflow.db, workflow.lead, proxies=[])
    await workflow.db.refresh(own)
    await workflow.db.refresh(delivery)
    assert Decimal(own.owned_qty_exact) == 0 and Decimal(own.entry_cost_exact) == 0
    debt = await workflow.db.scalar(select(BindingDebt).where(BindingDebt.binding_id == workflow.binding.id))
    assert Decimal(debt.pending_usd_exact) == 0
    assert simulation.physical == 5 and other.owned_qty_exact == "2" and other.entry_cost_exact == "200"
    assert [Decimal(order["quantity"]) for order in simulation.orders.values()] == [
        Decimal("1"), Decimal("1"), Decimal("1"), Decimal(".6"), Decimal("1")]
    assert (Decimal(delivery.exit_target_qty), Decimal(delivery.exit_filled_qty),
        Decimal(delivery.exit_remaining_qty)) == (Decimal("1"), Decimal("1"), Decimal("0"))
    deliveries = (await workflow.db.scalars(select(BindingDelivery))).all()
    intents = (await workflow.db.scalars(select(OrderIntent))).all()
    records = (await workflow.db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()
    notifications = (await workflow.db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
    assert len(deliveries) == 4 and all(row.state == "APPLIED" for row in deliveries)
    assert len(intents) == len(records) == len(notifications) == 5
    assert [Decimal(row.my_qty_exact) for row in records] == [
        Decimal("1"), Decimal("1"), Decimal(".4"), Decimal(".6"), Decimal("1")]
    assert {json.loads(row.payload)["execution_id"] for row in notifications} == {row.id for row in records}
    before = [[tuple(getattr(row, column.name) for column in row.__table__.columns) for row in rows]
        for rows in (records, notifications)]
    for replay in range(3):
        await engine.sync_lead(workflow.db, workflow.lead, proxies=[])
        assert len(simulation.orders) == 5
        records = (await workflow.db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()
        notifications = (await workflow.db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
        assert [[tuple(getattr(row, column.name) for column in row.__table__.columns) for row in rows]
            for rows in (records, notifications)] == before



async def test_entire_captured_open_add_reduce_close_replay_preserves_manual_positions(trading, monkeypatch):
    t = trading
    sim, engine, other = await prepare_simulation(t, monkeypatch)
    for index, (side, expected_owned) in enumerate((("BUY", 1), ("BUY", 2), ("SELL", 1), ("SELL", 0)), 1):
        sim.event(side)
        result = await engine.sync_lead(t.db, t.lead, proxies=[])
        assert result["ok"] is True
        own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
        assert own.owned_qty == expected_owned and sim.physical == 5 + expected_owned, [
            (r.status, r.details) for r in (await t.db.scalars(select(ExecutionRecord))).all()]
        assert other.owned_qty_exact == "2" and other.entry_cost_exact == "200"
        assert len(json.loads(t.lead.orders_store_json)) == index
        deliveries = (await t.db.scalars(select(BindingDelivery))).all()
        intents = (await t.db.scalars(select(OrderIntent))).all()
        records = (await t.db.scalars(select(ExecutionRecord))).all()
        notifications = (await t.db.scalars(select(NotificationOutbox))).all()
        assert len(deliveries) == len(intents) == len(records) == len(notifications) == index
        assert all(d.state == "APPLIED" for d in deliveries)
        assert all(i.state == "FILLED" and Decimal(i.filled_qty) == 1 for i in intents)
        assert {i.source_event_id for i in intents} == {d.source_event_id for d in deliveries}
        assert {r.order_id for r in records} == {i.exchange_order_id for i in intents}
        assert all(r.status == "EXECUTED" and r.my_qty == 1 for r in records)
        assert {json.loads(n.payload)["execution_id"] for n in notifications} == {r.id for r in records}
        assert all("✅" in json.loads(n.payload)["message"] for n in notifications)
        # New DB session and router emulate restart; replay all captured lead history.
        async with get_session_factory()() as db:
            lead = await db.get(LeadSource, t.lead.id)
            from app.services.engine.router import CopyRouter
            await CopyRouter(engine.router.vault).process_lead_events(db, lead,
                list(json.loads(lead.orders_store_json).values()), [], json.loads(lead.snapshot_json), [])
            assert len((await db.scalars(select(NotificationOutbox))).all()) == index
        assert len(sim.orders) == index
    assert (await t.db.scalar(select(BindingDebt).where(BindingDebt.binding_id == t.binding.id))).pending_usd == 0
    assert [row["side"] for row in sim.orders.values()] == ["BUY", "BUY", "SELL", "SELL"]


async def test_mocked_http_accepted_unknown_restart_recovers_without_resubmission(trading, monkeypatch):
    t = trading
    sim, engine, other = await prepare_simulation(t, monkeypatch)
    sim.lookup_error = sim.lose_ack = True
    sim.event("BUY")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    intent = await t.db.scalar(select(OrderIntent))
    assert intent.state == "UNKNOWN" and intent.filled_qty == "0"
    assert sim.physical == 6 and len(sim.orders) == 1  # Venue simulation filled; engine cannot claim it.
    assert not (await t.db.scalars(select(ExecutionRecord).where(ExecutionRecord.status == "EXECUTED"))).all()
    sim.lookup_error = sim.lose_ack = False
    async with get_session_factory()() as db:
        account = await db.get(TradingAccount, t.account.id)
        from app.services.engine.router import CopyRouter
        router = CopyRouter(engine.router.vault)
        adapter = BinanceAdapter(credentials={"api_key": "SYNTHETIC", "secret_key": "SYNTHETIC"})
        adapter.base_url = "https://simulation.invalid"
        assert await router.reconcile_pending_orders(db, account, adapter, {"BTCUSDT": t.spec})
        assert await router.reconcile_pending_orders(db, account, adapter, {"BTCUSDT": t.spec})
        assert len((await db.scalars(select(ExecutionRecord).where(ExecutionRecord.status == "EXECUTED"))).all()) == 1
        assert len((await db.scalars(select(NotificationOutbox))).all()) == 2  # Initial failure + actual recovery.
    assert len(sim.orders) == 1 and other.owned_qty == 2


@pytest.mark.parametrize("failure", ("empty", "malformed", "error"))
async def test_unreliable_upstream_never_closes_existing_allocation(trading, monkeypatch, failure):
    t = trading
    sim, engine, other = await prepare_simulation(t, monkeypatch)
    sim.event("BUY")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    sim.holdings = failure
    sim.now += 1000
    await engine.sync_lead(t.db, t.lead, proxies=[])
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    assert own.owned_qty == 1 and sim.physical == 6 and other.owned_qty == 2
    assert len(sim.orders) == 1
    assert json.loads(t.lead.snapshot_json)["openPositionsMeta"]["authoritative"] is False


async def test_independent_late_earlier_reduce_after_later_applied(trading, monkeypatch):
    # 证据矛盾的窗口内必须冻结；迟到事件补齐证据后，两笔减仓按因果顺序
    # 补执行，而不是永久冻结等人工。审查结论是派生 verdict，随证据重算。
    t = trading
    sim, engine, _ = await prepare_simulation(t, monkeypatch)
    for _ in range(4):
        sim.event("BUY")
        await engine.sync_lead(t.db, t.lead, proxies=[])
    sim.event("SELL", "2")
    delayed = sim.history.pop()
    sim.event("SELL", "1")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    print("after later event", own.owned_qty_exact, [o["quantity"] for o in sim.orders.values()])
    assert Decimal(own.owned_qty_exact) == Decimal("4")
    assert len(sim.orders) == 4
    state = await t.db.get(BindingPositionProgress, (t.binding.id, "BTCUSDT", "LONG"))
    assert state.review_reason and "causal" in state.review_reason
    sim.history.append(delayed)
    await engine.sync_lead(t.db, t.lead, proxies=[])
    await t.db.refresh(own)
    print("after late earlier event", own.owned_qty_exact, [o["quantity"] for o in sim.orders.values()])
    assert Decimal(own.owned_qty_exact) == Decimal("1")
    assert len(sim.orders) == 6
    assert [o["quantity"] for o in sim.orders.values()][-2:] == ["2.0", "1.0"]
    state = await t.db.get(BindingPositionProgress, (t.binding.id, "BTCUSDT", "LONG"))
    assert state.review_reason is None
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, t.lead.id)
        from app.services.engine.router import CopyRouter
        for replay in range(2):
            await CopyRouter(engine.router.vault).process_lead_events(restarted, lead,
                list(json.loads(lead.orders_store_json).values()), [], json.loads(lead.snapshot_json), [])
        blocked = (await restarted.scalars(select(ExecutionRecord).where(
            ExecutionRecord.status == "BLOCKED_SOURCE_ORDERING"))).all()
        assert len(blocked) == 1
        assert len((await restarted.scalars(select(OrderIntent))).all()) == 6
    assert len(sim.orders) == 6
