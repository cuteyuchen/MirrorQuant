"""Offline closure admission, durable ordering and notification recovery regressions."""
import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt, BindingDelivery, BindingPositionProgress, ExecutionRecord,
    LeadSource, NotificationOutbox, OrderIntent, PositionAllocation, PositionGuardState,
    SystemConfig, TradingAccount,
)
from app.services.adapters.base import BalanceInfo
from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import positions_have_close_evidence
from app.services.lead_collector.snapshot_clock import closed_interval
from test_follow_recovery_audit import source_delivery
from test_router_remediations import OfflineExchange, seed_allocation, trading
from test_routing_deliveries import exit_delivery_workflow, order, snapshot


def empty_snapshot():
    current = snapshot()
    current["openPositions"] = []
    current["openPositionsMeta"]["emptyVerified"] = True
    return current


def closure(workflow, **fields):
    closed = {"id": "security-closure", "symbol": "BTCUSDT", "side": "LONG",
              "opened": workflow.source_time - 1000, "closed": workflow.source_time}
    closed.update(fields)
    return closed


async def route(workflow, closed=(), orders=(), current=None, router=None):
    current = empty_snapshot() if current is None else current
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.snapshot_json = json.dumps(current)
        await db.commit()
        await (router or CopyRouter(CredentialVault())).process_lead_events(db, lead, orders, closed, current, [])


async def assert_preserved(workflow):
    async with get_session_factory()() as db:
        owned = await db.get(PositionAllocation, workflow.owned_id)
        other = await db.get(PositionAllocation, workflow.other_owned_id)
        debt = await db.get(BindingDebt, workflow.debt_id)
        assert Decimal(owned.owned_qty_exact) == 4 and Decimal(owned.entry_cost_exact) == 400
        assert Decimal(other.owned_qty_exact) == 5 and Decimal(other.entry_cost_exact) == 500
        assert Decimal(debt.pending_usd_exact) == 40
        assert await db.get(PositionGuardState, workflow.guard_id) is not None
    workflow.adapter.place_order.assert_not_awaited()


@pytest.mark.parametrize("field,value", [
    ("closed", True), ("opened", False), ("closed", None), ("opened", None),
    ("closed", "invalid"), ("opened", []), ("closed", float("nan")),
    ("closed", float("inf")), ("closed", 10 ** 100), ("closed", -1),
    ("opened", 0), ("closed", 1.5), ("updateTime", True), ("updateTime", None),
])
async def test_malformed_closed_cycles_are_sticky_and_never_liquidate(exit_delivery_workflow, field, value):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    closed = closure(workflow, **{field: value})
    for router in (CopyRouter(CredentialVault()), CopyRouter(CredentialVault())):
        await route(workflow, [closed], router=router)
    await assert_preserved(workflow)
    async with get_session_factory()() as db:
        state = await db.get(BindingPositionProgress, (workflow.binding_id, "BTCUSDT", "LONG"))
        assert state.review_reason
        assert await db.scalar(select(OrderIntent)) is None
        assert len((await db.scalars(select(ExecutionRecord))).all()) == 1


@pytest.mark.parametrize("kind", ["future", "reversed", "boolean", "equal", "after_observation", "wrong_cycle"])
async def test_independently_supplied_and_observation_adversaries(exit_delivery_workflow, kind):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = empty_snapshot()
    closed = closure(workflow)
    if kind == "future":
        closed["closed"] = int(time.time() * 1000) + 60000
    elif kind == "reversed":
        closed["opened"] = workflow.source_time + 1000
    elif kind == "boolean":
        closed["closed"] = True
    elif kind == "equal":
        closed["opened"] = closed["closed"]
    elif kind == "after_observation":
        current["openPositionsMeta"]["asOf"] = workflow.source_time - 1
    else:
        closed["opened"] = workflow.source_time - 500
    await route(workflow, [closed], current=current)
    await assert_preserved(workflow)


@pytest.mark.parametrize("kind", ["missing", "boolean", "stale", "future", "reversed"])
async def test_snapshot_provenance_never_renews_invalid_observation(exit_delivery_workflow, kind):
    workflow = exit_delivery_workflow
    current = empty_snapshot()
    observed = current["openPositionsMeta"]["asOf"]
    current["meta"] = {"generatedAt": observed}
    if kind == "missing":
        current["openPositionsMeta"].pop("asOf")
        current["meta"] = {}
    elif kind == "boolean":
        current["openPositionsMeta"]["asOf"] = True
    elif kind == "stale":
        current["openPositionsMeta"]["asOf"] -= 30001
    elif kind == "future":
        current["openPositionsMeta"]["asOf"] += 60000
    else:
        current["meta"]["collectionStartedAtMs"] = observed + 1
    await route(workflow, [closure(workflow)], current=current)
    await assert_preserved(workflow)
    async with get_session_factory()() as db:
        assert await db.scalar(select(BindingDelivery)) is None


