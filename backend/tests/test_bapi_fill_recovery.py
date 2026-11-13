"""Regression coverage for ID-less BAPI fills, rebuilds and stale-feed health."""
import copy
import importlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.maintenance.rebuild_lead_store import (
    apply_bundle, fetch_closed_history, prepare, rollback, state_of,
)
from app.models.domain import LeadSource, NotificationOutbox
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.lead_collector.binance_bapi import (
    BinanceBapiCollector, check_source_fill_update, expand_slim_order,
    group_orders_into_batches, order_dedup_key, reconstruct_open_positions, slim_order,
)


def fill(**changes):
    row = dict(orderId=None, orderTime=1793906086088, orderUpdateTime=1793906086088,
               symbol="ETHUSDT", side="BUY", positionSide="SHORT", type="MARKET",
               executedQty="889.836", avgPrice="2255.9932")
    return {**row, **changes}


def collector_for(rows):
    collector = BinanceBapiCollector()
    collector.fetch_order_history = AsyncMock(return_value=rows)
    return collector


async def make_lead(db, **changes):
    lead = LeadSource(name="test-lead", platform="binance_bapi", portfolio_id="test-portfolio",
                      is_enabled=True, is_public_dashboard=True, **changes)
    db.add(lead)
    await db.commit()
    return lead


async def recovery_fixture(db, monkeypatch):
    dirty = dict(h="d81c8038119ab77a2027bb66", sym="ETHUSDT", side="BUY", ps="SHORT",
                 ty="MARKET", ot=1793906086088, t=1793906086088, q="1000.0", p="2255.67115", pnl=0)
    lead = await make_lead(db, orders_store_json=json.dumps({dirty["h"]: dirty}),
                           snapshot_json=json.dumps({"meta": {"generatedAt": 1793906086000},
                                                     "openPositions": [{"symbol": "ETHUSDT"}]}),
                           status="error", last_error="Regressing cumulative lead fill",
                           last_sync_at=datetime(2026, 11, 13, 6, 50, 43, tzinfo=timezone.utc))
    collector = collector_for([
        fill(side="SELL", executedQty="1889.836", orderTime=1793906080000),
        fill(), fill(executedQty="1000.0", avgPrice="2256.12826"),
        fill(symbol="SPCXUSDT", side="SELL", executedQty="1425.17", avgPrice="12.34",
             orderTime=1793906087000),
    ])
    collector.fetch_detail = AsyncMock(return_value={"positionShow": True, "marginBalance": 1000})
    collector.fetch_holdings = AsyncMock(side_effect=RuntimeError("test unavailable"))
    collector.fetch_position_history = AsyncMock(return_value={"list": [{
        "positionId": "closed-eth-test", "symbol": "ETHUSDT", "side": "SHORT",
        "opened": 1793906080000, "closed": 1793906086090,
    }], "total": 1})
    monkeypatch.setattr("app.maintenance.rebuild_lead_store.ProxyManager.ordered_proxy_urls",
                        lambda *args, **kwargs: [None])
    return lead, collector, dirty


@pytest.mark.asyncio
async def test_idless_same_millisecond_fills_are_both_accounted_without_regression():
    rows = [fill(side="SELL"), fill(side="SELL", executedQty="1000.0", avgPrice="2256.12826")]
    assert order_dedup_key(rows[0]) != order_dedup_key(rows[1])
    store = {}
    assert await collector_for(rows).sync_orders_into_store("test", store) == 2
    assert {record["fillKind"] for record in store.values()} == {"individual"}
    positions = reconstruct_open_positions(store)
    assert len(store) == 2 and len(positions) == 1
    assert positions[0]["qty"] == 1889.836
    batch = group_orders_into_batches(list(store.values()))[0]
    assert Decimal(batch["qtyExact"]) == Decimal("1889.836")
    assert batch["orderCount"] == 2


@pytest.mark.asyncio
async def test_repeated_page_sync_preserves_exact_store_and_accounting():
    rows = [fill(side="SELL"), fill(side="SELL", executedQty="1000.0", avgPrice="2256.12826")]
    collector, store = collector_for(rows), {}
    assert await collector.sync_orders_into_store("test", store) == 2
    first = copy.deepcopy(store)
    batches = group_orders_into_batches(list(store.values()))
    assert await collector.sync_orders_into_store("test", store) == 0
    assert store == first
    assert group_orders_into_batches(list(store.values())) == batches
    assert reconstruct_open_positions(store) == reconstruct_open_positions(first)


