"""Keep receipt, source ordering and holdings observations distinct offline."""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDelivery, BindingPositionProgress, ExecutionRecord, LeadSource, OrderIntent, PositionAllocation,
    PositionGuardState,
)
from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import is_routable_snapshot
from app.services.lead_collector.snapshot_clock import (
    snapshot_observed_ms, snapshot_source_bound_ms, source_batch_interval,
)
from test_auto_follow_hidden_positions import (
    NOW_MS, fixed_clock, hidden_snapshot, opening_order, workflow,
)
from test_router_remediations import (
    NOW_MS as BUSINESS_NOW_MS, OfflineExchange, execute_batch, seed_allocation, trading,
)


def collection_only_snapshot(started=NOW_MS - 500):
    current = hidden_snapshot()
    current['openPositionsMeta']['asOf'] = None
    current['meta'] = {'collectionStartedAtMs': started, 'detailOk': True}
    return current


@pytest.mark.parametrize('receipt', [None, NOW_MS - 500, NOW_MS - 100, NOW_MS])
def test_receipt_never_advances_collection_only_source_bound(fixed_clock, receipt):
    current = collection_only_snapshot()
    assert snapshot_source_bound_ms(current, receipt) == NOW_MS - 500
    assert snapshot_observed_ms(current) is None
    assert source_batch_interval({'startTime': NOW_MS - 500, 'endTime': NOW_MS - 500},
                                 snapshot_source_bound_ms(current, receipt)) == (NOW_MS - 500, NOW_MS - 500)
    assert source_batch_interval({'startTime': NOW_MS - 500, 'endTime': NOW_MS - 499},
                                 snapshot_source_bound_ms(current, receipt)) is None


@pytest.mark.parametrize('receipt', [True, 'bad', 0, NOW_MS - 501, NOW_MS + 1, float('nan')])
def test_invalid_or_contradictory_receipt_fails_closed(fixed_clock, receipt):
    assert snapshot_source_bound_ms(collection_only_snapshot(), receipt) is None


@pytest.mark.parametrize('observation', ['asOf', 'generatedAt', 'generatedAt_missing_asof', 'both'])
def test_real_observation_can_bound_events_after_collection_start(fixed_clock, observation):
    current = collection_only_snapshot()
    if observation in ('asOf', 'both'):
        current['openPositionsMeta']['asOf'] = NOW_MS - 100
    if observation in ('generatedAt', 'generatedAt_missing_asof', 'both'):
        current['meta']['generatedAt'] = NOW_MS - 50
    if observation == 'generatedAt_missing_asof':
        current['openPositionsMeta'].pop('asOf')
    expected = NOW_MS - (50 if observation.startswith('generatedAt') else 100)
    assert snapshot_source_bound_ms(current, NOW_MS) == expected
    assert snapshot_observed_ms(current) == (None if observation == 'generatedAt' else expected)
    assert source_batch_interval({'startTime': NOW_MS - 200, 'endTime': expected}, expected)
    assert snapshot_source_bound_ms(current, NOW_MS - 200) is None


@pytest.mark.parametrize('defect', ['missing', 'stale', 'future', 'bad_asof', 'bad_generation', 'reversed'])
def test_receipt_cannot_rescue_invalid_original_clocks(fixed_clock, defect):
    current = collection_only_snapshot()
    if defect == 'missing':
        current['meta'].pop('collectionStartedAtMs')
    elif defect == 'stale':
        current['meta']['collectionStartedAtMs'] = NOW_MS - 30_001
    elif defect == 'future':
        current['meta']['collectionStartedAtMs'] = NOW_MS + 1
    elif defect == 'bad_asof':
        current['openPositionsMeta']['asOf'] = True
    elif defect == 'bad_generation':
        current['meta']['generatedAt'] = 'bad'
    else:
        current['meta']['generatedAt'] = NOW_MS - 501
    assert snapshot_source_bound_ms(current, NOW_MS) is None


@pytest.mark.parametrize('defect', ['alias', 'reversed', 'item_after_bound', 'fake_observation'])
def test_collection_bound_does_not_rescue_contradictory_event_evidence(fixed_clock, defect):
    payload = {'startTime': NOW_MS - 1000, 'endTime': NOW_MS - 500}
    if defect == 'alias':
        payload['_source_time_ms'] = NOW_MS - 600
    elif defect == 'reversed':
        payload['startTime'] = NOW_MS - 499
    elif defect == 'item_after_bound':
        payload['items'] = [{'orderTime': NOW_MS - 1000, 'time': NOW_MS - 499}]
    else:
        payload['_observed_at_ms'] = NOW_MS
    assert source_batch_interval(payload, snapshot_source_bound_ms(collection_only_snapshot(), NOW_MS)) is None


