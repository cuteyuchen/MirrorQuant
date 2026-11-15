"""Adversarial regressions for bounded clocks and direct source admission."""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import BindingDelivery, BindingPositionProgress, LeadSource, OrderIntent
from app.services.engine.router import CopyRouter, UnknownOrderOutcome
from app.services.lead_collector.snapshot_clock import snapshot_source_bound_ms, source_batch_interval
from test_auto_follow_hidden_positions import (
    NOW_MS as HIDDEN_NOW_MS, fixed_clock, hidden_snapshot, opening_order, workflow,
)
from test_recovery_business_evidence import evidence_snapshot
from test_router_remediations import NOW_MS, OfflineExchange, execute_batch, seed_allocation, trading


def batch_at(timestamp):
    return {"startTime": timestamp, "endTime": timestamp}


@pytest.mark.parametrize("timestamp", [HIDDEN_NOW_MS - 1000, HIDDEN_NOW_MS + 1, 10 ** 100])
def test_source_batch_without_observation_has_no_unbounded_admission(fixed_clock, timestamp):
    assert source_batch_interval(batch_at(timestamp), None) is None


def test_collection_start_bounds_ordering_without_inventing_holdings_observation(fixed_clock):
    current = hidden_snapshot()
    current["openPositionsMeta"]["asOf"] = None
    current["meta"] = {"collectionStartedAtMs": HIDDEN_NOW_MS - 500, "detailOk": True}
    assert snapshot_source_bound_ms(current) == HIDDEN_NOW_MS - 500


@pytest.mark.parametrize("timestamp", [HIDDEN_NOW_MS - 1000, HIDDEN_NOW_MS - 100, HIDDEN_NOW_MS + 1])
async def test_collection_only_ordering_is_bounded_and_restart_safe(workflow, timestamp):
    current = hidden_snapshot()
    current["openPositionsMeta"]["asOf"] = None
    current["meta"] = {"collectionStartedAtMs": HIDDEN_NOW_MS - 500, "detailOk": True}
    source = opening_order()
    source["orderTime"] = source["orderUpdateTime"] = timestamp
    workflow.lead.snapshot_json = json.dumps(current)
    await workflow.db.commit()
    for router in (workflow.router, CopyRouter(CredentialVault())):
        await router.process_lead_events(workflow.db, workflow.lead, [source], [], current, [workflow.proxy])
    if timestamp <= HIDDEN_NOW_MS - 500:
        workflow.adapter.place_order.assert_awaited_once()
    else:
        workflow.adapter.place_order.assert_not_awaited()
        state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, "BTCUSDT", "LONG"))
        assert state.review_reason and state.source_time_ms == 0
        assert await workflow.db.scalar(select(OrderIntent)) is None


async def test_payload_observation_cannot_precede_its_source(fixed_clock):
    payload = batch_at(HIDDEN_NOW_MS - 100)
    payload["_observed_at_ms"] = HIDDEN_NOW_MS - 200
    assert source_batch_interval(payload, HIDDEN_NOW_MS) is None


async def test_fresh_payload_clock_cannot_replace_missing_saved_holdings(trading):
    owned, other, debt, guard = await seed_allocation(trading)
    exchange = OfflineExchange()
    await execute_batch(trading, exchange, batch_fields={"_observed_at_ms": NOW_MS})
    assert exchange.orders == [] and owned.owned_qty == 1 and other.owned_qty == 9
    assert debt.pending_usd == 25
    assert await trading.db.scalar(select(OrderIntent)) is None


@pytest.mark.parametrize("kind", ["contradictory_leg", "live_lead"])
async def test_direct_closed_history_cannot_ignore_leg_or_live_inventory(trading, kind):
    owned, other, debt, guard = await seed_allocation(trading)
    trading.lead.snapshot_json = json.dumps(evidence_snapshot(1 if kind == "live_lead" else 0))
    await trading.db.commit()
    closed = {"id": "direct-identity", "symbol": "BTCUSDT", "side": "LONG", "opened": 50, "closed": 200}
    if kind == "contradictory_leg":
        closed["positionSide"] = "SHORT"
    exchange = OfflineExchange()
    assert not await trading.router._reconcile_closed_position(
        trading.db, trading.binding, trading.lead, trading.account, exchange, closed,
        [trading.pos], {"BTCUSDT": trading.spec}, {})
    assert exchange.orders == [] and owned.owned_qty == 1 and other.owned_qty == 9
    assert debt.pending_usd == 25


