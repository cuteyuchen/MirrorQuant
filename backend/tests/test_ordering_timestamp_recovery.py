"""Offline regressions for submission rejection and non-mutating diagnostics."""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDelivery, BindingPositionProgress, ExecutionRecord, LeadSource,
    NotificationOutbox, OrderIntent, PositionAllocation, SystemConfig,
)
from app.services.adapters.base import OrderBook
from app.services.engine.router import CopyRouter
from app.services.lead_collector.snapshot_clock import snapshot_is_current, snapshot_times
from test_auto_follow_hidden_positions import (
    NOW_MS, fixed_clock, hidden_snapshot, opening_order, workflow,
)
from test_follow_recovery_audit import source_delivery
from test_router_remediations import OfflineExchange, seed_allocation, trading


def unsafe_saved_snapshot(kind, snapshot):
    saved = json.loads(json.dumps(snapshot))
    if kind == "empty":
        return "{}"
    if kind == "null":
        return "null"
    if kind == "invalid_json":
        return "{"
    if kind == "nonobject":
        return "[]"
    if kind == "missing_clocks":
        saved["openPositionsMeta"]["asOf"] = None
        saved["meta"].pop("generatedAt")
    elif kind == "incomparable":
        saved["openPositionsMeta"]["asOf"] = None
        saved["meta"].pop("generatedAt")
        saved["meta"]["collectionStartedAtMs"] = NOW_MS
    elif kind == "huge_timestamp":
        saved["openPositionsMeta"]["asOf"] = 10 ** 1000
    return json.dumps(saved)


@pytest.mark.parametrize("kind", [
    "empty", "null", "invalid_json", "nonobject", "missing_clocks",
    "incomparable", "huge_timestamp",
])
async def test_invalid_saved_snapshot_after_intent_is_terminal_and_replay_safe(workflow, kind):
    snapshot = hidden_snapshot()
    workflow.lead.snapshot_json = json.dumps(snapshot)
    notifications = await workflow.db.get(SystemConfig, "notifications")
    notifications.value_json = json.dumps({"outbox_enabled": True})
    await workflow.db.commit()
    original_commit = workflow.db.commit
    changed = False

    async def commit():
        nonlocal changed
        submitting = any(isinstance(row, OrderIntent) and row.state == "SUBMITTING"
                         for row in workflow.db.new)
        await original_commit()
        if submitting and not changed:
            changed = True
            async with get_session_factory()() as concurrent:
                lead = await concurrent.get(LeadSource, workflow.lead.id)
                lead.snapshot_json = unsafe_saved_snapshot(kind, snapshot)
                await concurrent.commit()

    workflow.db.commit = commit
    await workflow.router.process_lead_events(workflow.db, workflow.lead,
        [opening_order()], [], snapshot, [workflow.proxy])
    assert changed
    workflow.adapter.place_order.assert_not_awaited()
    intent = await workflow.db.scalar(select(OrderIntent))
    assert intent.state == "REJECTED" and Decimal(intent.filled_qty) == 0
    delivery = await workflow.db.scalar(select(BindingDelivery))
    assert delivery.state == "APPLIED"
    allocation = await workflow.db.scalar(select(PositionAllocation))
    assert Decimal(allocation.owned_qty_exact) == Decimal(allocation.entry_cost_exact) == 0
    records = (await workflow.db.scalars(select(ExecutionRecord))).all()
    messages = (await workflow.db.scalars(select(NotificationOutbox))).all()
    assert len(records) == len(messages) == 1 and records[0].my_qty == 0
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        for saved in (unsafe_saved_snapshot(kind, snapshot), json.dumps(snapshot)):
            lead.snapshot_json = saved
            await restarted.commit()
            for replay in range(2):
                await CopyRouter(CredentialVault()).process_lead_events(restarted, lead,
                    [opening_order()], [], snapshot, [workflow.proxy])
        assert len((await restarted.scalars(select(OrderIntent))).all()) == 1
        assert len((await restarted.scalars(select(BindingDelivery))).all()) == 1
        assert len((await restarted.scalars(select(ExecutionRecord))).all()) == 1
        assert len((await restarted.scalars(select(NotificationOutbox))).all()) == 1
    workflow.adapter.place_order.assert_not_awaited()
    workflow.adapter.get_order.assert_not_awaited()


