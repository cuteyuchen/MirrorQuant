"""Independent audit regressions; offline exchanges and notification evidence only."""
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.domain import ExecutionRecord, NotificationOutbox, OrderIntent, BindingDelivery, TradingAccount, LeadSource
from app.core.database import get_session_factory
from app.services.lead_collector.binance_bapi import is_routable_snapshot, is_authoritative_snapshot
from app.services.engine.router import CopyRouter
from app.core.security import CredentialVault
from test_auto_follow_hidden_positions import workflow, fixed_clock, hidden_snapshot, opening_order, NOW_MS
from test_follow_recovery_audit import trading, source_delivery
from test_router_remediations import OfflineExchange


async def test_collection_start_superseded_must_not_route(workflow):
    incoming = hidden_snapshot()
    incoming['openPositionsMeta']['asOf'] = None
    incoming['meta'].pop('generatedAt')
    incoming['meta']['collectionStartedAtMs'] = NOW_MS
    latest = json.loads(json.dumps(incoming))
    latest['meta']['collectionStartedAtMs'] = NOW_MS + 1
    workflow.lead.snapshot_json = json.dumps(latest)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], incoming, [workflow.proxy])
    workflow.adapter.place_order.assert_not_awaited()


async def test_collection_start_cannot_refresh_at_submission(workflow, monkeypatch):
    snapshot = hidden_snapshot()
    snapshot['openPositionsMeta']['asOf'] = None
    snapshot['meta'].pop('generatedAt')
    snapshot['meta']['collectionStartedAtMs'] = NOW_MS - 29_000
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    async def delayed(*args, **kwargs):
        from app.services.adapters.base import OrderBook
        monkeypatch.setattr('app.services.engine.router.time.time', lambda: (NOW_MS + 2_000) / 1000)
        return OrderBook([[99.9, 100]], [[100, 100]])
    workflow.adapter.get_orderbook.side_effect = delayed
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], snapshot, [workflow.proxy])
    workflow.adapter.get_orderbook.assert_awaited_once()
    workflow.adapter.place_order.assert_not_awaited()


async def test_existing_legacy_execution_missing_outbox_requires_human_review(trading):
    delivery = await source_delivery(trading)
    delivery.state = 'APPLIED'
    trading.db.add(OrderIntent(id='legacy-existing-report', binding_id=trading.binding.id, account_id=trading.account.id,
        inst_id='BTCUSDT', pos_side='long', source_event_id='recovery', state='FILLED',
        requested_qty='1', filled_qty='1', avg_px='100', reduce_only=False, exchange_order_id='old-venue-order'))
    trading.db.add(ExecutionRecord(binding_id=trading.binding.id, lead_id=trading.lead.id, lead_name='test',
        account_id=trading.account.id, account_name='offline', inst_id='BTCUSDT', symbol='BTCUSDT',
        position_side='LONG', side='BUY', action='OPEN_LONG', status='EXECUTED',
        my_qty=1, my_price=100, order_id='old-venue-order', details='legacy'))
    await trading.db.commit()
    exchange = OfflineExchange()
    for router in (trading.router, CopyRouter(CredentialVault())):
        assert not await router.reconcile_pending_orders(trading.db, trading.account, exchange, {'BTCUSDT': trading.spec})
    records = (await trading.db.scalars(select(ExecutionRecord))).all()
    assert len(records) == 1
    assert 'notification delivery unknown' in records[0].details.lower()
    assert len((await trading.db.scalars(select(NotificationOutbox))).all()) == 0
    assert exchange.orders == []


@pytest.mark.parametrize('field', ['asOf', 'generatedAt', 'collectionStartedAtMs'])
@pytest.mark.parametrize('value', [None, 0, -1, True, 'bad', '1800000000000', float('nan'), float('inf'), NOW_MS + 1, NOW_MS - 30_001])
async def test_unsafe_timestamp_variants_never_place(workflow, field, value):
    snapshot = hidden_snapshot()
    snapshot['openPositionsMeta']['asOf'] = None
    snapshot['meta'].pop('generatedAt')
    metadata = snapshot['openPositionsMeta'] if field == 'asOf' else snapshot['meta']
    metadata[field] = value
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    assert not is_routable_snapshot(snapshot)
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_not_awaited()
    assert not (await workflow.db.scalars(select(OrderIntent))).all()


@pytest.mark.parametrize('field', ['asOf', 'generatedAt', 'collectionStartedAtMs'])
@pytest.mark.parametrize('age', [0, 30_000])
async def test_supported_legacy_timestamp_exact_boundaries(workflow, field, age):
    snapshot = hidden_snapshot()
    snapshot['openPositionsMeta']['asOf'] = None
    snapshot['meta'].pop('generatedAt')
    metadata = snapshot['openPositionsMeta'] if field == 'asOf' else snapshot['meta']
    metadata[field] = NOW_MS - age
    source = opening_order()
    source['orderTime'] = source['orderUpdateTime'] = NOW_MS - 31_000
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [source], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_awaited_once()


