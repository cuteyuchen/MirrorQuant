"""Independent reduction regressions: real router, sessions and durable accounting.

Only the exchange boundary is simulated. No exchange or notification transport runs.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core import database
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt, BindingDelivery, ExecutionRecord, LeadSource, NotificationOutbox,
    OrderIntent, PositionAllocation, PositionGuardState, SystemConfig, TradingAccount,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition, OrderResult
from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import order_dedup_key
from test_routing_deliveries import exit_delivery_workflow, order, snapshot


class ReductionExchange:
    def __init__(self, workflow):
        self.workflow = workflow
        self.orders = {}
        self.queries = []
        self.script = []
        self.lookup_mode = "normal"
        self.lose_ack = False
        self.evidence_fields = {}
        self.positions = [workflow.position]

    async def balance(self, *args, **kwargs):
        return BalanceInfo(eq=100, avail_eq=100, cash_bal=100, upl=0, currency="USDT")

    async def get_positions(self):
        return self.positions

    def resolve(self, client_id, state, quantity):
        submitted = self.orders[client_id]
        quantity = Decimal(quantity)
        assert submitted["physical_fill"] <= quantity <= Decimal(submitted["sz"])
        delta = quantity - submitted["physical_fill"]
        position = self.workflow.position
        position.pos = float(Decimal(str(position.pos)) - delta)
        position.avail_pos = float(Decimal(str(position.avail_pos)) - delta)
        submitted.update(state=state, fill=quantity, physical_fill=quantity)

    async def place_order(self, **submitted):
        workflow = self.workflow
        client_id = submitted["cl_ord_id"]
        quantity = Decimal(submitted["sz"])
        assert client_id not in self.orders, "a client ID must never be resubmitted"
        assert submitted["inst_id"] == workflow.spec.inst_id
        assert submitted["pos_side"] == workflow.position.pos_side
        assert submitted["side"] == ("sell" if workflow.position.pos_side == "long" else "buy")
        assert submitted["reduce_only"] is True
        assert submitted["ord_type"] == "market" and submitted["px"] is None
        assert submitted["td_mode"] == "cross"
        assert 0 < quantity <= Decimal(str(workflow.position.avail_pos))
        async with get_session_factory()() as db:
            intent = await db.get(OrderIntent, client_id)
            assert intent is not None and intent.state == "SUBMITTING"
            assert (intent.binding_id, intent.account_id) == (workflow.binding_id, workflow.account_id)
            assert intent.reduce_only and Decimal(intent.requested_qty) == quantity
            assert Decimal(intent.filled_qty) == 0
            delivery = await db.get(BindingDelivery, (workflow.binding_id, workflow.event_id))
            assert delivery.state == "PENDING"
            assert Decimal(delivery.exit_initial_owned_qty) == 4
            assert Decimal(delivery.exit_target_qty) == Decimal("1.6")
            assert quantity <= Decimal(delivery.exit_remaining_qty)
        self.orders[client_id] = dict(submitted, physical_fill=Decimal("0"))
        state, fill = self.script.pop(0) if self.script else ("filled", quantity)
        self.resolve(client_id, state, str(fill))
        if self.lose_ack:
            raise TimeoutError("synthetic lost acknowledgement")
        return OrderResult("offline-" + client_id, client_id)

    async def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        self.queries.append((inst_id, cl_ord_id))
        assert cl_ord_id in self.orders and inst_id == self.workflow.spec.inst_id
        if self.lookup_mode == "missing":
            return None
        if self.lookup_mode == "error":
            raise TimeoutError("synthetic unresolved lookup")
        submitted = self.orders[cl_ord_id]
        return {"ordId": "offline-" + cl_ord_id, "clOrdId": cl_ord_id,
            "instId": inst_id, "posSide": submitted["pos_side"], "side": submitted["side"],
            "accFillSz": str(submitted["fill"]), "avgPx": "100", "state": submitted["state"],
            **self.evidence_fields}


@pytest.fixture(autouse=True)
async def reduction_database(setup_test_db, tmp_path):
    await database.reset_db_engine()
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'persistent-reductions.sqlite'}"
    database.get_engine(database_url)
    await database.init_db()
    yield database_url
    await database.reset_db_engine()


@pytest.fixture
async def reduction_workflow(exit_delivery_workflow, reduction_database, request):
    workflow = exit_delivery_workflow
    workflow.database_url = reduction_database
    position_side = getattr(request, "param", "long")
    workflow.position.pos_side = position_side
    workflow.source_order = order(orderId="persistent-reduce",
        side="SELL" if position_side == "long" else "BUY", positionSide=position_side.upper(), executedQty="4",
        orderTime=workflow.source_time, orderUpdateTime=workflow.source_time)
    workflow.event_id = order_dedup_key(workflow.source_order) + ":4"
    workflow.exchange = ReductionExchange(workflow)
    workflow.adapter.get_contract_balance = workflow.exchange.balance
    workflow.adapter.get_positions = workflow.exchange.get_positions
    workflow.adapter.place_order = workflow.exchange.place_order
    workflow.adapter.get_order = workflow.exchange.get_order
    async with get_session_factory()() as db:
        for allocation_id in (workflow.owned_id, workflow.other_owned_id):
            (await db.get(PositionAllocation, allocation_id)).pos_side = position_side
        (await db.get(BindingDebt, workflow.debt_id)).position_side = position_side.upper()
        (await db.get(PositionGuardState, workflow.guard_id)).pos_side = position_side
        notifications = await db.get(SystemConfig, "notifications")
        notifications.value_json = json.dumps({"outbox_enabled": True,
            "notify_on_execution": True, "notify_on_lead_order": False,
            "notify_on_risk_block": True, "notify_on_security_alert": False,
            "webhook_enabled": False, "telegram_enabled": False,
            "wecom_enabled": False, "dingtalk_enabled": False})
        opposite = PositionAllocation(binding_id=workflow.binding_id, account_id=workflow.account_id,
            inst_id=workflow.spec.inst_id, pos_side="short" if position_side == "long" else "long",
            owned_qty=7, owned_qty_exact="7",
            entry_cost=700, entry_cost_exact="700", generation=3, opened_at_ms=100)
        other_instrument = PositionAllocation(binding_id=workflow.binding_id, account_id=workflow.account_id,
            inst_id="ETH-USDT-SWAP", pos_side="long", owned_qty=2, owned_qty_exact="2",
            entry_cost=200, entry_cost_exact="200", generation=2, opened_at_ms=100)
        db.add_all([opposite, other_instrument])
        await db.commit()
        workflow.isolated_ids = [workflow.other_owned_id, opposite.id, other_instrument.id]
        workflow.isolated_before = [allocation_values(row) for row in (await db.scalars(
            select(PositionAllocation).where(PositionAllocation.id.in_(workflow.isolated_ids))
                .order_by(PositionAllocation.id))).all()]
    workflow.exchange.positions.extend([
        ExchangePosition(workflow.spec.inst_id, "BTCUSDT", opposite.pos_side, "cross", 7, 7, 100, 0, 0, 700, 10),
        ExchangePosition("ETH-USDT-SWAP", "ETHUSDT", "long", "cross", 2, 2, 100, 0, 0, 200, 10),
    ])
    return workflow


def allocation_values(row):
    return (row.binding_id, row.account_id, row.inst_id, row.pos_side, row.owned_qty,
        row.owned_qty_exact, row.entry_cost, row.entry_cost_exact, row.generation, row.opened_at_ms)


async def restart_route(workflow):
    await database.reset_db_engine()
    database.get_engine(workflow.database_url)
    await database.init_db()
    lead_snapshot = snapshot()
    lead_snapshot["openPositions"][0].update(qty=6, qtyExact="6",
        positionSide=workflow.source_order["positionSide"])
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, workflow.lead_id)
        lead.snapshot_json = json.dumps(lead_snapshot)
        await db.commit()
        await CopyRouter(CredentialVault()).process_lead_events(db=db, lead=lead,
            raw_orders=[workflow.source_order], raw_closed=[], snapshot=lead_snapshot, proxies=[])


async def assert_progress(workflow, filled, remaining, state, fill_records):
    filled, remaining = Decimal(filled), Decimal(remaining)
    async with get_session_factory()() as db:
        deliveries = (await db.scalars(select(BindingDelivery))).all()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert (delivery.binding_id, delivery.source_event_id, delivery.state) == (
            workflow.binding_id, workflow.event_id, state)
        assert Decimal(delivery.exit_initial_owned_qty) == 4
        assert Decimal(delivery.exit_target_qty) == Decimal("1.6")
        assert Decimal(delivery.exit_filled_qty) == filled
        assert Decimal(delivery.exit_remaining_qty) == remaining
        assert filled + remaining == Decimal("1.6")
        own = await db.get(PositionAllocation, workflow.owned_id)
        assert (delivery.exit_account_id, delivery.exit_inst_id, delivery.exit_pos_side,
            delivery.exit_allocation_id, delivery.exit_generation) == (
            workflow.account_id, own.inst_id, own.pos_side, own.id, own.generation)
        assert Decimal(own.owned_qty_exact) == 4 - filled and own.owned_qty == float(4 - filled)
        assert Decimal(own.entry_cost_exact) == (4 - filled) * 100
        assert own.entry_cost == float((4 - filled) * 100)
        assert own.generation == 1 and own.opened_at_ms == workflow.source_time - 1000
        debt = await db.get(BindingDebt, workflow.debt_id)
        assert Decimal(debt.pending_usd_exact) == (4 - filled) * 10
        assert debt.pending_usd == float((4 - filled) * 10)
        assert await db.get(PositionGuardState, workflow.guard_id) is not None
        isolated = (await db.scalars(select(PositionAllocation).where(
            PositionAllocation.id.in_(workflow.isolated_ids)).order_by(PositionAllocation.id))).all()
        assert [allocation_values(row) for row in isolated] == workflow.isolated_before
        intents = (await db.scalars(select(OrderIntent))).all()
        assert sum((Decimal(row.filled_qty) for row in intents), Decimal("0")) == filled
        assert {row.id for row in intents} == set(workflow.exchange.orders)
        assert all(row.binding_id == workflow.binding_id and row.account_id == workflow.account_id
            and row.inst_id == own.inst_id and row.pos_side == own.pos_side and row.reduce_only
            and (row.delivery_event_id or row.source_event_id) == workflow.event_id for row in intents)
        records = (await db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()
        executions = [row for row in records if row.my_qty > 0]
        assert len(executions) == fill_records
        assert sum((Decimal(row.my_qty_exact) for row in executions), Decimal("0")) == filled
        assert all(row.binding_id == workflow.binding_id and row.delivery_event_id == workflow.event_id
            and row.inst_id == own.inst_id and row.position_side == own.pos_side.upper()
            and row.side == workflow.source_order["side"] for row in executions)
        assert all(row.status in ("EXECUTED", "PARTIAL") and Decimal(row.my_price_exact) == 100
            and row.my_qty == float(Decimal(row.my_qty_exact)) for row in executions)
        assert len({(row.order_id, row.details) for row in executions}) == len(executions)
        for intent in intents:
            assert sum((Decimal(row.my_qty_exact) for row in executions if row.order_id == intent.exchange_order_id),
                Decimal("0")) == Decimal(intent.filled_qty)
        outbox = (await db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
        execution_notifications = [row for row in outbox if json.loads(row.payload)["event_type"] in ("EXECUTED", "PARTIAL")]
        assert len(execution_notifications) == fill_records
        assert {json.loads(row.payload)["execution_id"] for row in execution_notifications} == {
            row.id for row in executions}
        assert all(row.status == "pending" and row.attempts == 0 for row in outbox)
        assert len({json.loads(row.payload)["event_key"] for row in outbox}) == len(outbox)
        assert all(json.loads(row.payload)["channels"] == ["outbox"] for row in outbox)
        return ([tuple(getattr(row, column.name) for column in row.__table__.columns) for row in records],
            [tuple(getattr(row, column.name) for column in row.__table__.columns) for row in outbox])


@pytest.mark.parametrize("reduction_workflow", ["long", "short"], indirect=True)
async def test_availability_cap_persists_target_across_restart_and_post_fill_replay(reduction_workflow):
    workflow = reduction_workflow
    workflow.position.avail_pos = 1
    await restart_route(workflow)
    await assert_progress(workflow, "1", "0.6", "PENDING", 1)
    assert [Decimal(row["sz"]) for row in workflow.exchange.orders.values()] == [Decimal("1")]
    workflow.position.avail_pos = workflow.position.pos
    await restart_route(workflow)
    before = await assert_progress(workflow, "1.6", "0", "APPLIED", 2)
    assert [Decimal(row["sz"]) for row in workflow.exchange.orders.values()] == [Decimal("1"), Decimal("0.6")]
    for replay in range(3):
        await restart_route(workflow)
        async with get_session_factory()() as db:
            account = await db.get(TradingAccount, workflow.account_id)
            router = CopyRouter(CredentialVault())
            assert await router.reconcile_pending_orders(db, account, workflow.adapter,
                {workflow.spec.inst_id: workflow.spec})
        assert await assert_progress(workflow, "1.6", "0", "APPLIED", 2) == before
        assert len(workflow.exchange.orders) == 2


@pytest.mark.parametrize("terminal_state,initial_fill,remainder", [
    ("canceled", "0.7", "0.9"), ("expired", "0.7", "0.9"),
    ("rejected", "0.7", "0.9"), ("canceled", "0", "1.6"),
])
async def test_terminal_partial_fill_restarts_with_only_fixed_target_remainder(
    reduction_workflow, terminal_state, initial_fill, remainder,
):
    workflow = reduction_workflow
    workflow.exchange.script = [(terminal_state, initial_fill), ("filled", remainder)]
    await restart_route(workflow)
    initial_records = int(Decimal(initial_fill) > 0)
    await assert_progress(workflow, initial_fill, remainder, "PENDING", initial_records)
    assert [Decimal(row["sz"]) for row in workflow.exchange.orders.values()] == [Decimal("1.6")]
    async with get_session_factory()() as db:
        delivery = await db.get(BindingDelivery, (workflow.binding_id, workflow.event_id))
        assert delivery.exit_progress == "READY"
        delivery.exit_progress = "WAITING_SOURCE_EXIT"
        await db.commit()
    await restart_route(workflow)
    before = await assert_progress(workflow, "1.6", "0", "APPLIED", initial_records + 1)
    assert [Decimal(row["sz"]) for row in workflow.exchange.orders.values()] == [Decimal("1.6"), Decimal(remainder)]
    for replay in range(3):
        await restart_route(workflow)
        assert await assert_progress(workflow, "1.6", "0", "APPLIED", initial_records + 1) == before
        assert len(workflow.exchange.orders) == 2


@pytest.mark.parametrize("lookup_mode,venue_state,initial_fill", [
    ("missing", "new", "0"), ("error", "new", "0"),
    ("normal", "new", "0"), ("normal", "partially_filled", "0.4"),
])
async def test_unknown_and_inflight_only_query_until_terminal_then_retry_remainder(
    reduction_workflow, lookup_mode, venue_state, initial_fill,
):
    workflow = reduction_workflow
    exchange = workflow.exchange
    exchange.lookup_mode = lookup_mode
    exchange.lose_ack = lookup_mode != "normal"
    exchange.script = [(venue_state, initial_fill), ("filled", "0.6")]
    await restart_route(workflow)
    before = await assert_progress(workflow, initial_fill, str(Decimal("1.6") - Decimal(initial_fill)),
        "PENDING", int(Decimal(initial_fill) > 0))
    client_id = next(iter(exchange.orders))
    for replay in range(3):
        queries_before = len(exchange.queries)
        await restart_route(workflow)
        assert len(exchange.orders) == 1
        assert len(exchange.queries) > queries_before
        assert set(exchange.queries) == {(workflow.spec.inst_id, client_id)}
        assert await assert_progress(workflow, initial_fill,
            str(Decimal("1.6") - Decimal(initial_fill)), "PENDING", int(Decimal(initial_fill) > 0)) == before
    exchange.lookup_mode, exchange.lose_ack = "normal", False
    exchange.resolve(client_id, "canceled", "1")
    async with get_session_factory()() as db:
        router = CopyRouter(CredentialVault())
        account = await db.get(TradingAccount, workflow.account_id)
        assert await router.reconcile_pending_orders(db, account, workflow.adapter,
            {workflow.spec.inst_id: workflow.spec})
    records_before_retry = 2 if Decimal(initial_fill) > 0 else 1
    await assert_progress(workflow, "1", "0.6", "PENDING", records_before_retry)
    await restart_route(workflow)
    before = await assert_progress(workflow, "1.6", "0", "APPLIED", records_before_retry + 1)
    assert [Decimal(row["sz"]) for row in exchange.orders.values()] == [Decimal("1.6"), Decimal("0.6")]
    await restart_route(workflow)
    assert await assert_progress(workflow, "1.6", "0", "APPLIED", records_before_retry + 1) == before
    assert len(exchange.orders) == 2


@pytest.mark.parametrize("physical", ["5", "8"])
async def test_total_allocation_deficit_fails_closed_and_alert_is_idempotent(reduction_workflow, physical):
    workflow = reduction_workflow
    workflow.position.pos = workflow.position.avail_pos = float(physical)
    await restart_route(workflow)
    before = await assert_progress(workflow, "0", "1.6", "PENDING", 0)
    assert workflow.exchange.orders == {}
    async with get_session_factory()() as db:
        records = (await db.scalars(select(ExecutionRecord))).all()
        assert len(records) == 1 and records[0].status == "BLOCKED_ALLOCATION_DEFICIT"
        assert len((await db.scalars(select(NotificationOutbox))).all()) == 1
    for replay in range(3):
        await restart_route(workflow)
        assert await assert_progress(workflow, "0", "1.6", "PENDING", 0) == before
        assert workflow.exchange.orders == {}
    workflow.position.pos = workflow.position.avail_pos = 10
    await restart_route(workflow)
    await assert_progress(workflow, "1.6", "0", "APPLIED", 1)
    assert [Decimal(row["sz"]) for row in workflow.exchange.orders.values()] == [Decimal("1.6")]


async def test_missing_matching_position_cannot_reduce_opposite_side_or_other_instrument(reduction_workflow):
    workflow = reduction_workflow
    workflow.exchange.positions = workflow.exchange.positions[1:]
    await restart_route(workflow)
    before = await assert_progress(workflow, "0", "1.6", "PENDING", 0)
    for replay in range(3):
        await restart_route(workflow)
        assert await assert_progress(workflow, "0", "1.6", "PENDING", 0) == before
        assert workflow.exchange.orders == {}
    async with get_session_factory()() as db:
        record = await db.scalar(select(ExecutionRecord))
        assert record.status == "SKIPPED_NO_LOCAL_POS"
        assert record.details == "Binding-owned position absent from exchange holdings"


@pytest.mark.parametrize("field,value", [
    ("instId", "ETH-USDT-SWAP"), ("posSide", "short"), ("side", "buy"),
    ("accountId", "999"), ("clOrdId", "another-bindings-order"),
])
async def test_conflicting_order_scope_cannot_apply_fill_or_resubmit(reduction_workflow, field, value):
    workflow = reduction_workflow
    workflow.exchange.evidence_fields = {field: value}
    await restart_route(workflow)
    before = await assert_progress(workflow, "0", "1.6", "PENDING", 0)
    for replay in range(3):
        queries_before = len(workflow.exchange.queries)
        await restart_route(workflow)
        assert await assert_progress(workflow, "0", "1.6", "PENDING", 0) == before
        assert len(workflow.exchange.orders) == 1
        assert len(workflow.exchange.queries) > queries_before
    workflow.exchange.evidence_fields = {}
    await restart_route(workflow)
    before = await assert_progress(workflow, "1.6", "0", "APPLIED", 1)
    await restart_route(workflow)
    assert await assert_progress(workflow, "1.6", "0", "APPLIED", 1) == before
    assert len(workflow.exchange.orders) == 1


@pytest.mark.parametrize("table", ["execution_records", "notification_outbox"])
async def test_reporting_insert_failure_rolls_back_fill_and_restart_recovers_once(reduction_workflow, table):
    workflow = reduction_workflow
    async with database.get_engine().begin() as connection:
        await connection.execute(text("CREATE TRIGGER reject_reduction_reporting "
            f"BEFORE INSERT ON {table} BEGIN "
            "SELECT RAISE(ABORT, 'synthetic reporting persistence outage'); END"))
    await restart_route(workflow)
    await assert_progress(workflow, "0", "1.6", "PENDING", 0)
    assert len(workflow.exchange.orders) == 1
    assert Decimal(str(workflow.position.pos)) == Decimal("8.4")
    async with get_session_factory()() as db:
        intent = await db.scalar(select(OrderIntent))
        assert intent.state == "UNKNOWN" and Decimal(intent.filled_qty) == 0
        assert (await db.scalars(select(ExecutionRecord))).all() == []
        assert (await db.scalars(select(NotificationOutbox))).all() == []
    async with database.get_engine().begin() as connection:
        await connection.execute(text("DROP TRIGGER reject_reduction_reporting"))
    await restart_route(workflow)
    before = await assert_progress(workflow, "1.6", "0", "APPLIED", 1)
    for replay in range(3):
        await restart_route(workflow)
        assert await assert_progress(workflow, "1.6", "0", "APPLIED", 1) == before
        assert len(workflow.exchange.orders) == 1


async def test_legacy_exit_without_target_upgrade_requires_review_not_recomputed_reduction(reduction_workflow):
    workflow = reduction_workflow
    workflow.position.avail_pos = 1
    await restart_route(workflow)
    before = await assert_progress(workflow, "1", "0.6", "PENDING", 1)
    async with get_session_factory()() as db:
        delivery = await db.get(BindingDelivery, (workflow.binding_id, workflow.event_id))
        delivery.state = "APPLIED"
        await db.commit()
    async with database.get_engine().begin() as connection:
        for column in BindingDelivery.__table__.columns:
            if column.name.startswith("exit_"):
                await connection.execute(text(f"ALTER TABLE binding_deliveries DROP COLUMN {column.name}"))
    await database.reset_db_engine()
    database.get_engine(workflow.database_url)
    await database.init_db()
    await database.init_db()
    async with get_session_factory()() as db:
        delivery = await db.get(BindingDelivery, (workflow.binding_id, workflow.event_id))
        assert delivery.state == "PENDING" and delivery.exit_progress == "REVIEW_REQUIRED"
        assert delivery.exit_target_qty is None and delivery.exit_remaining_qty is None
        records = (await db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()
        outbox = (await db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
        assert [tuple(getattr(row, column.name) for column in row.__table__.columns) for row in records] == before[0]
        assert [tuple(getattr(row, column.name) for column in row.__table__.columns) for row in outbox] == before[1]
    workflow.position.avail_pos = workflow.position.pos
    for replay in range(3):
        await restart_route(workflow)
        async with get_session_factory()() as db:
            delivery = await db.get(BindingDelivery, (workflow.binding_id, workflow.event_id))
            assert delivery.state == "PENDING" and delivery.exit_progress == "REVIEW_REQUIRED"
            assert delivery.exit_target_qty is None
            own = await db.get(PositionAllocation, workflow.owned_id)
            assert Decimal(own.owned_qty_exact) == 3 and Decimal(own.entry_cost_exact) == 300
            assert Decimal((await db.scalar(select(OrderIntent))).filled_qty) == 1
            records = (await db.scalars(select(ExecutionRecord).where(ExecutionRecord.my_qty > 0))).all()
            assert len(records) == 1 and Decimal(records[0].my_qty_exact) == 1
        assert len(workflow.exchange.orders) == 1
