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
from app.models.domain import BindingDelivery, BindingSourceProgress, CopyBinding, LeadSource, TradingAccount
from app.services.adapters.base import BalanceInfo
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
