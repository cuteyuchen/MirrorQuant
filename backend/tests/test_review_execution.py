import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import BindingDelivery, ExecutionRecord, LeadSource, OrderIntent, PositionAllocation
from app.services.adapters.base import BalanceInfo, OrderBook, OrderResult
from app.services.engine.router import CopyRouter
from test_router_remediations import OfflineExchange, seed_allocation, trading
from test_routing_deliveries import exit_delivery_workflow, snapshot


async def run_closed(workflow):
    closed = {"id": "review-close", "symbol": "BTCUSDT", "side": "LONG",
              "opened": workflow.source_time - 1000, "closed": workflow.source_time}
    current = snapshot()
    current["openPositions"] = []
    current["openPositionsMeta"]["emptyVerified"] = True
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.snapshot_json = json.dumps(current)
        await db.commit()
        await CopyRouter(CredentialVault()).process_lead_events(
            db, lead, [], [closed], current, [])
    async with get_session_factory()() as db:
        delivery = await db.get(BindingDelivery, (workflow.binding_id, "closed:review-close"))
        owned = await db.get(PositionAllocation, workflow.owned_id)
        other = await db.get(PositionAllocation, workflow.other_owned_id)
        assert Decimal(other.owned_qty_exact) == 5
        return delivery.state, Decimal(owned.owned_qty_exact)


def close_transport(workflow, outcomes):
    orders = {}
    answers = {}
    workflow.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))

    async def submit(**order):
        assert order["reduce_only"] and order["inst_id"] == workflow.spec.inst_id
        assert order["cl_ord_id"] not in orders
        assert Decimal(order["sz"]) <= Decimal(str(workflow.position.avail_pos))
        orders[order["cl_ord_id"]] = order
        state, filled = outcomes[len(orders) - 1]
        assert filled <= Decimal(order["sz"])
        answers[order["cl_ord_id"]] = {
            "ordId": f"review-order-{len(orders)}", "state": state,
            "accFillSz": str(filled), "avgPx": "100" if filled else "0"}
        workflow.position.pos -= float(filled)
        workflow.position.avail_pos -= float(filled)
        return OrderResult(answers[order["cl_ord_id"]]["ordId"], order["cl_ord_id"])

    async def lookup(inst_id, cl_ord_id=None, **kwargs):
        answer = answers.get(cl_ord_id)
        if answer and answer["state"] == "missing":
            return None
        if answer and answer["state"] == "lookup_error":
            raise TimeoutError("offline lookup unavailable")
        return answer

    workflow.adapter.place_order = AsyncMock(side_effect=submit)
    workflow.adapter.get_order = AsyncMock(side_effect=lookup)
    return orders, answers


