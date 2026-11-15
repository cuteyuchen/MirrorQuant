"""Synthetic simulations only: no venue, account, notification or network writes."""
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (BindingDelivery, CopyBinding, ExecutionRecord, NotificationOutbox,
    OrderIntent, PositionAllocation, TradingAccount, SystemConfig)
from app.services.engine.router import CopyRouter, UnknownOrderOutcome
from test_router_remediations import (trading, OfflineExchange, seed_allocation,
    prepare_event_router, source_order, current_snapshot, NOW_MS)


async def source_delivery(t, event="recovery"):
    payload = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
        "avgPrice": "100", "notional": "100", "startTime": NOW_MS - 1000}
    delivery = BindingDelivery(binding_id=t.binding.id, source_event_id=event,
        source_sequence=1, payload_json=json.dumps(payload))
    t.db.add(delivery)
    notifications = await t.db.get(SystemConfig, "notifications")
    notifications.value_json = json.dumps({"outbox_enabled": True, "notify_on_execution": True})
    await t.db.commit()
    t.db.info["delivery"] = delivery
    return delivery


def order(event="recovery"):
    return dict(source_event_id=event, target_usd=100, inst_id="BTCUSDT", side="buy", pos_side="long",
        ord_type="ioc", sz="1", px="100", reduce_only=False, td_mode="cross")


async def test_restart_recovery_records_fill_outbox_once(trading):
    t = trading
    await source_delivery(t)
    exchange = OfflineExchange(fill=0, state="new")
    with pytest.raises(UnknownOrderOutcome):
        await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order())
    exchange.fill, exchange.state = 1, "filled"
    async with get_session_factory()() as db:
        account = await db.get(TradingAccount, t.account.id)
        router = CopyRouter(CredentialVault())
        assert await router.reconcile_pending_orders(db, account, exchange, {"BTCUSDT": t.spec})
        records = (await db.scalars(select(ExecutionRecord).where(ExecutionRecord.my_qty > 0))).all()
        assert len(records) == 1 and records[0].my_qty == 1
        messages = (await db.scalars(select(NotificationOutbox))).all()
        assert len(messages) == 1
        assert json.loads(messages[0].payload)["execution_id"] == records[0].id
        assert "BTCUSDT" in json.loads(messages[0].payload)["message"]
        assert await router.reconcile_pending_orders(db, account, exchange, {"BTCUSDT": t.spec})
        assert len((await db.scalars(select(NotificationOutbox))).all()) == 1
        assert len(exchange.orders) == 1


async def test_terminal_fill_has_atomic_record_and_notification(trading):
    t = trading
    await source_delivery(t)
    exchange = OfflineExchange()
    # Stop immediately after the fill application boundary, before caller reporting.
    await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order())
    async with get_session_factory()() as db:
        assert (await db.scalar(select(PositionAllocation))).owned_qty == 1
        assert (await db.scalar(select(BindingDelivery))).state == "APPLIED"
        assert (await db.scalar(select(ExecutionRecord))).my_qty == 1
        assert len((await db.scalars(select(NotificationOutbox))).all()) == 1


async def test_partial_fill_incremental_price_and_canceled_remainder(trading):
    t = trading
    await source_delivery(t)
    exchange = OfflineExchange()
    exchange.get_order = AsyncMock(side_effect=[
        {"ordId": "one", "state": "new", "accFillSz": "0.4", "avgPx": "100"},
        {"ordId": "one", "state": "canceled", "accFillSz": "0.7", "avgPx": "103"},
    ])
    result = await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order())
    assert result.fill_sz == pytest.approx(.3)
    assert result.avg_px == pytest.approx(107)  # 72.1 cumulative cost minus 40 prior cost.
    intent = await t.db.scalar(select(OrderIntent))
    allocation = await t.db.scalar(select(PositionAllocation))
    assert Decimal(intent.requested_qty) - Decimal(intent.filled_qty) == Decimal(".3")
    assert allocation.entry_cost_exact == "72.1"
    records = (await t.db.scalars(select(ExecutionRecord))).all()
    assert sum(r.my_notional for r in records) == pytest.approx(72.1)
    assert len(exchange.orders) == 1  # Cancellation does not authorize a blind resend.