@pytest.mark.parametrize("field,value", [
    ("orderTime", True), ("orderUpdateTime", False), ("orderTime", None),
    ("orderUpdateTime", "invalid"), ("orderTime", 10 ** 100),
])
async def test_ordinary_close_fills_require_both_times(exit_delivery_workflow, field, value):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    source = order(side="SELL", executedQty="10", orderTime=workflow.source_time,
                   orderUpdateTime=workflow.source_time)
    source[field] = value
    await route(workflow, orders=[source])
    await assert_preserved(workflow)


@pytest.mark.parametrize("kind", ["reversed", "after_observation", "equal_to_opening", "overlap"])
async def test_ordinary_close_fill_causality(exit_delivery_workflow, kind):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = empty_snapshot()
    source = order(side="SELL", executedQty="10", orderTime=workflow.source_time,
                   orderUpdateTime=workflow.source_time)
    orders = [source]
    if kind == "reversed":
        source["orderTime"] += 1
    elif kind == "after_observation":
        current["openPositionsMeta"]["asOf"] = workflow.source_time - 1
    elif kind == "equal_to_opening":
        source["orderTime"] = source["orderUpdateTime"] = workflow.source_time - 1000
    else:
        source["executedQty"] = "5"
        source["orderTime"] -= 500
        orders.append(order(orderId="overlapping", side="SELL", executedQty="5",
                            orderTime=workflow.source_time - 200, orderUpdateTime=workflow.source_time + 200))
    await route(workflow, orders=orders, current=current)
    await assert_preserved(workflow)


@pytest.mark.parametrize("kind", ["explicit", "fill"])
@pytest.mark.parametrize("mutation", ["payload", "ordering", "generation", "snapshot"])
async def test_exit_evidence_rechecked_after_durable_submitting_commit(exit_delivery_workflow, kind, mutation):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = empty_snapshot()
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.snapshot_json = json.dumps(current)
        await db.commit()
        commit = db.commit
        changed = False

        async def commit_with_mutation():
            nonlocal changed
            await commit()
            intent = await db.scalar(select(OrderIntent).where(OrderIntent.state == "SUBMITTING"))
            if intent is None or changed:
                return
            changed = True
            if mutation == "payload":
                delivery = await db.scalar(select(BindingDelivery))
                payload = json.loads(delivery.payload_json)
                if kind == "explicit":
                    payload["_closed"]["closed"] = True
                else:
                    payload["_source_time_ms"] = True
                delivery.payload_json = json.dumps(payload)
            elif mutation == "ordering":
                # 审查结论是派生 verdict：并发证据变化须以证据形式注入，
                # 重算只认有备份据的证据，不认直接改写的结论。
                if kind == "explicit":
                    overlapping = closure(workflow, id="concurrent-closure",
                                          opened=workflow.source_time - 500)
                    db.add(BindingDelivery(binding_id=workflow.binding_id,
                        source_event_id="closed:concurrent-closure",
                        source_sequence=workflow.source_time,
                        payload_json=json.dumps({"_closed": overlapping,
                                                 "_event_id": "closed:concurrent-closure"}),
                        state="PENDING"))
                else:
                    state = await db.get(BindingPositionProgress, (workflow.binding_id, "BTCUSDT", "LONG"))
                    state.source_time_ms = workflow.source_time + 1000
                    state.source_event_id = "concurrent-newer-event"
            elif mutation == "generation":
                allocation = await db.get(PositionAllocation, workflow.owned_id)
                allocation.generation += 1
            else:
                lead.snapshot_json = "{}"
            await commit()

        db.commit = commit_with_mutation
        orders = [order(side="SELL", executedQty="10", orderTime=workflow.source_time,
                        orderUpdateTime=workflow.source_time)] if kind == "fill" else []
        closed = [closure(workflow)] if kind == "explicit" else []
        await CopyRouter(CredentialVault()).process_lead_events(db, lead, orders, closed, current, [])
        assert changed
        intents = (await db.scalars(select(OrderIntent))).all()
        assert len(intents) == 1 and intents[0].state == "REJECTED"
        query = select(BindingDelivery)
        if kind == "explicit" and mutation == "ordering":
            query = query.where(BindingDelivery.source_event_id == "closed:security-closure")
        delivery = await db.scalar(query)
        assert delivery.state == "PENDING" and Decimal(delivery.exit_remaining_qty) == 4
    await assert_preserved(workflow)