async def test_closed_unavailable_exposure_stays_pending_then_recovers(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    orders, _ = close_transport(workflow, [("filled", Decimal("4"))])
    workflow.position.avail_pos = 0
    assert await run_closed(workflow) == ("PENDING", Decimal("4"))
    assert orders == {}
    workflow.position.avail_pos = 10
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert len(orders) == 1


@pytest.mark.parametrize("terminal,filled", [("canceled", "0"), ("rejected", "0"),
                                            ("canceled", "1.2"), ("filled", "1.2")])
async def test_closed_terminal_remainder_retries_once_after_reconciliation(
    exit_delivery_workflow, terminal, filled,
):
    workflow = exit_delivery_workflow
    first = Decimal(filled)
    orders, answers = close_transport(workflow, [("new", first), ("filled", Decimal("4") - first)])
    assert await run_closed(workflow) == ("PENDING", Decimal("4") - first)
    for attempt in range(2):
        assert await run_closed(workflow) == ("PENDING", Decimal("4") - first)
    assert len(orders) == 1
    answers[next(iter(orders))]["state"] = terminal
    async with get_session_factory()() as db:
        from app.models.domain import TradingAccount
        account = await db.get(TradingAccount, workflow.account_id)
        assert await CopyRouter(CredentialVault()).reconcile_pending_orders(
            db, account, workflow.adapter, {workflow.spec.inst_id: workflow.spec})
        delivery = await db.get(BindingDelivery, (workflow.binding_id, "closed:review-close"))
        assert delivery.state == "PENDING"
    assert len(orders) == 1
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert len(orders) == 2
    assert [Decimal(order["sz"]) for order in orders.values()] == [Decimal("4"), Decimal("4") - first]
    async with get_session_factory()() as db:
        records = (await db.scalars(select(ExecutionRecord).where(ExecutionRecord.my_qty > 0))).all()
        assert sum(Decimal(str(record.my_qty)) for record in records) == 4
        intents = (await db.scalars(select(OrderIntent))).all()
        assert len(intents) == 2
        assert all((intent.delivery_event_id or intent.source_event_id) == "closed:review-close" for intent in intents)


async def test_closed_limited_availability_full_fill_keeps_remainder(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    orders, _ = close_transport(workflow, [("filled", Decimal("1.2")), ("filled", Decimal("2.8"))])
    workflow.position.avail_pos = 1.2
    assert await run_closed(workflow) == ("PENDING", Decimal("2.8"))
    workflow.position.avail_pos = workflow.position.pos
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert len(orders) == 2


@pytest.mark.parametrize("unknown", ["missing", "lookup_error"])
async def test_closed_unknown_only_queries_until_terminal(exit_delivery_workflow, unknown):
    workflow = exit_delivery_workflow
    orders, answers = close_transport(workflow, [(unknown, Decimal("0")), ("filled", Decimal("4"))])
    for attempt in range(3):
        assert await run_closed(workflow) == ("PENDING", Decimal("4"))
        assert len(orders) == 1
    answers[next(iter(orders))]["state"] = "canceled"
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert len(orders) == 2


async def test_legacy_applied_closed_delivery_with_exposure_is_recovered(exit_delivery_workflow):
    workflow = exit_delivery_workflow
    orders, _ = close_transport(workflow, [("filled", Decimal("4"))])
    closed = {"id": "review-close", "symbol": "BTCUSDT", "side": "LONG",
              "opened": workflow.source_time - 1000, "closed": workflow.source_time}
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.seen_closed_ids_json = json.dumps(["review-close"])
        db.add(BindingDelivery(binding_id=workflow.binding_id, source_event_id="closed:review-close",
            source_sequence=workflow.source_time, state="APPLIED",
            payload_json=json.dumps({"_closed": closed, "_event_id": "closed:review-close"})))
        await db.commit()
    workflow.position.avail_pos = 0
    assert await run_closed(workflow) == ("PENDING", Decimal("4"))
    workflow.position.avail_pos = 10
    assert await run_closed(workflow) == ("APPLIED", Decimal("0"))
    assert len(orders) == 1


async def seed_history(trading, state="FILLED", filled="1", reported=True, matching=False):
    inst_id = "BTCUSDT" if matching else "DELISTEDUSDT"
    trading.db.add(OrderIntent(id="review-history", binding_id=trading.other.id,
        account_id=trading.account.id, inst_id=inst_id, pos_side="long", source_event_id="old",
        state=state, requested_qty="1", filled_qty=filled, avg_px="100",
        exchange_order_id="historical-order", reduce_only=False))
    if reported and Decimal(filled) > 0:
        trading.db.add(ExecutionRecord(binding_id=trading.other.id, lead_id=trading.other.lead_id,
            lead_name="offline history", account_name="offline",
            account_id=trading.account.id, inst_id=inst_id, symbol=inst_id, position_side="LONG",
            side="BUY", action="OPEN_LONG", status="EXECUTED", my_qty=float(filled), my_price=100,
            order_id="historical-order", details="Intent accounting key review-history"))
    await trading.db.commit()


async def protect(trading):
    trading.binding.tp_sl_mode = "independent"
    trading.binding.enable_stop_loss = True
    await trading.db.commit()
    exchange = OfflineExchange(price=90)
    exchange.get_orderbook.return_value = OrderBook([[90, 100]], [[90.1, 100]])
    actions = await trading.router.check_position_guards(trading.db, trading.lead, trading.account,
        trading.binding, exchange, [trading.pos], {"BTCUSDT": trading.spec},
        notify_cfg={"notify_on_execution": False, "notify_on_risk_block": False})
    return actions, exchange


@pytest.mark.parametrize("filled,reported", [("0", False), ("1", True)])
async def test_completed_delisted_history_does_not_block_account_reconcile(trading, filled, reported):
    await seed_history(trading, filled=filled, reported=reported)
    assert await trading.router.reconcile_pending_orders(trading.db, trading.account,
        OfflineExchange(), {"BTCUSDT": trading.spec})


@pytest.mark.parametrize("state,filled,reported", [("UNKNOWN", "0", False), ("NEW", "0", False),
                                                 ("FILLED", "1", False)])
async def test_unresolved_unrelated_history_blocks_entries_not_unrelated_protection(
    trading, state, filled, reported,
):
    owned, other, _, _ = await seed_allocation(trading)
    await seed_history(trading, state, filled, reported)
    assert not await trading.router.reconcile_pending_orders(trading.db, trading.account,
        OfflineExchange(), {"BTCUSDT": trading.spec})
    actions, exchange = await protect(trading)
    assert actions == ["BTCUSDT:long"]
    assert len(exchange.orders) == 1 and exchange.orders[0]["inst_id"] == "BTCUSDT"
    assert owned.owned_qty == 0 and other.owned_qty == 9


async def test_unknown_other_binding_on_matching_position_blocks_protection(trading):
    owned, other, _, _ = await seed_allocation(trading)
    await seed_history(trading, "UNKNOWN", "0", False, matching=True)
    actions, exchange = await protect(trading)
    assert actions == [] and exchange.orders == []
    assert owned.owned_qty == 1 and other.owned_qty == 9


async def test_matching_unknown_blocks_only_that_position_not_same_bindings_other_risk(trading):
    owned, other, _, _ = await seed_allocation(trading)
    await seed_history(trading, "UNKNOWN", "0", False, matching=True)
    unrelated = PositionAllocation(binding_id=trading.binding.id, account_id=trading.account.id,
        inst_id="ETHUSDT", pos_side="long", owned_qty=2, owned_qty_exact="2",
        entry_cost=200, entry_cost_exact="200", opened_at_ms=100)
    trading.db.add(unrelated)
    trading.binding.tp_sl_mode = "independent"
    trading.binding.enable_stop_loss = True
    await trading.db.commit()
    exchange = OfflineExchange(price=90)
    eth_position = replace(trading.pos, inst_id="ETHUSDT", symbol="ETHUSDT", pos=2, avail_pos=2)
    exchange.get_positions.return_value = [trading.pos, eth_position]
    exchange.get_orderbook.return_value = OrderBook([[90, 100]], [[90.1, 100]])
    actions = await trading.router.check_position_guards(trading.db, trading.lead, trading.account,
        trading.binding, exchange, [trading.pos, eth_position],
        {"BTCUSDT": trading.spec, "ETHUSDT": replace(trading.spec, inst_id="ETHUSDT")},
        notify_cfg={"notify_on_execution": False, "notify_on_risk_block": False})
    assert actions == ["ETHUSDT:long"]
    assert len(exchange.orders) == 1 and exchange.orders[0]["inst_id"] == "ETHUSDT"
    assert owned.owned_qty == 1 and other.owned_qty == 9 and unrelated.owned_qty == 0


async def test_cross_binding_order_id_is_not_reporting_ambiguity(trading):
    owned, other, _, _ = await seed_allocation(trading)
    for binding in (trading.binding, trading.other):
        trading.db.add(OrderIntent(id=f"history-{binding.id}", binding_id=binding.id,
            account_id=trading.account.id, inst_id="BTCUSDT", pos_side="long", source_event_id="history",
            state="FILLED", requested_qty="1", filled_qty="1", avg_px="100", reduce_only=False,
            exchange_order_id="venue-reused-order-id"))
        trading.db.add(ExecutionRecord(binding_id=binding.id, lead_id=binding.lead_id,
            lead_name="offline history", account_name="offline",
            account_id=trading.account.id, inst_id="BTCUSDT", symbol="BTCUSDT", position_side="LONG",
            side="BUY", action="OPEN_LONG", status="EXECUTED", my_qty=1, my_price=100,
            order_id="venue-reused-order-id", details="legacy untagged"))
    await trading.db.commit()
    actions, exchange = await protect(trading)
    assert actions == ["BTCUSDT:long"] and len(exchange.orders) == 1
    assert owned.owned_qty == 0 and other.owned_qty == 9
