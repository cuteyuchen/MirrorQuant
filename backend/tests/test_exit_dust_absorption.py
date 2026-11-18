"""Exit remainders below the exchange minimum must complete, never stall.

A quantized fill leaves a dust remainder (target precision exceeds the lot
step). Retrying it forever parks the delivery and blocks every later event
for the binding, so the remainder is absorbed and the delivery completes.
"""
import json
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.domain import (BindingDelivery, ExecutionRecord, NotificationOutbox,
    OrderIntent, PositionAllocation)
from test_router_remediations import trading, OfflineExchange, seed_allocation


async def _exit_delivery(t, event, target, filled, allocation):
    remaining = Decimal(target) - Decimal(filled)
    delivery = BindingDelivery(
        binding_id=t.binding.id, source_event_id=event, source_sequence=1,
        payload_json=json.dumps({"symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG",
                                 "qty": "10", "avgPrice": "100", "notional": "1000"}),
        state="PENDING", exit_progress="READY",
        exit_account_id=t.account.id, exit_inst_id="BTCUSDT", exit_pos_side="long",
        exit_allocation_id=allocation.id, exit_generation=allocation.generation,
        exit_initial_owned_qty="1", exit_target_qty=target,
        exit_filled_qty=filled, exit_remaining_qty=str(remaining))
    t.db.add(delivery)
    if Decimal(filled) > 0:
        t.db.add(OrderIntent(id=f"intent-{event}", binding_id=t.binding.id, account_id=t.account.id,
            inst_id="BTCUSDT", pos_side="long", source_event_id=event, state="FILLED",
            requested_qty=filled, filled_qty=filled, avg_px="100", reduce_only=True,
            exchange_order_id=f"order-{event}", reporting_complete=True))
    await t.db.commit()
    t.db.info["delivery"] = delivery
    return delivery


NOTIFY = {"outbox_enabled": True, "notify_on_execution": True, "notify_on_risk_block": True}


async def test_dust_remainder_completes_delivery_without_order(trading):
    t = trading
    allocation, _, _, _ = await seed_allocation(t, qty=0.1)
    delivery = await _exit_delivery(t, "dust-exit", "0.95", "0.9", allocation)
    exchange = OfflineExchange()
    result = await t.router._close_owned(t.db, t.binding, t.lead, t.account, exchange, t.pos, t.spec,
        Decimal("0.05"), "dust-exit", "PARTIAL_REDUCE", NOTIFY)
    assert result is False
    assert exchange.orders == []
    assert delivery.state == "APPLIED" and delivery.exit_progress == "COMPLETE"
    assert Decimal(delivery.exit_remaining_qty) == 0
    assert Decimal(delivery.exit_filled_qty) == Decimal("0.95")
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "EXIT_DUST_ABSORBED"
    assert (await t.db.scalars(select(NotificationOutbox))).all() == []
    assert (await t.db.get(PositionAllocation, allocation.id)).owned_qty == 0.1


async def test_entire_dust_exit_completes_without_order(trading):
    t = trading
    allocation, _, _, _ = await seed_allocation(t)
    delivery = await _exit_delivery(t, "dust-only", "0.05", "0", allocation)
    exchange = OfflineExchange()
    await t.router._close_owned(t.db, t.binding, t.lead, t.account, exchange, t.pos, t.spec,
        Decimal("0.05"), "dust-only", "PARTIAL_REDUCE", NOTIFY)
    assert delivery.state == "APPLIED"
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "EXIT_DUST_ABSORBED"


async def test_substantial_remainder_with_no_availability_still_waits(trading):
    t = trading
    allocation, _, _, _ = await seed_allocation(t)
    delivery = await _exit_delivery(t, "held-exit", "1", "0", allocation)
    t.pos.avail_pos = 0
    exchange = OfflineExchange()
    result = await t.router._close_owned(t.db, t.binding, t.lead, t.account, exchange, t.pos, t.spec,
        Decimal("1"), "held-exit", "PARTIAL_REDUCE", NOTIFY)
    assert result is False
    assert delivery.state == "PENDING" and delivery.exit_progress == "WAITING_AVAILABILITY"
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "SKIPPED_BELOW_MIN"


async def test_no_local_position_with_dust_remainder_completes(trading, monkeypatch):
    t = trading
    allocation, _, _, _ = await seed_allocation(t, qty=0.1)
    delivery = await _exit_delivery(t, "dust-exit-nopos", "0.95", "0.9", allocation)
    monkeypatch.setattr(t.router, "_validate_exit_source", AsyncMock(return_value=({}, False)))
    batch = {"symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "qty": "10",
             "avgPrice": "100", "notional": "1000", "startTime": 200, "endTime": 200,
             "_event_id": "dust-exit-nopos"}
    exchange = OfflineExchange()
    await t.router._execute_batch_for_binding(t.db, t.binding, t.lead, t.account, exchange,
        batch, 1000, {}, 1000, [], {"BTCUSDT": t.spec}, False, NOTIFY)
    assert delivery.state == "APPLIED" and delivery.exit_progress == "COMPLETE"
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "EXIT_DUST_ABSORBED"
    assert (await t.db.scalars(select(NotificationOutbox))).all() == []