def test_idless_key_uses_canonical_quantity_price_and_ignores_unverified_fields():
    row = fill()
    same = fill(executedQty="889.8360", avgPrice="2255.993200", orderUpdateTime=999,
                id="unverified", positionId="unverified", orderKey="unverified")
    assert order_dedup_key(row) == order_dedup_key(same)
    assert order_dedup_key(row) != order_dedup_key(fill(avgPrice="2256.12826"))
    assert order_dedup_key(expand_slim_order(slim_order(row))) == order_dedup_key(row)


@pytest.mark.asyncio
async def test_real_order_id_cumulative_regression_still_fails_atomically():
    record = slim_order(fill(orderId="test-order", executedQty="1000"))
    store = {record["h"]: record}
    original = copy.deepcopy(store)
    assert order_dedup_key(fill(orderId="test-order")) == record["h"]
    with pytest.raises(RuntimeError, match="Regressing cumulative lead fill"):
        await collector_for([fill(orderId="test-order")]).sync_orders_into_store("test", store)
    assert store == original


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", [None, "test-order"])
@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-Infinity", "0", "-1"])
async def test_invalid_fill_quantity_fails_closed_without_partial_store(quantity, order_id):
    store = {}
    collector = collector_for([fill(side="SELL"), fill(orderId=order_id, executedQty=quantity)])
    with pytest.raises(ValueError):
        await collector.sync_orders_into_store("test", store)
    assert store == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "0", "-1"])
async def test_invalid_stored_quantity_fails_closed(quantity):
    record = {**slim_order(fill()), "q": quantity}
    store = {record["h"]: record}
    with pytest.raises(ValueError):
        await collector_for([]).sync_orders_into_store("test", store)
    with pytest.raises(ValueError):
        reconstruct_open_positions(store)


def test_conflicting_individual_identity_fails_closed():
    with pytest.raises(RuntimeError, match="Conflicting individual"):
        check_source_fill_update(slim_order(fill()), slim_order(fill(executedQty="1000")))


