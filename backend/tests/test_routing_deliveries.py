"""Durable fan-out and source identity regression tests, without exchange I/O."""
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt, BindingDelivery, BindingSourceProgress, CopyBinding, ExecutionRecord,
    LeadSource, NotificationOutbox, OrderIntent, PositionAllocation, PositionGuardState,
    SystemConfig, TradingAccount,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition, InstrumentSpec, OrderResult
from app.services.adapters.okx import OKXAdapter
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import order_dedup_key, slim_order


def order(**kwargs):
    value = dict(orderId="one", orderTime=1000, orderUpdateTime=1000, symbol="BTCUSDT",
                 side="BUY", positionSide="LONG", type="MARKET", executedQty="0.1", avgPrice="100")
    value.update(kwargs)
    return value


def snapshot():
    return {"profile": {"marginBalance": 1000}, "openPositions": [
        {"symbol": "BTCUSDT", "positionSide": "LONG", "qty": 10, "qtyExact": "10"},
    ], "openPositionsMeta": {
        "source": "api", "authoritative": True, "complete": True,
        "positionShow": True, "asOf": int(time.time() * 1000),
    }}


async def make_workflow(db, monkeypatch, bindings=(True,)):
    vault = CredentialVault()
    lead = LeadSource(name="lead", portfolio_id="delivery-tests", margin_balance=1000, status="running")
    db.add(lead)
    await db.flush()
    rows, adapters, calls = [], {}, []
    for index, seeded in enumerate(bindings):
        name = f"account-{index}"
        account = TradingAccount(name=name, exchange="okx", api_key_masked="***",
            encrypted_credentials=vault.encrypt_dict({"api_key": name, "secret_key": "dummy"}))
        db.add(account)
        await db.flush()
        binding = CopyBinding(name=name, lead_id=lead.id, account_id=account.id, baseline_seeded=seeded)
        db.add(binding)
        rows.append((binding, account))
        adapters[name] = SimpleNamespace(
            resolve_symbol=OKXAdapter({}).resolve_symbol,
            get_instruments=AsyncMock(return_value={}),
            get_usdt_balance=AsyncMock(return_value=BalanceInfo(eq=100, avail_eq=100, cash_bal=100, upl=0)),
            get_positions=AsyncMock(return_value=[]), sync_time=AsyncMock(return_value=0),
            verify_account_configuration=AsyncMock(return_value=True),
        )
    await db.commit()
    monkeypatch.setattr("app.services.engine.router.create_exchange_adapter", lambda **kw: adapters[kw["credentials"]["api_key"]])
    router = CopyRouter(vault)

    async def execute(db, binding, lead, account, adapter, batch, *args, **kwargs):
        calls.append((binding.id, batch["side"], Decimal(batch["qty"]), batch["_event_id"]))
        db.info["delivery"].state = "APPLIED"

    monkeypatch.setattr(router, "_execute_batch_for_binding", execute)
    return lead, rows, adapters, calls, router


async def route(db, router, lead, orders):
    await router.process_lead_events(db=db, lead=lead, raw_orders=orders, raw_closed=[], snapshot=snapshot(), proxies=[])