async def test_concurrent_distinct_proven_closed_events_reduce_once_and_notify_once(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = empty_snapshot()
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.snapshot_json = json.dumps(current)
        config = await db.get(SystemConfig, "notifications")
        config.value_json = json.dumps({"outbox_enabled": True, "notify_on_execution": True,
                                       "notify_on_risk_block": False})
        await db.commit()

    async def submit(label):
        async with get_session_factory()() as db:
            lead = await db.get(LeadSource, workflow.lead_id)
            await CopyRouter(CredentialVault()).process_lead_events(
                db, lead, [], [closure(workflow, id=label)], current, [])

    await asyncio.gather(submit("first"), submit("second"))
    await submit("first")
    async with get_session_factory()() as db:
        owned = await db.get(PositionAllocation, workflow.owned_id)
        other = await db.get(PositionAllocation, workflow.other_owned_id)
        assert Decimal(owned.owned_qty_exact) == 0 and Decimal(other.owned_qty_exact) == 5
        assert len((await db.scalars(select(OrderIntent))).all()) == 1
        assert len((await db.scalars(select(NotificationOutbox))).all()) == 1
        assert len((await db.scalars(select(ExecutionRecord))).all()) == 1
    assert workflow.adapter.place_order.await_count == 1


@pytest.mark.parametrize("kind", ["explicit", "fill"])
async def test_direct_close_without_observation_fails_closed(trading, kind):
    owned, other, _, _ = await seed_allocation(trading)
    exchange = OfflineExchange()
    if kind == "explicit":
        result = await trading.router._reconcile_closed_position(trading.db, trading.binding, trading.lead,
            trading.account, exchange, {"id": "direct", "symbol": "BTCUSDT", "side": "LONG",
                                       "opened": 50, "closed": 200}, [trading.pos], {"BTCUSDT": trading.spec}, {})
        assert result is False
    else:
        batch = {"symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "qty": "10",
                 "avgPrice": "100", "notional": "1000", "startTime": 200, "endTime": 200}
        await trading.router._execute_batch_for_binding(trading.db, trading.binding, trading.lead,
            trading.account, exchange, batch, 1000, {}, 1000, [trading.pos], {"BTCUSDT": trading.spec}, False, {})
    assert exchange.orders == [] and owned.owned_qty == 1 and other.owned_qty == 9


@pytest.mark.parametrize("state", ["unsent", "unknown", "pending", "failed"])
async def test_historical_notification_recovery_without_blocking_audit(trading, state):
    delivery = await source_delivery(trading)
    delivery.state = "APPLIED"
    intent = OrderIntent(id="historical-proof", binding_id=trading.binding.id, account_id=trading.account.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="recovery", state="FILLED",
        requested_qty="1", filled_qty="1", avg_px="100", reduce_only=False, exchange_order_id="historical-fill")
    record = ExecutionRecord(binding_id=trading.binding.id, lead_id=trading.lead.id, lead_name="synthetic",
        account_id=trading.account.id, account_name="offline", inst_id="BTCUSDT", symbol="BTCUSDT",
        position_side="LONG", side="BUY", action="OPEN_LONG", status="EXECUTED", my_qty=1,
        my_price=100, order_id="historical-fill", details="Historical confirmed fill", notification_state=state)
    trading.db.add_all([intent, record])
    config = await trading.db.get(SystemConfig, "notifications")
    config.value_json = json.dumps({"outbox_enabled": True, "notify_on_execution": True})
    await trading.db.commit()
    exchange = OfflineExchange()
    # 账本已证实：审计通过；unsent 仍补发一条通知，其余状态标 unknown 留痕，均不阻断。
    for router in (trading.router, CopyRouter(CredentialVault())):
        assert await router.reconcile_pending_orders(trading.db, trading.account, exchange,
            {"BTCUSDT": trading.spec}) is True
    outboxes = (await trading.db.scalars(select(NotificationOutbox))).all()
    assert len(outboxes) == (1 if state == "unsent" else 0)
    assert record.notification_state == ("pending" if state == "unsent" else "unknown")
    assert len((await trading.db.scalars(select(ExecutionRecord))).all()) == 1
    assert exchange.orders == []


@pytest.mark.parametrize("kind", ["future", "reversed", "boolean", "missing"])
def test_collector_close_evidence_uses_the_same_closed_cycle_validation(kind):
    now = int(time.time() * 1000)
    position = {"symbol": "BTCUSDT", "positionSide": "LONG", "qty": 4, "openTime": now - 2000}
    closed = {"symbol": "BTCUSDT", "side": "LONG", "opened": now - 2000, "closed": now - 1000}
    assert closed_interval(closed) is not None
    assert positions_have_close_evidence([position], [], [closed], now - 1500)
    if kind == "future":
        closed["closed"] = now + 60000
    elif kind == "reversed":
        closed["opened"] = now
    elif kind == "boolean":
        closed["closed"] = True
    else:
        closed.pop("opened")
    assert not positions_have_close_evidence([position], [], [closed], now - 1500)


async def test_overlapping_closed_cycles_are_not_ordered_by_id(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    first = closure(workflow, id="zz-first")
    second = closure(workflow, id="aa-second", opened=workflow.source_time - 500,
                     closed=workflow.source_time + 200)
    await route(workflow, [second, first])
    await assert_preserved(workflow)
    async with get_session_factory()() as db:
        state = await db.get(BindingPositionProgress, (workflow.binding_id, "BTCUSDT", "LONG"))
        assert "overlap" in state.review_reason.lower()


async def test_fill_interval_crossing_closed_cycle_is_ambiguous(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    source = order(side="SELL", executedQty="10", orderTime=workflow.source_time - 200,
                   orderUpdateTime=workflow.source_time + 200)
    await route(workflow, [closure(workflow)], [source])
    await assert_preserved(workflow)


@pytest.mark.parametrize("kind", ["explicit", "fill"])
async def test_direct_historical_but_provably_ordered_close_still_works(trading, kind):
    owned, other, _, _ = await seed_allocation(trading)
    exchange = OfflineExchange()
    trading.lead.snapshot_json = json.dumps(empty_snapshot())
    await trading.db.commit()
    if kind == "explicit":
        assert await trading.router._reconcile_closed_position(trading.db, trading.binding, trading.lead,
            trading.account, exchange, {"id": "direct-proven", "symbol": "BTCUSDT", "side": "LONG",
                                       "opened": "50", "closed": "200"}, [trading.pos], {"BTCUSDT": trading.spec}, {})
    else:
        batch = {"symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "qty": "10",
                 "avgPrice": "100", "notional": "1000", "startTime": 200, "endTime": 200}
        await trading.router._execute_batch_for_binding(trading.db, trading.binding, trading.lead,
            trading.account, exchange, batch, 1000, {}, 1000, [trading.pos], {"BTCUSDT": trading.spec}, False, {})
    assert len(exchange.orders) == 1 and owned.owned_qty == 0 and other.owned_qty == 9


@pytest.mark.parametrize("field", ["opened", "closed"])
async def test_missing_closed_field_is_not_replaced_by_an_update_timestamp(exit_delivery_workflow, field):
    workflow = exit_delivery_workflow
    closed = closure(workflow, updateTime=workflow.source_time)
    closed.pop(field)
    await route(workflow, [closed])
    await assert_preserved(workflow)


async def test_closed_cycle_older_than_reopened_allocation_does_not_reduce(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    closed = closure(workflow, opened=workflow.source_time - 3000, closed=workflow.source_time - 2000)
    await route(workflow, [closed])
    await assert_preserved(workflow)


async def test_incomparable_saved_observation_at_durable_boundary_never_posts(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = empty_snapshot()
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.snapshot_json = json.dumps(current)
        await db.commit()
        commit = db.commit
        changed = False

        async def commit_with_incomparable_clock():
            nonlocal changed
            await commit()
            if not changed and await db.scalar(select(OrderIntent).where(OrderIntent.state == "SUBMITTING")):
                changed = True
                lead.snapshot_json = json.dumps({**current, "openPositionsMeta": {
                    **current["openPositionsMeta"], "asOf": None}, "meta": {
                    "generatedAt": current["openPositionsMeta"]["asOf"]}})
                await commit()

        db.commit = commit_with_incomparable_clock
        await CopyRouter(CredentialVault()).process_lead_events(db, lead, [], [closure(workflow)], current, [])
        assert changed
        assert (await db.scalar(select(OrderIntent))).state == "REJECTED"
    await assert_preserved(workflow)