@pytest.mark.parametrize("kind", ["equal", "overlap", "reversed", "malformed"])
async def test_unsupported_diagnostics_do_not_authorize_ambiguous_trading(workflow, kind):
    snapshot = hidden_snapshot()
    snapshot["openPositions"][0].update(qty=2, qtyExact="2")
    first = opening_order()
    second = {**opening_order(), "orderId": "second",
        "orderTime": NOW_MS - 700, "orderUpdateTime": NOW_MS - 700}
    if kind == "equal":
        second["orderTime"] = second["orderUpdateTime"] = first["orderTime"]
    elif kind == "overlap":
        first["orderUpdateTime"] = NOW_MS - 500
    elif kind == "reversed":
        first["orderUpdateTime"] -= 1
    else:
        first["orderTime"] = first["orderUpdateTime"] = "invalid"
    workflow.adapter.get_instruments.return_value = {}
    workflow.lead.snapshot_json = json.dumps(snapshot)
    notifications = await workflow.db.get(SystemConfig, "notifications")
    notifications.value_json = json.dumps({"outbox_enabled": True})
    await workflow.db.commit()
    for router in (workflow.router, CopyRouter(CredentialVault())):
        await router.process_lead_events(workflow.db, workflow.lead,
            [second, first], [], snapshot, [workflow.proxy])
    records = (await workflow.db.scalars(select(ExecutionRecord))).all()
    assert len(records) == 2 and all(row.status == "SKIPPED_UNSUPPORTED" for row in records)
    assert len((await workflow.db.scalars(select(NotificationOutbox))).all()) == 2
    deliveries = (await workflow.db.scalars(select(BindingDelivery))).all()
    assert len(deliveries) == 2 and all(row.state == "APPLIED" for row in deliveries)
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, "BTCUSDT", "LONG"))
    reason = state.review_reason
    assert reason and state.source_time_ms == 0 and state.source_remaining_qty is None
    assert not (await workflow.db.scalars(select(PositionAllocation))).all()
    workflow.adapter.get_orderbook.assert_not_awaited()
    workflow.adapter.place_order.assert_not_awaited()
    workflow.adapter.get_instruments.return_value = {"BTCUSDT": workflow.spec}
    second.update(executedQty="2", orderUpdateTime=NOW_MS - 100)
    snapshot["openPositions"][0].update(qty=3, qtyExact="3")
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        lead.snapshot_json = json.dumps(snapshot)
        await restarted.commit()
        for replay in range(2):
            await CopyRouter(CredentialVault()).process_lead_events(restarted, lead,
                [first, second], [], snapshot, [workflow.proxy])
        state = await restarted.get(BindingPositionProgress, (workflow.binding.id, "BTCUSDT", "LONG"))
        assert state.review_reason == reason and state.source_time_ms == 0
        records = (await restarted.scalars(select(ExecutionRecord))).all()
        assert len(records) == 3 and sum(row.status == "BLOCKED_SOURCE_ORDERING" for row in records) == 1
        assert len((await restarted.scalars(select(NotificationOutbox))).all()) == 3
        assert not (await restarted.scalars(select(OrderIntent))).all()
        assert not (await restarted.scalars(select(PositionAllocation))).all()
    workflow.adapter.place_order.assert_not_awaited()


async def test_unknown_historical_notification_does_not_block_confirmed_reduction(trading):
    delivery = await source_delivery(trading)
    delivery.state = "APPLIED"
    owned, other, _, _ = await seed_allocation(trading)
    intent = OrderIntent(id="confirmed-history", binding_id=trading.binding.id,
        account_id=trading.account.id, inst_id="BTCUSDT", pos_side="long",
        source_event_id=delivery.source_event_id, state="FILLED", requested_qty="1",
        filled_qty="1", avg_px="100", reduce_only=False, exchange_order_id="old-order")
    record = ExecutionRecord(binding_id=trading.binding.id, lead_id=trading.lead.id,
        account_id=trading.account.id, lead_name="synthetic", account_name="offline",
        inst_id="BTCUSDT", symbol="BTCUSDT", position_side="LONG", side="BUY",
        action="OPEN_LONG", status="EXECUTED", my_qty=1, my_price=100,
        order_id="old-order", details="Historical fill", notification_state="pending")
    trading.db.add_all([intent, record])
    await trading.db.commit()
    exchange = OfflineExchange()
    # 账本已证实：通知缺口只留痕（状态 unknown），审计通过，不阻断跟单。
    assert await trading.router.reconcile_pending_orders(trading.db, trading.account,
        exchange, {"BTCUSDT": trading.spec})
    assert record.notification_state == "unknown"
    assert not (await trading.db.scalars(select(NotificationOutbox))).all()
    for router in (trading.router, CopyRouter(CredentialVault())):
        await router._close_owned(trading.db, trading.binding, trading.lead, trading.account,
            exchange, trading.pos, trading.spec, 1, "protective-reduction", "CLOSE_LONG",
            {"outbox_enabled": True})
    assert owned.owned_qty == 0 and other.owned_qty == 9 and len(exchange.orders) == 1
    records = (await trading.db.scalars(select(ExecutionRecord))).all()
    messages = (await trading.db.scalars(select(NotificationOutbox))).all()
    assert len(records) == 2 and len(messages) == 1
    assert json.loads(messages[0].payload)["execution_id"] != record.id
    assert record.notification_state == "unknown"


def test_saved_timestamp_provenance_is_fresh_typed_and_comparable(fixed_clock):
    snapshot = hidden_snapshot()
    assert snapshot_is_current(snapshot, snapshot)
    assert not snapshot_is_current(snapshot, None)
    assert not snapshot_is_current(snapshot, [])
    stale = hidden_snapshot()
    stale["openPositionsMeta"]["asOf"] = NOW_MS - 30_001
    assert not snapshot_is_current(snapshot, stale)
    huge = hidden_snapshot()
    huge["meta"]["generatedAt"] = 10 ** 1000
    assert snapshot_times(huge) is None
    assert not snapshot_is_current(snapshot, huge)