@pytest.mark.parametrize("kind", ["newer_empty", "newer_live", "incomparable", "expired"])
async def test_direct_snapshot_is_pinned_across_durable_boundary(trading, monkeypatch, kind):
    owned, other, debt, guard = await seed_allocation(trading)
    current = evidence_snapshot()
    current["meta"]["generatedAt"] = NOW_MS - 1
    current["openPositionsMeta"]["asOf"] = NOW_MS - 1
    trading.lead.snapshot_json = json.dumps(current)
    await trading.db.commit()
    original_commit = trading.db.commit
    changed = False

    async def mutate_after_intent():
        nonlocal changed
        submitting = any(isinstance(row, OrderIntent) and row.state == "SUBMITTING" for row in trading.db.new)
        await original_commit()
        if submitting and not changed:
            changed = True
            if kind == "expired":
                monkeypatch.setattr("app.services.engine.router.time.time", lambda: (NOW_MS + 30_001) / 1000)
            else:
                saved = evidence_snapshot(1 if kind == "newer_live" else 0)
                if kind == "incomparable":
                    saved["openPositionsMeta"]["asOf"] = None
                async with get_session_factory()() as concurrent:
                    lead = await concurrent.get(LeadSource, trading.lead.id)
                    lead.snapshot_json = json.dumps(saved)
                    await concurrent.commit()

    trading.db.commit = mutate_after_intent
    exchange = OfflineExchange()
    closed = {"id": "direct-boundary", "symbol": "BTCUSDT", "side": "LONG", "opened": 50, "closed": 200}
    try:
        result = await trading.router._reconcile_closed_position(
            trading.db, trading.binding, trading.lead, trading.account, exchange, closed,
            [trading.pos], {"BTCUSDT": trading.spec}, {})
        assert not result
    except UnknownOrderOutcome:
        pass
    assert changed and exchange.orders == []
    assert owned.owned_qty == 1 and other.owned_qty == 9 and debt.pending_usd == 25
    intent = await trading.db.scalar(select(OrderIntent))
    assert intent.state == "REJECTED" and Decimal(intent.filled_qty) == 0


@pytest.mark.parametrize("defect", ["future_source", "future_sequence"])
async def test_applied_import_cannot_seed_unbounded_position_watermark(trading, defect):
    source_time = NOW_MS + 1 if defect == "future_source" else NOW_MS - 1000
    payload = {"symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "qty": "1",
               "startTime": source_time, "endTime": source_time, "_imported_consumed_source": True}
    delivery = BindingDelivery(binding_id=trading.binding.id, source_event_id="applied-poison",
        source_sequence=NOW_MS + 1, payload_json=json.dumps(payload), state="APPLIED")
    trading.db.add(delivery)
    await trading.db.commit()
    states = await trading.router._position_ordering(
        trading.db, trading.binding, [delivery], evidence_snapshot(), {})
    state = states[("BTCUSDT", "LONG")]
    assert state.review_reason and state.source_time_ms == 0


@pytest.mark.parametrize("kind", ["unknown_order", "invalid_side"])
async def test_valid_snapshot_never_overrides_order_or_exit_identity_uncertainty(trading, kind):
    owned, other, debt, guard = await seed_allocation(trading)
    trading.lead.snapshot_json = json.dumps(evidence_snapshot())
    if kind == "unknown_order":
        trading.db.add(OrderIntent(id="prior-unknown", binding_id=trading.other.id,
            account_id=trading.account.id, inst_id="BTCUSDT", pos_side="long", source_event_id="unknown",
            state="UNKNOWN", requested_qty="1", filled_qty="0", avg_px="0", reduce_only=True))
    await trading.db.commit()
    exchange = OfflineExchange()
    await execute_batch(trading, exchange, side="INVALID" if kind == "invalid_side" else "SELL",
                        batch_fields={"startTime": 200, "endTime": 200})
    assert exchange.orders == [] and owned.owned_qty == 1 and other.owned_qty == 9
    assert debt.pending_usd == 25