async def test_increased_fill_missing_price_stays_unknown(trading):
    t = trading
    exchange = OfflineExchange()
    exchange.get_order = AsyncMock(side_effect=[
        {"ordId": "one", "state": "new", "accFillSz": "0.4", "avgPx": "100"},
        {"ordId": "one", "state": "filled", "accFillSz": "1"},
    ])
    with pytest.raises(UnknownOrderOutcome, match="price"):
        await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order())
    assert (await t.db.scalar(select(PositionAllocation))).owned_qty == .4
    assert (await t.db.scalar(select(OrderIntent))).state == "UNKNOWN"


@pytest.mark.parametrize("meta", ({}, {"partial": True}, {"complete": False}, {"error": "offline"}))
async def test_single_missing_leg_requires_close_evidence(trading, meta):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    good_leg = {"symbol": "ETHUSDT", "positionSide": "LONG", "qty": 1}
    snapshot = {"openPositions": [good_leg], "meta": {"detailOk": True},
        "openPositionsMeta": {"source": "api", "authoritative": True, "complete": True,
            "positionShow": True, "asOf": NOW_MS, **meta}}
    exchange = OfflineExchange()
    await t.router.reconcile_orphan_positions(t.db, t.lead, t.account, t.binding, exchange, [good_leg], [t.pos],
        {"BTCUSDT": t.spec}, resilience_cfg={"auto_close_orphan_positions": "auto_close"},
        notify_cfg={"outbox_enabled": True, "notify_on_risk_block": True}, snapshot=snapshot)
    assert exchange.orders == []
    assert own.owned_qty == 1 and other.owned_qty == 9
    if not meta:
        assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_ORPHAN_CLOSE_EVIDENCE"
        assert len((await t.db.scalars(select(NotificationOutbox))).all()) == 1


async def test_partial_close_cancellation_preserves_remaining_owned_quantity(trading):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    exchange = OfflineExchange(fill=.4, state="canceled")
    await t.router._close_owned(t.db, t.binding, t.lead, t.account, exchange, t.pos, t.spec,
        1, "partial-close", "CLOSE_LONG", {"outbox_enabled": True})
    intent = await t.db.scalar(select(OrderIntent))
    assert Decimal(intent.requested_qty) - Decimal(intent.filled_qty) == Decimal(".6")
    assert own.owned_qty == .6 and other.owned_qty == 9
    assert len(exchange.orders) == 1


async def test_reporting_failure_cannot_commit_unreported_fill(trading, monkeypatch):
    t = trading
    await source_delivery(t)
    exchange = OfflineExchange()
    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("synthetic outbox transaction failure")
    with monkeypatch.context() as patch:
        patch.setattr("app.services.engine.router.enqueue_notification", fail_enqueue)
        with pytest.raises(RuntimeError, match="outbox transaction"):
            await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order())
        # Caller may record an unrelated failure; it must not commit tentative fills.
        await t.db.commit()
    async with get_session_factory()() as db:
        intent = await db.scalar(select(OrderIntent))
        assert intent.filled_qty == "0" and intent.state == "UNKNOWN"
        allocation = await db.scalar(select(PositionAllocation))
        assert allocation is None or allocation.owned_qty == 0
        assert (await db.scalar(select(BindingDelivery))).state == "PENDING"
        assert await CopyRouter(CredentialVault()).reconcile_pending_orders(db,
            await db.get(TradingAccount, t.account.id), exchange, {"BTCUSDT": t.spec})
        assert (await db.scalar(select(ExecutionRecord))).my_qty == 1
        assert len((await db.scalars(select(NotificationOutbox))).all()) == 1
        assert len(exchange.orders) == 1