@pytest.mark.asyncio
async def test_rebuild_migration_is_idempotent_and_rollback_restores_dirty_state(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    async with get_session_factory()() as db:
        lead, collector, dirty = await recovery_fixture(db, monkeypatch)
        original = state_of(lead)
        expected_backup = original

        async def detail(*args, **kwargs):
            backups = list(tmp_path.glob("*-backup-*.json"))
            assert backups  # Durable backup exists before remote collection begins.
            saved = json.loads(max(backups).read_text())
            assert saved["before"] == expected_backup
            return {"positionShow": True, "marginBalance": 1000}

        collector.fetch_detail = AsyncMock(side_effect=detail)
        bundle = await prepare(db, lead.id, tmp_path, collector)
        await db.refresh(lead)
        assert state_of(lead) == original  # prepare is read-only
        backup = await apply_bundle(db, bundle, tmp_path)
        await db.refresh(lead)
        rebuilt = state_of(lead)
        store = json.loads(lead.orders_store_json)
        snap = json.loads(lead.snapshot_json)
        assert dirty["h"] not in store
        assert len(store) == 4
        assert {row["p"] for row in store.values() if row["side"] == "BUY"} == {"2255.9932", "2256.12826"}
        assert [(p["symbol"], p["positionSide"], p["qty"]) for p in snap["openPositions"]] == [
            ("SPCXUSDT", "SHORT", 1425.17)]
        assert snap["openPositionsMeta"]["authoritative"] is False
        assert set(store).issubset(json.loads(lead.seen_order_keys_json))  # rebuilt history cannot replay trades
        assert await apply_bundle(db, bundle, tmp_path) is None
        expected_backup = rebuilt
        second_bundle = await prepare(db, lead.id, tmp_path, collector)
        assert await apply_bundle(db, second_bundle, tmp_path) is None
        await db.refresh(lead)
        assert state_of(lead) == rebuilt
        assert backup is not None and str(backup) in caplog.text
        assert backup.stat().st_mode & 0o777 == 0o600
        await rollback(db, backup)
        await rollback(db, backup)
        await db.refresh(lead)
        assert state_of(lead) == original


@pytest.mark.asyncio
async def test_rebuild_failure_keeps_backup_and_database_unchanged(tmp_path, monkeypatch):
    async with get_session_factory()() as db:
        lead, collector, _ = await recovery_fixture(db, monkeypatch)
        original = state_of(lead)
        collector.fetch_order_history = AsyncMock(side_effect=RuntimeError("test page unavailable"))
        with pytest.raises(RuntimeError, match="no database writes"):
            await prepare(db, lead.id, tmp_path, collector)
        await db.refresh(lead)
        assert state_of(lead) == original
        assert len(list(tmp_path.glob("*-backup-*.json"))) == 1
        assert not list(tmp_path.glob("*-bundle-*.json"))


@pytest.mark.asyncio
async def test_rebuild_apply_and_rollback_refuse_newer_source_data(tmp_path, monkeypatch):
    async with get_session_factory()() as db:
        lead, collector, _ = await recovery_fixture(db, monkeypatch)
        bundle = await prepare(db, lead.id, tmp_path, collector)
        original = lead.orders_store_json
        lead.orders_store_json = "{}"
        await db.commit()
        with pytest.raises(RuntimeError, match="changed since prepare"):
            await apply_bundle(db, bundle, tmp_path)
        lead.orders_store_json = original
        await db.commit()
        backup = await apply_bundle(db, bundle, tmp_path)
        await db.refresh(lead)
        lead.snapshot_json = json.dumps({"newer": True})
        await db.commit()
        with pytest.raises(RuntimeError, match="overwrite newer data"):
            await rollback(db, backup)
        await db.refresh(lead)
        assert json.loads(lead.snapshot_json) == {"newer": True}


@pytest.mark.asyncio
async def test_rebuild_blocks_inflight_old_sync_publication(tmp_path, monkeypatch):
    async with get_session_factory()() as db:
        lead, collector, _ = await recovery_fixture(db, monkeypatch)
        before_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 1000
        bundle = await prepare(db, lead.id, tmp_path, collector)
        await apply_bundle(db, bundle, tmp_path)
        await db.refresh(lead)
        monitor = EngineOrchestrator()
        assert not await monitor._publish_lead_snapshot(db, lead, {}, {}, before_ms)
        assert not await monitor._record_lead_sync_failure(db, lead, before_ms, "test old failure")


@pytest.mark.asyncio
async def test_truncated_closed_history_fails_closed():
    collector = BinanceBapiCollector()
    collector.fetch_position_history = AsyncMock(return_value={"list": [], "total": 1})
    with pytest.raises(RuntimeError, match="Truncated"):
        await fetch_closed_history(collector, "test", None)


@pytest.mark.asyncio
async def test_full_sweep_cannot_accept_duplicate_pages_as_complete(monkeypatch):
    rows = [fill(orderId=f"test-{i}", orderTime=1000 + i) for i in range(100)]
    store = {}
    collector = collector_for(rows)
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.asyncio.sleep", AsyncMock())
    with pytest.raises(RuntimeError, match="truncated"):
        await collector.sync_orders_into_store("test", store, full_sweep=True, history_max_pages=3,
                                              require_complete=True)
    assert store == {}


@pytest.mark.asyncio
async def test_stale_failure_notifies_once_and_preserves_original_generated_at(async_client, caplog):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    async with get_session_factory()() as db:
        generated = int((datetime.now(timezone.utc) - timedelta(hours=9)).timestamp() * 1000)
        lead = await make_lead(db, snapshot_json=json.dumps({"meta": {"generatedAt": generated},
                                                            "openPositions": []}), status="running")
        monitor = EngineOrchestrator()
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        for i in range(4):
            assert await monitor._record_lead_sync_failure(db, lead, now + i, "test unavailable")
        notices = list((await db.scalars(select(NotificationOutbox))).all())
        assert len(notices) == 1
        assert json.loads(notices[0].payload)["event_type"] == "error"
        assert "consecutiveFailures=3" in caplog.text
        failure_logs = [record for record in caplog.records if "sync failed" in record.message]
        assert [record.levelno for record in failure_logs] == [logging.ERROR] + [logging.DEBUG] * 3
        result = (await async_client.get("/api/dashboard/public")).json()["leads"][0]
        assert result["stale"] is True and result["generatedAt"] == generated
        assert result["snapshot"]["meta"]["generatedAt"] == generated
        assert result["consecutiveSyncFailures"] == 4
        assert result["snapshotAgeSec"] >= 9 * 3600


@pytest.mark.asyncio
async def test_consecutive_failures_have_error_log_outbox_and_degraded_readiness(async_client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main.settings, "enable_background_engine", True)
    monkeypatch.setattr(main, "orchestrator", SimpleNamespace(heartbeat_age_sec=lambda: 0.1, is_running=True))
    async with get_session_factory()() as db:
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        lead = await make_lead(db, snapshot_json=json.dumps({"meta": {"generatedAt": now}}), status="running")
        monitor = EngineOrchestrator()
        for i in range(get_settings().lead_sync_failure_limit):
            assert await monitor._record_lead_sync_failure(db, lead, now + i, "test failure")
        assert "consecutiveFailures=3" in caplog.text
        failure_logs = [record for record in caplog.records if "sync failed" in record.message]
        assert [record.levelno for record in failure_logs] == [logging.ERROR] + [logging.DEBUG] * (
            get_settings().lead_sync_failure_limit - 1
        )
        assert len(list((await db.scalars(select(NotificationOutbox))).all())) == 1
        response = await async_client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["leadSync"] == {"ok": False, "degradedLeadIds": [lead.id]}


@pytest.mark.asyncio
async def test_recovery_resets_failure_incident_and_rearms_notification(monkeypatch):
    async with get_session_factory()() as db:
        lead = await make_lead(db, status="running")
        monitor = EngineOrchestrator()
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        assert await monitor._record_lead_sync_failure(db, lead, now, "test failure")
        first_notice = await db.scalar(select(NotificationOutbox))
        # Recovery starts after the failure notification has been persisted.
        now = int(first_notice.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        snap = {"meta": {"generatedAt": now + 1}, "openPositions": [], "openPositionsMeta": {
            "asOf": now + 1, "authoritative": True, "complete": True, "positionShow": True, "source": "api"}}
        assert await monitor._publish_lead_snapshot(db, lead, snap, {}, now + 1)
        assert not (json.loads(lead.snapshot_json)["openPositionsMeta"]).get("consecutiveSyncFailures")
        next_started = now + 2
        monkeypatch.setattr("app.services.engine.orchestrator.time.time", lambda: next_started / 1000)
        assert await monitor._record_lead_sync_failure(db, lead, next_started, "test next incident")
        assert len(list((await db.scalars(select(NotificationOutbox))).all())) == 1
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["consecutiveSyncFailures"] == 1
        for i in range(1, get_settings().lead_sync_failure_limit):
            assert await monitor._record_lead_sync_failure(db, lead, next_started + i, "test next incident")
        assert len(list((await db.scalars(select(NotificationOutbox))).all())) == 2


@pytest.mark.asyncio
async def test_restored_order_id_retains_legacy_progress_alias_on_repeated_sync():
    collector = collector_for([fill(orderId="test-restored", executedQty="1000")])
    record = slim_order(fill())
    store = {record["h"]: record}
    await collector.sync_orders_into_store("test", store)
    first = copy.deepcopy(store)
    assert record["h"] in next(iter(store.values()))["legacySourceKeys"]
    await collector.sync_orders_into_store("test", store)
    assert store == first


@pytest.mark.asyncio
async def test_apply_accepts_only_failure_metadata_changes_since_prepare(tmp_path, monkeypatch):
    async with get_session_factory()() as db:
        lead, collector, _ = await recovery_fixture(db, monkeypatch)
        bundle = await prepare(db, lead.id, tmp_path, collector)
        monitor = EngineOrchestrator()
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        assert await monitor._record_lead_sync_failure(db, lead, now, "test failure")
        backup = await apply_bundle(db, bundle, tmp_path)
        assert backup is not None
        assert json.loads(backup.read_text())["before"]["status"] == "error"


@pytest.mark.asyncio
async def test_duplicate_failure_attempt_does_not_increment_count_or_notify_twice():
    async with get_session_factory()() as db:
        lead = await make_lead(db, status="running")
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        monitor = EngineOrchestrator()
        assert await monitor._record_lead_sync_failure(db, lead, now, "test failure")
        assert not await monitor._record_lead_sync_failure(db, lead, now, "test duplicate")
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["consecutiveSyncFailures"] == 1
        assert len(list((await db.scalars(select(NotificationOutbox))).all())) == 1


@pytest.mark.asyncio
async def test_explicit_rollback_of_newer_data_backs_it_up_before_restore(tmp_path, monkeypatch):
    async with get_session_factory()() as db:
        lead, collector, _ = await recovery_fixture(db, monkeypatch)
        original = state_of(lead)
        bundle = await prepare(db, lead.id, tmp_path, collector)
        backup = await apply_bundle(db, bundle, tmp_path)
        await db.refresh(lead)
        lead.snapshot_json = json.dumps({"newer": True})
        await db.commit()
        newer = state_of(lead)
        await rollback(db, backup, tmp_path, allow_newer=True)
        await db.refresh(lead)
        assert state_of(lead) == original
        rescue = list(tmp_path.glob("*-rollback-rescue-*.json"))
        assert len(rescue) == 1
        assert json.loads(rescue[0].read_text())["before"] == newer