async def test_collection_start_must_be_fresh_even_when_generation_and_holdings_are_fresh(workflow):
    snapshot = hidden_snapshot()
    snapshot['meta']['collectionStartedAtMs'] = NOW_MS - 30_001
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    assert not is_routable_snapshot(snapshot)
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_not_awaited()


@pytest.mark.parametrize('newer_source', ['generatedAt', 'collectionStartedAtMs'])
async def test_incomparable_generations_are_blocked(workflow, newer_source):
    incoming = hidden_snapshot()
    incoming['openPositionsMeta']['asOf'] = None
    incoming['meta'] = {'generatedAt': NOW_MS}
    saved = hidden_snapshot()
    saved['openPositionsMeta']['asOf'] = None
    saved['meta'] = {newer_source: NOW_MS - 1}
    if newer_source == 'generatedAt':
        incoming['meta'] = {'collectionStartedAtMs': NOW_MS}
    workflow.lead.snapshot_json = json.dumps(saved)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], incoming, [workflow.proxy])
    workflow.adapter.place_order.assert_not_awaited()


async def test_snapshot_superseded_during_orderbook_wait_is_blocked_after_restart(workflow):
    snapshot = hidden_snapshot()
    snapshot['meta']['collectionStartedAtMs'] = NOW_MS - 2
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    async def save_newer(*args, **kwargs):
        from app.services.adapters.base import OrderBook
        latest = json.loads(json.dumps(snapshot))
        latest['meta']['collectionStartedAtMs'] = NOW_MS - 1
        async with get_session_factory()() as concurrent:
            lead = await concurrent.get(LeadSource, workflow.lead.id)
            lead.snapshot_json = json.dumps(latest)
            await concurrent.commit()
        return OrderBook([[99.9, 100]], [[100, 100]])
    workflow.adapter.get_orderbook.side_effect = save_newer
    await CopyRouter(CredentialVault()).process_lead_events(workflow.db, workflow.lead, [opening_order()], [], snapshot, [workflow.proxy])
    workflow.adapter.get_orderbook.assert_awaited_once()
    workflow.adapter.place_order.assert_not_awaited()


@pytest.mark.parametrize('delivery_state', ['sent', 'pending'])
async def test_recovery_preserves_proven_notification_evidence(trading, delivery_state):
    delivery = await source_delivery(trading)
    delivery.state = 'APPLIED'
    intent = OrderIntent(id='proven-report', binding_id=trading.binding.id, account_id=trading.account.id,
        inst_id='BTCUSDT', pos_side='long', source_event_id='recovery', state='FILLED',
        requested_qty='1', filled_qty='1', avg_px='100', reduce_only=False, exchange_order_id='proven-venue-order')
    record = ExecutionRecord(binding_id=trading.binding.id, lead_id=trading.lead.id, lead_name='test',
        account_id=trading.account.id, account_name='offline', inst_id='BTCUSDT', symbol='BTCUSDT',
        position_side='LONG', side='BUY', action='OPEN_LONG', status='EXECUTED',
        my_qty=1, my_price=100, order_id='proven-venue-order', details='legacy')
    trading.db.add_all([intent, record])
    await trading.db.flush()
    payload = json.dumps({'execution_id': record.id, 'event_key': f'execution:{record.id}',
        'channels': ['outbox'], 'delivered_channels': ['outbox'] if delivery_state == 'sent' else [],
        'title': 'Synthetic', 'message': 'Synthetic fill', 'event_type': 'EXECUTED'})
    trading.db.add(NotificationOutbox(payload=payload, status=delivery_state, attempts=0))
    await trading.db.commit()
    exchange = OfflineExchange()
    for router in (trading.router, CopyRouter(CredentialVault())):
        assert await router.reconcile_pending_orders(trading.db, trading.account, exchange, {'BTCUSDT': trading.spec})
    assert len((await trading.db.scalars(select(ExecutionRecord))).all()) == 1
    outboxes = (await trading.db.scalars(select(NotificationOutbox))).all()
    assert len(outboxes) == 1 and outboxes[0].status == delivery_state and outboxes[0].payload == payload
    assert record.notification_state == delivery_state
    assert exchange.orders == []


async def test_newly_reported_historical_terminal_fill_has_unknown_notification_state(trading):
    delivery = await source_delivery(trading)
    delivery.state = 'APPLIED'
    trading.db.add(OrderIntent(id='legacy-unreported', binding_id=trading.binding.id, account_id=trading.account.id,
        inst_id='BTCUSDT', pos_side='long', source_event_id='recovery', state='FILLED',
        requested_qty='1', filled_qty='1', avg_px='100', reduce_only=False, exchange_order_id='old-fill'))
    await trading.db.commit()
    exchange = OfflineExchange()
    for router in (trading.router, CopyRouter(CredentialVault())):
        assert not await router.reconcile_pending_orders(trading.db, trading.account, exchange, {'BTCUSDT': trading.spec})
    records = (await trading.db.scalars(select(ExecutionRecord))).all()
    assert len(records) == 1 and records[0].my_qty == 1 and records[0].notification_state == 'unknown'
    assert 'human reconciliation' in records[0].details.lower()
    assert not (await trading.db.scalars(select(NotificationOutbox))).all()
    assert exchange.orders == []