@pytest.mark.parametrize('offset', [-1000, -500, -499])
async def test_collection_only_restart_pins_original_source_timestamp(workflow, offset):
    current = collection_only_snapshot()
    source = opening_order()
    source['orderTime'] = source['orderUpdateTime'] = NOW_MS + offset
    workflow.lead.snapshot_json = json.dumps(current)
    await workflow.db.commit()
    batches = []
    original_execute = CopyRouter._execute_batch_for_binding

    async def capture_batch(router, *args, **kwargs):
        batches.append(dict(args[5]))
        return await original_execute(router, *args, **kwargs)

    routers = [workflow.router, CopyRouter(CredentialVault())]
    for router in routers:
        router._execute_batch_for_binding = capture_batch.__get__(router, CopyRouter)
        async with get_session_factory()() as restarted:
            lead = await restarted.get(LeadSource, workflow.lead.id)
            await router.process_lead_events(restarted, lead, [source], [], current, [workflow.proxy])
    async with get_session_factory()() as restarted:
        state = await restarted.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
        intents = (await restarted.scalars(select(OrderIntent))).all()
        delivery = await restarted.scalar(select(BindingDelivery))
        saved_payload = json.loads(delivery.payload_json)
        assert saved_payload['_source_time_ms'] == saved_payload['_source_start_ms'] == NOW_MS + offset
        assert '_observed_at_ms' not in saved_payload
        if offset <= -500:
            workflow.adapter.place_order.assert_awaited_once()
            assert len(intents) == 1 and intents[0].state == 'FILLED'
            assert intents[0].source_time_ms == state.source_time_ms == NOW_MS + offset
            allocation = await restarted.scalar(select(PositionAllocation))
            assert allocation.opened_at_ms == NOW_MS + offset
            assert not state.review_reason
            assert len(batches) == 1 and '_observed_at_ms' not in batches[0]
            assert batches[0]['_freshness_at_ms'] == NOW_MS - 500
        else:
            workflow.adapter.place_order.assert_not_awaited()
            assert state.review_reason and state.source_time_ms == 0
            assert intents == [] and batches == []
            workflow.adapter.ensure_leverage.assert_not_awaited()


@pytest.mark.parametrize('kind', ['expired', 'superseded', 'missing', 'contradictory'])
async def test_collection_only_revalidated_after_intent_commit_and_restart(workflow, monkeypatch, kind):
    current = collection_only_snapshot()
    workflow.lead.snapshot_json = json.dumps(current)
    await workflow.db.commit()
    original_commit = workflow.db.commit
    changed = False

    async def mutate_after_intent():
        nonlocal changed
        submitting = any(isinstance(row, OrderIntent) and row.state == 'SUBMITTING' for row in workflow.db.new)
        await original_commit()
        if submitting and not changed:
            changed = True
            if kind == 'expired':
                monkeypatch.setattr('app.services.engine.router.time.time', lambda: (NOW_MS + 30_001) / 1000)
            else:
                saved = collection_only_snapshot(NOW_MS - 499)
                if kind == 'missing':
                    saved = {}
                elif kind == 'contradictory':
                    saved['meta']['generatedAt'] = NOW_MS - 500
                async with get_session_factory()() as concurrent:
                    lead = await concurrent.get(LeadSource, workflow.lead.id)
                    lead.snapshot_json = json.dumps(saved)
                    await concurrent.commit()

    workflow.db.commit = mutate_after_intent
    await workflow.router.process_lead_events(
        workflow.db, workflow.lead, [opening_order()], [], current, [workflow.proxy])
    assert changed
    workflow.adapter.place_order.assert_not_awaited()
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        await CopyRouter(CredentialVault()).process_lead_events(
            restarted, lead, [opening_order()], [], current, [workflow.proxy])
        intent = await restarted.scalar(select(OrderIntent))
        assert intent.state == 'REJECTED' and Decimal(intent.filled_qty) == 0
        assert not (await restarted.scalars(select(PositionAllocation).where(
            PositionAllocation.owned_qty > 0))).all()
    workflow.adapter.place_order.assert_not_awaited()