async def test_legacy_terminal_reporting_gap_is_recovered_for_human_review_without_reapplying_fill(trading):
    t = trading
    delivery = await source_delivery(t)
    delivery.state = "APPLIED"
    t.db.add(OrderIntent(id="synthetic-legacy", binding_id=t.binding.id, account_id=t.account.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="recovery", state="FILLED",
        requested_qty="1", filled_qty="1", avg_px="100", reduce_only=False,
        exchange_order_id="synthetic-legacy-order"))
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id,
        inst_id="BTCUSDT", pos_side="long", owned_qty=1, owned_qty_exact="1",
        entry_cost=100, entry_cost_exact="100"))
    await t.db.commit()
    exchange = OfflineExchange()
    assert not await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.my_qty == 1 and record.notification_state == "unknown"
    assert "human reconciliation required" in record.details
    assert len((await t.db.scalars(select(NotificationOutbox))).all()) == 0
    assert not await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})
    assert len((await t.db.scalars(select(ExecutionRecord))).all()) == 1
    assert (await t.db.scalar(select(PositionAllocation))).owned_qty == 1
    assert exchange.orders == []


@pytest.mark.parametrize("kind", ("binance", "okx", "bybit", "bitget"))
async def test_adapter_missing_quantity_is_unknown_not_zero(kind):
    from app.services.adapters.binance import BinanceAdapter
    from app.services.adapters.okx import OKXAdapter
    from app.services.adapters.bybit import BybitAdapter
    from app.services.adapters.bitget import BitgetAdapter
    adapter = {"binance": BinanceAdapter, "okx": OKXAdapter, "bybit": BybitAdapter,
        "bitget": BitgetAdapter}[kind]({})
    rows = {"binance": {"orderId": "one", "status": "FILLED"},
        "okx": [{"ordId": "one", "state": "filled"}],
        "bybit": {"list": [{"orderId": "one", "orderStatus": "Filled"}]},
        "bitget": {"orderId": "one", "state": "filled"}}
    adapter._request = AsyncMock(return_value=rows[kind])
    result = await adapter.get_order("BTCUSDT", cl_ord_id="synthetic")
    assert result["accFillSz"] is None


async def test_okx_last_fill_price_cannot_replace_unknown_cumulative_average():
    from app.services.adapters.okx import OKXAdapter
    adapter = OKXAdapter({})
    adapter._request = AsyncMock(return_value=[{"ordId": "one", "state": "filled",
        "accFillSz": ".7", "fillPx": "107"}])
    assert (await adapter.get_order("BTC-USDT-SWAP", cl_ord_id="synthetic"))["avgPx"] in (None, 0)


async def test_protection_disconnection_keeps_exposure_and_alerts_once(trading):
    t = trading
    own, other, _, guard = await seed_allocation(t)
    t.binding.tp_sl_mode = "independent"
    t.binding.enable_stop_loss = True
    await t.db.commit()
    exchange = OfflineExchange()
    exchange.get_orderbook.side_effect = TimeoutError("synthetic disconnected quote")
    for _ in range(2):
        assert await t.router.check_position_guards(t.db, t.lead, t.account, t.binding,
            exchange, [t.pos], {"BTCUSDT": t.spec}, {"outbox_enabled": True, "notify_on_risk_block": True}) == []
    assert own.owned_qty == 1 and other.owned_qty == 9 and exchange.orders == []
    exchange.place_tp_sl.assert_not_awaited()  # Software guard cannot act during outage.
    assert t.binding.circuit_breaker_tripped
    assert len((await t.db.scalars(select(NotificationOutbox))).all()) == 1
    assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_PROTECTION_UNAVAILABLE"


async def test_physical_deficit_cannot_close_another_bindings_allocation(trading):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    t.pos.pos = t.pos.avail_pos = 9  # Only the other binding's tracked exposure remains.
    exchange = OfflineExchange()
    assert not await t.router._close_owned(t.db, t.binding, t.lead, t.account, exchange, t.pos, t.spec,
        1, "deficit", "CLOSE_LONG", {"outbox_enabled": True, "notify_on_risk_block": True})
    assert exchange.orders == [] and own.owned_qty == 1 and other.owned_qty == 9
    assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_ALLOCATION_DEFICIT"