@pytest.fixture
async def exit_delivery_workflow(monkeypatch):
    vault = CredentialVault()
    spec = InstrumentSpec("BTC-USDT-SWAP", Decimal("0.1"), Decimal("0.1"),
        Decimal("0.1"), Decimal("0.1"), settle_ccy="USDT", base_ccy="BTC", quote_ccy="USDT")
    source_time = int(time.time() * 1000) - 1000
    async with get_session_factory()() as db:
        lead = LeadSource(name="exit lead", portfolio_id="exit-delivery", margin_balance=1000, status="running")
        other_lead = LeadSource(name="other owner", portfolio_id="other-exit-delivery")
        account = TradingAccount(name="offline exit", exchange="okx", api_key_masked="***",
            encrypted_credentials=vault.encrypt_dict({"api_key": "offline-exit", "secret_key": "dummy"}))
        db.add_all([lead, other_lead, account])
        await db.flush()
        binding = CopyBinding(name="exit binding", lead_id=lead.id, account_id=account.id,
            baseline_seeded=True, symbol_map_json=json.dumps({"BTCUSDT": spec.inst_id}))
        other_binding = CopyBinding(name="other binding", lead_id=other_lead.id, account_id=account.id,
            baseline_seeded=True)
        db.add_all([binding, other_binding])
        await db.flush()
        owned = PositionAllocation(binding_id=binding.id, account_id=account.id, inst_id=spec.inst_id,
            pos_side="long", owned_qty=4, owned_qty_exact="4", entry_cost=400, entry_cost_exact="400",
            generation=1, opened_at_ms=source_time - 1000)
        other_owned = PositionAllocation(binding_id=other_binding.id, account_id=account.id, inst_id=spec.inst_id,
            pos_side="long", owned_qty=5, owned_qty_exact="5", entry_cost=500, entry_cost_exact="500",
            generation=1, opened_at_ms=source_time - 1000)
        debt = BindingDebt(binding_id=binding.id, symbol=spec.inst_id, position_side="LONG",
            pending_usd=40, pending_usd_exact="40")
        guard = PositionGuardState(binding_id=binding.id, inst_id=spec.inst_id, pos_side="long",
            entry_px=100, peak_px=100, trough_px=100)
        notifications = await db.get(SystemConfig, "notifications")
        notifications.value_json = json.dumps({"notify_on_execution": False, "notify_on_lead_order": False,
            "notify_on_risk_block": False, "notify_on_security_alert": False})
        db.add_all([owned, other_owned, debt, guard])
        await db.commit()
        workflow = SimpleNamespace(lead_id=lead.id, binding_id=binding.id, account_id=account.id,
            owned_id=owned.id, other_owned_id=other_owned.id, debt_id=debt.id, guard_id=guard.id,
            spec=spec, source_time=source_time)

    position = ExchangePosition(spec.inst_id, "BTCUSDT", "long", "cross", 10, 10, 100, 0, 0, 100, 10)
    exchange_orders = {}

    async def place_order(**submitted):
        assert submitted["inst_id"] == spec.inst_id
        assert submitted["side"] == "sell" and submitted["pos_side"] == "long"
        assert submitted["reduce_only"] is True and submitted["ord_type"] == "market"
        assert submitted["td_mode"] == "cross" and submitted["px"] is None
        assert submitted["cl_ord_id"] not in exchange_orders
        quantity = Decimal(submitted["sz"])
        assert Decimal("0") < quantity <= Decimal("4")
        async with get_session_factory()() as db:
            intent = await db.get(OrderIntent, submitted["cl_ord_id"])
            assert intent is not None and intent.state == "SUBMITTING"
            assert intent.binding_id == workflow.binding_id and intent.account_id == workflow.account_id
            assert intent.reduce_only and Decimal(intent.requested_qty) == quantity
            assert Decimal(intent.filled_qty) == 0
            delivery = await db.get(BindingDelivery, (intent.binding_id, intent.source_event_id))
            assert delivery is not None and delivery.state == "PENDING"
            assert Decimal((await db.get(PositionAllocation, workflow.owned_id)).owned_qty_exact) == 4
        exchange_orders[submitted["cl_ord_id"]] = dict(submitted)
        position.pos = position.avail_pos = float(Decimal(str(position.pos)) - quantity)
        position.notional_usd = float(Decimal(str(position.pos)) * spec.ct_val * 100)
        return OrderResult("offline-exit-order", submitted["cl_ord_id"])

    async def get_order(inst_id, ord_id=None, cl_ord_id=None):
        submitted = exchange_orders.get(cl_ord_id)
        if submitted is None or submitted["inst_id"] != inst_id:
            return None
        return {"ordId": "offline-exit-order", "clOrdId": cl_ord_id,
            "accFillSz": submitted["sz"], "avgPx": "100", "state": "filled"}

    adapter = SimpleNamespace(
        resolve_symbol=OKXAdapter({}).resolve_symbol,
        sync_time=AsyncMock(return_value=0), get_instruments=AsyncMock(return_value={spec.inst_id: spec}),
        get_contract_balance=AsyncMock(side_effect=RuntimeError("USDT collateral temporarily unavailable")),
        get_usdt_balance=AsyncMock(return_value=BalanceInfo(eq=100, avail_eq=100, cash_bal=100, upl=0)),
        get_positions=AsyncMock(return_value=[position]),
        place_order=AsyncMock(side_effect=place_order), get_order=AsyncMock(side_effect=get_order),
    )
    monkeypatch.setattr("app.services.engine.router.create_exchange_adapter", lambda **kwargs: adapter)
    workflow.adapter, workflow.position, workflow.exchange_orders = adapter, position, exchange_orders
    return workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind,close_quantity,remaining_owned,expected_action", [
    ("reduce", Decimal("1.6"), Decimal("2.4"), "PARTIAL_REDUCE"),
    ("full_close", Decimal("4"), Decimal("0"), "FULL_CLOSE"),
    ("closed", Decimal("4"), Decimal("0"), "RECONCILE_CLOSE"),
])
async def test_collateral_failure_exit_recovery_is_durable_and_idempotent(
    exit_delivery_workflow, event_kind, close_quantity, remaining_owned, expected_action,
):
    workflow = exit_delivery_workflow
    adapter = workflow.adapter
    source_order = order(orderId="exit-order", side="SELL", executedQty="4" if event_kind == "reduce" else "10",
        orderTime=workflow.source_time, orderUpdateTime=workflow.source_time)
    closed = {"id": "exit-closed", "symbol": "BTCUSDT", "side": "LONG",
        "opened": workflow.source_time - 2000, "closed": workflow.source_time}
    raw_orders = [] if event_kind == "closed" else [source_order]
    raw_closed = [closed] if event_kind == "closed" else []
    event_id = "closed:exit-closed" if event_kind == "closed" else (
        order_dedup_key(source_order) + ":" + source_order["executedQty"])
    intent_id = "mq" + hashlib.sha256(
        f"{workflow.account_id}:{workflow.binding_id}:{event_id}".encode()).hexdigest()[:30]

    for attempt in range(3):
        if attempt == 1:
            adapter.get_contract_balance.side_effect = None
            adapter.get_contract_balance.return_value = BalanceInfo(
                eq=100, avail_eq=100, cash_bal=100, upl=0, currency="USDT")
        lead_snapshot = snapshot()
        if event_kind == "reduce":
            lead_snapshot["openPositions"][0].update(qty=6, qtyExact="6")
        else:
            lead_snapshot["openPositions"] = []
            lead_snapshot["openPositionsMeta"]["emptyVerified"] = True
        async with get_session_factory()() as db:
            lead = await db.get(LeadSource, workflow.lead_id)
            lead.snapshot_json = json.dumps(lead_snapshot)
            await db.commit()
            await CopyRouter(CredentialVault()).process_lead_events(
                db=db, lead=lead, raw_orders=raw_orders, raw_closed=raw_closed, snapshot=lead_snapshot, proxies=[])

        async with get_session_factory()() as db:
            deliveries = (await db.scalars(select(BindingDelivery))).all()
            assert len(deliveries) == 1
            delivery = deliveries[0]
            assert delivery.binding_id == workflow.binding_id and delivery.source_event_id == event_id
            payload = json.loads(delivery.payload_json)
            assert payload["_event_id"] == event_id
            if event_kind == "closed":
                assert payload["_closed"] == closed
            else:
                assert payload["side"] == "SELL" and payload["positionSide"] == "LONG"
                assert Decimal(payload["qtyExact"]) == Decimal(source_order["executedQty"])
            lead = await db.get(LeadSource, workflow.lead_id)
            assert json.loads(lead.snapshot_json) == lead_snapshot
            if event_kind == "closed":
                assert json.loads(lead.seen_closed_ids_json) == [closed["id"]]
                assert await db.scalar(select(BindingSourceProgress)) is None
            else:
                assert json.loads(lead.seen_order_keys_json) == [order_dedup_key(source_order)]
                progress = await db.get(BindingSourceProgress, (workflow.binding_id, order_dedup_key(source_order)))
                assert Decimal(progress.cumulative_qty) == Decimal(source_order["executedQty"])
                assert Decimal(progress.cumulative_notional) == Decimal(source_order["executedQty"]) * 100
            owned = await db.get(PositionAllocation, workflow.owned_id)
            other_owned = await db.get(PositionAllocation, workflow.other_owned_id)
            debt = await db.get(BindingDebt, workflow.debt_id)
            guard = await db.get(PositionGuardState, workflow.guard_id)
            account = await db.get(TradingAccount, workflow.account_id)
            intents = (await db.scalars(select(OrderIntent))).all()
            records = (await db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()
            assert other_owned.owned_qty == 5 and Decimal(other_owned.owned_qty_exact) == 5
            assert other_owned.entry_cost == 500 and Decimal(other_owned.entry_cost_exact) == 500
            assert owned.generation == 1 and owned.opened_at_ms == workflow.source_time - 1000
            assert await db.scalar(select(NotificationOutbox)) is None
            adapter.get_usdt_balance.assert_not_awaited()
            adapter.get_contract_balance.assert_awaited_with(workflow.spec)
            assert records[0].status == "BLOCKED_COLLATERAL_BALANCE"
            assert records[0].inst_id == workflow.spec.inst_id
            assert records[0].my_qty == 0 and records[0].order_id is None

            if attempt == 0:
                assert delivery.state == "PENDING"
                assert intents == [] and len(records) == 1
                assert account.status == "error" and "USDT collateral temporarily unavailable" in account.last_error
                assert owned.owned_qty == 4 and Decimal(owned.owned_qty_exact) == 4
                assert owned.entry_cost == 400 and Decimal(owned.entry_cost_exact) == 400
                assert debt.pending_usd == 40 and Decimal(debt.pending_usd_exact) == 40
                assert guard is not None
                assert workflow.position.pos == 10 and workflow.exchange_orders == {}
                adapter.get_positions.assert_not_awaited()
                adapter.place_order.assert_not_awaited()
                adapter.get_order.assert_not_awaited()
            else:
                assert delivery.state == "APPLIED"
                assert len(intents) == 1 and len(records) == 2, [
                    (record.status, record.action, record.details) for record in records]
                intent = intents[0]
                assert intent.id == intent_id and intent.source_event_id == event_id
                assert intent.state == "FILLED" and intent.reduce_only is True
                assert Decimal(intent.requested_qty) == close_quantity and Decimal(intent.filled_qty) == close_quantity
                assert intent.exchange_order_id == "offline-exit-order" and Decimal(intent.avg_px) == 100
                assert records[1].status == "EXECUTED" and records[1].action == expected_action
                assert records[1].binding_id == workflow.binding_id and records[1].order_id == intent.exchange_order_id
                assert Decimal(str(records[1].my_qty)) == close_quantity
                assert records[1].my_price == 100 and Decimal(str(records[1].my_notional)) == close_quantity * 10
                assert Decimal(str(owned.owned_qty)) == remaining_owned and Decimal(owned.owned_qty_exact) == remaining_owned
                assert Decimal(str(owned.entry_cost)) == remaining_owned * 100
                assert Decimal(owned.entry_cost_exact) == remaining_owned * 100
                assert Decimal(str(debt.pending_usd)) == remaining_owned * 10
                assert Decimal(debt.pending_usd_exact) == remaining_owned * 10
                assert (guard is not None) == (remaining_owned > 0)
                assert account.status == "connected" and account.last_error is None
                assert Decimal(str(workflow.position.pos)) == Decimal("10") - close_quantity
                assert Decimal(str(workflow.position.pos)) - remaining_owned - Decimal(other_owned.owned_qty_exact) == 1
                assert len(workflow.exchange_orders) == 1 and intent_id in workflow.exchange_orders
                adapter.place_order.assert_awaited_once()
                assert Decimal(adapter.place_order.call_args.kwargs["sz"]) == close_quantity
                assert adapter.place_order.call_args.kwargs["cl_ord_id"] == intent_id
                adapter.get_order.assert_awaited_once_with(workflow.spec.inst_id, cl_ord_id=intent_id)
                adapter.get_positions.assert_awaited_once()
                assert adapter.get_contract_balance.await_count == 2


@pytest.mark.asyncio
async def test_new_binding_baseline_does_not_consume_established_followers_event(monkeypatch):
    async with get_session_factory()() as db:
        lead, rows, _, calls, router = await make_workflow(db, monkeypatch, (True, False))
        await route(db, router, lead, [order(side="SELL")])
        assert [(c[0], c[1]) for c in calls] == [(rows[0][0].id, "SELL")]
        assert rows[1][0].baseline_seeded
        assert await db.get(BindingSourceProgress, (rows[1][0].id, order_dedup_key(order(side="SELL"))))


@pytest.mark.asyncio
async def test_empty_baseline_still_delivers_first_real_order(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, _, calls, router = await make_workflow(db, monkeypatch, (False,))
        await route(db, router, lead, [])
        await route(db, router, lead, [order()])
        assert len(calls) == 1 and calls[0][2] == Decimal("0.1")


@pytest.mark.asyncio
async def test_mapped_usdc_delivery_never_falls_back_to_legacy_usdt_balance(monkeypatch):
    async with get_session_factory()() as db:
        lead, rows, adapters, calls, router = await make_workflow(db, monkeypatch)
        binding, account = rows[0]
        spec = InstrumentSpec("BTC-USDC-SWAP", Decimal("0.01"), Decimal("1"),
            Decimal("1"), Decimal("0.1"), settle_ccy="USDC", base_ccy="BTC", quote_ccy="USDC")
        binding.symbol_map_json = json.dumps({"BTCUSDT": spec.inst_id})
        adapter = adapters[account.name]
        adapter.get_instruments.return_value = {spec.inst_id: spec}
        await db.commit()

        await route(db, router, lead, [order()])

        assert calls == []
        adapter.get_usdt_balance.assert_not_awaited()
        adapter.get_positions.assert_not_awaited()
        assert await db.scalar(select(OrderIntent)) is None
        assert (await db.scalar(select(BindingDelivery))).state == "PENDING"
        records = (await db.scalars(select(ExecutionRecord))).all()
        assert len(records) == 1
        assert records[0].status == "BLOCKED_COLLATERAL_BALANCE"
        assert records[0].inst_id == spec.inst_id


@pytest.mark.asyncio
async def test_failed_follower_delivery_survives_global_source_ack_and_retry(monkeypatch):
    async with get_session_factory()() as db:
        lead, rows, adapters, calls, router = await make_workflow(db, monkeypatch, (True, True))
        adapters["account-0"].get_instruments.side_effect = RuntimeError("temporarily offline")
        await route(db, router, lead, [order(side="SELL")])
        pending = (await db.scalars(select(BindingDelivery).where(BindingDelivery.binding_id == rows[0][0].id))).all()
        assert len(pending) == 1 and pending[0].state == "PENDING"
        assert [call[0] for call in calls] == [rows[1][0].id]
        adapters["account-0"].get_instruments.side_effect = None
        await route(db, router, lead, [order(side="SELL")])
        assert [call[0] for call in calls] == [rows[1][0].id, rows[0][0].id]
        assert pending[0].state == "APPLIED"


@pytest.mark.asyncio
async def test_cumulative_source_updates_deliver_only_incremental_fill(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, _, calls, router = await make_workflow(db, monkeypatch)
        await route(db, router, lead, [order()])
        await route(db, router, lead, [order(executedQty="0.300000000000000001", orderUpdateTime=2000)])
        await route(db, router, lead, [order(executedQty="0.300000000000000001", orderUpdateTime=2000)])
        assert [call[2] for call in calls] == [Decimal("0.1"), Decimal("0.200000000000000001")]


@pytest.mark.asyncio
async def test_deliveries_preserve_chronological_open_close_open_order(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, _, calls, router = await make_workflow(db, monkeypatch)
        await route(db, router, lead, [order(orderId="three", orderTime=3000),
            order(orderId="two", orderTime=2000, side="SELL"), order()])
        assert [call[1] for call in calls] == ["BUY", "SELL", "BUY"]


@pytest.mark.asyncio
async def test_legacy_float_hash_cursor_is_migrated_without_replay(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, _, calls, router = await make_workflow(db, monkeypatch)
        legacy_hash = hashlib.sha1(json.dumps([1000, "BTCUSDT", "BUY", "LONG", "MARKET", 0.1, 100.0], sort_keys=True).encode()).hexdigest()[:24]
        lead.seen_order_keys_json = json.dumps([legacy_hash])
        await db.commit()
        legacy = dict(h="raw-api-string-hash", t=1000, ot=1000, sym="BTCUSDT", side="BUY", ps="LONG", ty="MARKET", q=0.1, p=100.0, pnl=0)
        await route(db, router, lead, [legacy])
        assert calls == []


@pytest.mark.asyncio
async def test_discarded_order_id_migration_retains_cumulative_progress(monkeypatch):
    async with get_session_factory()() as db:
        lead, rows, _, calls, router = await make_workflow(db, monkeypatch)
        without_id = order(orderId=None)
        old_key = order_dedup_key(without_id)
        db.add(BindingSourceProgress(binding_id=rows[0][0].id, source_key=old_key,
            cumulative_qty="0.1", cumulative_notional="10"))
        await db.commit()
        adopted = slim_order(order(executedQty="0.3"))
        adopted["legacySourceKeys"] = [old_key]
        await route(db, router, lead, [adopted])
        assert [call[2] for call in calls] == [Decimal("0.2")]


@pytest.mark.asyncio
async def test_independent_monitor_latches_loss_breaker_and_keeps_disabled_protection(monkeypatch):
    async with get_session_factory()() as db:
        lead, rows, adapters, _, _ = await make_workflow(db, monkeypatch)
        binding, account = rows[0]
        binding.is_enabled = account.is_enabled = lead.is_enabled = False
        lead.status = "error"
        account.hwm_equity = account.day_start_equity = 200
        account.day_start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await db.commit()
        monitor = EngineOrchestrator()
        monitor.router.check_position_guards = AsyncMock()
        monitor.router.reconcile_orphan_positions = AsyncMock()
        monkeypatch.setattr("app.services.engine.orchestrator.create_exchange_adapter", lambda **kw: adapters[kw["credentials"]["api_key"]])
        await monitor.risk_tick()
        monitor.router.check_position_guards.assert_awaited_once()
        monitor.router.reconcile_orphan_positions.assert_not_awaited()
        await db.refresh(binding)
        assert binding.circuit_breaker_tripped
        assert "Drawdown" in binding.circuit_breaker_reason


@pytest.mark.asyncio
async def test_snapshot_publication_and_failure_never_overwrite_newer_success(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, _, _, _ = await make_workflow(db, monkeypatch)
        monitor = EngineOrchestrator()
        now = int(time.time() * 1000)
        fresh = snapshot()
        fresh["openPositionsMeta"]["asOf"] = now
        record = slim_order(order())
        assert await monitor._publish_lead_snapshot(db, lead, fresh, {record["h"]: record}, now - 20)
        older = snapshot()
        older["openPositionsMeta"]["asOf"] = now - 100
        assert not await monitor._publish_lead_snapshot(db, lead, older, {}, now - 200)
        assert not await monitor._record_lead_sync_failure(db, lead, now - 200, "late timeout")
        await db.refresh(lead)
        assert lead.status == "running"
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["asOf"] == now
        assert len(json.loads(lead.orders_store_json)) == 1


@pytest.mark.asyncio
async def test_newer_source_failure_invalidates_cached_authoritative_snapshot(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, _, _, _ = await make_workflow(db, monkeypatch)
        monitor = EngineOrchestrator()
        now = int(time.time() * 1000)
        fresh = snapshot()
        fresh["openPositionsMeta"]["asOf"] = now
        assert await monitor._publish_lead_snapshot(db, lead, fresh, {}, now - 20)
        assert await monitor._record_lead_sync_failure(db, lead, now + 1000, "source unavailable")
        stale = snapshot()
        stale["openPositionsMeta"]["asOf"] = now + 500
        assert not await monitor._publish_lead_snapshot(db, lead, stale, {}, now)
        await db.refresh(lead)
        assert lead.status == "error"
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["authoritative"] is False


@pytest.mark.asyncio
async def test_newer_source_failure_invalidates_older_inflight_router_snapshot(monkeypatch):
    async with get_session_factory()() as db:
        lead, _, adapters, calls, router = await make_workflow(db, monkeypatch)
        incoming = snapshot()
        persisted = snapshot()
        persisted["openPositionsMeta"].update(
            asOf=incoming["openPositionsMeta"]["asOf"], authoritative=False, complete=False,
            lastFailureStartedAtMs=incoming["openPositionsMeta"]["asOf"] + 1,
        )
        lead.snapshot_json = json.dumps(persisted)
        await db.commit()
        await router.process_lead_events(db, lead, [order()], [], incoming, [])
        assert calls == []
        adapters["account-0"].get_instruments.assert_not_awaited()
        assert await db.scalar(select(BindingDelivery)) is None