async def test_bound_source_cannot_refresh_collection_age_during_quote(workflow, monkeypatch):
    current = collection_only_snapshot(NOW_MS - 29_000)
    source = opening_order()
    source['orderTime'] = source['orderUpdateTime'] = NOW_MS - 30_000
    workflow.lead.snapshot_json = json.dumps(current)
    await workflow.db.commit()
    book = workflow.adapter.get_orderbook.return_value

    async def delay_quote(*args, **kwargs):
        monkeypatch.setattr('app.services.engine.router.time.time', lambda: (NOW_MS + 2000) / 1000)
        return book

    workflow.adapter.get_orderbook.side_effect = delay_quote
    await workflow.router.process_lead_events(
        workflow.db, workflow.lead, [source], [], current, [workflow.proxy])
    workflow.adapter.get_orderbook.assert_awaited_once()
    workflow.adapter.place_order.assert_not_awaited()
    assert await workflow.db.scalar(select(OrderIntent)) is None
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        await CopyRouter(CredentialVault()).process_lead_events(
            restarted, lead, [source], [], current, [workflow.proxy])
        assert await restarted.scalar(select(OrderIntent)) is None
    workflow.adapter.place_order.assert_not_awaited()


@pytest.mark.parametrize('quote', ['valid', 'unavailable'])
async def test_receipt_bounded_review_quote_never_authorizes_a_trade(workflow, quote):
    current = collection_only_snapshot()
    source = opening_order()
    source['orderTime'] = source['orderUpdateTime'] = NOW_MS - 100
    workflow.lead.snapshot_json = json.dumps(current)
    await workflow.db.commit()
    if quote == 'unavailable':
        workflow.adapter.get_orderbook.side_effect = RuntimeError('offline quote unavailable')
    await workflow.router.process_lead_events(
        workflow.db, workflow.lead, [source], [], current, [workflow.proxy])
    workflow.adapter.get_orderbook.assert_awaited_once()
    workflow.adapter.place_order.assert_not_awaited()
    workflow.adapter.ensure_leverage.assert_not_awaited()
    assert await workflow.db.scalar(select(OrderIntent)) is None
    assert await workflow.db.scalar(select(PositionAllocation)) is None
    record = await workflow.db.scalar(select(ExecutionRecord))
    assert record.status == 'BLOCKED_SOURCE_ORDERING'
    assert record.my_price == (100 if quote == 'valid' else 0)
    assert record.my_qty == 0
    async with get_session_factory()() as restarted:
        state = await restarted.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
        assert state.review_reason and state.source_time_ms == 0


async def test_collection_only_closed_cycle_cannot_authorize_opening_on_replay(workflow):
    current = collection_only_snapshot()
    source = opening_order()
    closed = {'id': 'unobserved-cycle', 'symbol': 'BTCUSDT', 'side': 'LONG',
              'opened': NOW_MS - 2000, 'closed': NOW_MS - 800}
    workflow.lead.snapshot_json = json.dumps(current)
    await workflow.db.commit()
    for router in (workflow.router, CopyRouter(CredentialVault())):
        async with get_session_factory()() as restarted:
            lead = await restarted.get(LeadSource, workflow.lead.id)
            await router.process_lead_events(restarted, lead, [source], [closed], current, [workflow.proxy])
            state = await restarted.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
            assert state.review_reason and state.source_time_ms == 0
            assert await restarted.scalar(select(OrderIntent)) is None
            assert await restarted.scalar(select(PositionAllocation)) is None
    workflow.adapter.place_order.assert_not_awaited()
    workflow.adapter.ensure_leverage.assert_not_awaited()


@pytest.mark.parametrize('direct', [False, True])
async def test_collection_start_is_not_exit_holdings_evidence_after_restart(trading, direct):
    owned, other, debt, guard = await seed_allocation(trading)
    current = collection_only_snapshot(BUSINESS_NOW_MS - 500)
    current['openPositions'] = []
    assert is_routable_snapshot(current)
    assert snapshot_source_bound_ms(current) == BUSINESS_NOW_MS - 500
    assert snapshot_observed_ms(current) is None
    trading.lead.snapshot_json = json.dumps(current)
    await trading.db.commit()
    exchange = OfflineExchange()
    for router in (trading.router, CopyRouter(CredentialVault())):
        trading.router = router
        if direct:
            assert not await router._reconcile_closed_position(
                trading.db, trading.binding, trading.lead, trading.account, exchange,
                {'id': 'collection-is-not-holdings', 'symbol': 'BTCUSDT', 'side': 'LONG',
                 'opened': 50, 'closed': 200}, [trading.pos], {'BTCUSDT': trading.spec}, {})
        else:
            await execute_batch(trading, exchange, batch_fields={'startTime': 200, 'endTime': 200})
    async with get_session_factory()() as restarted:
        assert (await restarted.get(PositionAllocation, owned.id)).owned_qty == 1
        assert (await restarted.get(PositionAllocation, other.id)).owned_qty == 9
        assert await restarted.get(PositionGuardState, guard.id) is not None
        assert await restarted.scalar(select(OrderIntent)) is None
    assert exchange.orders == [] and debt.pending_usd == 25
